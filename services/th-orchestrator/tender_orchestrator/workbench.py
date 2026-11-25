import asyncio
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
from pydantic import BaseModel, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from tender_orchestrator.workbench_flow import Run, WorkbenchSettings, new_run, work_run
from tender_orchestrator.workbench_inputs import (
    MAX_BODY_BYTES,
    DecisionInput,
    RunInput,
    load_uploads,
    normalization_record,
)

_STATIC = Path(__file__).with_name("web_static")
_FINISHED = {"done", "declined", "failed", "needs_clarification"}
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


def create_app(settings: WorkbenchSettings) -> FastAPI:
    runs: dict[UUID, Run] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
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

    def get_run(run_id: UUID) -> Run:
        if run_id not in runs:
            raise HTTPException(status_code=404, detail="Прогон не найден.")
        return runs[run_id]

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/workbench/runs", dependencies=[Depends(_local_write)], status_code=202)
    async def start(request: Request) -> dict[str, object]:
        incoming = await _body(request, RunInput, MAX_BODY_BYTES)
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
        runs[run_id] = run
        run.task = asyncio.create_task(work_run(settings, run, None))
        return run.snapshot()

    @app.get("/api/workbench/runs/{run_id}")
    async def status(run: Annotated[Run, Depends(get_run)]) -> dict[str, object]:
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
        run.task = asyncio.create_task(work_run(settings, run, (card, body)))
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
