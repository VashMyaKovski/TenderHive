import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from a2a.helpers.proto_helpers import get_data_parts, new_data_message, new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Role,
    TaskState,
)
from a2a.utils.errors import UnsupportedOperationError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, TypeAdapter, ValidationError

from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1
from tender_agents.config import Settings
from tender_agents.gateway import GatewayFailure, ModelGateway

_LOG = logging.getLogger("uvicorn.error.risk_agent")
_RPC_PATH = "/a2a"
_JSON = "application/json"
_RPC_ERROR_MESSAGES = {
    -32700: "Parse error",
    -32600: "Invalid Request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
}
_SCHEMA = TypeAdapter(
    Annotated[RiskTaskV1 | RiskResultV1 | RiskFailureV1, Field(discriminator="kind")]
)


def _identifier(data: object, key: str) -> UUID | None:
    value = data.get(key) if isinstance(data, Mapping) else None
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


class RiskExecutor(AgentExecutor):
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is not None:
            raise UnsupportedOperationError(message="Submit a new message for each assessment")

        started = monotonic()
        task_id = context.task_id or str(uuid4())
        context_id = context.context_id or str(uuid4())
        await event_queue.enqueue_event(
            new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED)
        )
        updater = TaskUpdater(event_queue, task_id, context_id)
        message = context.message
        parts = get_data_parts(message.parts) if message is not None else []
        data = parts[0] if len(parts) == 1 else None
        application_id = _identifier(data, "task_id")
        job_id = _identifier(data, "job_id")
        trace_id = None
        outcome = "ok"

        try:
            if message is None or message.role != Role.ROLE_USER or len(message.parts) != 1:
                raise GatewayFailure("invalid_request", None)
            try:
                request = RiskTaskV1.model_validate(data)
            except ValidationError:
                raise GatewayFailure("invalid_request", None) from None
            result = await self._gateway.assess(request, task_id)
            trace_id = result.trace_id
            await updater.add_artifact(
                [new_data_part(result.model_dump(mode="json"), media_type=_JSON)],
                name="risk_result",
            )
            await updater.complete()
        except GatewayFailure as error:
            trace_id = error.trace_id
            outcome = error.code
            failure = RiskFailureV1(
                kind="risk_failure.v1",
                task_id=application_id,
                job_id=job_id,
                a2a_task_id=task_id,
                trace_id=trace_id,
                code=error.code,
                retryable=error.retryable,
            )
            await updater.failed(
                new_data_message(
                    failure.model_dump(mode="json"),
                    media_type=_JSON,
                    task_id=task_id,
                    context_id=context_id,
                )
            )

        _LOG.info(
            "risk_assessment task_id=%s job_id=%s a2a_task_id=%s trace_id=%s outcome=%s duration_ms=%d",
            application_id,
            job_id,
            task_id,
            trace_id,
            outcome,
            (monotonic() - started) * 1000,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id is None or context.context_id is None:
            raise UnsupportedOperationError(message="Task context is unavailable")
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def create_app(settings: Settings, gateway_transport: httpx.AsyncBaseTransport | None) -> FastAPI:
    client = httpx.AsyncClient(
        base_url=str(settings.model_gateway_url),
        timeout=settings.model_gateway_timeout_seconds,
        trust_env=False,
        follow_redirects=False,
        transport=gateway_transport,
    )
    gateway = ModelGateway(client, settings.model_gateway_timeout_seconds)
    card = AgentCard(
        name="TenderHive Risk Agent",
        description="Assesses contract risk through the TenderHive model gateway.",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=str(settings.public_url).rstrip("/") + _RPC_PATH,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=[_JSON],
        default_output_modes=[_JSON],
        skills=[
            AgentSkill(
                id="risk_assessment",
                name="Contract risk assessment",
                description="Assess the risk described in a reviewed document fragment.",
                tags=["risk", "procurement"],
            )
        ],
    )
    handler = DefaultRequestHandler(RiskExecutor(gateway), InMemoryTaskStore(), card)
    dispatcher = JsonRpcDispatcher(handler)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with client:
            yield

    app = FastAPI(title="TenderHive Risk Agent", lifespan=lifespan)
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card))

    @app.post(_RPC_PATH, include_in_schema=False)
    async def json_rpc(request: Request) -> Response:
        response = await dispatcher.handle_requests(request)
        if isinstance(response, JSONResponse):
            envelope = json.loads(bytes(response.body))
            if "error" in envelope:
                # The SDK may include the original validation exception in a protocol error.
                code = envelope["error"]["code"]
                _LOG.info("a2a_protocol outcome=error code=%s", code)
                envelope["error"] = {
                    "code": code,
                    "message": _RPC_ERROR_MESSAGES.get(code, "A2A request failed"),
                }
                return JSONResponse(envelope, status_code=response.status_code)
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/schemas/risk-v1.json")
    async def schema() -> dict[str, object]:
        return _SCHEMA.json_schema(mode="validation")

    return app
