import hmac
import logging
from datetime import date
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tender_kb.application import Knowledge
from tender_kb.dependencies import EVAL_HEADER
from tender_kb.domain import (
    Conflict,
    Context,
    ContextSelection,
    DependencyUnavailable,
    Identifier,
    IntegrityError,
    Text,
    VersionNotFound,
    VersionResult,
    VersionUpload,
)

_LOG = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    code: str
    message: str


def create_app(knowledge: Knowledge, eval_token: str) -> FastAPI:
    app = FastAPI(
        title="TenderHive Knowledge Base",
        version="1.0.0",
        responses={code: {"model": ErrorResponse} for code in (404, 409, 422, 503)},
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        if any(item["loc"][-1:] == ("checksum",) for item in error.errors()):
            return _error(422, "CHECKSUM_MISMATCH", "Checksum must be a lowercase SHA-256 digest.")
        return _error(422, "INTEGRITY_ERROR", "The knowledge request is invalid.")

    @app.exception_handler(IntegrityError)
    async def integrity_error(_: Request, error: IntegrityError) -> JSONResponse:
        return _error(422, "CHECKSUM_MISMATCH", str(error))

    @app.exception_handler(VersionNotFound)
    async def missing(_: Request, error: VersionNotFound) -> JSONResponse:
        return _error(404, "VERSION_NOT_FOUND", str(error))

    @app.exception_handler(Conflict)
    async def conflict(_: Request, error: Conflict) -> JSONResponse:
        return _error(409, "VERSION_CONFLICT", str(error))

    @app.exception_handler(DependencyUnavailable)
    async def unavailable(_: Request, error: DependencyUnavailable) -> JSONResponse:
        _LOG.warning("Knowledge dependency failed: %s", error)
        return _error(503, "DEPENDENCY_UNAVAILABLE", "Knowledge dependency is unavailable.")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        await knowledge.ready()
        return {"status": "ok"}

    @app.post("/api/v1/versions", status_code=201)
    async def create_version(payload: VersionUpload) -> VersionResult:
        return await knowledge.create(payload)

    @app.post("/api/v1/versions/{version}/activate")
    async def activate_version(version: Identifier) -> VersionResult:
        return await knowledge.activate(version)

    @app.post("/api/v1/versions/{version}/rollback")
    async def rollback_version(version: Identifier) -> VersionResult:
        return await knowledge.rollback(version)

    @app.get("/api/v1/context")
    async def get_context(
        version: Identifier | None = None,
        regime: Identifier | None = None,
        task_class: Identifier | None = None,
        mode: Identifier | None = None,
        lot_ids: Annotated[list[Identifier] | None, Query(max_length=100)] = None,
        query: Text | None = None,
        as_of: date | None = None,
        token: Annotated[str | None, Header(alias=EVAL_HEADER)] = None,
    ) -> Context:
        if mode is not None and task_class is not None and mode != task_class:
            raise Conflict("The mode and task_class parameters disagree.")
        selection = ContextSelection(
            lot_ids=tuple(sorted(set(lot_ids or []))),
            regime=regime,
            task_class=task_class or mode,
            query=query,
            as_of=as_of,
        )
        allow_check = token is not None and hmac.compare_digest(token.encode(), eval_token.encode())
        return await knowledge.context(version, selection, allow_check=allow_check)

    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=ErrorResponse(code=code, message=message).model_dump()
    )
