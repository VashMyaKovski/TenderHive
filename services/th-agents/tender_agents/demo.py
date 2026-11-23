import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest, Task, TaskState
from a2a.utils.errors import A2AError
from pydantic import ValidationError

from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1


async def send(http: httpx.AsyncClient, origin: str, payload: dict[str, object]) -> Task:
    factory = ClientFactory(
        ClientConfig(
            httpx_client=http,
            streaming=False,
            accepted_output_modes=["application/json"],
        )
    )
    client = await factory.create_from_url(origin)
    try:
        request = SendMessageRequest(
            message=new_data_message(payload, media_type="application/json", role=Role.ROLE_USER)
        )
        async for response in client.send_message(request):
            if response.HasField("task"):
                return response.task
    finally:
        await client.close()
    raise ValueError("agent did not return a task")


async def run(
    origin: str, payload: RiskTaskV1, timeout_seconds: float, expected_reason: str | None
) -> int:
    async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as http:
        task = await send(http, origin, payload.model_dump(mode="json"))
    if task.status.state == TaskState.TASK_STATE_FAILED:
        failure = RiskFailureV1.model_validate(get_data_parts(task.status.message.parts)[0])
        print(failure.model_dump_json(indent=2))
        return 1
    if task.status.state != TaskState.TASK_STATE_COMPLETED or len(task.artifacts) != 1:
        raise ValueError("agent did not return a completed risk result")
    result = RiskResultV1.model_validate(get_data_parts(task.artifacts[0].parts)[0])
    print(result.model_dump_json(indent=2))
    return int(expected_reason is not None and result.human_reason != expected_reason)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--expect-reason")
    options = parser.parse_args()
    try:
        payload = RiskTaskV1.model_validate_json(options.fixture.read_bytes())
        return asyncio.run(run(options.url, payload, options.timeout, options.expect_reason))
    except A2AError, httpx.HTTPError, OSError, ValidationError, ValueError, IndexError:
        print("The A2A request or response could not be processed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
