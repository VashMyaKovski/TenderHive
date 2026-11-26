from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from contracts.docintel_v1 import ErrorResponse, ExtractResponse
from tender_docintel.application import ExtractionService
from tender_docintel.config import Settings
from tender_docintel.document_reader import DocumentReader
from tender_docintel.errors import DocIntelError, ErrorCode
from tender_docintel.ports import CoreClient
from tender_docintel.rpc import GrpcCoreClient

_HTTP_STATUS: dict[ErrorCode, int] = {
    "UNSUPPORTED_FORMAT": 415,
    "FILE_TOO_LARGE": 413,
    "IMAGE_TOO_LARGE": 413,
    "TOO_MANY_PAGES": 413,
    "TOO_MANY_BLOCKS": 413,
    "CORRUPTED_FILE": 422,
    "OCR_UNAVAILABLE": 503,
    "TEMPLATE_NOT_FOUND": 404,
    "GRPC_UNAVAILABLE": 503,
    "CORE_REJECTED": 422,
    "CORE_FAILURE": 502,
}
_ERROR_MESSAGE: dict[ErrorCode, str] = {
    "UNSUPPORTED_FORMAT": "Only PDF, DOCX and supported image files are accepted.",
    "FILE_TOO_LARGE": "The uploaded file exceeds the configured size limit.",
    "IMAGE_TOO_LARGE": "The document image exceeds the configured pixel limit.",
    "TOO_MANY_PAGES": "The document exceeds the configured page limit.",
    "TOO_MANY_BLOCKS": "The document exceeds the configured block limit.",
    "CORRUPTED_FILE": "The document is damaged or contains no extractable text.",
    "OCR_UNAVAILABLE": "The OCR engine is unavailable.",
    "TEMPLATE_NOT_FOUND": "The requested requirement template version is unavailable.",
    "GRPC_UNAVAILABLE": "The document processing core is unavailable.",
    "CORE_REJECTED": "The document processing core rejected the extraction request.",
    "CORE_FAILURE": "The document processing core returned an invalid result.",
}


def _trace_id(request: Request) -> UUID:
    value = request.headers.get("X-Trace-Id")
    try:
        return UUID(value) if value is not None else uuid4()
    except ValueError:
        return uuid4()


def _error(request: Request, code: ErrorCode, status: int | None = None) -> JSONResponse:
    trace_id = _trace_id(request)
    body = ErrorResponse(code=code, message=_ERROR_MESSAGE[code], trace_id=trace_id)
    return JSONResponse(
        status_code=status or _HTTP_STATUS[code], content=body.model_dump(mode="json")
    )


def create_app(settings: Settings, core: CoreClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = core if core is not None else GrpcCoreClient(settings)
        reader = DocumentReader(
            max_pages=settings.max_pages,
            max_blocks=settings.max_blocks,
            max_image_pixels=settings.max_image_pixels,
            max_uncompressed_bytes=settings.max_uncompressed_bytes,
            ocr_lang=settings.ocr_lang,
            ocr_dpi=settings.ocr_dpi,
            chunk_size=settings.chunk_size,
            text_layer_min_chars=settings.text_layer_min_chars,
        )
        app.state.extraction_service = ExtractionService(settings, reader, client)
        try:
            yield
        finally:
            if core is None:
                await client.close()

    app = FastAPI(title="TenderHive Document Intelligence", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(DocIntelError)
    async def on_docintel_error(request: Request, error: DocIntelError) -> JSONResponse:
        return _error(request, error.code)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(
        request: Request, _error_value: RequestValidationError
    ) -> JSONResponse:
        trace_id = _trace_id(request)
        body = ErrorResponse(
            code="INVALID_REQUEST",
            message="Request does not match the extraction contract.",
            trace_id=trace_id,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, _error_value: Exception) -> JSONResponse:
        trace_id = _trace_id(request)
        body = ErrorResponse(
            code="INTERNAL_ERROR", message="Document extraction failed.", trace_id=trace_id
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.get("/healthz")
    async def health(request: Request) -> dict[str, str]:
        service: ExtractionService = request.app.state.extraction_service
        await service.check_health()
        return {"status": "ok"}

    @app.post("/api/v1/extract", response_model=ExtractResponse)
    async def extract(
        request: Request,
        job_id: Annotated[UUID, Form()],
        file_id: Annotated[UUID, Form()],
        file: Annotated[UploadFile, File()],
        template_set_version: Annotated[str | None, Form()] = None,
    ) -> ExtractResponse:
        service: ExtractionService = request.app.state.extraction_service
        try:
            content = await file.read(settings.max_file_bytes + 1)
            return await service.extract(
                job_id=job_id,
                file_id=file_id,
                content=content,
                template_set_version=template_set_version or settings.template_set_version,
                trace_id=_trace_id(request),
            )
        finally:
            await file.close()

    return app
