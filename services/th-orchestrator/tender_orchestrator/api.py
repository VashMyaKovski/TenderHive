import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, Error
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from tender_orchestrator.agent import AgentClient
from tender_orchestrator.application import Orchestrator
from tender_orchestrator.config import Settings
from tender_orchestrator.db import Database, connection_info
from tender_orchestrator.graph import RiskGraph
from tender_orchestrator.models import (
    Clock,
    DecisionRequest,
    ErrorCode,
    JobView,
    Reviewer,
    StartJob,
    WorkflowError,
)
from tender_orchestrator.ports import RiskAgent

_LOG = logging.getLogger("uvicorn.error.orchestrator")
_ERROR_STATUS: dict[ErrorCode, int] = {
    "job_not_found": 404,
    "card_not_found": 404,
    "access_denied": 403,
    "job_conflict": 409,
    "decision_conflict": 409,
}


async def _poll(operation: Callable[[], Awaitable[None]], interval: float) -> None:
    while True:
        try:
            await operation()
        except Exception as error:
            _LOG.error("poll operation=%s error=%s", operation.__name__, type(error).__name__)
        await asyncio.sleep(interval)


def _token(auth: HTTPAuthorizationCredentials | None) -> bytes:
    if auth is None or not auth.credentials.isascii():
        raise _unauthenticated()
    return auth.credentials.encode("ascii")


def _unauthenticated() -> HTTPException:
    return HTTPException(
        401, detail="authentication_required", headers={"WWW-Authenticate": "Bearer"}
    )


def create_app(settings: Settings, clock: Clock, agent: RiskAgent | None) -> FastAPI:
    conninfo = connection_info(settings)
    database = Database(conninfo, settings.database_max_connections)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[dict[str, Orchestrator]]:
        await database.check_ready()
        pool = AsyncConnectionPool[AsyncConnection[DictRow]](
            conninfo,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
            min_size=0,
            max_size=settings.database_max_connections,
            timeout=settings.database_timeout_seconds,
            open=False,
        )
        async with (
            pool,
            httpx.AsyncClient(
                timeout=settings.agent_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as http,
        ):
            client = (
                agent
                if agent is not None
                else AgentClient(http, str(settings.agent_url), settings.agent_timeout_seconds)
            )
            graph = RiskGraph(
                AsyncPostgresSaver(pool),
                client,
                clock,
                max_attempts=settings.agent_max_attempts,
                retry_delay_seconds=settings.agent_retry_delay_seconds,
                review_role=settings.review_role,
                review_timeout_seconds=settings.review_timeout_seconds,
            )
            service = Orchestrator(database, graph, clock, settings.poll_batch_size)
            workers = [
                asyncio.create_task(_poll(operation, settings.poll_interval_seconds))
                for operation in (service.recover_pending, service.expire_due)
            ]
            try:
                yield {"orchestrator": service}
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    app = FastAPI(title="TenderHive Orchestrator", version="1.0.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    service_token = settings.service_token.get_secret_value().encode("ascii")

    async def trusted_service(
        auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if not compare_digest(_token(auth), service_token):
            raise _unauthenticated()

    async def authorised_reviewer(
        auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Reviewer:
        token = _token(auth)
        for reviewer in settings.reviewers:
            if compare_digest(token, reviewer.token.get_secret_value().encode("ascii")):
                return reviewer
        raise _unauthenticated()

    def orchestrator(request: Request) -> Orchestrator:
        return request.state.orchestrator

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, error: WorkflowError) -> JSONResponse:
        return JSONResponse({"code": error.code}, status_code=_ERROR_STATUS[error.code])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"code": "invalid_request"}, status_code=422)

    @app.exception_handler(Error)
    async def database_error(_: Request, error: Error) -> JSONResponse:
        _LOG.error("database_error cause=%s", type(error).__name__)
        return JSONResponse({"code": "database_unavailable"}, status_code=503)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        await database.check_ready()
        return {"status": "ok"}

    @app.post("/api/v1/jobs", dependencies=[Depends(trusted_service)])
    async def start(
        body: StartJob, service: Annotated[Orchestrator, Depends(orchestrator)]
    ) -> JobView:
        return await service.start(body)

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(trusted_service)])
    async def status(
        job_id: UUID, service: Annotated[Orchestrator, Depends(orchestrator)]
    ) -> JobView:
        return await service.status(job_id)

    @app.post("/api/v1/jobs/{job_id}/cards/{card_id}/decision")
    async def decide(
        job_id: UUID,
        card_id: UUID,
        body: DecisionRequest,
        reviewer: Annotated[Reviewer, Depends(authorised_reviewer)],
        service: Annotated[Orchestrator, Depends(orchestrator)],
    ) -> JobView:
        return await service.decide(job_id, card_id, reviewer, body)

    return app
