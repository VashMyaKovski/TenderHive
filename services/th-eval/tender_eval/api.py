import logging
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from tender_eval.application import compare_saved_runs, evaluate_run
from tender_eval.cases import DatasetError
from tender_eval.config import ApiSettings
from tender_eval.knowledge import router_for
from tender_eval.run_models import ErrorResponse, RunRequest, RunResult
from tender_eval.storage import RunInProgress, Store
from tender_eval.telemetry import publish_metrics

_LOG = logging.getLogger(__name__)
_TRACE_HEADER = "X-Trace-Id"


class EvaluationError(HTTPException):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(status_code=status, detail=message)
        self.code = code


def _error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    trace_id = request.state.trace_id
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(code=code, message=message, trace_id=trace_id).model_dump(
            mode="json"
        ),
        headers={_TRACE_HEADER: str(trace_id)},
    )


def create_app(settings: ApiSettings, store: Store) -> FastAPI:
    app = FastAPI(
        title="TenderHive Evaluation",
        version="1.0.0",
        responses={status: {"model": ErrorResponse} for status in (422, 503)},
    )
    app.include_router(router_for(settings))

    @app.middleware("http")
    async def correlate(request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            trace_id = UUID(request.headers.get(_TRACE_HEADER, ""))
        except ValueError:
            trace_id = uuid4()
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[_TRACE_HEADER] = str(trace_id)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _: RequestValidationError) -> JSONResponse:
        return _error(request, 422, "invalid_request", "Invalid evaluation request.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        if isinstance(error, EvaluationError):
            return _error(request, error.status_code, error.code, error.detail)
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        response = _error(
            request, error.status_code, code, "Evaluation request could not be served."
        )
        response.headers.update(error.headers or {})
        return response

    async def storage_error(request: Request, error: Exception) -> JSONResponse:
        _LOG.warning(
            "evaluation store unavailable trace_id=%s (%s)",
            request.state.trace_id,
            type(error).__name__,
        )
        return _error(request, 503, "storage_unavailable", "Evaluation store unavailable.")

    app.add_exception_handler(psycopg.Error, storage_error)
    app.add_exception_handler(TimeoutError, storage_error)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        _LOG.error(
            "evaluation failed trace_id=%s (%s)", request.state.trace_id, type(error).__name__
        )
        return _error(request, 500, "internal_error", "Evaluation request failed.")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        if not await store.ready():
            raise EvaluationError(503, "schema_unavailable", "Evaluation schema is unavailable.")
        return {"status": "ok"}

    @app.post(
        "/api/v1/runs",
        status_code=201,
        responses={
            200: {"model": RunResult},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
        },
    )
    async def create_run(body: RunRequest, response: Response) -> RunResult:
        try:
            async with store.claim(body.run_id) as runs:
                existing = await runs.get(body.run_id)
                if existing is not None:
                    original, result = existing
                    if original != body:
                        raise EvaluationError(
                            409, "run_conflict", "Run ID belongs to a different request."
                        )
                    response.status_code = 200
                    return result
                dataset = await runs.dataset(body.suite, body.kb_version)
                if dataset is None:
                    raise EvaluationError(
                        404, "suite_not_found", "Suite and KB version are unavailable."
                    )
                result, observations = await evaluate_run(
                    settings, dataset, body, settings.api_mode
                )
                await runs.save(body, result, observations)
        except RunInProgress:
            raise EvaluationError(409, "run_in_progress", "Run is already in progress.") from None
        except DatasetError as error:
            raise EvaluationError(422, "invalid_dataset", str(error)) from None

        if result.gateway is not None and settings.otlp_metrics_url is not None:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                exported = await publish_metrics(
                    client,
                    str(settings.otlp_metrics_url),
                    settings.otlp_timeout_seconds,
                    result.gateway,
                )
            if not exported:
                _LOG.warning("metrics export failed for evaluation run %s", body.run_id)
        return result

    @app.get("/api/v1/runs/{run_id}", responses={404: {"model": ErrorResponse}})
    async def get_run(run_id: UUID) -> RunResult:
        async with store.read() as runs:
            existing = await runs.get(run_id)
        if existing is None:
            raise EvaluationError(404, "run_not_found", "Evaluation run not found.")
        return existing[1]

    @app.get(
        "/api/v1/runs/{run_id}/comparison",
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def comparison(run_id: UUID, baseline_run_id: UUID) -> dict[str, object]:
        async with store.read() as runs:
            before = await runs.get(baseline_run_id)
            after = await runs.get(run_id)
            if before is None or after is None:
                raise EvaluationError(404, "run_not_found", "Evaluation run not found.")
            baseline, candidate = before[1], after[1]
            baseline_cases = await runs.dataset(baseline.suite, baseline.kb_version)
            candidate_cases = await runs.dataset(candidate.suite, candidate.kb_version)
        if baseline_cases is None or candidate_cases is None:
            raise EvaluationError(409, "inputs_unavailable", "Evaluation inputs are unavailable.")
        try:
            return compare_saved_runs(baseline, candidate, baseline_cases, candidate_cases)
        except DatasetError as error:
            raise EvaluationError(409, "incompatible_runs", str(error)) from None

    return app
