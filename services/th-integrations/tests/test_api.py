from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from tender_integrations.api import create_app
from tender_integrations.config import Settings
from tender_integrations.processor import MailProcessor
from tests.conftest import (
    FakeMailSource,
    MemoryFiles,
    MemoryIdempotency,
    MemoryJobs,
    MemoryPublisher,
    message,
)


def client(settings: Settings) -> TestClient:
    processor = MailProcessor(
        settings,
        FakeMailSource([message()]),
        MemoryIdempotency(),
        MemoryFiles(),
        MemoryJobs(),
        MemoryPublisher(),
        lambda: datetime(2026, 11, 23, tzinfo=UTC),
        uuid4,
    )
    return TestClient(create_app(processor, settings))


def test_poll_endpoint(settings: Settings) -> None:
    response = client(settings).post("/api/v1/mail/poll")
    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["jobs"][0]["source"] == "mail"
    assert body["jobs"][0]["status"] == "принято"


def test_mcp_stubs(settings: Settings) -> None:
    http = client(settings)
    profile = http.post("/api/v1/mcp/1c/getOrganizationProfile")
    assert profile.status_code == 200
    assert profile.json()["payload"]["profile_ref"] == "profile-1"

    registry = http.post("/api/v1/mcp/registry/checkCounterparty")
    assert registry.json()["payload"]["active"] is True

    unknown = http.post("/api/v1/mcp/1c/getDocumentStatus")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "MCP_STUB_ERROR"
