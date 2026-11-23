from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tender_solver.errors import ErrorCode, SolverError
from tender_solver.models import SolveRequest, SolverResult
from tender_solver.solver import Solver

_STATUS: dict[ErrorCode, int] = {
    ErrorCode.KB_VERSION_NOT_FOUND: 404,
    ErrorCode.INCOMPLETE_INPUT: 422,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.INCOMPATIBLE_RULES: 409,
    ErrorCode.NO_SOLUTION: 422,
}


def create_app(solver: Solver) -> FastAPI:
    app = FastAPI(title="TenderHive Solver", version="0.1.0")

    @app.exception_handler(SolverError)
    async def on_solver_error(request: Request, error: SolverError) -> JSONResponse:
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
                "message": "solve request does not match the contract",
                "trace_id": request.headers.get("X-Trace-Id", str(uuid4())),
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/solve", response_model=SolverResult)
    async def solve(request: SolveRequest) -> SolverResult:
        return await solver.solve(request)

    return app
