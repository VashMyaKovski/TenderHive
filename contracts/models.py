from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

type JobStatus = Literal[
    "ACCEPTED", "PROCESSING", "NEEDS_CLARIFICATION", "WAITING_HUMAN", "DONE", "ERROR"
]
type StopReason = Literal[
    "PENALTY_ABOVE_THRESHOLD",
    "LOW_MARGIN",
    "RESOURCE_GAP",
    "UNCLEAR_REQUIREMENTS",
    "NO_SOLUTION",
    "FINAL_APPROVAL",
]
type HumanRole = Literal["LAWYER", "DIVISION_HEAD", "SUPPLY", "TENDER_SPECIALIST", "ADMINISTRATOR"]
type DecisionValue = Literal["APPROVED", "REJECTED", "NEEDS_INFO"]
type NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type Checksum = Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{64}$")]
type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


type UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc)]

_LEGACY_JOB_STATUS: dict[str, JobStatus] = {
    "принято": "ACCEPTED",
    "обрабатывается": "PROCESSING",
    "ожидает уточнения": "NEEDS_CLARIFICATION",
    "ожидает решения человека": "WAITING_HUMAN",
    "завершено": "DONE",
    "ошибка": "ERROR",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Document(Contract):
    file_id: UUID
    name: NonEmpty
    mime: NonEmpty
    checksum: Checksum
    storage_path: NonEmpty


class LegacyDocument(Contract):
    file_id: UUID
    name: NonEmpty
    source: NonEmpty


class Job(Contract):
    job_id: UUID
    idempotency_key: NonEmpty
    source: Literal["mail", "telegram", "web"]
    external_ref: NonEmpty | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    status: JobStatus
    kb_version: NonEmpty
    lot_ids: list[NonEmpty]
    documents: list[Document | LegacyDocument]
    profile_ref: NonEmpty
    trace_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_historical_status(cls, data: object) -> object:
        if isinstance(data, Mapping):
            status = data.get("status")
            if isinstance(status, str) and status in _LEGACY_JOB_STATUS:
                return {**data, "status": _LEGACY_JOB_STATUS[status]}
        return data

    @model_validator(mode="after")
    def validate_timestamps(self) -> Job:
        if self.updated_at is not None and self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self


class RequirementSource(Contract):
    file_id: UUID
    page: Annotated[int, Field(ge=1)]
    block_id: UUID
    bbox: tuple[float, float, float, float]


class Requirement(Contract):
    requirement_id: UUID
    text: NonEmpty
    kind: Literal[
        "penalty", "deadline", "security", "qualification", "resource", "financial", "other"
    ]
    mandatory: bool
    value: JsonValue
    unit: NonEmpty | None
    source: RequirementSource
    confidence: Confidence
    needs_review: bool
    template_set_version: NonEmpty


class Compliance(Contract):
    requirement_id: UUID
    status: Literal["MET", "NOT_MET", "NEEDS_REVIEW"]
    fact_ref: UUID | None
    rule_id: NonEmpty
    reason: NonEmpty


class ResourceCheck(Contract):
    position_id: NonEmpty
    covered: bool
    shortage: JsonValue
    supply_deadline: UtcDatetime | None
    alternatives: list[JsonValue]


class Estimate(Contract):
    positions: list[JsonValue]
    total_cost: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    offer_price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    margin_percent: Annotated[Decimal, Field(allow_inf_nan=False)]
    threshold: Annotated[Decimal, Field(allow_inf_nan=False)]
    threshold_source: JsonValue


class Risk(Contract):
    risk_id: UUID
    category: NonEmpty
    level: NonEmpty
    basis: NonEmpty
    mitigation: JsonValue
    source_ref: JsonValue


class Recommendation(Contract):
    value: Literal["PARTICIPATE", "NOT_PARTICIPATE", "PARTICIPATE_WITH_CONDITIONS"]
    conditions: list[JsonValue]


class PreparationPlan(Contract):
    steps: list[JsonValue]
    deadlines: list[JsonValue]
    owners: list[JsonValue]


class Explanation(Contract):
    steps: list[JsonValue]
    rule_trace: list[JsonValue]
    source_refs: list[JsonValue]


class Artifact(Contract):
    artifact_id: UUID
    kind: Literal["DOCX", "PDF", "XLSX"]
    file_path: NonEmpty
    version: NonEmpty
    kb_version: NonEmpty
    checksum: Checksum


class EmptyResultPart(Contract):
    pass


class SolverResult(Contract):
    job_id: UUID
    kb_version: NonEmpty
    status: Literal["READY", "INCOMPLETE", "NO_SOLUTION"]
    requirements: list[Requirement]
    compliance: list[Compliance]
    resources: list[ResourceCheck]
    estimate: Estimate | EmptyResultPart
    risks: list[Risk]
    recommendation: Recommendation | EmptyResultPart
    plan: PreparationPlan | EmptyResultPart
    explanation: Explanation
    artifacts: list[Artifact]
    stop_reasons: list[StopReason]


class HumanDecision(Contract):
    decision_id: UUID
    job_id: UUID
    reason_code: StopReason
    role: HumanRole
    question: NonEmpty
    deadline: UtcDatetime
    timeout: bool
    decision: DecisionValue
    comment: str
    decided_at: UtcDatetime
