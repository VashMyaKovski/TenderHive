from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from contracts.agent_task import AgentResult
from contracts.models import Contract, NonEmpty, Requirement, UtcDatetime
from pydantic import Field, JsonValue, model_validator

type Scalar = str | int | float | bool | None
type Number = Annotated[Decimal, Field(allow_inf_nan=False)]


class Fact(Contract):
    fact_id: UUID
    key: NonEmpty
    value: Scalar
    source: NonEmpty


class KnowledgeFact(Contract):
    fact_id: UUID
    key: NonEmpty | None = None
    value: Scalar
    source: NonEmpty
    valid_from: date | None = None
    valid_until: date | None = None
    value_type: str | None = None


class Position(Contract):
    position_id: NonEmpty
    description: NonEmpty
    quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    available: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    unit_cost: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    offer_unit_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    supply_deadline: UtcDatetime | None
    alternatives: list[JsonValue]


class Profile(Contract):
    regime: NonEmpty
    task_class: NonEmpty
    positions: list[Position]


class SolveRequest(Contract):
    job_id: UUID
    kb_version: NonEmpty
    requirements: Annotated[list[Requirement], Field(max_length=500)]
    facts: Annotated[list[Fact], Field(max_length=2000)]
    profile: Profile
    lot_ids: list[NonEmpty]
    trace_id: UUID
    as_of: date | None = None
    agent_results: list[AgentResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def verify_agent_jobs(self) -> SolveRequest:
        if any(result.job_id != self.job_id for result in self.agent_results):
            raise ValueError("Agent result belongs to a different job")
        if len({result.task_id for result in self.agent_results}) != len(self.agent_results):
            raise ValueError("Duplicate agent result")
        return self


class Condition(Contract):
    field: NonEmpty
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    value: Scalar


class Consequence(Contract):
    status: Literal["MET", "NOT_MET", "NEEDS_REVIEW"]
    reason: NonEmpty
