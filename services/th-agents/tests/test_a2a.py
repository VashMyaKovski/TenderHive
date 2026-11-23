import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from a2a.helpers.proto_helpers import get_data_parts
from a2a.types.a2a_pb2 import TaskState

from contracts.agent_task import AgentResult
from contracts.risk_task_v1 import HumanReason, RiskFailureV1, RiskResultV1
from tender_agents.api import create_app
from tender_agents.config import Settings
from tender_agents.demo import send
from tender_agents.ledger import Claim, RiskLedger
from tests.gateway_stub import response_for

ORIGIN = "http://agent.invalid"


class MemoryLedger:
    def __init__(self) -> None:
        self._states: dict[UUID, tuple[str, UUID | None, str]] = {}

    async def claim(self, task_id: UUID, fingerprint: str, owner: UUID) -> Claim:
        current = self._states.get(task_id)
        if current is not None:
            original, active, outcome = current
            if original != fingerprint:
                return "conflict", ""
            if outcome:
                return "recorded", outcome
            if active is not None:
                return "busy", ""
        self._states[task_id] = fingerprint, owner, ""
        return "owned", ""

    async def finish(self, task_id: UUID, owner: UUID, outcome: str) -> bool:
        fingerprint, active, _ = self._states[task_id]
        if active != owner:
            return False
        self._states[task_id] = fingerprint, None, outcome
        return True

    async def ready(self) -> bool:
        return True


@pytest.fixture
def payload() -> dict[str, object]:
    return json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "minsk-supply-risk.json").read_bytes()
    )


@asynccontextmanager
async def agent(
    gateway: Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
    ledger: RiskLedger | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate(
        {
            "model_gateway_url": "http://gateway.invalid",
            "model_gateway_timeout_seconds": 0.05,
            "public_url": ORIGIN,
            "bind_host": "127.0.0.1",
            "port": 8000,
            "redis_url": "redis://unused.invalid:6379/0",
            "redis_timeout_seconds": 0.05,
            "task_ttl_seconds": 300,
            "task_lease_seconds": 10,
            "task_wait_seconds": 0.2,
            "task_poll_seconds": 0.001,
        }
    )
    app = create_app(settings, httpx.MockTransport(gateway), ledger or MemoryLedger())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client,
    ):
        yield client


@pytest.mark.parametrize(
    ("rate", "reason", "confidence"),
    [
        ("0.05", None, 0.9),
        ("0.10", None, 0.9),
        ("0.15", "penalty_above_threshold", 0.9),
        ("0.05", "unclear_requirements", 0.0),
    ],
)
async def test_a2a_sdk_reads_card_and_returns_validated_result(
    payload: dict[str, object],
    rate: str,
    reason: HumanReason | None,
    confidence: float,
) -> None:
    payload["penalty_daily_percent"] = rate
    payload["trace_id"] = str(uuid4())
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/invoke"
        body = json.loads(request.content)
        assert request.headers["X-Trace-Id"] == payload["trace_id"] == body["trace_id"]
        requests.append(body)
        return httpx.Response(200, json=response_for(body, reason, confidence))

    async with agent(gateway) as client:
        raw = await send(client, ORIGIN, payload)
        card = (await client.get(ORIGIN + "/.well-known/agent-card.json")).json()
        schema = (await client.get(ORIGIN + "/api/v1/schemas/risk-v1.json")).json()

    assert [item["id"] for item in card["skills"]] == ["risk_assessment"]
    assert len(card["supportedInterfaces"]) == 1
    assert "RiskTaskV1" in schema["$defs"]
    assert raw.status.state == TaskState.TASK_STATE_COMPLETED
    result = RiskResultV1.model_validate(get_data_parts(raw.artifacts[0].parts)[0])
    assert str(result.task_id) == payload["task_id"]
    assert result.a2a_task_id == raw.id != payload["task_id"]
    assert str(result.job_id) == payload["job_id"]
    assert result.human_reason == reason and result.confidence == confidence
    assert result.sources[0].source.model_dump(mode="json") == payload["source"]
    assert str(result.sources[0].fragment_id) == payload["fragment_id"]
    assert requests == [
        {
            "job_id": payload["job_id"],
            "task": "risk_assessment",
            "mask": True,
            "prompt_version": "v1",
            "trace_id": payload["trace_id"],
            "context": [
                {"type": "public_text", "text": payload["fragment_text"]},
                {
                    "type": "knowledge",
                    "rules_version": "kb-demo-v1",
                    "penalty_daily_threshold_percent": "0.10",
                },
                {"type": "penalty", "daily_percent": rate},
            ],
        }
    ]


