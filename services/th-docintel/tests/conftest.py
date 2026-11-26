from collections.abc import Iterator
from ipaddress import IPv4Address
from uuid import UUID, uuid4

import pytest

from tender_docintel.config import Settings
from tender_docintel.models import DocumentBlock
from tender_docintel.proto import docintel_pb2 as pb


@pytest.fixture
def settings() -> Settings:
    return Settings(
        grpc_host="127.0.0.1",
        grpc_port=50051,
        grpc_timeout_seconds=2,
        grpc_max_message_bytes=1_000_000,
        max_file_mb=1,
        max_uncompressed_mb=4,
        max_pages=20,
        max_blocks=1000,
        max_image_pixels=2_000_000,
        ocr_lang="rus+eng",
        ocr_dpi=150,
        chunk_size=12000,
        text_layer_min_chars=20,
        confidence_floor=0.75,
        table_sum_tolerance=0.01,
        template_set_version="ts-test-v1",
        bind_host=IPv4Address("127.0.0.1"),
        port=8000,
    )


class FakeCore:
    def __init__(self, *, low_confidence: bool = False, health_error: bool = False) -> None:
        self.low_confidence = low_confidence
        self.health_error = health_error
        self.calls = 0
        self.last_trace_id: UUID | None = None

    async def extract(
        self,
        job_id: UUID,
        file_id: UUID,
        blocks: list[DocumentBlock],
        template_set_version: str,
        trace_id: UUID,
    ) -> pb.ExtractRequirementsResponse:
        del file_id, template_set_version
        self.calls += 1
        self.last_trace_id = trace_id
        block = blocks[0]
        confidence = 0.62 if self.low_confidence else block.ocr_confidence
        response = pb.ExtractRequirementsResponse(
            job_id=str(job_id),
            status=pb.EXTRACTION_STATUS_INCOMPLETE
            if self.low_confidence
            else pb.EXTRACTION_STATUS_OK,
            status_reason="REVIEW_REQUIRED" if self.low_confidence else "",
        )
        response.requirements.add(
            template_id="penalty",
            text=block.text,
            kind="penalty",
            mandatory=True,
            value="0.0015",
            unit="fraction_per_day",
            source_block_id=str(block.block_id),
            page=block.page,
            bbox={
                "x0": block.bbox[0],
                "y0": block.bbox[1],
                "x1": block.bbox[2],
                "y1": block.bbox[3],
            },
            confidence=confidence,
            needs_review=self.low_confidence,
        )
        response.numbers.add(
            value="0.0015",
            unit="fraction_per_day",
            kind="percent",
            condition="exact",
            role="value",
            source_block_id=str(block.block_id),
            page=block.page,
            confidence=confidence,
            bbox={
                "x0": block.bbox[0],
                "y0": block.bbox[1],
                "x1": block.bbox[2],
                "y1": block.bbox[3],
            },
        )
        return response

    async def check_health(self) -> None:
        if self.health_error:
            from tender_docintel.errors import DocIntelError

            raise DocIntelError("GRPC_UNAVAILABLE")

    async def close(self) -> None:
        return None


@pytest.fixture
def fake_core() -> Iterator[FakeCore]:
    yield FakeCore()


@pytest.fixture
def identifiers() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()
