from base64 import b64encode
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from json import loads
from threading import Event
from uuid import UUID, uuid4, uuid5

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pytest import MonkeyPatch, raises

from contracts.agent_task import AgentResult
from contracts.models import HumanDecision, Job, SolverResult
from tender_orchestrator import workbench, workbench_flow
from tender_orchestrator.demo_flow import _pdf
from tender_orchestrator.models import JobView, ReviewCard, SupplyConfirmation
from tender_orchestrator.workbench_flow import Run, WorkbenchSettings
from tender_orchestrator.workbench_inputs import (
    DecisionInput,
    RunInput,
    has_readable_text,
    load_uploads,
    normalization_record,
)


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


def _shortage_input() -> RunInput:
    data = RunInput.model_validate(_incoming("source.txt", b"Published tender terms."))
    return data.model_copy(
        update={
            "facts": data.facts.model_copy(
                update={"available_quantity": Decimal(6), "delivery_deadline": date(2026, 12, 10)}
            )
        }
    )


def _run(inputs: RunInput) -> Run:
    run_id = uuid4()
    origin = "http://th-workbench:8000"
    return workbench_flow.new_run(
        run_id,
        inputs,
        load_uploads(run_id, inputs, origin),
        normalization_record(run_id, inputs, origin),
    )


def _view(run: Run) -> JobView:
    return JobView(
        job=Job(
            job_id=run.id,
            idempotency_key=str(run.id),
            source="web",
            created_at=datetime.now(UTC),
            status="PROCESSING",
            kb_version="kb-demo-v1",
            lot_ids=["uploaded-lot"],
            documents=[item.document for item in run.uploads],
            profile_ref="demo-profile",
            trace_id=uuid4(),
        ),
        phase="ready_for_next_stage",
        risk_attempts=0,
        risk_result=None,
        failure=None,
        cards=[],
        stage="solve",
        execution_id=uuid4(),
    )


def _card(run: Run, *, risk: bool = False) -> ReviewCard:
    return ReviewCard(
        card_id=uuid4(),
        job_id=run.id,
        step="risk_review" if risk else "review",
        reason_code="PENALTY_ABOVE_THRESHOLD" if risk else "RESOURCE_GAP",
        role="LAWYER" if risk else "SUPPLY",
        question="Условие согласовано?",
        deadline=datetime.now(UTC),
        state="open",
        timeout=False,
        decision=None,
    )


def test_workbench_requires_confirmation_before_dispatching_supply_approval(
    monkeypatch: MonkeyPatch,
) -> None:
    ready = Event()
    received = Event()
    submissions: list[DecisionInput] = []

    async def record(
        _: WorkbenchSettings, run: Run, decision: tuple[ReviewCard, DecisionInput] | None
    ) -> None:
        if decision is None:
            run.view = _view(run).model_copy(update={"cards": [_card(run)]})
            ready.set()
        else:
            submissions.append(decision[1])
            received.set()

    monkeypatch.setattr(workbench, "work_run", record)
    with TestClient(workbench.create_app(_settings()), base_url="http://localhost:8787") as client:
        response = client.post(
            "/api/workbench/runs", json=_shortage_input().model_dump(mode="json")
        )
        assert response.status_code == 202
        assert ready.wait(2)
        url = f"/api/workbench/runs/{response.json()['id']}"
        card = client.get(url).json()["card"]
        assert card["supply"] == {"shortage": "4", "delivery_deadline": "2026-12-10"}
        approval = {"decision": "APPROVED", "card_id": card["id"]}
        assert client.post(f"{url}/decision", json=approval).status_code == 422
        confirmation = {"quantity": "4", "delivery_date": "2026-12-08", "additional_cost": "50"}
        assert (
            client.post(
                f"{url}/decision",
                json={
                    **approval,
                    "card_id": str(uuid4()),
                    "supply_confirmation": confirmation,
                },
            ).status_code
            == 409
        )
        assert submissions == []
        assert (
            client.post(
                f"{url}/decision", json={**approval, "supply_confirmation": confirmation}
            ).status_code
            == 202
        )
        assert received.wait(2)
        assert submissions[0].supply_confirmation == SupplyConfirmation.model_validate(confirmation)


