from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tender_integrations.adapters import mcp_result
from tender_integrations.config import Settings
from tender_integrations.errors import ErrorCode, IntegrationError
from tender_integrations.models import Job, McpResult
from tender_integrations.processor import MailProcessor

_STATUS = {
    ErrorCode.UNSUPPORTED_ATTACHMENT: 422,
    ErrorCode.DUPLICATE_MESSAGE: 409,
    ErrorCode.MAIL_UNAVAILABLE: 503,
    ErrorCode.MCP_STUB_ERROR: 404,
}


class PollResponse(BaseModel):
    jobs: list[Job]
    errors: list[str]


def create_app(processor: MailProcessor, settings: Settings) -> FastAPI:
    app = FastAPI(title="TenderHive Integrations", version="0.1.0")

    @app.exception_handler(IntegrationError)
    async def on_integration_error(request: Request, error: IntegrationError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS[error.code],
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
                "message": "request does not match the contract",
                "trace_id": request.headers.get("X-Trace-Id", str(uuid4())),
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/mail/poll", response_model=PollResponse)
    async def poll() -> PollResponse:
        jobs, errors = await processor.poll()
        return PollResponse(jobs=jobs, errors=errors)

    @app.post("/api/v1/mcp/{system}/{method}", response_model=McpResult)
    async def mcp(system: str, method: str) -> McpResult:
        return mcp_result(system, method, settings)

    return app
