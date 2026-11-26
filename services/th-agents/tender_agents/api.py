import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from hmac import compare_digest
from math import ceil
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
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, TypeAdapter, ValidationError
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from contracts.agent_task import (
    AgentCompleted,
    AgentResult,
    AgentTask,
    DocumentAgentTask,
    TypedAgentTask,
)
from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1
from tender_agents.artifacts import MIME_TYPES, ArtifactStore, FileExtension
from tender_agents.assessment import Assessment
from tender_agents.completions import publish_completions
from tender_agents.config import Settings
from tender_agents.gateway import GatewayFailure, ModelGateway
from tender_agents.ledger import RedisRiskLedger, RiskLedger

_LOG = logging.getLogger("uvicorn.error.tender_agents")
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
_TASK_INPUT = TypeAdapter(TypedAgentTask)
_AGENT_SCHEMA = TypeAdapter(TypedAgentTask | AgentResult)
_COMPLETION_SCHEMA = TypeAdapter(AgentCompleted)
_SKILLS = (
    ("risk_assessment", "Contract risk assessment", "Assess a reviewed contract fragment."),
    (
        "extraction",
        "Requirement normalization",
        "Review normalized requirements and their sources.",
    ),
    ("legal", "Legal review", "Report the procurement regime and flagged clauses."),
    ("resource", "Resource coverage", "Compare required and available quantities."),
    ("estimate", "Estimate review", "Calculate cost and compare margin to the supplied threshold."),
    ("supply", "Supply alternatives", "Check supplier options against quantities and deadlines."),
    ("document", "Proposal documents", "Render the approved proposal, risk report, and estimate."),
)


def _identifier(data: object, key: str) -> UUID | None:
    value = data.get(key) if isinstance(data, Mapping) else None
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _authorized(token: str, authorization: object) -> bool:
    return isinstance(authorization, str) and compare_digest(
        authorization.encode(), f"Bearer {token}".encode()
    )


