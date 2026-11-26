from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Coordinate = Annotated[float, Field(allow_inf_nan=False)]
type BBox = tuple[Coordinate, Coordinate, Coordinate, Coordinate]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class DocumentBlock(Contract):
    block_id: UUID
    page: Annotated[int, Field(ge=1)]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    bbox: BBox
    kind: Literal["text", "table_cell", "table_row"]
    ocr_confidence: Confidence

    @model_validator(mode="after")
    def validate_bbox(self) -> DocumentBlock:
        x0, y0, x1, y1 = self.bbox
        if x1 < x0 or y1 < y0:
            raise ValueError("bounding box coordinates are invalid")
        return self
