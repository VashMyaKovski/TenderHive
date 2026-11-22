from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

type Task = Literal["risk_assessment", "document_draft", "embeddings", "vision"]
type Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")
]
type Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)
]
type Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=10, decimal_places=6)]
type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
type Category = Literal["personal", "organization", "banking", "registration", "commercial"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PublicText(Contract):
    type: Literal["public_text"]
    text: Text


class SensitiveText(Contract):
    type: Literal["sensitive_text"]
    category: Category
    text: Text


class PublicImage(Contract):
    type: Literal["public_image"]
    data: Annotated[str, Field(min_length=1, max_length=2_800_000)]


class Penalty(Contract):
    type: Literal["penalty"]
    daily_percent: Percent


class Knowledge(Contract):
    type: Literal["knowledge"]
    rules_version: Identifier
    penalty_daily_threshold_percent: Percent


type ContextItem = Annotated[
    PublicText | SensitiveText | PublicImage | Penalty | Knowledge, Field(discriminator="type")
]


class InferenceRequest(Contract):
    job_id: UUID
    task: Task
    context: Annotated[list[ContextItem], Field(min_length=1, max_length=12)]
    prompt_version: Identifier
    mask: Literal[True]


class ModelRisk(Contract):
    summary: Text
    risk_level: Literal["low", "medium", "high"]
    penalty_daily_percent: Percent | None = None
    confidence: Confidence
    sources: Annotated[list[Annotated[int, Field(ge=1, strict=True)]], Field(max_length=12)]


class ModelText(Contract):
    text: Text
    confidence: Confidence


type EmbeddingVector = Annotated[
    list[Annotated[float, Field(allow_inf_nan=False, strict=True)]],
    Field(min_length=1, max_length=8192),
]


class ModelEmbeddings(Contract):
    vectors: Annotated[list[EmbeddingVector], Field(min_length=1, max_length=12)]


class RiskAnswer(Contract):
    kind: Literal["risk_assessment"] = "risk_assessment"
    hypothesis: ModelRisk
    source_ids: list[int]
    rules_version: Identifier
    rule_id: Literal["R-RISK-04"] = "R-RISK-04"
    penalty_daily_percent: Percent
    penalty_daily_threshold_percent: Percent
    threshold_exceeded: bool
    conflict: bool
    human_reason: Literal["penalty_above_threshold", "unclear_requirements"] | None


class TextAnswer(Contract):
    kind: Literal["document_draft", "vision"]
    text: Text
    human_reason: Literal["unclear_requirements"] | None


class EmbeddingAnswer(ModelEmbeddings):
    kind: Literal["embeddings"] = "embeddings"
    source_ids: list[int]


class InferenceResponse(Contract):
    job_id: UUID
    answer: Annotated[RiskAnswer | TextAnswer | EmbeddingAnswer, Field(discriminator="kind")]
    confidence: Confidence
    model_version: Identifier
    prompt_version: Identifier
    masked: Literal[True] = True
    trace_id: UUID
