import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from a2a.helpers.proto_helpers import get_data_parts
from a2a.types.a2a_pb2 import TaskState

from contracts.risk_task_v1 import HumanReason, RiskFailureV1, RiskResultV1
from tender_agents.api import create_app
from tender_agents.config import Settings
from tender_agents.demo import send
from tests.gateway_stub import response_for

ORIGIN = "http://agent.invalid"


@pytest.fixture
def payload() -> dict[str, object]:
    return json.loads(
        (Path(__file__).resolve().parents[1] / "examples" / "minsk-supply-risk.json").read_bytes()
    )


@asynccontextmanager
async def agent(
    gateway: Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate(
        {
            "model_gateway_url": "http://gateway.invalid",
            "model_gateway_timeout_seconds": 0.05,
            "public_url": ORIGIN,
            "bind_host": "127.0.0.1",
            "port": 8000,
        }
    )
    app = create_app(settings, httpx.MockTransport(gateway))
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
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/inference"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=response_for(body, reason, confidence))

    async with agent(gateway) as client:
        raw = await send(client, ORIGIN, payload)
        card = (await client.get(ORIGIN + "/.well-known/agent-card.json")).json()
        schema = (await client.get(ORIGIN + "/schemas/risk-v1.json")).json()

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
    ("mutation", "success"),
    [("empty_sources", True), ("wrong_job", False), ("bad_source", False), ("bad_schema", False)],
)
async def test_response_correlation_and_source_validation(
    payload: dict[str, object],
    mutation: str,
    success: bool,
) -> None:
    def gateway(request: httpx.Request) -> httpx.Response:
        result = response_for(json.loads(request.content), "penalty_above_threshold", 0.9)
        match mutation:
            case "empty_sources":
                result["answer"]["source_ids"] = []
            case "wrong_job":
                result["job_id"] = str(uuid4())
            case "bad_source":
                result["answer"]["source_ids"] = [2]
            case "bad_schema":
                result["answer"]["hypothesis"].pop("summary")
        return httpx.Response(200, json=result)

    async with agent(gateway) as client:
        task = await send(client, ORIGIN, payload)
    if success:
        result = RiskResultV1.model_validate(get_data_parts(task.artifacts[0].parts)[0])
        assert result.sources == []
    else:
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
    ("status", "code", "retryable"),
    [
        (422, "unsafe_context", False),
        (422, "unsupported_prompt_version", False),
        (503, "provider_unavailable", True),
        (504, "provider_timeout", True),
        (502, "provider_rejected", False),
        (503, "task_disabled", False),
    ],
)
async def test_gateway_failures_preserve_trace_and_make_one_attempt(
    status: int,
    code: str,
    retryable: bool,
    payload: dict[str, object],
) -> None:
    count = 0
    trace_id = uuid4()

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(status, json={"code": code, "trace_id": str(trace_id)})

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
        if attempts == 1:
            return httpx.Response(
                503, json={"code": "provider_unavailable", "trace_id": str(uuid4())}
            )
        result = response_for(json.loads(request.content), "penalty_above_threshold", 0.9)
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
    assert failure.retryable and failure.trace_id is None
