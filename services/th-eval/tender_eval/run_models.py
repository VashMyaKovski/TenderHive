from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from tender_eval.cases import Contract, Stratum, Version
from tender_eval.metrics import CalibrationBin, QualityPoint
from tender_eval.report import MetricReport, Mode
from tender_eval.runner import Issue

type Target = Literal["th-docintel", "th-solver", "th-modelgw", "th-orchestrator"]


class ErrorResponse(Contract):
    code: str
    message: str
    trace_id: UUID


class RunRequest(Contract):
    run_id: UUID
    suite: Version
    kb_version: Version
    model_version: Version
    prompt_version: Version
    targets: Annotated[tuple[Target, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be distinct")
        return self


class TargetResult(Contract):
    status: Literal["completed", "unavailable"]
    reason: Literal["no_adapter", "no_gateway_response"] | None


class RunMetrics(Contract):
    precision: None = None
    recall: None = None
    ece: float | None = None
    p95_latency_ms: float | None = None
    trace_completeness: None = None
    trace_id_coverage: float | None = None
    repeatability: float | None = None
    escalation_rate: float | None = None


class Regression(Contract):
    case_id: UUID | None
    repeat: int | None
    trace_id: UUID | None
    issues: tuple[Issue | Literal["unexpected_model_version", "quality_gate_failed"], ...]


class Curves(Contract):
    group: Stratum | Literal["all"]
    calibration: tuple[CalibrationBin, ...]
    quality_coverage: tuple[QualityPoint, ...]


class RunResult(Contract):
    run_id: UUID
    suite: Version
    kb_version: Version
    model_version: Version
    prompt_version: Version
    mode: Mode
    targets: dict[Target, TargetResult]
    metrics: RunMetrics
    by_group: dict[Stratum, RunMetrics]
    regressions: tuple[Regression, ...]
    curves: tuple[Curves, ...]
    gateway: MetricReport | None
