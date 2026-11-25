from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StringConstraints, model_validator

from contracts.models import HumanDecision, Requirement, SolverResult, UtcDatetime
from contracts.risk_task_v1 import Contract, Identifier, RiskInput, SourceReference, Text

type AgentRole = Literal[
    "risk", "legal", "resource", "estimate", "supply", "extraction", "document"
]
type AgentStatus = Literal["DONE", "NEEDS_REVIEW", "FAILED"]
type Quantity = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]
type PositiveQuantity = Annotated[Quantity, Field(gt=0)]
type Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
type Margin = Annotated[Decimal, Field(ge=-100, le=100, max_digits=7, decimal_places=4)]
type Supplier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class _TaskHeader(Contract):
    task_id: UUID
    job_id: UUID
    kb_version: Identifier
    trace_id: UUID


class AgentTask(_TaskHeader):
    agent_role: AgentRole
    input: dict[str, JsonValue]


class ExtractionInput(Contract):
    requirements: Annotated[list[Requirement], Field(max_length=200)]

    @model_validator(mode="after")
    def validate_requirement_ids(self) -> ExtractionInput:
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("requirement IDs must be unique")
        return self


class LegalClause(Contract):
    text: Text
    source: SourceReference
    requires_review: bool


class LegalInput(Contract):
    regime: Literal["state", "own_funds", "commercial"]
    regime_source: SourceReference
    clauses: Annotated[list[LegalClause], Field(max_length=200)]


class ResourcePosition(Contract):
    position_id: Identifier
    required_quantity: PositiveQuantity
    available_quantity: Quantity
    source: SourceReference


class EstimatePosition(Contract):
    position_id: Identifier
    quantity: PositiveQuantity
    unit_cost: Quantity
    source: SourceReference


class SupplyOffer(Contract):
    supplier: Supplier
    available_quantity: Quantity
    delivery_at: UtcDatetime
    source: SourceReference


class SupplyPosition(Contract):
    position_id: Identifier
    required_quantity: PositiveQuantity
    delivery_deadline: UtcDatetime
    source: SourceReference
    offers: Annotated[list[SupplyOffer], Field(max_length=100)]


class ResourceInput(Contract):
    positions: Annotated[list[ResourcePosition], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def validate_position_ids(self) -> ResourceInput:
        _check_positions(self.positions)
        return self


class EstimateInput(Contract):
    currency: Currency
    offer_price: PositiveQuantity
    margin_threshold_percent: Margin
    threshold_source: SourceReference
    positions: Annotated[list[EstimatePosition], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def validate_position_ids(self) -> EstimateInput:
        _check_positions(self.positions)
        return self


class SupplyInput(Contract):
    positions: Annotated[list[SupplyPosition], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def validate_position_ids(self) -> SupplyInput:
        _check_positions(self.positions)
        return self


def _check_positions(
    positions: list[ResourcePosition] | list[EstimatePosition] | list[SupplyPosition],
) -> None:
    if len({item.position_id for item in positions}) != len(positions):
        raise ValueError("position IDs must be unique")


class DocumentInput(Contract):
    solver_result: SolverResult
    approval: HumanDecision


class RiskAgentTask(_TaskHeader):
    agent_role: Literal["risk"]
    input: RiskInput


class ExtractionAgentTask(_TaskHeader):
    agent_role: Literal["extraction"]
    input: ExtractionInput


class LegalAgentTask(_TaskHeader):
    agent_role: Literal["legal"]
    input: LegalInput


class ResourceAgentTask(_TaskHeader):
    agent_role: Literal["resource"]
    input: ResourceInput


class EstimateAgentTask(_TaskHeader):
    agent_role: Literal["estimate"]
    input: EstimateInput


class SupplyAgentTask(_TaskHeader):
    agent_role: Literal["supply"]
    input: SupplyInput


class DocumentAgentTask(_TaskHeader):
    agent_role: Literal["document"]
    input: DocumentInput


type TypedAgentTask = Annotated[
    RiskAgentTask
    | ExtractionAgentTask
    | LegalAgentTask
    | ResourceAgentTask
    | EstimateAgentTask
    | SupplyAgentTask
    | DocumentAgentTask,
    Field(discriminator="agent_role"),
]


class ModelCall(Contract):
    trace_id: UUID
    model_version: Identifier
    prompt_version: Identifier


class AgentResult(Contract):
    task_id: UUID
    job_id: UUID
    agent_role: AgentRole
    status: AgentStatus
    result: dict[str, JsonValue]
    evidence: list[dict[str, JsonValue]]
    model_calls: list[ModelCall]
    trace_id: UUID


AGENT_COMPLETED_TOPIC = "tender.agent.completed"


class AgentCompleted(Contract):
    event_id: UUID
    version: Literal[1]
    event_type: Literal["tender.agent.completed"]
    job_id: UUID
    occurred_at: UtcDatetime
    payload: AgentResult
