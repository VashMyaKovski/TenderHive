from typing import Protocol
from uuid import UUID

from tender_docintel.models import DocumentBlock
from tender_docintel.proto import docintel_pb2


class CoreClient(Protocol):
    async def extract(
        self,
        job_id: UUID,
        file_id: UUID,
        blocks: list[DocumentBlock],
        template_set_version: str,
        trace_id: UUID,
    ) -> docintel_pb2.ExtractRequirementsResponse: ...

    async def check_health(self) -> None: ...

    async def close(self) -> None: ...