@pytest.mark.parametrize(
    "mutation",
    ["empty_sources", "wrong_job", "wrong_trace", "bad_source", "bad_schema"],
)
async def test_response_correlation_and_source_validation(
    payload: dict[str, object],
    mutation: str,
) -> None:
    def gateway(request: httpx.Request) -> httpx.Response:
        result = response_for(json.loads(request.content), "penalty_above_threshold", 0.9)
        match mutation:
            case "empty_sources":
                result["answer"]["source_ids"] = []
            case "wrong_job":
                result["job_id"] = str(uuid4())
            case "wrong_trace":
                result["trace_id"] = str(uuid4())
            case "bad_source":
                result["answer"]["source_ids"] = [2]
            case "bad_schema":
                result["answer"]["hypothesis"].pop("summary")
        return httpx.Response(200, json=result)

    async with agent(gateway) as client:
        task = await send(client, ORIGIN, payload)
    failure = RiskFailureV1.model_validate(get_data_parts(task.status.message.parts)[0])
    assert failure.code == "invalid_gateway_response"
    assert failure.trace_id is not None and not failure.retryable


@pytest.mark.parametrize("invalid_rate", [0.15, "1.5e-1", "0.0000001", "100.000001"])
async def test_rejects_invalid_percent_before_calling_gateway(
    payload: dict[str, object], invalid_rate: object
) -> None:
    payload["penalty_daily_percent"] = invalid_rate

    def gateway(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid data reached the gateway")

    async with agent(gateway) as client:
        task = await send(client, ORIGIN, payload)
    assert task.status.state == TaskState.TASK_STATE_FAILED
    failure = RiskFailureV1.model_validate(get_data_parts(task.status.message.parts)[0])
    assert str(failure.job_id) == payload["job_id"]
    assert failure.code == "invalid_request" and not failure.retryable
    assert not task.artifacts


async def test_protocol_error_omits_decoder_details() -> None:
    def gateway(_: httpx.Request) -> httpx.Response:
        pytest.fail("invalid A2A envelope reached the gateway")

    async with agent(gateway) as client:
        response = await client.post(
            ORIGIN + "/a2a",
            headers={"A2A-Version": "1.0"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "SendMessage",
                "params": {"message": {"messageId": {"private-input": "example"}}},
            },
        )

    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "Invalid params"},
    }


@pytest.mark.parametrize(
    ("status", "code", "reason", "retryable"),
    [
        (422, "unsafe_context", None, False),
        (422, "unsupported_prompt_version", None, False),
        (503, "provider_unavailable", "unavailable", True),
        (503, "provider_unavailable", "rate_limit", True),
        (504, "provider_timeout", "timeout", True),
        (502, "provider_rejected", "auth", False),
        (502, "invalid_provider_response", "schema_mismatch", False),
        (503, "task_disabled", None, False),
        (503, "gateway_storage_unavailable", None, True),
    ],
)
async def test_gateway_failures_preserve_trace_and_make_one_attempt(
    status: int,
    code: str,
    reason: str | None,
    retryable: bool,
    payload: dict[str, object],
) -> None:
    count = 0
    trace_id = uuid4()
    payload["trace_id"] = str(trace_id)

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if reason is not None:
            body = {"status": "UNAVAILABLE", "reason": reason, "job_id": payload["job_id"]}
        else:
            wire_code = "storage_unavailable" if code == "gateway_storage_unavailable" else code
            body = {"code": wire_code, "message": "Request rejected"}
        return httpx.Response(status, json={**body, "trace_id": str(trace_id)})

    async with agent(gateway) as client:
        task = await send(client, ORIGIN, payload)

    failure = RiskFailureV1.model_validate(get_data_parts(task.status.message.parts)[0])
    assert task.status.state == TaskState.TASK_STATE_FAILED and not task.artifacts
    assert count == 1
    assert failure.code == code and failure.retryable is retryable
    assert failure.trace_id == trace_id


