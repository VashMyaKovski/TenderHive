from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import FakeCache, FakeGraph, FakeVector, MemoryRepository, make_payload
from th_kb.api import create_app
from th_kb.application import KnowledgeBase
from th_kb.config import Settings
from th_kb.models import TestCase as CaseModel


def client(settings: Settings) -> tuple[TestClient, MemoryRepository]:
    repository = MemoryRepository()
    kb = KnowledgeBase(
        settings,
        repository,
        FakeGraph(),
        FakeVector(),
        FakeCache(),
        lambda: datetime(2026, 11, 23, tzinfo=UTC),
    )
    return TestClient(create_app(kb)), repository


def test_version_lifecycle(settings: Settings) -> None:
    http, repository = client(settings)
    payload = make_payload(
        test_cases=[
            CaseModel(case_id="case-1", input_facts={"margin_percent": 12}, expected_rule_id="R-07")
        ]
    )
    body = payload.model_dump(mode="json")

    response = http.post("/api/v1/versions", json=body)
    assert response.status_code == 201
    assert response.json()["status"] == "DRAFT"

    activated = http.post(f"/api/v1/versions/{payload.version}/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "ACTIVE"
    assert repository.active == payload.version

    listed = http.get("/api/v1/versions")
    assert listed.status_code == 200
    assert listed.json()[0]["version"] == payload.version

    context = http.get("/api/v1/context", params={"version": payload.version, "query": "terms"})
    assert context.status_code == 200
    assert context.json()["version"] == payload.version
    assert context.json()["corpus_fragments"][0]["score"] == 0.9


def test_errors_have_expected_shape(settings: Settings) -> None:
    http, _ = client(settings)
    missing = http.get("/api/v1/versions/kb-v9")
    assert missing.status_code == 404
    assert missing.json()["code"] == "VERSION_NOT_FOUND"
    assert "trace_id" in missing.json()

    invalid = http.post("/api/v1/versions", json={"version": "bad"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_request"


def test_rollback_restores_version(settings: Settings) -> None:
    http, repository = client(settings)
    for version in ("kb-v1", "kb-v2"):
        response = http.post("/api/v1/versions", json=make_payload(version).model_dump(mode="json"))
        assert response.status_code == 201
    http.post("/api/v1/versions/kb-v1/activate")
    http.post("/api/v1/versions/kb-v2/activate")
    rolled = http.post("/api/v1/versions/kb-v1/rollback")
    assert rolled.status_code == 200
    assert repository.active == "kb-v1"
