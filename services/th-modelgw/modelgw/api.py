import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from modelgw.application import Gateway
from modelgw.cache import RedisCache
from modelgw.config import Settings
from modelgw.errors import Failure, GatewayError
from modelgw.openrouter import OpenRouter
from modelgw.schemas import InferenceRequest, InferenceResponse

_LOG = logging.getLogger("uvicorn.error.modelgw")
_STATUS = {
    Failure.INVALID_CONTEXT: 422,
    Failure.UNSAFE_CONTEXT: 422,
    Failure.UNSUPPORTED_PROMPT: 422,
    Failure.TASK_DISABLED: 503,
    Failure.PROVIDER_REJECTED: 502,
    Failure.PROVIDER_UNAVAILABLE: 503,
    Failure.PROVIDER_TIMEOUT: 504,
    Failure.INVALID_PROVIDER_RESPONSE: 502,
}


class ErrorResponse(BaseModel):
    code: Failure | Literal["invalid_request"]
    trace_id: UUID


def _error(status: int, error: ErrorResponse) -> JSONResponse:
    return JSONResponse(status_code=status, content=error.model_dump(mode="json"))


def gateway_for(request: Request) -> Gateway:
    return request.state.gateway


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[dict[str, Gateway]]:
        client = Redis.from_url(
            str(settings.redis_url),
            decode_responses=True,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            retry=Retry(NoBackoff(), 0),
        )
        async with (
            client,
            httpx.AsyncClient(
                base_url=str(settings.openrouter_url),
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}"
                },
                timeout=settings.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as http,
        ):
            yield {
                "gateway": Gateway(
                    provider=OpenRouter(
                        http,
                        settings.timeout_seconds,
                        settings.max_retries,
                        settings.retry_delay_seconds,
                        settings.max_completion_tokens,
                        settings.synthetic_provider_collection,
                    ),
                    cache=RedisCache(
                        client, settings.cache_ttl_seconds, settings.redis_timeout_seconds
                    ),
                    models=settings.models(),
                    prompt_version=settings.prompt_version,
                    confidence_floor=settings.confidence_floor,
                    cache_namespace=f"{settings.openrouter_url}|{settings.synthetic_provider_collection}",
                )
            }

    app = FastAPI(title="TenderHive Model Gateway", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        trace_id = uuid4()
        _LOG.info("inference trace_id=%s outcome=invalid_request", trace_id)
        return _error(422, ErrorResponse(code="invalid_request", trace_id=trace_id))

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/v1/inference",
        response_model=InferenceResponse,
        responses={code: {"model": ErrorResponse} for code in (422, 502, 503, 504)},
    )
    async def inference(
        body: InferenceRequest, gateway: Annotated[Gateway, Depends(gateway_for)]
    ) -> InferenceResponse | JSONResponse:
        trace_id = uuid4()
        try:
            return await gateway.infer(body, trace_id)
        except GatewayError as error:
            return _error(_STATUS[error.code], ErrorResponse(code=error.code, trace_id=trace_id))

    return app
