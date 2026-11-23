from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.models import (
    Action,
    AssessmentFailure,
    DecisionRequest,
    JobView,
    Phase,
    Reviewer,
    ReviewStep,
    Role,
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
    request: StartJob
    needs_advance: bool
    risk_attempts: int
    risk_result: RiskResultV1 | None
    failure: AssessmentFailure | None


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    decision_id: UUID
    action: Action


class JobRepository(Protocol):
    async def lock(self, job_id: UUID, *, wait: bool) -> bool: ...
    async def register(self, request: StartJob) -> None: ...
    async def job(self, job_id: UUID) -> JobRecord: ...
    async def view(self, job_id: UUID) -> JobView: ...
    async def reserve_attempt(self, job_id: UUID) -> None: ...
    async def save_result(self, job_id: UUID, result: RiskResultV1) -> None: ...
    async def save_failure(self, job_id: UUID, failure: AssessmentFailure) -> None: ...
    async def open_review(
        self,
        job_id: UUID,
        reason: HumanReason,
        role: Role,
        deadline: datetime,
        *,
        step: ReviewStep = "risk_review",
    ) -> None: ...
    async def set_phase(self, job_id: UUID, phase: Phase) -> None: ...
    async def mark_advanced(self, job_id: UUID) -> None: ...
    async def card_role(self, job_id: UUID, card_id: UUID) -> Role: ...
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

    async def decision_by_id(self, job_id: UUID, decision_id: UUID) -> RecordedDecision | None: ...

    async def mark_decision_applied(self, decision_id: UUID) -> None: ...
    async def pending_jobs(self, limit: int) -> list[UUID]: ...
    async def due_jobs(self, now: datetime, limit: int) -> list[UUID]: ...


class JobDatabase(Protocol):
    def session(self) -> AbstractAsyncContextManager[JobRepository]: ...
