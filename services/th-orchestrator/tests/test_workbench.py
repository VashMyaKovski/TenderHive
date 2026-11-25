from base64 import b64encode
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pytest import MonkeyPatch, raises

from tender_orchestrator import workbench
from tender_orchestrator.demo_flow import _pdf
from tender_orchestrator.workbench_flow import Run, WorkbenchSettings
from tender_orchestrator.workbench_inputs import RunInput, has_readable_text, load_uploads


def _incoming(name: str, data: bytes) -> dict[str, object]:
    return {
        "title": "Тестовая заявка",
        "context": "Предложение действительно в течение указанного срока.",
        "facts": {
            "quantity": "10",
            "offer_validity_workdays": 65,
            "unit_cost": "4.50",
            "offer_price": "180",
            "available_quantity": "12",
        },
        "documents": [
            {"name": name, "content_type": "", "content_base64": b64encode(data).decode("ascii")}
        ],
    }


def _settings() -> WorkbenchSettings:
    return WorkbenchSettings.model_validate(
        {
            "smtp_host": "mail-sandbox",
            "smtp_port": 3025,
            "recipient": "demo@example.test",
            "mail_url": "http://th-integrations:8000",
            "mail_token": "mail-token",
            "kb_url": "http://th-kb:8000",
            "kb_version": "kb-demo-v1",
            "solver_url": "http://th-solver:8000",
            "agents_url": "http://th-agents:8000",
            "artifact_token": "artifact-token",
            "orchestrator_url": "http://th-orchestrator:8000",
            "service_token": "service-token",
            "reviewer_tokens": {},
            "timeout_seconds": 15,
            "web_url": "http://th-workbench:8000",
            "web_port": 8000,
            "web_max_runs": 3,
            "web_margin_threshold_percent": "10",
            "web_penalty_threshold_percent": "0.1",
            "web_profile_ref": "demo-profile",
        }
    )


def test_pdf_bytes_and_document_reference_are_preserved() -> None:
    data = _pdf()
    inputs = RunInput.model_validate(_incoming("Тендер.pdf", data))
    run_id = uuid4()
    [upload] = load_uploads(run_id, inputs, "http://th-workbench:8000")
    assert upload.document.checksum == sha256(data).hexdigest()
    assert upload.document.storage_path.endswith(
        f"/api/workbench/runs/{run_id}/documents/{upload.document.file_id}"
    )
    assert upload.data == data
    assert has_readable_text(upload)


def test_upload_rejects_unknown_content_and_absent_risk_context() -> None:
    inputs = RunInput.model_validate(_incoming("../tender.txt", b"Some text"))
    with raises(ValueError, match="Имя файла"):
        load_uploads(uuid4(), inputs, "http://th-workbench:8000")
    inputs = RunInput.model_validate(_incoming("tender.pdf", b"Fake PDF"))
    with raises(ValueError, match="неверный заголовок"):
        load_uploads(uuid4(), inputs, "http://th-workbench:8000")
    missing = _incoming("tender.txt", b"Some text")
    del missing["context"]
    with raises(ValidationError, match="context"):
        RunInput.model_validate(missing)


def test_local_api_keeps_documents_and_blocks_cross_site_writes(monkeypatch: MonkeyPatch) -> None:
    async def record(_: WorkbenchSettings, run: Run, _decision: object) -> None:
        run.message("extraction", "orchestrator", "DONE", "Вход проверен", None)

    monkeypatch.setattr(workbench, "work_run", record)
    data = b"Published tender terms are available."
    with TestClient(workbench.create_app(_settings()), base_url="http://localhost:8787") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz", headers={"Host": "external.example"}).status_code == 400
        incoming = _incoming("source.txt", data)
        assert (
            client.post(
                "/api/workbench/runs",
                json=incoming,
                headers={"Origin": "https://external.example"},
            ).status_code
            == 403
        )
        assert client.post("/api/workbench/runs", content="ignored").status_code == 415
        created = client.post(
            "/api/workbench/runs", json=incoming, headers={"Origin": "http://localhost:8787"}
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        assert created.json()["documents"] == [{"name": "source.txt", "size": len(data)}]
        assert created.headers["cache-control"] == "no-store"
        assert "service-token" not in created.text
        file_id = uuid5(UUID(run_id), "attachment:0")
        download = client.get(f"/api/workbench/runs/{run_id}/documents/{file_id}")
        assert download.content == data
        assert download.headers["content-disposition"].startswith("attachment;")
        assert client.get(f"/api/workbench/runs/{run_id}/artifacts/offer.docx").status_code == 404
        assert (
            client.post(
                f"/api/workbench/runs/{run_id}/decision", json={"decision": "APPROVED"}
            ).status_code
            == 409
        )


def test_internal_mode_requires_workbench_token() -> None:
    token = "workbench-test-token-" * 3
    settings = _settings().model_copy(update={"web_service_token": SecretStr(token)})
    with TestClient(workbench.create_app(settings), base_url="http://th-workbench:8000") as client:
        path = f"/api/workbench/runs/{uuid4()}"
        assert client.get("/healthz").status_code == 200
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Workbench-Token": "incorrect"}).status_code == 401
        assert client.get(path, headers={"X-Workbench-Token": token}).status_code == 404