async def test_retry_resubmits_the_application_task(payload: dict[str, object]) -> None:
    attempts = 0

    def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        if attempts == 1:
            return httpx.Response(
                503,
                json={
                    "job_id": body["job_id"],
                    "status": "UNAVAILABLE",
                    "reason": "unavailable",
                    "trace_id": body["trace_id"],
                },
            )
        result = response_for(body, "penalty_above_threshold", 0.9)
        return httpx.Response(200, json=result)

    async with agent(gateway) as client:
        failed = await send(client, ORIGIN, payload)
        completed = await send(client, ORIGIN, payload)

    assert failed.status.state == TaskState.TASK_STATE_FAILED
    assert completed.status.state == TaskState.TASK_STATE_COMPLETED
    assert attempts == 2 and failed.id != completed.id
    failure = RiskFailureV1.model_validate(get_data_parts(failed.status.message.parts)[0])
    result = RiskResultV1.model_validate(get_data_parts(completed.artifacts[0].parts)[0])
    assert failure.task_id == result.task_id
    assert failure.job_id == result.job_id
    assert failure.trace_id != result.trace_id


@pytest.mark.parametrize("simulate_timeout", [True, False])
async def test_transport_failures_are_retryable(
    payload: dict[str, object], simulate_timeout: bool
) -> None:
    async def gateway(request: httpx.Request) -> httpx.Response:
        if simulate_timeout:
            await asyncio.sleep(2)
        raise httpx.ConnectError("test fixture", request=request)

    async with agent(gateway) as client:
        task = await send(client, ORIGIN, payload)

    failure = RiskFailureV1.model_validate(get_data_parts(task.status.message.parts)[0])
    assert failure.code == ("gateway_timeout" if simulate_timeout else "gateway_transport_error")
    assert failure.retryable and failure.trace_id is not None


async def test_duplicate_task_reuses_outcome_across_workers(payload: dict[str, object]) -> None:
    calls = 0
    ledger = MemoryLedger()

    async def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200, json=response_for(json.loads(request.content), "penalty_above_threshold", 0.9)
        )

    async with agent(gateway, ledger) as worker_one, agent(gateway, ledger) as worker_two:
        first, second = await asyncio.gather(
            send(worker_one, ORIGIN, payload), send(worker_two, ORIGIN, payload)
        )
        changed = await send(
            worker_two, ORIGIN, {**payload, "fragment_text": "A different public clause."}
        )

    assert calls == 1
    first_result = RiskResultV1.model_validate(get_data_parts(first.artifacts[0].parts)[0])
    second_result = RiskResultV1.model_validate(get_data_parts(second.artifacts[0].parts)[0])
    assert first_result.trace_id == second_result.trace_id
    assert first_result.a2a_task_id == first.id != second.id == second_result.a2a_task_id
    failure = RiskFailureV1.model_validate(get_data_parts(changed.status.message.parts)[0])
    assert failure.code == "task_id_conflict" and not failure.retryable


async def test_common_agent_contract_reports_review_and_unavailable_roles(
    payload: dict[str, object],
) -> None:
    trace_id = uuid4()
    fields = {
        "fragment_id",
        "source",
        "fragment_text",
        "fragment_type",
        "penalty_daily_percent",
        "penalty_daily_threshold_percent",
        "prompt_version",
    }
    body = {
        "task_id": payload["task_id"],
        "job_id": payload["job_id"],
        "agent_role": "risk",
        "input": {key: payload[key] for key in fields},
        "kb_version": payload["rules_version"],
        "trace_id": str(trace_id),
    }
    calls = 0

    def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        doc = response_for(json.loads(request.content), "penalty_above_threshold", 0.9)
        return httpx.Response(200, json=doc)

    async with agent(gateway) as client:
        completed = await send(client, ORIGIN, body)
        unavailable = await send(client, ORIGIN, {**body, "agent_role": "legal"})
    assert calls == 1
    result = AgentResult.model_validate(get_data_parts(completed.artifacts[0].parts)[0])
    assert result.status == "NEEDS_REVIEW"
    assert result.model_calls[0].trace_id == result.trace_id == trace_id
    assert result.evidence[0]["source"] == payload["source"]
    failed = AgentResult.model_validate(get_data_parts(unavailable.status.message.parts)[0])
    assert failed.status == "FAILED" and failed.result["code"] == "role_unavailable"
