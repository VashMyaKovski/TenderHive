from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.models import Job
from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.models import (
    Action,
    ArtifactReceipt,
    AssessmentFailure,
    DecisionRequest,
    JobView,
    Phase,
    RetryStage,
    ReviewCard,
    Reviewer,
    RiskInput,
    Role,
    SolverHandoff,
    Stage,
    StageFailure,
    StartJob,
)


class AgentError(Exception):
    def __init__(self, failure: AssessmentFailure) -> None:
        super().__init__(failure.code)
        self.failure = failure


def agent_error(code: str, *, retryable: bool) -> AgentError:
    return AgentError(
        AssessmentFailure(code=code, retryable=retryable, a2a_task_id=None, trace_id=None)
    )


class RiskAgent(Protocol):
    async def assess(self, request: RiskTaskV1) -> RiskResultV1: ...


@dataclass(frozen=True, slots=True)
class JobRecord:
    job: Job
    risk: RiskTaskV1 | None
    needs_advance: bool
    risk_generation: int
    risk_attempts: int
    risk_result: RiskResultV1 | None
    failure: AssessmentFailure | None
    stage: Stage
    risk_review: HumanReason | None


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    decision_id: UUID
    action: Action


class JobRepository(Protocol):
    async def lock(self, job_id: UUID, *, wait: bool) -> bool: ...
    async def register(self, request: Job | StartJob) -> None: ...
    async def job(self, job_id: UUID) -> JobRecord: ...
    async def view(self, job_id: UUID) -> JobView: ...
    async def reserve_attempt(self, job_id: UUID) -> None: ...
    async def save_result(self, job_id: UUID, result: RiskResultV1) -> None: ...
    async def save_failure(self, job_id: UUID, failure: AssessmentFailure) -> None: ...
    async def open_review(
        self, job_id: UUID, reason: HumanReason, role: Role, deadline: datetime
    ) -> None: ...
    async def set_phase(self, job_id: UUID, phase: Phase) -> None: ...
    async def finish_risk(self, job_id: UUID, deadline: datetime, *, failed: bool) -> None: ...
    async def mark_advanced(self, job_id: UUID) -> None: ...
    async def review_card(self, job_id: UUID, card_id: UUID) -> ReviewCard: ...
    async def expire(self, job_id: UUID, now: datetime) -> None: ...
    async def record_decision(
        self,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
        now: datetime,
    ) -> None: ...
    async def recorded_decision(self, job_id: UUID) -> RecordedDecision | None: ...
    async def accept_risk_input(self, job_id: UUID, body: RiskInput) -> None: ...
    async def accept_solver_result(
        self, job_id: UUID, body: SolverHandoff, deadline: datetime
    ) -> None: ...
    async def accept_artifacts(self, job_id: UUID, body: ArtifactReceipt) -> None: ...
    async def fail_stage(self, job_id: UUID, body: StageFailure) -> None: ...
    async def retry_stage(self, job_id: UUID, body: RetryStage) -> None: ...
    async def cancel(self, job_id: UUID) -> None: ...
    async def advance_reviews(self, job_id: UUID, deadline: datetime) -> None: ...
    async def pending_jobs(self, limit: int) -> list[UUID]: ...
    async def due_jobs(self, now: datetime, limit: int) -> list[UUID]: ...


class JobDatabase(Protocol):
    def session(self) -> AbstractAsyncContextManager[JobRepository]: ...