def test_risk_decision_remains_visible_in_run_snapshot() -> None:
    run = _run(_shortage_input())
    card = _card(run, risk=True)
    decision = HumanDecision(
        decision_id=uuid4(),
        job_id=run.id,
        reason_code=card.reason_code,
        role=card.role,
        question=card.question,
        deadline=card.deadline,
        timeout=False,
        decision="APPROVED",
        comment="Принимаем риск в пределах лимита договора.",
        decided_at=datetime.now(UTC),
    )
    run.view = _view(run).model_copy(
        update={"cards": [card.model_copy(update={"state": "decided", "decision": decision})]}
    )
    assert run.snapshot()["risk_decisions"] == [
        {
            "reason": "PENALTY_ABOVE_THRESHOLD",
            "role": "LAWYER",
            "decision": "APPROVED",
            "comment": decision.comment,
        }
    ]


def _extraction(run: Run, view: JobView) -> AgentResult:
    return AgentResult.model_validate(
        {
            "task_id": uuid4(),
            "job_id": run.id,
            "agent_role": "extraction",
            "status": "DONE",
            "result": {"requirements": []},
            "evidence": [],
            "model_calls": [],
            "trace_id": view.job.trace_id,
        }
    )


async def test_loss_is_sent_to_solver_before_risk(monkeypatch: MonkeyPatch) -> None:
    inputs = _shortage_input()
    inputs = inputs.model_copy(
        update={"facts": inputs.facts.model_copy(update={"offer_price": Decimal(10)})}
    )
    run = _run(inputs)
    view = _view(run).model_copy(update={"phase": "accepted", "stage": "extract"})
    calls: list[str] = []

    def register(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=view.model_dump_json())

    async def extract(*_: object) -> AgentResult:
        return _extraction(run, view)

    async def solve(*_: object) -> JobView:
        calls.append("solve")
        return view.model_copy(update={"phase": "declined"})

    monkeypatch.setattr(workbench_flow, "_ask", extract)
    monkeypatch.setattr(workbench_flow, "_solve", solve)
    async with httpx.AsyncClient(transport=httpx.MockTransport(register)) as http:
        await workbench_flow._start(http, _settings(), run)
    assert run.phase == "declined"
    assert calls == ["/api/v1/jobs", "solve"]


async def test_corrected_solve_keeps_original_inputs_and_forwards_persisted_confirmation(
    monkeypatch: MonkeyPatch,
) -> None:
    run = _run(_shortage_input())
    view = _view(run)
    confirmation = SupplyConfirmation.model_validate(
        {"quantity": "4", "delivery_date": "2026-12-10", "additional_cost": "50"}
    )
    view = view.model_copy(
        update={
            "cards": [
                _card(run).model_copy(
                    update={"state": "decided", "supply_confirmation": confirmation}
                )
            ]
        }
    )
    run.extraction = _extraction(run, view)
    result = SolverResult.model_validate(
        {
            "job_id": run.id,
            "kb_version": view.job.kb_version,
            "status": "READY",
            "requirements": [],
            "compliance": [],
            "resources": [],
            "estimate": {},
            "risks": [],
            "recommendation": {"value": "PARTICIPATE", "conditions": []},
            "plan": {},
            "explanation": {"steps": [], "rule_trace": [], "source_refs": []},
            "artifacts": [],
            "stop_reasons": [],
        }
    )

    async def ask(
        _: httpx.AsyncClient,
        _settings: WorkbenchSettings,
        _run: Run,
        _view: JobView,
        role: str,
        payload: dict[str, object],
    ) -> AgentResult:
        if role == "resource":
            assert payload["positions"] == [
                {
                    "position_id": "uploaded-lot",
                    "required_quantity": "10",
                    "available_quantity": "10",
                    "source": workbench_flow._source(run),
                }
            ]
        return _extraction(run, view).model_copy(update={"agent_role": role})

    def solve(request: httpx.Request) -> httpx.Response:
        payload = loads(request.content)
        if request.url.path == "/api/v1/solve":
            position = payload["profile"]["positions"][0]
            assert position["available"] == "6"
            assert position["unit_cost"] == "4.50"
            assert position["offer_total_price"] == "180"
            assert position["supply_deadline"] == "2026-12-10T00:00:00+00:00"
            assert position["supply_confirmation"] == confirmation.model_dump(mode="json")
            return httpx.Response(200, content=result.model_dump_json())
        assert request.url.path == f"/api/v1/jobs/{run.id}/solver-result"
        assert payload["execution_id"] == str(view.execution_id)
        return httpx.Response(
            200, content=view.model_copy(update={"solver_result": result}).model_dump_json()
        )

    monkeypatch.setattr(workbench_flow, "_ask", ask)
    async with httpx.AsyncClient(transport=httpx.MockTransport(solve)) as http:
        updated = await workbench_flow._solve(http, _settings(), run, view)
    assert updated.solver_result == result
