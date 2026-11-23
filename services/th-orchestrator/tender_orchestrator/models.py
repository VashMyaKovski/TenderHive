from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from contracts.models import (
    Artifact,
    DecisionValue,
    HumanDecision,
    HumanRole,
    Job,
    JobStatus,
    SolverResult,
    StopReason,
)
from contracts.risk_task_v1 import Contract, HumanReason, RiskResultV1, RiskTaskV1

type Clock = Callable[[], datetime]
_LEGACY_ROLES: dict[str, HumanRole] = {
    "юрист": "LAWYER",
    "руководитель подразделения": "DIVISION_HEAD",
    "служба снабжения": "SUPPLY",
    "тендерный специалист": "TENDER_SPECIALIST",
}


def normalize_role(value: object) -> object:
    return _LEGACY_ROLES.get(value, value) if isinstance(value, str) else value


type Role = Annotated[HumanRole, BeforeValidator(normalize_role)]
type Phase = Literal[
    "accepted",
    "assessing",
    "awaiting_review",
    "ready_for_next_stage",
    "needs_clarification",
    "awaiting_artifacts",
    "done",
    "declined",
    "failed",
]
type CardState = Literal["open", "decided", "timed_out", "cancelled"]
type ReviewStep = Literal["risk_review", "risk_escalation", "review", "review_escalation"]
type Action = Literal["continue", "stop", "needs_info"]
type ExternalStage = Literal["extract", "solve", "artifacts"]
type Stage = Literal["extract", "risk", "solve", "review", "artifacts"]
type ErrorCode = Literal[
    "job_not_found",
    "card_not_found",
    "access_denied",
    "job_conflict",
    "decision_conflict",
    "stage_conflict",
    "result_mismatch",
]
type Profile = Annotated[str, StringConstraints(min_length=1, max_length=200)]

LEAD_ROLE: Role = "DIVISION_HEAD"
JOB_STATUS: dict[Phase, JobStatus] = {
    "accepted": "ACCEPTED",
    "assessing": "PROCESSING",
    "awaiting_review": "WAITING_HUMAN",
    "ready_for_next_stage": "PROCESSING",
    "needs_clarification": "NEEDS_CLARIFICATION",
    "awaiting_artifacts": "PROCESSING",
    "done": "DONE",
    "declined": "ERROR",
    "failed": "ERROR",
}
RISK_REASONS: dict[HumanReason, StopReason] = {
    "penalty_above_threshold": "PENALTY_ABOVE_THRESHOLD",
    "unclear_requirements": "UNCLEAR_REQUIREMENTS",
}
DECISIONS: dict[Action, DecisionValue] = {
    "continue": "APPROVED",
    "stop": "REJECTED",
    "needs_info": "NEEDS_INFO",
}
ACTIONS: dict[DecisionValue, Action] = {decision: action for action, decision in DECISIONS.items()}
REVIEW_ROLES: dict[StopReason, HumanRole] = {
    "PENALTY_ABOVE_THRESHOLD": "LAWYER",
    "LOW_MARGIN": "DIVISION_HEAD",
    "RESOURCE_GAP": "SUPPLY",
    "UNCLEAR_REQUIREMENTS": "TENDER_SPECIALIST",
    "NO_SOLUTION": "DIVISION_HEAD",
    "FINAL_APPROVAL": "TENDER_SPECIALIST",
}
REVIEW_QUESTIONS: dict[StopReason, str] = {
    "PENALTY_ABOVE_THRESHOLD": "Можно ли продолжить с указанными штрафами и условиями?",
    "LOW_MARGIN": "Допустимо ли продолжить с указанной маржинальностью?",
    "RESOURCE_GAP": "Можно ли продолжить с указанным дефицитом ресурсов?",
    "UNCLEAR_REQUIREMENTS": "Можно ли продолжить с указанными неясными требованиями?",
    "NO_SOLUTION": "Следует ли запросить пересмотр результата решателя?",
    "FINAL_APPROVAL": "Утверждаете ли вы итоговый проект предложения?",
}


class WorkflowError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code: ErrorCode = code


