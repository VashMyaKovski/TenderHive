from __future__ import annotations

import asyncio
from uuid import UUID, uuid5

from contracts.docintel_v1 import ExtractedRequirement as Requirement
from contracts.docintel_v1 import (
    ExtractionWarning,
    ExtractResponse,
    NumberCondition,
    NumberKind,
    NumberRole,
    NumberValue,
    RequirementKind,
    SourceReference,
)
from tender_docintel.config import Settings
from tender_docintel.document_reader import DocumentReader
from tender_docintel.errors import DocIntelError
from tender_docintel.models import DocumentBlock
from tender_docintel.ports import CoreClient
from tender_docintel.proto import docintel_pb2 as pb
from tender_docintel.tables import assess_tables

_REQUIREMENT_KINDS: dict[str, RequirementKind] = {
    value: value
    for value in (
        "penalty",
        "deadline",
        "security",
        "qualification",
        "resource",
        "financial",
        "other",
    )
}
_NUMBER_KINDS: dict[str, NumberKind] = {value: value for value in ("date", "number", "percent")}
_NUMBER_CONDITIONS: dict[str, NumberCondition] = {
    value: value for value in ("exact", "at_least", "at_most")
}
_NUMBER_ROLES: dict[str, NumberRole] = {value: value for value in ("value", "upper_bound")}


class ExtractionService:
    def __init__(self, settings: Settings, reader: DocumentReader, core: CoreClient) -> None:
        self._settings = settings
        self._reader = reader
        self._core = core

    async def check_health(self) -> None:
        await self._core.check_health()

    async def extract(
        self,
        *,
        job_id: UUID,
        file_id: UUID,
        content: bytes,
        template_set_version: str,
        trace_id: UUID,
    ) -> ExtractResponse:
        if not content:
            raise DocIntelError("CORRUPTED_FILE")
        if len(content) > self._settings.max_file_bytes:
            raise DocIntelError("FILE_TOO_LARGE")
        if len(template_set_version) > 100:
            raise DocIntelError("TEMPLATE_NOT_FOUND")

        parsed = await asyncio.to_thread(self._reader.parse, content, file_id)
        if not parsed.blocks:
            raise DocIntelError("CORRUPTED_FILE")
        if len(parsed.blocks) > self._settings.max_blocks:
            raise DocIntelError("TOO_MANY_BLOCKS")

        table_assessment = assess_tables(parsed.tables, file_id, self._settings.table_sum_tolerance)
        try:
            result = await self._core.extract(
                job_id,
                file_id,
                parsed.blocks,
                template_set_version,
                trace_id,
            )
        except DocIntelError:
            raise
        except Exception:
            raise DocIntelError("GRPC_UNAVAILABLE") from None

        if result.job_id != str(job_id):
            raise DocIntelError("CORE_FAILURE")
        if result.status == pb.EXTRACTION_STATUS_REJECTED:
            raise DocIntelError("CORE_REJECTED")
        if result.status not in (pb.EXTRACTION_STATUS_OK, pb.EXTRACTION_STATUS_INCOMPLETE):
            raise DocIntelError("CORE_FAILURE")

        blocks_by_id = {str(block.block_id): block for block in parsed.blocks}
        requirements: list[Requirement] = []
        numbers: list[NumberValue] = []
        warnings = list(table_assessment.warnings)
        reasons: list[str] = []
        for requirement in result.requirements:
            block = blocks_by_id.get(requirement.source_block_id)
            if block is None:
                raise DocIntelError("CORE_FAILURE")
            kind = _REQUIREMENT_KINDS.get(requirement.kind)
            if kind is None:
                raise DocIntelError("CORE_FAILURE")
            source = _source(file_id, block)
            needs_review = (
                requirement.needs_review or requirement.confidence < self._settings.confidence_floor
            )
            requirements.append(
                Requirement(
                    requirement_id=uuid5(
                        file_id,
                        f"{template_set_version}:{requirement.template_id}:{requirement.source_block_id}",
                    ),
                    text=requirement.text,
                    kind=kind,
                    mandatory=requirement.mandatory,
                    value=requirement.value or None,
                    unit=requirement.unit or None,
                    source=source,
                    confidence=requirement.confidence,
                    needs_review=needs_review,
                    template_set_version=template_set_version,
                )
            )
            if needs_review:
                code = (
                    "LOW_OCR_CONFIDENCE"
                    if requirement.confidence < self._settings.confidence_floor
                    else "REQUIREMENT_INCOMPLETE"
                )
                message = (
                    "OCR confidence is below the configured review threshold."
                    if code == "LOW_OCR_CONFIDENCE"
                    else "A required value could not be extracted."
                )
                warnings.append(ExtractionWarning(code=code, message=message, source=source))

        for number in result.numbers:
            block = blocks_by_id.get(number.source_block_id)
            if block is None:
                raise DocIntelError("CORE_FAILURE")
            kind = _NUMBER_KINDS.get(number.kind)
            condition = _NUMBER_CONDITIONS.get(number.condition)
            role = _NUMBER_ROLES.get(number.role)
            if kind is None or condition is None or role is None:
                raise DocIntelError("CORE_FAILURE")
            numbers.append(
                NumberValue(
                    value=number.value,
                    unit=number.unit,
                    kind=kind,
                    condition=condition,
                    role=role,
                    source=_source(file_id, block),
                    confidence=number.confidence,
                )
            )

        if result.status == pb.EXTRACTION_STATUS_INCOMPLETE and result.status_reason:
            reasons.append(result.status_reason)
        if not requirements and not any(
            item.code == "NO_REQUIREMENTS_MATCHED" for item in warnings
        ):
            warnings.append(
                ExtractionWarning(
                    code="NO_REQUIREMENTS_MATCHED",
                    message="No configured requirement template matched the extracted document.",
                    source=_source(file_id, parsed.blocks[0]),
                )
            )
            reasons.append("NO_REQUIREMENTS_MATCHED")
        if table_assessment.table.sum_check is False:
            reasons.append("SUM_MISMATCH")
        if any(item.code == "LOW_OCR_CONFIDENCE" for item in warnings):
            reasons.append("LOW_OCR_CONFIDENCE")
        if any(item.code == "REQUIREMENT_INCOMPLETE" for item in warnings):
            reasons.append("REQUIREMENT_INCOMPLETE")

        status = "INCOMPLETE" if reasons or warnings else "OK"
        return ExtractResponse(
            job_id=job_id,
            requirements=requirements,
            numbers=numbers,
            table=table_assessment.table,
            status=status,
            status_reason=";".join(dict.fromkeys(reasons)) or None,
            warnings=warnings,
            trace_id=trace_id,
        )


def _source(file_id: UUID, block: DocumentBlock) -> SourceReference:
    return SourceReference(
        file_id=file_id,
        page=block.page,
        block_id=block.block_id,
        bbox=block.bbox,
    )
