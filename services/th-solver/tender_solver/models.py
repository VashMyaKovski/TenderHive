from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Requirement(Contract):
    requirement_id: UUID
    text: Text
    kind: Literal["penalty", "deadline", "security", "qualification", "resource", "other"]
    value: str | None = None
    mandatory: bool
    confidence: Annotated[float, Field(ge=0, le=1)]
    source_ref: Text


class FactInput(Contract):
    key: Identifier
    value: JsonValue
    subject: Identifier | None = None
    unit: str | None = None


class SolveRequest(Contract):
    job_id: UUID
    kb_version: Identifier
    requirements: list[Requirement]
    facts: list[FactInput]
    profile: dict[str, JsonValue]
    lot_ids: list[Identifier] = Field(default_factory=list)
    trace_id: UUID
    regime: Identifier | None = None
    task_class: Identifier | None = None


class Rule(Contract):
    rule_id: Identifier
    condition: Text
    consequence: Text
    priority: Annotated[int, Field(ge=0, le=1000)]
    scope: Identifier
    version: Annotated[int, Field(ge=1)]


class KnowledgeContext(Contract):
    version: Identifier
    rules: list[Rule]
    facts: list[FactInput]
    reference_data: dict[str, JsonValue]
    graph_slice: dict[str, JsonValue]
    corpus_fragments: list[JsonValue]


class Compliance(Contract):
    requirement_id: UUID
    status: Literal["MET", "NOT_MET", "NEEDS_REVIEW"]
    rule_id: Identifier | None
    reason: Text
    source_ref: Text


class ResourceCheck(Contract):
    position_id: Identifier
    covered: bool
    shortage: str | None = None
    supply_deadline: str | None = None


class Estimate(Contract):
    total_cost: str
    offer_price: str
    margin_percent: str
    threshold: str
    threshold_source: Identifier


class Risk(Contract):
    risk_id: Identifier
    category: Literal["penalty", "quality", "delivery", "other"]
    level: Literal["low", "medium", "high"]
    basis: Text
    mitigation: Text
    source_ref: Text


class Plan(Contract):
    steps: list[Text]
    deadlines: list[Text]
    owners: list[Identifier]


class ExplanationStep(Contract):
    step: Text
    rule_id: Identifier | None
    version: Identifier
    source_ref: Text | None = None


class Explanation(Contract):
    steps: list[ExplanationStep]
    rule_trace: list[Identifier]
    source_refs: list[Text]


class ArtifactDescription(Contract):
    artifact_id: Identifier
    kind: Literal["DOCX", "PDF", "XLSX"]
    version: Identifier
    description: Text


class StopReason(Contract):
    code: Literal[
        "PENALTY_ABOVE_THRESHOLD",
        "LOW_MARGIN",
        "RESOURCE_GAP",
        "UNCLEAR_REQUIREMENTS",
        "NO_SOLUTION",
        "INCOMPATIBLE_RULES",
    ]
    reason: Text


class SolverResult(Contract):
    job_id: UUID
    kb_version: Identifier
    status: Literal["READY", "INCOMPLETE", "NO_SOLUTION"]
    requirements: list[Requirement]
    compliance: list[Compliance]
    resources: list[ResourceCheck]
    estimate: Estimate
    risks: list[Risk]
    recommendation: Literal["PARTICIPATE", "NOT_PARTICIPATE", "PARTICIPATE_WITH_CONDITIONS"]
    plan: Plan
    explanation: Explanation
    artifacts: list[ArtifactDescription]
    stop_reasons: list[StopReason]
