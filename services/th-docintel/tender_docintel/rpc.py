from __future__ import annotations

from uuid import UUID

import grpc

from tender_docintel.config import Settings
from tender_docintel.errors import DocIntelError
from tender_docintel.models import DocumentBlock
from tender_docintel.proto import docintel_pb2 as pb
from tender_docintel.proto.docintel_pb2_grpc import DocIntelStub


class GrpcCoreClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._channel = grpc.aio.insecure_channel(
            settings.grpc_target,
            options=(
                ("grpc.max_send_message_length", settings.grpc_max_message_bytes),
                ("grpc.max_receive_message_length", settings.grpc_max_message_bytes),
            ),
        )
        self._stub = DocIntelStub(self._channel)

    async def extract(
        self,
        job_id: UUID,
        file_id: UUID,
        blocks: list[DocumentBlock],
        template_set_version: str,
        trace_id: UUID,
    ) -> pb.ExtractRequirementsResponse:
        kinds = {
            "text": pb.BLOCK_KIND_TEXT,
            "table_cell": pb.BLOCK_KIND_TABLE_CELL,
            "table_row": pb.BLOCK_KIND_TABLE_ROW,
        }
        request = pb.ExtractRequirementsRequest(
            job_id=str(job_id),
            job_doc_id=str(file_id),
            template_set_version=template_set_version,
            blocks=[
                pb.Block(
                    block_id=str(block.block_id),
                    page=block.page,
                    text=block.text,
                    bbox=pb.BoundingBox(
                        x0=block.bbox[0],
                        y0=block.bbox[1],
                        x1=block.bbox[2],
                        y1=block.bbox[3],
                    ),
                    kind=kinds[block.kind],
                    ocr_confidence=block.ocr_confidence,
                )
                for block in blocks
            ],
        )
        try:
            return await self._stub.ExtractRequirements(
                request,
                timeout=self._settings.grpc_timeout_seconds,
                wait_for_ready=True,
                metadata=(("x-trace-id", str(trace_id)),),
            )
        except grpc.aio.AioRpcError as error:
            match error.code():
                case grpc.StatusCode.NOT_FOUND:
                    raise DocIntelError("TEMPLATE_NOT_FOUND") from None
                case grpc.StatusCode.UNAVAILABLE | grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise DocIntelError("GRPC_UNAVAILABLE") from None
                case grpc.StatusCode.RESOURCE_EXHAUSTED:
                    raise DocIntelError("TOO_MANY_BLOCKS") from None
                case grpc.StatusCode.INVALID_ARGUMENT:
                    raise DocIntelError("CORE_REJECTED") from None
                case _:
                    raise DocIntelError("CORE_FAILURE") from None

    async def check_health(self) -> None:
        try:
            result = await self._stub.CheckHealth(
                pb.HealthRequest(), timeout=self._settings.grpc_timeout_seconds
            )
        except grpc.aio.AioRpcError:
            raise DocIntelError("GRPC_UNAVAILABLE") from None
        if not result.ready:
            raise DocIntelError("GRPC_UNAVAILABLE")

    async def close(self) -> None:
        await self._channel.close()