class StartJob(Contract):
    job: Job
    risk: RiskTaskV1

    @model_validator(mode="after")
    def validate_risk_task(self) -> StartJob:
        if self.job.status != "ACCEPTED":
            raise ValueError("a new job must have status ACCEPTED")
        if self.job.job_id != self.risk.job_id or self.job.kb_version != self.risk.rules_version:
            raise ValueError("risk task must match the job and knowledge version")
        if not 1 <= len(self.job.idempotency_key) <= 200:
            raise ValueError("job idempotency key must have between 1 and 200 characters")
        if not 1 <= len(self.job.profile_ref) <= 200:
            raise ValueError("job must reference a profile")
        return self


class DecisionRequest(Contract):
    decision_id: UUID
    decision: DecisionValue
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]

    @model_validator(mode="before")
    @classmethod
    def parse_legacy_action(cls, value: object) -> object:
        if isinstance(value, Mapping) and "action" in value and "decision" not in value:
            data = dict(value)
            action = data.pop("action")
            data["decision"] = (
                DECISIONS[cast(Action, action)]
                if isinstance(action, str) and action in DECISIONS
                else None
            )
            return data
        return value

    @property
    def action(self) -> Action:
        return ACTIONS[self.decision]

    @model_validator(mode="after")
    def validate_comment(self) -> DecisionRequest:
        if self.decision != "APPROVED" and not self.comment:
            raise ValueError("a rejection or clarification request requires a comment")
        return self


class Reviewer(Contract):
    actor_id: UUID
    role: Role
    profile_refs: Annotated[frozenset[Profile], Field(min_length=1)]


class LegacyHumanDecision(Contract):
    decision_id: UUID
    job_id: UUID
    reason_code: HumanReason
    role: Literal["юрист", "руководитель подразделения", "служба снабжения", "тендерный специалист"]
    deadline: AwareDatetime
    timeout: bool
    decision: Literal["continue", "stop"]
    comment: str


class ReviewCard(Contract):
    card_id: UUID
    job_id: UUID
    step: ReviewStep
    reason_code: StopReason
    role: Role
    question: str
    deadline: AwareDatetime
    state: CardState
    timeout: bool
    decision: HumanDecision | LegacyHumanDecision | None


class AssessmentFailure(Contract):
    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
    retryable: bool
    a2a_task_id: str | None
    trace_id: UUID | None


class JobView(Contract):
    job: Job
    phase: Phase
    risk_attempts: Annotated[int, Field(ge=0)]
    risk_result: RiskResultV1 | None
    failure: AssessmentFailure | None
    cards: list[ReviewCard]
    stage: Stage | None = None
    execution_id: UUID | None = None
    solver_result: SolverResult | None = None
    artifacts: list[Artifact] = Field(default_factory=list)


class SolverHandoff(Contract):
    execution_id: UUID
    result: SolverResult


class ArtifactReceipt(Contract):
    execution_id: UUID
    artifacts: Annotated[list[Artifact], Field(min_length=3)]

    @model_validator(mode="after")
    def validate_package(self) -> ArtifactReceipt:
        if {item.kind for item in self.artifacts} != {"DOCX", "PDF", "XLSX"}:
            raise ValueError("the package requires DOCX, PDF and XLSX artifacts")
        if len({item.artifact_id for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact IDs must be distinct")
        if len({item.file_path for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact paths must be distinct")
        return self


class StageFailure(Contract):
    execution_id: UUID
    stage: ExternalStage
    failure: AssessmentFailure


class RetryStage(Contract):
    execution_id: UUID
    stage: Literal["extract", "risk", "solve", "artifacts"]


class RiskInput(Contract):
    execution_id: UUID
    risk: RiskTaskV1


class IncomingDecision(Contract):
    card_id: UUID
    actor_id: UUID
    decision: HumanDecision


class OutboxEvent(Contract):
    event_id: UUID
    version: Literal[1] = 1
    event_type: str
    job_id: UUID
    occurred_at: AwareDatetime
    payload: dict[str, JsonValue]


class CachedPosition(Contract):
    job_id: UUID
    status: JobStatus
    phase: Phase
    stage: Stage
    execution_id: UUID
    version: int
