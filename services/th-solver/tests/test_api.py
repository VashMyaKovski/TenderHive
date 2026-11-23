from uuid import uuid4

from fastapi.testclient import TestClient

from tender_solver.api import create_app
from tender_solver.config import Settings
from tender_solver.models import FactInput, SolveRequest
from tender_solver.solver import Solver
from tests.conftest import FakeKnowledge, FakeRisk, MemoryCache, context, rule
from tests.test_solver import request


def client(settings: Settings) -> TestClient:
    solver = Solver(
        settings,
        FakeKnowledge(context(rules=[rule()])),
        FakeRisk(),
        MemoryCache(),
        settings.confidence_floor,
    )
    return TestClient(create_app(solver))


def test_solve_endpoint(settings: Settings) -> None:
    http = client(settings)
    body = SolveRequest.model_validate(
        request(facts=[FactInput(key="margin_percent", value=12)]).model_dump(mode="json")
    )
    response = http.post("/api/v1/solve", json=body.model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["status"] == "READY"


def test_error_shape(settings: Settings) -> None:
    http = client(settings)
    body = request(kb_version="kb-v9", facts=[FactInput(key="margin_percent", value=12)])
    response = http.post("/api/v1/solve", json=body.model_dump(mode="json"))
    assert response.status_code == 404
    assert response.json()["code"] == "KB_VERSION_NOT_FOUND"
    assert "trace_id" in response.json()

    invalid = http.post("/api/v1/solve", json={"job_id": str(uuid4())})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_request"
