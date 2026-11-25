from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

type Stratum = Literal["typical", "boundary", "negative"]
type RiskLevel = Literal["low", "medium", "high"]
type HumanReason = Literal["penalty_above_threshold", "unclear_requirements"]
type Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
type Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=10, decimal_places=6)]
type SourceIndex = Annotated[int, Field(ge=1, le=12, strict=True)]
type Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}$")]
type GatewayErrorCode = Literal[
    "invalid_request",
    "invalid_context",
    "unsafe_context",
    "unsupported_prompt_version",
    "provider_rejected",
    "provider_unavailable",
    "provider_timeout",
    "invalid_provider_response",
    "task_disabled",
    "storage_unavailable",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class SyntheticText(Contract):
    type: Literal["synthetic_text"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12_000)]


class Knowledge(Contract):
    type: Literal["knowledge"]
    rules_version: Version
    penalty_daily_threshold_percent: Percent


class Penalty(Contract):
    type: Literal["penalty"]
    daily_percent: Percent


class RiskExpectation(Contract):
    kind: Literal["risk"]
    level: RiskLevel | None = None
    threshold_exceeded: bool
    source_ids: tuple[SourceIndex, ...] = ()
    conflict: bool | None = None
    human_reason: HumanReason | None = None
    confidence: Confidence | None = None


class ErrorExpectation(Contract):
    kind: Literal["error"]
    status: Literal[422, 502, 503, 504]
    code: GatewayErrorCode

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        match self.code:
            case "provider_rejected" | "invalid_provider_response":
                status = 502
            case "provider_unavailable" | "task_disabled" | "storage_unavailable":
                status = 503
            case "provider_timeout":
                status = 504
            case _:
                status = 422
        if self.status != status:
            raise ValueError("error code and HTTP status disagree")
        return self


class EvalCase(Contract):
    case_id: UUID
    stratum: Stratum
    context: Annotated[tuple[JsonValue, ...], Field(min_length=1, max_length=12)]
    expected: Annotated[RiskExpectation | ErrorExpectation, Field(discriminator="kind")]
    live: bool = False
    mask: bool = True

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if isinstance(self.expected, ErrorExpectation) and not self.live:
            return self
        texts, knowledge, penalty = self.risk_context()
        if not self.mask:
            raise ValueError("risk and live scenarios require masking")
        if isinstance(self.expected, RiskExpectation):
            if any(source > len(texts) for source in self.expected.source_ids):
                raise ValueError("expected source is outside the context")
            threshold_exceeded = penalty.daily_percent > knowledge.penalty_daily_threshold_percent
            if self.expected.threshold_exceeded != threshold_exceeded:
                raise ValueError("expected threshold does not match the input")
        return self

    def risk_context(self) -> tuple[list[SyntheticText], Knowledge, Penalty]:
        texts: list[SyntheticText] = []
        rules: list[Knowledge] = []
        penalties: list[Penalty] = []
        for item in self.context:
            if not isinstance(item, dict):
                raise ValueError("risk context items must be objects")
            match item.get("type"):
                case "synthetic_text":
                    texts.append(SyntheticText.model_validate(item))
                case "knowledge":
                    rules.append(Knowledge.model_validate(item))
                case "penalty":
                    penalties.append(Penalty.model_validate(item))
                case _:
                    raise ValueError("only synthetic text and typed risk data are allowed")
        if not texts or len(rules) != 1 or len(penalties) != 1:
            raise ValueError("risk context requires text, one knowledge snapshot and one penalty")
        return texts, rules[0], penalties[0]


class DatasetHeader(Contract):
    schema_version: Literal[1]
    dataset_version: Version


@dataclass(frozen=True, slots=True)
class Dataset:
    version: str
    digest: str
    cases: tuple[EvalCase, ...]


class DatasetError(ValueError):
    """Invalid or unreadable evaluation input."""


def load_dataset(path: Path) -> Dataset:
    try:
        content = path.read_bytes()
        lines = content.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DatasetError("cannot read UTF-8 dataset") from error
    if len(lines) < 2:
        raise DatasetError("dataset requires a header and at least one case")
    try:
        header = DatasetHeader.model_validate_json(lines[0])
    except ValidationError:
        raise DatasetError("invalid dataset header at line 1") from None
    cases: dict[UUID, EvalCase] = {}
    for number, line in enumerate(lines[1:], 2):
        try:
            case = EvalCase.model_validate_json(line)
        except ValidationError:
            raise DatasetError(f"invalid dataset case at line {number}") from None
        if case.case_id in cases:
            raise DatasetError(f"duplicate case ID at line {number}")
        cases[case.case_id] = case
    return Dataset(header.dataset_version, sha256(content).hexdigest(), tuple(cases.values()))
