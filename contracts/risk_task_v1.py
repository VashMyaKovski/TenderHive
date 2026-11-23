import re
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, StringConstraints

type Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")
]
type Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)
]
type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
type Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=10, decimal_places=6)]
type RiskLevel = Literal["low", "medium", "high"]
type HumanReason = Literal[
    "penalty_above_threshold",
    "unclear_requirements",
    "low_margin",
    "resource_gap",
    "no_solution",
    "final_approval",
]
type FailureCode = Literal[
    "invalid_request",
    "invalid_context",
    "unsafe_context",
    "unsupported_prompt_version",
    "task_disabled",
    "provider_rejected",
    "provider_unavailable",
    "provider_timeout",
    "invalid_provider_response",
    "invalid_gateway_response",
    "gateway_timeout",
    "gateway_transport_error",
]

_DECIMAL = r"^(?:(?:0|[1-9][0-9]?)(?:\.[0-9]{1,6})?|100(?:\.0{1,6})?)$"
type DecimalString = Annotated[str, StringConstraints(pattern=_DECIMAL)]


def decimal_string(value: object) -> Decimal:
    if not isinstance(value, str) or re.fullmatch(_DECIMAL, value) is None:
        raise ValueError("percent must be a decimal string")
    return Decimal(value)


type WirePercent = Annotated[
    Percent, BeforeValidator(decimal_string, json_schema_input_type=DecimalString)
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceReference(Contract):
    url: HttpUrl
    locator: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RiskTaskV1(Contract):
    kind: Literal["risk_task.v1"]
    task_id: UUID
    job_id: UUID
    fragment_id: UUID
    source: SourceReference
    fragment_text: Text
    fragment_type: Literal["synthetic_text", "public_text"]
    rules_version: Identifier
    penalty_daily_percent: WirePercent
    penalty_daily_threshold_percent: WirePercent
    prompt_version: Identifier


class RiskSourceV1(Contract):
    fragment_id: UUID
    source: SourceReference


class RiskResultV1(Contract):
    kind: Literal["risk_result.v1"]
    task_id: UUID
    job_id: UUID
    a2a_task_id: str
    trace_id: UUID
    rules_version: Identifier
    model_version: Identifier
    prompt_version: Identifier
    risk_level: RiskLevel
    summary: Text
    confidence: Confidence
    human_reason: HumanReason | None
    sources: list[RiskSourceV1]


class RiskFailureV1(Contract):
    kind: Literal["risk_failure.v1"]
    task_id: UUID | None
    job_id: UUID | None
    a2a_task_id: str
    trace_id: UUID | None
    code: FailureCode
    retryable: bool
