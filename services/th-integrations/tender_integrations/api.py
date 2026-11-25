import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from psycopg import Error as DatabaseError
from psycopg_pool import PoolTimeout
from redis.asyncio import Redis

from tender_integrations.application import IntakePolicy, Mailbox, MailIntake, OutboxRelay, repeat
from tender_integrations.config import Settings
from tender_integrations.mail import ExchangeMailbox, ImapMailbox
from tender_integrations.messaging import KafkaPublisher, RedisDedup
from tender_integrations.postgres import Postgres
from tender_integrations.stubs import stub_server

_LOG = logging.getLogger(__name__)
_BEARER = HTTPBearer(auto_error=False)


def create_app(settings: Settings) -> FastAPI:
    mcp = stub_server()
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        ),
    )
    database = Postgres(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(database.close)
            await database.open()
            await stack.enter_async_context(mcp.session_manager.run())
            redis: Redis = await stack.enter_async_context(
                Redis.from_url(
                    str(settings.redis_url),
                    decode_responses=True,
                    socket_connect_timeout=settings.io_timeout_seconds,
                    socket_timeout=settings.io_timeout_seconds,
                )
            )
            kafka = KafkaPublisher(settings.kafka_brokers, settings.io_timeout_seconds)
            stack.push_async_callback(kafka.close)

            mailbox: Mailbox
            if settings.imap is not None:
                mailbox = ImapMailbox(
                    settings.imap, settings.mail_timeout_seconds, settings.max_message_bytes
                )
            else:
                assert settings.exchange is not None
                http = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=settings.mail_timeout_seconds, trust_env=False)
                )
                mailbox = ExchangeMailbox(
                    http,
                    settings.exchange,
                    settings.max_message_bytes,
                    settings.mail_timeout_seconds,
                )

            traces = TracerProvider(resource=Resource.create({"service.name": "th-integrations"}))
            stack.push_async_callback(asyncio.to_thread, traces.shutdown)
            if settings.otlp_traces_endpoint is not None:
                traces.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(
                            endpoint=str(settings.otlp_traces_endpoint),
                            timeout=settings.io_timeout_seconds,
                        )
                    )
                )
            tracer = traces.get_tracer(__name__)
            intake = MailIntake(
                mailbox,
                database,
                RedisDedup(redis),
                IntakePolicy(
                    settings.mailbox_id,
                    settings.kb_version,
                    settings.profile_ref,
                    str(settings.document_base_url),
                    settings.max_attachment_bytes,
                ),
                tracer,
            )
            relay = OutboxRelay(database, kafka, settings.outbox_batch_size, tracer)
            workers = [
                asyncio.create_task(repeat(intake.poll, settings.mail_poll_seconds)),
                asyncio.create_task(repeat(relay.flush, settings.outbox_poll_seconds)),
            ]
            try:
                yield
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    app = FastAPI(title="TenderHive Integrations", lifespan=lifespan)
    secret = settings.document_token.get_secret_value().encode()

    @app.get("/api/v1/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    async def ready(request: Request) -> Response:
        if await database.ready():
            return JSONResponse({"status": "ready"})
        return _error(request, 503, "NOT_READY", "Mail integration storage is unavailable")

    @app.get("/api/v1/jobs")
    async def find_jobs(
        request: Request,
        external_ref: Annotated[str, Query(min_length=1, max_length=998)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> Response:
        if credentials is None or not compare_digest(credentials.credentials.encode(), secret):
            return _unauthorized(request)
        jobs = await database.lookup(external_ref)
        return JSONResponse(
            {"jobs": [{"job_id": str(job.job_id), "published": job.published} for job in jobs]},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/documents/{file_id}")
    async def document(
        request: Request,
        file_id: UUID,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> Response:
        if credentials is None or not compare_digest(credentials.credentials.encode(), secret):
            return _unauthorized(request)
        record = await database.document(file_id)
        if record is None:
            return _error(request, 404, "DOCUMENT_NOT_FOUND", "Document not found")
        return Response(
            record.content,
            media_type=record.mime,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(record.name, safe='')}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _: RequestValidationError) -> Response:
        return _error(request, 422, "INVALID_REQUEST", "Invalid request")

    async def database_failure(request: Request, error: Exception) -> Response:
        _LOG.warning("request storage error=%s", type(error).__name__)
        return _error(
            request, 503, "STORAGE_UNAVAILABLE", "Mail integration storage is unavailable"
        )

    app.add_exception_handler(DatabaseError, database_failure)
    app.add_exception_handler(PoolTimeout, database_failure)
    app.mount("/api/v1", mcp_app)
    return app


def _unauthorized(request: Request) -> JSONResponse:
    return _error(
        request,
        401,
        "UNAUTHORIZED",
        "Service credentials required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _error(
    request: Request, status: int, code: str, message: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    try:
        trace_id = UUID(request.headers.get("X-Trace-Id", ""))
    except ValueError:
        trace_id = uuid4()
    return JSONResponse(
        {"code": code, "message": message, "trace_id": str(trace_id)},
        status_code=status,
        headers=headers,
    )
