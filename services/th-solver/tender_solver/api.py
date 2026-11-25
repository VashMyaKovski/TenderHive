from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import psycopg
from contracts.models import SolverResult
from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tender_solver.config import Settings
from tender_solver.models import SolveRequest
from tender_solver.rules import solve
from tender_solver.storage import TraceStore


def _error(trace_id: UUID, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        {"code": code, "message": code.replace("_", " ").capitalize(), "trace_id": str(trace_id)},
        status_code=status,
        headers={"X-Trace-Id": str(trace_id)},
    )


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    store = TraceStore(settings.postgres_dsn.get_secret_value(), settings.timeout_seconds)
    client = httpx.AsyncClient(
        base_url=str(settings.kb_url),
        timeout=settings.timeout_seconds,
        trust_env=False,
        transport=transport,
    )
    redis = Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        socket_timeout=settings.timeout_seconds,
        socket_connect_timeout=settings.timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with client, redis:
            yield

    app = FastAPI(title="TenderHive solver", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, _: RequestValidationError) -> Response:
        try:
            trace_id = UUID(request.headers.get("X-Trace-Id", ""))
        except ValueError:
            trace_id = uuid4()
        return _error(trace_id, 422, "INVALID_INPUT")

    @app.get("/healthz")
    async def health() -> Response:
        try:
            await store.ready()
        except psycopg.Error, TimeoutError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/api/v1/solve", response_model=SolverResult)
    async def solution(
        body: SolveRequest,
        request: Request,
        response: Response,
        evaluation_token: Annotated[str | None, Header(alias="X-KB-Eval-Token")] = None,
    ) -> SolverResult | Response:
        trace = str(body.trace_id)
        supplied_trace = request.headers.get("X-Trace-Id")
        if supplied_trace is not None and supplied_trace != trace:
            return _error(body.trace_id, 422, "INVALID_TRACE_ID")
        response.headers["X-Trace-Id"] = trace
        headers = {"X-Trace-Id": trace}
        if evaluation_token is not None:
            if not compare_digest(
                evaluation_token.encode(), settings.kb_eval_token.get_secret_value().encode()
            ):
                return _error(body.trace_id, 403, "EVAL_ACCESS_DENIED")
            headers["X-KB-Eval-Token"] = evaluation_token

        params = [
            ("version", body.kb_version),
            ("regime", body.profile.regime),
            ("task_class", body.profile.task_class),
        ]
        params.extend(("lot_ids", lot) for lot in body.lot_ids)
        as_of = body.as_of or datetime.now(UTC).date()
        body = body.model_copy(update={"as_of": as_of})
        params.append(("as_of", as_of.isoformat()))
        kinds = sorted({item.kind for item in body.requirements})
        if kinds:
            params.append(("query", " ".join(kinds)))
        try:
            kb = await client.get("/api/v1/context", params=tuple(params), headers=headers)
            if kb.status_code == 404:
                return _error(body.trace_id, 404, "KB_VERSION_NOT_FOUND")
            kb.raise_for_status()
            context = kb.json()
        except httpx.HTTPError, ValueError:
            return _error(body.trace_id, 503, "KB_UNAVAILABLE")
        if not isinstance(context, dict) or context.get("version") != body.kb_version:
            return _error(body.trace_id, 502, "KB_VERSION_MISMATCH")

        fingerprint = sha256(body.model_dump_json(exclude={"trace_id"}).encode()).hexdigest()
        cache_key = f"solver:result:{settings.margin_threshold_percent}:{fingerprint}"
        result = None
        if evaluation_token is None:
            try:
                cached = await redis.get(cache_key)
                if cached:
                    result = SolverResult.model_validate_json(cached)
            except RedisError, ValidationError:
                pass
        cache_hit = result is not None
        if result is None:
            result = solve(body, context, settings.margin_threshold_percent)
        try:
            await store.record(body.trace_id, result, cache_hit)
        except psycopg.Error, TimeoutError:
            return _error(body.trace_id, 503, "TRACE_STORE_UNAVAILABLE")
        if evaluation_token is None and not cache_hit and result.status == "READY":
            with suppress(RedisError):
                await redis.set(
                    cache_key, result.model_dump_json(), ex=settings.result_cache_ttl_seconds
                )
        return result

    return app
