from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from tender_orchestrator.agent import AgentClient
from tender_orchestrator.models import HITL_POINTS, DecisionRequest, StartJob
from tender_orchestrator.ports import AgentError


@pytest.mark.parametrize("field", ["job_id", "rules_version", "penalty_daily_percent"])
def test_start_validates_correlated_decimal_task(request_body: StartJob, field: str) -> None:
    payload = request_body.model_dump(mode="json")
    payload["risk"][field] = 0.15 if field == "penalty_daily_percent" else str(uuid4())
    with pytest.raises(ValidationError):
        StartJob.model_validate(payload)


def test_all_six_hitl_points_have_a_step_and_role() -> None:
    assert set(HITL_POINTS) == {
        "penalty_above_threshold",
        "unclear_requirements",
        "low_margin",
        "resource_gap",
        "no_solution",
        "final_approval",
    }
    assert all(step.endswith("review") and role for step, role in HITL_POINTS.values())


def test_start_job_accepts_review_points(request_body: StartJob) -> None:
    payload = request_body.model_dump(mode="json")
    payload["reviews"] = [{"reason_code": "low_margin"}, {"reason_code": "final_approval"}]
    parsed = StartJob.model_validate(payload)
    assert [point.reason_code for point in parsed.reviews] == ["low_margin", "final_approval"]


def test_duplicate_review_points_are_rejected(request_body: StartJob) -> None:
    payload = request_body.model_dump(mode="json")
    payload["reviews"] = [{"reason_code": "low_margin"}, {"reason_code": "low_margin"}]
    with pytest.raises(ValidationError):
        StartJob.model_validate(payload)


def test_stop_requires_a_review_comment() -> None:
    with pytest.raises(ValidationError):
        DecisionRequest(decision_id=uuid4(), action="stop", comment="  ")


@pytest.mark.parametrize(("status", "retryable"), [(404, False), (429, True), (503, True)])
async def test_agent_http_failures_have_a_bounded_retry_classification(
    request_body: StartJob,
    status: int,
    retryable: bool,
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/.well-known/agent-card.json"
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(AgentError) as error:
            await AgentClient(http, "http://agent.invalid/", 1).assess(request_body.risk)
    assert error.value.failure.code == "agent_http_error"
    assert error.value.failure.retryable is retryable


async def test_agent_card_cannot_redirect_a_risk_request(request_body: StartJob) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "name": "Example Agent",
                "description": "Risk assessment",
                "version": "1.0.0",
                "supportedInterfaces": [
                    {
                        "url": "http://other.invalid/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "skills": [
                    {
                        "id": "risk_assessment",
                        "name": "Risk",
                        "description": "Assess risk",
                        "tags": ["risk"],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(AgentError) as error:
            await AgentClient(http, "http://agent.invalid/", 1).assess(request_body.risk)
    assert error.value.failure.code == "invalid_agent_card"
    assert not error.value.failure.retryable
