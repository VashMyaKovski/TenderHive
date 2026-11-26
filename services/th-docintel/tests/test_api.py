from uuid import UUID, uuid4

import pymupdf
from fastapi.testclient import TestClient

from tender_docintel.api import create_app
from tender_docintel.config import Settings
from tests.conftest import FakeCore


def pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Penalty is 0.15 percent per day")
    payload = document.tobytes()
    document.close()
    return payload


def test_extract_endpoint_returns_sources_for_consumers(
    settings: Settings, fake_core: FakeCore, identifiers: tuple[UUID, UUID]
) -> None:
    job_id, file_id = identifiers
    trace_id = uuid4()
    with TestClient(create_app(settings, fake_core)) as client:
        response = client.post(
            "/api/v1/extract",
            data={
                "job_id": str(job_id),
                "file_id": str(file_id),
                "template_set_version": "ts-test-v1",
            },
            files={"file": ("contract.pdf", pdf_bytes(), "application/pdf")},
            headers={"X-Trace-Id": str(trace_id)},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["requirements"][0]["kind"] == "penalty"
    assert payload["requirements"][0]["value"] == "0.0015"
    assert payload["requirements"][0]["source"]["file_id"] == str(file_id)
    assert payload["requirements"][0]["source"]["page"] == 1
    assert payload["trace_id"] == str(trace_id)
    assert fake_core.last_trace_id == trace_id


def test_low_ocr_confidence_is_explicitly_incomplete(
    settings: Settings, identifiers: tuple[UUID, UUID]
) -> None:
    job_id, file_id = identifiers
    core = FakeCore(low_confidence=True)
    with TestClient(create_app(settings, core)) as client:
        response = client.post(
            "/api/v1/extract",
            data={
                "job_id": str(job_id),
                "file_id": str(file_id),
                "template_set_version": "ts-test-v1",
            },
            files={"file": ("contract.pdf", pdf_bytes(), "application/pdf")},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "INCOMPLETE"
    assert response.json()["requirements"][0]["needs_review"] is True
    assert response.json()["warnings"][0]["code"] == "LOW_OCR_CONFIDENCE"


def test_uses_configured_template_version_when_not_sent(
    settings: Settings, fake_core: FakeCore, identifiers: tuple[UUID, UUID]
) -> None:
    job_id, file_id = identifiers
    with TestClient(create_app(settings, fake_core)) as client:
        response = client.post(
            "/api/v1/extract",
            data={"job_id": str(job_id), "file_id": str(file_id)},
            files={"file": ("contract.pdf", pdf_bytes(), "application/pdf")},
        )

    assert response.status_code == 200
    assert response.json()["requirements"][0]["template_set_version"] == "ts-test-v1"


def test_rejects_unsupported_upload_with_trace_id(settings: Settings, fake_core: FakeCore) -> None:
    trace_id = uuid4()
    with TestClient(create_app(settings, fake_core)) as client:
        response = client.post(
            "/api/v1/extract",
            data={
                "job_id": str(uuid4()),
                "file_id": str(uuid4()),
                "template_set_version": "ts-test-v1",
            },
            files={"file": ("archive.zip", b"not an image", "application/zip")},
            headers={"X-Trace-Id": str(trace_id)},
        )

    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_FORMAT"
    assert response.json()["trace_id"] == str(trace_id)
    assert fake_core.calls == 0


def test_rejects_oversized_file_before_processing(settings: Settings, fake_core: FakeCore) -> None:
    with TestClient(create_app(settings, fake_core)) as client:
        response = client.post(
            "/api/v1/extract",
            data={
                "job_id": str(uuid4()),
                "file_id": str(uuid4()),
                "template_set_version": "ts-test-v1",
            },
            files={
                "file": (
                    "oversized.pdf",
                    b"x" * (settings.max_file_bytes + 1),
                    "application/pdf",
                )
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"
    assert fake_core.calls == 0


def test_health_reports_unavailable_core(settings: Settings) -> None:
    core = FakeCore(health_error=True)
    with TestClient(create_app(settings, core)) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["code"] == "GRPC_UNAVAILABLE"
