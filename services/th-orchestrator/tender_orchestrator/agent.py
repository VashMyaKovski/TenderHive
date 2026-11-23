import asyncio

import httpx
from a2a.client import (
    A2ACardResolver,
    A2AClientTimeoutError,
    AgentCardResolutionError,
    ClientConfig,
    ClientFactory,
)
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest, Task, TaskState
from a2a.utils.errors import A2AError
from google.protobuf.json_format import ParseError
from pydantic import ValidationError

from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1
from tender_orchestrator.models import AssessmentFailure
from tender_orchestrator.ports import AgentError, agent_error


class AgentClient:
    def __init__(self, http: httpx.AsyncClient, origin: str, timeout_seconds: float) -> None:
        self._origin = httpx.URL(origin)
        self._timeout_seconds = timeout_seconds
        self._resolver = A2ACardResolver(http, origin)
        self._factory = ClientFactory(
            ClientConfig(
                httpx_client=http,
                streaming=False,
                accepted_output_modes=["application/json"],
            )
        )

    async def assess(self, request: RiskTaskV1) -> RiskResultV1:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                task = await self._send(request)
            return _result(task, request)
        except TimeoutError, A2AClientTimeoutError:
            raise agent_error("agent_timeout", retryable=True) from None
        except (A2AError, ValueError, ParseError, httpx.InvalidURL) as error:
            cause = error.__cause__
            if isinstance(cause, httpx.TimeoutException):
                raise agent_error("agent_timeout", retryable=True) from None
            if isinstance(cause, httpx.HTTPStatusError):
                status = cause.response.status_code
                retryable = status in (408, 429) or status >= 500
                raise agent_error("agent_http_error", retryable=retryable) from None
            if isinstance(cause, httpx.TransportError):
                raise agent_error("agent_unavailable", retryable=True) from None
            code = (
                "invalid_agent_card"
                if isinstance(error, AgentCardResolutionError)
                else "invalid_agent_response"
            )
            raise agent_error(code, retryable=False) from None

    async def _send(self, request: RiskTaskV1) -> Task:
        card = await self._resolver.get_agent_card()
        if len(card.supported_interfaces) != 1 or not any(
            skill.id == "risk_assessment" for skill in card.skills
        ):
            raise agent_error("invalid_agent_card", retryable=False)
        interface = card.supported_interfaces[0]
        endpoint = httpx.URL(interface.url)
        if (
            interface.protocol_binding != "JSONRPC"
            or interface.protocol_version != "1.0"
            or endpoint.copy_with(path="/", query=None, fragment=None) != self._origin
            or endpoint.query
            or endpoint.fragment
        ):
            raise agent_error("invalid_agent_card", retryable=False)

        client = self._factory.create(card)
        try:
            message = new_data_message(
                request.model_dump(mode="json"), media_type="application/json", role=Role.ROLE_USER
            )
            async for response in client.send_message(SendMessageRequest(message=message)):
                if response.HasField("task"):
                    return response.task
        finally:
            await client.close()
        raise agent_error("invalid_agent_response", retryable=False)


def _result(task: Task, request: RiskTaskV1) -> RiskResultV1:
    try:
        if task.status.state == TaskState.TASK_STATE_FAILED and not task.artifacts:
            parts = get_data_parts(task.status.message.parts)
            if len(parts) == 1:
                failure = RiskFailureV1.model_validate(parts[0])
                if (failure.task_id, failure.job_id, failure.a2a_task_id) == (
                    request.task_id,
                    request.job_id,
                    task.id,
                ):
                    raise AgentError(
                        AssessmentFailure(
                            code=failure.code,
                            retryable=failure.retryable,
                            a2a_task_id=failure.a2a_task_id,
                            trace_id=failure.trace_id,
                        )
                    )
        if task.status.state == TaskState.TASK_STATE_COMPLETED and len(task.artifacts) == 1:
            parts = get_data_parts(task.artifacts[0].parts)
            if len(parts) == 1 and task.artifacts[0].name == "risk_result":
                result = RiskResultV1.model_validate(parts[0])
                if (
                    result.task_id == request.task_id
                    and result.job_id == request.job_id
                    and result.a2a_task_id == task.id
                    and result.rules_version == request.rules_version
                    and result.prompt_version == request.prompt_version
                ):
                    return result
    except ValidationError:
        raise agent_error("invalid_agent_response", retryable=False) from None
    raise agent_error("invalid_agent_response", retryable=False)
