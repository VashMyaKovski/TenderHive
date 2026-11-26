from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Coordinate = Annotated[float, Field(allow_inf_nan=False)]
type BBox = tuple[Coordinate, Coordinate, Coordinate, Coordinate]
type TemplateVersion = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
]
type RequirementKind = Literal[
    "penalty", "deadline", "security", "qualification", "resource", "financial", "other"
]
type NumberKind = Literal["date", "number", "percent"]
type NumberCondition = Literal["exact", "at_least", "at_most"]
type NumberRole = Literal["value", "upper_bound"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceReference(Contract):
    file_id: UUID
    page: Annotated[int, Field(ge=1)]
    block_id: UUID
    bbox: BBox

    @model_validator(mode="after")
    def validate_bbox(self) -> SourceReference:
        x0, y0, x1, y1 = self.bbox
        if x1 < x0 or y1 < y0:
            raise ValueError("bounding box coordinates are invalid")
        return self


class ExtractedRequirement(Contract):
    requirement_id: UUID
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    kind: RequirementKind
    mandatory: bool
    value: str | None
    unit: str | None
    source: SourceReference
    confidence: Confidence
    needs_review: bool
    template_set_version: TemplateVersion


class NumberValue(Contract):
    value: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    unit: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    kind: NumberKind
    condition: NumberCondition
    role: NumberRole
    source: SourceReference
    confidence: Confidence


class TableRow(Contract):
    row_index: Annotated[int, Field(ge=0)]
    cells: list[str]
    source: SourceReference


class ExtractedTable(Contract):
    rows: list[TableRow]
    sum_check: bool | None


class ExtractionWarning(Contract):
    code: Literal[
        "LOW_OCR_CONFIDENCE",
        "SUM_MISMATCH",
        "NO_REQUIREMENTS_MATCHED",
        "REQUIREMENT_INCOMPLETE",
    ]
    message: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    source: SourceReference | None


class ExtractResponse(Contract):
    job_id: UUID
    requirements: list[ExtractedRequirement]
    numbers: list[NumberValue]
    table: ExtractedTable
    status: Literal["OK", "INCOMPLETE", "REJECTED"]
    status_reason: str | None
    warnings: list[ExtractionWarning]
    trace_id: UUID


class ErrorResponse(Contract):
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")]
    message: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    trace_id: UUID
