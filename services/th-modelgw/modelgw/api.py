import logging
from asyncio import to_thread
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.trace import NoOpTracerProvider, Tracer
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from modelgw.application import Gateway
from modelgw.cache import RedisCache
from modelgw.config import Settings
from modelgw.errors import Failure, GatewayError
from modelgw.postgres import PostgresJournal
from modelgw.provider import OpenAICompatibleProvider
from modelgw.schemas import (
    InferenceRequest,
    InferenceResponse,
    InvokeRequest,
    InvokeResponse,
    ProviderReason,
    UnavailableResponse,
)
from modelgw.telemetry import traced_inference, tracer_provider

_LOG = logging.getLogger("uvicorn.error.modelgw")
_STATUS = {
    Failure.INVALID_CONTEXT: 422,
    Failure.UNSAFE_CONTEXT: 422,
    Failure.UNSUPPORTED_PROMPT: 422,
    Failure.TASK_DISABLED: 503,
    Failure.STORAGE_UNAVAILABLE: 503,
    Failure.PROVIDER_AUTH: 502,
    Failure.RATE_LIMIT: 503,
    Failure.PROVIDER_REJECTED: 502,
    Failure.PROVIDER_UNAVAILABLE: 503,
    Failure.PROVIDER_TIMEOUT: 504,
    Failure.INVALID_PROVIDER_RESPONSE: 502,
}
_PROVIDER_REASON: dict[Failure, ProviderReason] = {
    Failure.PROVIDER_TIMEOUT: "timeout",
    Failure.PROVIDER_AUTH: "auth",
    Failure.RATE_LIMIT: "rate_limit",
    Failure.PROVIDER_REJECTED: "schema_mismatch",
    Failure.INVALID_PROVIDER_RESPONSE: "schema_mismatch",
    Failure.PROVIDER_UNAVAILABLE: "unavailable",
}
_LEGACY_CODE = {
    Failure.PROVIDER_AUTH: Failure.PROVIDER_REJECTED,
    Failure.RATE_LIMIT: Failure.PROVIDER_UNAVAILABLE,
}
_MESSAGES = {
    "invalid_request": "The request does not match the required schema.",
    Failure.INVALID_CONTEXT: "The context does not match the requested task.",
    Failure.UNSAFE_CONTEXT: "The context contains data that could not be masked safely.",
    Failure.UNSUPPORTED_PROMPT: "The requested prompt version is unavailable.",
    Failure.TASK_DISABLED: "A model is not configured for the requested task.",
    Failure.STORAGE_UNAVAILABLE: "The model gateway journal is unavailable.",
}
_INVOKE_PATH = "/api/v1/invoke"
_TRACE_HEADER = "X-Trace-Id"


class ErrorResponse(BaseModel):
    code: Failure | Literal["invalid_request"]
    trace_id: UUID


class InputErrorResponse(ErrorResponse):
    message: str


def _error(status: int, error: ErrorResponse | UnavailableResponse) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error.model_dump(mode="json"),
        headers={_TRACE_HEADER: str(error.trace_id)},
    )


def _parse_trace(value: object) -> UUID | None:
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def gateway_for(request: Request) -> Gateway:
    return request.state.gateway


def journal_for(settings: Settings) -> PostgresJournal:
    key = settings.masking_key
    return PostgresJournal(
        settings.postgres_dsn.get_secret_value(),
        settings.db_timeout_seconds,
        Fernet(key.get_secret_value()) if key is not None else None,
    )