class Executor(AgentExecutor):
    def __init__(self, assessment: Assessment, document_token: str) -> None:
        self._assessment = assessment
        self._document_token = document_token

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
        trace_id = _identifier(data, "trace_id")
        outcome = "ok"
        task = None

        try:
            if message is None or message.role != Role.ROLE_USER or len(message.parts) != 1:
                raise GatewayFailure("invalid_request", trace_id)
            try:
                if isinstance(data, Mapping) and "agent_role" in data:
                    task = AgentTask.model_validate(data)
                    request = _TASK_INPUT.validate_python(data)
                else:
                    request = RiskTaskV1.model_validate(data)
            except ValidationError:
                raise GatewayFailure("invalid_request", trace_id) from None
            if isinstance(request, DocumentAgentTask):
                headers = context.call_context.state.get("headers", {})
                if not _authorized(self._document_token, headers.get("authorization")):
                    raise GatewayFailure("document_unauthorized", trace_id)
            result = await self._assessment.assess(request, task_id)
            trace_id = result.trace_id
            if isinstance(result, AgentResult) and result.status == "FAILED":
                outcome = str(result.result["code"])
                await updater.failed(
                    new_data_message(
                        result.model_dump(mode="json"),
                        media_type=_JSON,
                        task_id=task_id,
                        context_id=context_id,
                    )
                )
            else:
                await updater.add_artifact(
                    [new_data_part(result.model_dump(mode="json"), media_type=_JSON)],
                    name=f"{task.agent_role if task is not None else 'risk'}_result",
                )
                await updater.complete()
        except GatewayFailure as error:
            trace_id = error.trace_id
            outcome = error.code
            if task is None:
                failure = RiskFailureV1(
                    kind="risk_failure.v1",
                    task_id=application_id,
                    job_id=job_id,
                    a2a_task_id=task_id,
                    trace_id=trace_id,
                    code=error.code,
                    retryable=error.retryable,
                )
            else:
                failure = AgentResult(
                    task_id=task.task_id,
                    job_id=task.job_id,
                    agent_role=task.agent_role,
                    status="FAILED",
                    result={"code": error.code, "retryable": error.retryable},
                    evidence=[],
                    model_calls=[],
                    trace_id=trace_id or task.trace_id,
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
            "agent_assessment task_id=%s job_id=%s a2a_task_id=%s trace_id=%s outcome=%s duration_ms=%d",
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


def create_app(
    settings: Settings,
    gateway_transport: httpx.AsyncBaseTransport | None,
    risk_ledger: RiskLedger | None,
) -> FastAPI:
    client = httpx.AsyncClient(
        base_url=str(settings.model_gateway_url),
        timeout=settings.model_gateway_timeout_seconds,
        trust_env=False,
        follow_redirects=False,
        transport=gateway_transport,
    )
    redis = Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_connect_timeout=settings.redis_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )
    redis_ledger = RedisRiskLedger(redis, settings.task_ttl_seconds, settings.task_lease_seconds)
    ledger = risk_ledger if risk_ledger is not None else redis_ledger
    gateway = ModelGateway(client, settings.model_gateway_timeout_seconds)
    artifacts = ArtifactStore(settings.artifact_root, settings.pdf_font_path)
    document_token = settings.artifact_token.get_secret_value()
    assessment = Assessment(
        gateway, ledger, artifacts, settings.task_wait_seconds, settings.task_poll_seconds
    )
    card = AgentCard(
        name="TenderHive Procurement Agents",
        description="Analyzes procurement evidence and prepares approved proposal artifacts.",
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
            AgentSkill(id=role, name=name, description=description, tags=["procurement"])
            for role, name, description in _SKILLS
        ],
    )
    handler = DefaultRequestHandler(Executor(assessment, document_token), InMemoryTaskStore(), card)
    dispatcher = JsonRpcDispatcher(handler)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with client, redis:
            await asyncio.to_thread(artifacts.prepare)
            if risk_ledger is not None:
                yield
                return

            publisher = asyncio.create_task(
                publish_completions(
                    redis_ledger,
                    settings.kafka_brokers,
                    settings.completion_poll_seconds,
                    settings.completion_timeout_seconds,
                    ceil(
                        (2 * settings.completion_timeout_seconds + settings.redis_timeout_seconds)
                        * 1000
                    ),
                ),
                name="agent-completions",
            )
            try:
                yield
            finally:
                publisher.cancel()
                with suppress(asyncio.CancelledError):
                    await publisher

    app = FastAPI(title="TenderHive Procurement Agents", lifespan=lifespan)
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
    async def health() -> JSONResponse:
        try:
            ready = await ledger.ready()
        except RedisError:
            ready = False
        return JSONResponse(
            {"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503
        )

    @app.get("/schemas/risk-v1.json", include_in_schema=False)
    @app.get("/api/v1/schemas/risk-v1.json")
    async def schema() -> dict[str, object]:
        return _SCHEMA.json_schema(mode="validation")

    @app.get("/api/v1/schemas/agent-task.json")
    async def agent_schema() -> dict[str, object]:
        return _AGENT_SCHEMA.json_schema(mode="validation")

    @app.get("/api/v1/schemas/agent-completed.json")
    async def completion_schema() -> dict[str, object]:
        return _COMPLETION_SCHEMA.json_schema(mode="validation")

    @app.get("/api/v1/artifacts/{job_id}/{task_id}/{extension}")
    async def download(
        job_id: UUID, task_id: UUID, extension: FileExtension, request: Request
    ) -> FileResponse:
        if not _authorized(document_token, request.headers.get("authorization")):
            raise HTTPException(
                401, "Service authorization required", {"WWW-Authenticate": "Bearer"}
            )
        path = artifacts.file_path(job_id, task_id, extension)
        if not await asyncio.to_thread(path.is_file):
            raise HTTPException(404, "Artifact not found")
        return FileResponse(
            path,
            media_type=MIME_TYPES[extension],
            filename=path.name,
            headers={"Cache-Control": "private, no-store"},
        )

    return app
