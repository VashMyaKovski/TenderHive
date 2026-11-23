from pathlib import Path
from uuid import uuid4

import pytest

from tender_orchestrator.models import StartJob
from tests.demo import demo_request


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--with-postgres",
        action="store_true",
        help="Run integrations on the migrated Compose database",
    )


@pytest.fixture
def request_body() -> StartJob:
    service = Path(__file__).resolve().parents[1]
    fixture = service / "examples/minsk-supply-risk.json"
    if not fixture.is_file():
        fixture = service.parent / "th-agents/examples/minsk-supply-risk.json"
    return demo_request(uuid4(), fixture)