def create_app(settings: Settings) -> FastAPI:
    journal = journal_for(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Gateway]]:
        await journal.ready(settings.prompt_version)
        client = Redis.from_url(
            str(settings.redis_url),
            decode_responses=True,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            retry=Retry(NoBackoff(), 0),
        )
        async with AsyncExitStack() as exit_stack, client:
            models = settings.models()
            model_provider = None
            base_url, api_key = settings.provider_connection()
            embedding_input_type = (
                settings.embedding_input_type if settings.provider == "nvidia" else None
            )
            if base_url is not None and api_key is not None and any(models.values()):
                http = await exit_stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=str(base_url),
                        headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
                        timeout=settings.timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                    )
                )
                model_provider = OpenAICompatibleProvider(
                    http,
                    settings.timeout_seconds,
                    settings.max_retries,
                    settings.retry_delay_seconds,
                    settings.max_completion_tokens,
                    embedding_input_type,
                    request_json_output=settings.provider == "openrouter",
                )
            if settings.otlp_endpoint is not None:
                provider = tracer_provider(
                    str(settings.otlp_endpoint), settings.otlp_timeout_seconds
                )
                app.state.tracer = provider.get_tracer(__name__)
                exit_stack.push_async_callback(to_thread, provider.shutdown)
            yield {
                "gateway": Gateway(
                    provider=model_provider,
                    journal=journal,
                    cache=RedisCache(
                        client, settings.cache_ttl_seconds, settings.redis_timeout_seconds
                    ),
                    models=models,
                    prompt_version=settings.prompt_version,
                    confidence_floor=settings.confidence_floor,
                    cache_namespace=(
                        f"{settings.provider}|{base_url}|{embedding_input_type}"
                        f"|{settings.max_completion_tokens}"
                    ),
                )
            }

    app = FastAPI(title="TenderHive Model Gateway", version="1.0.0", lifespan=lifespan)
    app.state.tracer = NoOpTracerProvider().get_tracer(__name__)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        header_trace = _parse_trace(request.headers.get(_TRACE_HEADER))
        trace_id = header_trace or uuid4()
        if request.url.path == _INVOKE_PATH:
            if isinstance(error.body, dict):
                trace_id = _parse_trace(error.body.get("trace_id")) or trace_id
            _LOG.info("inference trace_id=%s outcome=invalid_request", trace_id)
            return _error(
                422,
                InputErrorResponse(
                    code="invalid_request", message=_MESSAGES["invalid_request"], trace_id=trace_id
                ),
            )
        _LOG.info("inference trace_id=%s outcome=invalid_request", trace_id)
        return _error(422, ErrorResponse(code="invalid_request", trace_id=trace_id))

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readiness() -> dict[str, str] | JSONResponse:
        try:
            await journal.ready(settings.prompt_version)
        except GatewayError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    @app.post(
        _INVOKE_PATH,
        response_model=InvokeResponse,
        responses={
            422: {"model": InputErrorResponse},
            502: {"model": UnavailableResponse},
            503: {"model": UnavailableResponse | InputErrorResponse},
            504: {"model": UnavailableResponse},
        },
    )
    async def invoke(
        body: InvokeRequest,
        request: Request,
        response: Response,
        gateway: Annotated[Gateway, Depends(gateway_for)],
    ) -> InvokeResponse | JSONResponse:
        trace_id = body.trace_id
        header_value = request.headers.get(_TRACE_HEADER)
        if header_value is not None and _parse_trace(header_value) != trace_id:
            return _error(
                422,
                InputErrorResponse(
                    code="invalid_request",
                    message="X-Trace-Id must match the request trace_id.",
                    trace_id=trace_id,
                ),
            )
        response.headers[_TRACE_HEADER] = str(trace_id)
        tracer: Tracer = request.app.state.tracer
        try:
            return await traced_inference(tracer, request.headers, gateway, body, trace_id)
        except GatewayError as error:
            reason = _PROVIDER_REASON.get(error.code)
            if reason is not None:
                return _error(
                    _STATUS[error.code],
                    UnavailableResponse(job_id=body.job_id, reason=reason, trace_id=trace_id),
                )
            return _error(
                _STATUS[error.code],
                InputErrorResponse(
                    code=error.code, message=_MESSAGES[error.code], trace_id=trace_id
                ),
            )

    @app.post(
        "/api/v1/inference",
        response_model=InferenceResponse,
        responses={code: {"model": ErrorResponse} for code in (422, 502, 503, 504)},
    )
    async def inference(
        body: InferenceRequest,
        request: Request,
        response: Response,
        gateway: Annotated[Gateway, Depends(gateway_for)],
    ) -> InferenceResponse | JSONResponse:
        trace_id = _parse_trace(request.headers.get(_TRACE_HEADER)) or uuid4()
        response.headers[_TRACE_HEADER] = str(trace_id)
        tracer: Tracer = request.app.state.tracer
        try:
            return await traced_inference(tracer, request.headers, gateway, body, trace_id)
        except GatewayError as error:
            return _error(
                _STATUS[error.code],
                ErrorResponse(code=_LEGACY_CODE.get(error.code, error.code), trace_id=trace_id),
            )

    return app
