from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from th_kb.application import KnowledgeBase
from th_kb.errors import ErrorCode, KbError
from th_kb.models import ActivationReport, Context, ContextRequest, VersionInfo, VersionPayload

_ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VERSION_NOT_FOUND: 404,
    ErrorCode.VERSION_EXISTS: 409,
    ErrorCode.CHECKSUM_MISMATCH: 422,
    ErrorCode.INTEGRITY_ERROR: 422,
    ErrorCode.INCOMPATIBLE_RULES: 422,
    ErrorCode.REGRESSION_DETECTED: 422,
    ErrorCode.STORAGE_UNAVAILABLE: 503,
}


def create_app(kb: KnowledgeBase) -> FastAPI:
    app = FastAPI(title="TenderHive Knowledge Base", version="0.1.0")

    @app.exception_handler(KbError)
    async def on_kb_error(request: Request, error: KbError) -> JSONResponse:
        return JSONResponse(
            status_code=_ERROR_STATUS[error.code],
            content={
                "code": str(error.code),
                "message": error.message,
                "trace_id": request.headers.get("X-Trace-Id", str(uuid4())),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "invalid_request",
                "message": "request does not match the version contract",
                "trace_id": request.headers.get("X-Trace-Id", str(uuid4())),
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/versions", status_code=201, response_model=VersionInfo)
    async def create_version(payload: VersionPayload) -> VersionInfo:
        return await kb.create_version(payload)

    @app.get("/api/v1/versions", response_model=list[VersionInfo])
    async def list_versions() -> list[VersionInfo]:
        return await kb.list_versions()

    @app.get("/api/v1/versions/{version}", response_model=VersionInfo)
    async def get_version(version: str) -> VersionInfo:
        return await kb.get_version(version)

    @app.post("/api/v1/versions/{version}/activate", response_model=ActivationReport)
    async def activate(version: str) -> ActivationReport:
        return await kb.activate(version)

    @app.post("/api/v1/versions/{version}/rollback", response_model=ActivationReport)
    async def rollback(version: str) -> ActivationReport:
        return await kb.rollback(version)

    @app.get("/api/v1/context", response_model=Context)
    async def context(
        http_request: Request,
        version: str | None = None,
        regime: str | None = None,
        task_class: str | None = None,
        query: str | None = None,
    ) -> Context:
        request = ContextRequest(
            version=version,
            regime=regime,
            task_class=task_class,
            lot_ids=http_request.query_params.getlist("lot_ids"),
            query=query,
        )
        return await kb.get_context(request)

    return app
