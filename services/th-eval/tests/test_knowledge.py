import httpx
import pytest
from pydantic import HttpUrl

from tender_eval.knowledge import CheckRequest, check


@pytest.mark.parametrize("candidate_status", ["READY", "NO_SOLUTION"])
async def test_knowledge_check_uses_both_versions(candidate_status: str) -> None:
    calls = []

    def solver(request: httpx.Request) -> httpx.Response:
        import json

        assert request.headers["X-KB-Eval-Token"] == "test-token"
        body = json.loads(request.content)
        assert body["trace_id"] == request.headers["X-Trace-Id"]
        version = body["kb_version"]
        calls.append(version)
        return httpx.Response(
            200,
            json={
                "kb_version": version,
                "status": candidate_status if version == "candidate" else "READY",
                "stop_reasons": [],
                "compliance": [{"status": "MET"}],
            },
            headers={"X-Trace-Id": body["trace_id"]},
        )

    result = await check(
        CheckRequest.model_validate(
            {
                "candidate_version": "candidate",
                "baseline_version": "active",
                "test_set_version": "demo-v1",
                "test_cases": [
                    {
                        "case_id": "experience",
                        "input": {},
                        "expected": {"status": "READY", "stop_reasons": [], "compliance": ["MET"]},
                    }
                ],
            }
        ),
        HttpUrl("http://th-solver:8000"),
        "test-token",
        1,
        httpx.MockTransport(solver),
    )
    assert calls == ["active", "candidate"]
    assert result.passed == (candidate_status == "READY")
    if not result.passed:
        assert result.report[0].code == "REGRESSION_DETECTED"
