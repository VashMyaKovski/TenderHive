import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from psycopg import Error as PostgresError
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from contracts.models import Contract
from tender_orchestrator.db import Database
from tender_orchestrator.models import ReviewCard
from tender_orchestrator.workbench_extraction import confirm
from tender_orchestrator.workbench_flow import Run, WorkbenchSettings, new_run, work_run
from tender_orchestrator.workbench_inputs import (
    MAX_BODY_BYTES,
    DecisionInput,
    ExtractionConfirmation,
    RunInput,
    confirmed_normalization_record,
    load_uploads,
    normalization_record,
)
from tender_orchestrator.workbench_store import WorkbenchStore

_STATIC = Path(__file__).with_name("web_static")
_FINISHED = {"done", "declined", "failed", "needs_clarification", "extraction_failed"}
_LOG = logging.getLogger("uvicorn.error.workbench")
_ARTIFACT_MIMES = {
    "offer.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "offer.pdf": "application/pdf",
    "offer.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _local_write(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} or (
        origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}"
    ):
        raise HTTPException(status_code=403, detail="Запрос отправлен с другого сайта.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(status_code=415, detail="Ожидается запрос JSON.")


async def _body[Model: BaseModel](request: Request, schema: type[Model], limit: int) -> Model:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Размер запроса превышает лимит.")
        body.extend(chunk)
    try:
        return schema.model_validate_json(body)
    except ValidationError as error:
        details = ", ".join(
            ".".join(map(str, item["loc"]))
            for item in error.errors(include_input=False, include_url=False, include_context=False)
        )
        raise HTTPException(
            status_code=422,
            detail=f"Проверьте поля запроса: {details}." if details else "Некорректный JSON.",
        ) from None


def _file(body: bytes, name: str, content_type: str) -> Response:
    return Response(
        body,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"},
    )


class RetryExtraction(Contract):
    pass


def create_app(settings: WorkbenchSettings) -> FastAPI:
    runs: dict[UUID, Run] = {}
    load_lock = asyncio.Lock()
    dsn, storage = settings.web_database_dsn, settings.web_storage_dir
    if (dsn is None) != (storage is None):
        raise ValueError("Configure both Workbench PostgreSQL and storage settings.")
    store = (
        WorkbenchStore(Database(dsn.get_secret_value(), settings.web_max_runs), storage)
        if dsn is not None and storage is not None
        else None
    )

    async def perform(run: Run, decision: tuple[ReviewCard, DecisionInput] | None) -> None:
        await work_run(settings, run, decision)
        run.pending = False
        run.pending_decision = None
        try:
            await run.checkpoint()
        except (OSError, PostgresError) as error:
            _LOG.warning("could not save run_id=%s (%s)", run.id, type(error).__name__)
            run.error = "Не удалось сохранить состояние прогона."

    def restore_decision(run: Run) -> tuple[ReviewCard, DecisionInput] | None:
        body = run.pending_decision
        if body is not None and run.view is not None:
            card = next((item for item in run.view.cards if item.card_id == body.card_id), None)
            if card is not None:
                return card, body
        return None

    async def launch(run: Run, decision: tuple[ReviewCard, DecisionInput] | None = None) -> None:
        run.pending = True
        run.pending_decision = (
            decision[1].model_copy(update={"card_id": decision[0].card_id})
            if decision is not None
            else None
        )
        try:
            await run.checkpoint()
        except (OSError, PostgresError) as error:
            run.pending = False
            run.pending_decision = None
            raise HTTPException(503, detail="Не удалось сохранить изменения прогона.") from error
        run.task = asyncio.create_task(perform(run, decision))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            await store.ready()
            for run_id in await store.pending_ids():
                run = await store.get(run_id)
                if run is not None:
                    runs[run_id] = run
                    run.task = asyncio.create_task(perform(run, restore_decision(run)))
        try:
            yield
        finally:
            tasks = [run.task for run in runs.values() if run.task is not None and run.is_running]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = ["localhost", "127.0.0.1"]
    if settings.web_url.host is not None and settings.web_url.host not in allowed_hosts:
        allowed_hosts.append(settings.web_url.host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=_STATIC, check_dir=False), name="static")

    @app.middleware("http")
    async def protect_demo_api(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = settings.web_service_token
        if token is not None and request.url.path.startswith("/api/workbench/"):
            supplied = request.headers.get("X-Workbench-Token", "")
            if not compare_digest(supplied.encode(), token.get_secret_value().encode()):
                return JSONResponse({"detail": "Workbench service authentication required."}, 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'"
        return response

    async def get_run(run_id: UUID) -> Run:
        async with load_lock:
            if run_id in runs:
                return runs[run_id]
            if store is not None:
                try:
                    run = await store.get(run_id)
                except (OSError, PostgresError) as error:
                    raise HTTPException(503, detail="Хранилище прогонов недоступно.") from error
                if run is not None:
                    runs[run_id] = run
                    return run
        raise HTTPException(status_code=404, detail="Прогон не найден.")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/workbench/runs", dependencies=[Depends(_local_write)], status_code=202)
    async def start(request: Request) -> dict[str, object]:
        incoming = await _body(request, RunInput, MAX_BODY_BYTES)
        if settings.web_docintel_url is None and incoming.facts is None:
            raise HTTPException(422, detail="Заполните сведения для ручного прогона.")
        run_id = uuid4()
        try:
            uploads = load_uploads(run_id, incoming, str(settings.web_url))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if len(runs) >= settings.web_max_runs:
            finished = next(
                (
                    item.id
                    for item in runs.values()
                    if item.phase in _FINISHED and not item.is_running
                ),
                None,
            )
            if finished is None:
                raise HTTPException(status_code=429, detail="Дождитесь завершения другого прогона.")
            del runs[finished]
        run = new_run(
            run_id, incoming, uploads, normalization_record(run_id, incoming, str(settings.web_url))
        )
        if store is not None:
            run.save = store.save
        runs[run_id] = run
        try:
            await launch(run)
        except HTTPException:
            del runs[run_id]
            raise
        return run.snapshot()

    @app.get("/api/workbench/runs/{run_id}")
    async def status(run: Annotated[Run, Depends(get_run)]) -> dict[str, object]:
        return run.snapshot()

    @app.post(
        "/api/workbench/runs/{run_id}/extraction/confirm",
        dependencies=[Depends(_local_write)],
        status_code=202,
    )
    async def confirm_extraction(
        request: Request, run: Annotated[Run, Depends(get_run)]
    ) -> dict[str, object]:
        body = await _body(request, ExtractionConfirmation, 4 * 1024**2)
        earlier = run.extraction_review
        if run.is_running or run.phase != "awaiting_extraction_confirmation" or earlier is None:
            raise HTTPException(409, detail="Разбор уже подтверждён или ещё выполняется.")
        try:
            reviewed = confirm(earlier, body)
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from None
        normalization = confirmed_normalization_record(
            run.id, run.title, body.facts, body.context, str(settings.web_url)
        )
        previous = run.facts, run.context, run.normalization
        run.facts, run.context, run.normalization = body.facts, body.context, normalization
        run.extraction_review = reviewed
        run.extraction_confirmed = True
        try:
            await launch(run)
        except HTTPException:
            run.facts, run.context, run.normalization = previous
            run.extraction_review = earlier
            run.extraction_confirmed = False
            raise
        return run.snapshot()

    @app.post(
        "/api/workbench/runs/{run_id}/extraction/retry",
        dependencies=[Depends(_local_write)],
        status_code=202,
    )
    async def retry_extraction(
        request: Request, run: Annotated[Run, Depends(get_run)]
    ) -> dict[str, object]:
        await _body(request, RetryExtraction, 256)
        if run.is_running or run.phase != "extraction_failed":
            raise HTTPException(409, detail="Повторный разбор для этого прогона недоступен.")
        earlier = run.extraction_review
        run.extraction_review = None
        try:
            await launch(run)
        except HTTPException:
            run.extraction_review = earlier
            raise
        return run.snapshot()

    @app.post(
        "/api/workbench/runs/{run_id}/decision",
        dependencies=[Depends(_local_write)],
        status_code=202,
    )
    async def decide(request: Request, run: Annotated[Run, Depends(get_run)]) -> dict[str, object]:
        body = await _body(request, DecisionInput, 4096)
        if body.decision == "REJECTED" and not body.comment:
            raise HTTPException(status_code=422, detail="Укажите причину отклонения.")
        card = run.card
        if (
            card is None
            or run.is_running
            or run.error is not None
            or (body.card_id is not None and body.card_id != card.card_id)
        ):
            raise HTTPException(status_code=409, detail="Карточка уже изменилась или закрыта.")
        if run.requires_comment(card) and not body.comment:
            raise HTTPException(status_code=422, detail="Укажите обоснование согласования.")
        if card.reason_code == "RESOURCE_GAP" and body.decision == "APPROVED":
            if (
                run.facts is None
                or run.facts.delivery_deadline is None
                or body.supply_confirmation is None
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Укажите срок, объём, дату и дополнительные затраты на поставку.",
                )
        elif body.supply_confirmation is not None:
            raise HTTPException(
                status_code=422, detail="Подтверждение поставки относится к карточке дефицита."
            )
        await launch(run, (card, body))
        return run.snapshot()

    @app.get("/api/workbench/runs/{run_id}/documents/{file_id}")
    async def document(file_id: UUID, run: Annotated[Run, Depends(get_run)]) -> Response:
        upload = next(
            (
                item
                for item in [*run.uploads, run.normalization]
                if item.document.file_id == file_id
            ),
            None,
        )
        if upload is None:
            raise HTTPException(status_code=404, detail="Документ не найден.")
        return _file(upload.data, upload.document.name, upload.document.mime)

    @app.get("/api/workbench/runs/{run_id}/artifacts/{name}")
    async def artifact(name: str, run: Annotated[Run, Depends(get_run)]) -> Response:
        if run.phase != "done" or name not in run.artifacts:
            raise HTTPException(status_code=404, detail="Файл комплекта пока недоступен.")
        body = run.artifacts[name]
        response = _file(body, name, _ARTIFACT_MIMES[name])
        response.headers["X-Artifact-SHA256"] = sha256(body).hexdigest()
        return response

    return app


if __name__ == "__main__":
    settings = WorkbenchSettings.model_validate({})
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.web_port)
