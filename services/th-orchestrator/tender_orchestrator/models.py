from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from contracts.models import HumanDecision, Job, JobStatus
from contracts.risk_task_v1 import Contract, HumanReason, RiskResultV1, RiskTaskV1

type Clock = Callable[[], datetime]
type Role = Literal[
    "юрист", "руководитель подразделения", "служба снабжения", "тендерный специалист"
]
type Phase = Literal[
    "accepted", "assessing", "awaiting_review", "ready_for_next_stage", "declined", "failed"
]
type CardState = Literal["open", "decided", "timed_out"]
type ReviewStep = Literal["risk_review", "risk_escalation"]
type Action = Literal["continue", "stop"]
type ErrorCode = Literal[
    "job_not_found", "card_not_found", "access_denied", "job_conflict", "decision_conflict"
]
type Profile = Annotated[str, StringConstraints(min_length=1, max_length=200)]

LEAD_ROLE: Role = "руководитель подразделения"
JOB_STATUS: dict[Phase, JobStatus] = {
    "accepted": "принято",
    "assessing": "обрабатывается",
    "awaiting_review": "ожидает решения человека",
    "ready_for_next_stage": "обрабатывается",
    "declined": "завершено",
    "failed": "ошибка",
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
        if self.job.status != "принято":
            raise ValueError("a new job must have status принято")
        if self.job.job_id != self.risk.job_id or self.job.kb_version != self.risk.rules_version:
            raise ValueError("risk task must match the job and knowledge version")
        if not 1 <= len(self.job.idempotency_key) <= 200:
            raise ValueError("job idempotency key must have between 1 and 200 characters")
        if not 1 <= len(self.job.profile_ref) <= 200:
            raise ValueError("job must reference a profile")
        return self


class DecisionRequest(Contract):
    decision_id: UUID
    action: Action
    comment: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]

    @model_validator(mode="after")
    def validate_comment(self) -> DecisionRequest:
        if self.action == "stop" and not self.comment:
            raise ValueError("a stop decision requires a reason")
        return self


class Reviewer(Contract):
    actor_id: UUID
    role: Role
    profile_refs: Annotated[frozenset[Profile], Field(min_length=1)]


class ReviewCard(Contract):
    card_id: UUID
    job_id: UUID
    step: ReviewStep
    reason_code: HumanReason
    role: Role
    deadline: AwareDatetime
    state: CardState
    timeout: bool
    decision: HumanDecision | None


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
