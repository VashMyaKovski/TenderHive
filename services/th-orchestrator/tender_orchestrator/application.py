import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from contracts.models import HumanDecision, Job
from tender_orchestrator.graph import RiskGraph
from tender_orchestrator.models import (
    ArtifactReceipt,
    Clock,
    DecisionRequest,
    JobView,
    RetryStage,
    Reviewer,
    RiskInput,
    SolverHandoff,
    StageFailure,
    StartJob,
    WorkflowError,
)
from tender_orchestrator.ports import JobDatabase, JobRepository

_LOG = logging.getLogger("uvicorn.error.orchestrator")


class Orchestrator:
    def __init__(
        self,
        database: JobDatabase,
        graph: RiskGraph,
        clock: Clock,
        batch_size: int,
        review_timeout_seconds: float,
    ) -> None:
        self._database = database
        self._graph = graph
        self._clock = clock
        self._batch_size = batch_size
        self._review_wait = timedelta(seconds=review_timeout_seconds)

    async def start(self, request: Job | StartJob) -> JobView:
        job_id = request.job.job_id if isinstance(request, StartJob) else request.job_id
        async with self._database.session() as store:
            await store.register(request)
            if await store.lock(job_id, wait=False):
                await self._advance(store, job_id)
            return await store.view(job_id)

    async def enqueue(self, request: Job | StartJob) -> None:
        async with self._database.session() as store:
            await store.register(request)

    async def status(self, job_id: UUID) -> JobView:
        async with self._database.session() as store:
            return await store.view(job_id)

    async def decide(
        self,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
        *,
        source: HumanDecision | None = None,
    ) -> JobView:
        async with self._database.session() as store:
            await store.lock(job_id, wait=True)
            role = await store.card_role(job_id, card_id)
            job = await store.job(job_id)
            allowed_role = role == actor.role or (
                role == "TENDER_SPECIALIST"
                and actor.role == "LAWYER"
                and await store.card_reason(job_id, card_id) == "UNCLEAR_REQUIREMENTS"
            )
            if not allowed_role or job.job.profile_ref not in actor.profile_refs:
                raise WorkflowError("access_denied")
            now = self._clock().astimezone(UTC)
            if source is not None:
                card = next(
                    item for item in (await store.view(job_id)).cards if item.card_id == card_id
                )
                if (
                    source.job_id != job_id
                    or source.role != actor.role
                    or source.reason_code != card.reason_code
                    or source.deadline != card.deadline
                    or source.timeout != card.timeout
                    or source.question != card.question
                    or not job.job.created_at <= source.decided_at <= now
                ):
                    raise WorkflowError("decision_conflict")
            await store.expire(job_id, now)
            await store.record_decision(
                job_id, card_id, actor, request, source.decided_at if source else now
            )
            await self._advance(store, job_id)
            return await store.view(job_id)

    async def risk_input(self, job_id: UUID, body: RiskInput) -> JobView:
        return await self._update(job_id, lambda store: store.accept_risk_input(job_id, body))

    async def solver_result(self, job_id: UUID, body: SolverHandoff) -> JobView:
        return await self._update(
            job_id, lambda store: store.accept_solver_result(job_id, body, self._deadline())
        )

    async def artifacts(self, job_id: UUID, body: ArtifactReceipt) -> JobView:
        return await self._update(job_id, lambda store: store.accept_artifacts(job_id, body))

    async def fail(self, job_id: UUID, body: StageFailure) -> JobView:
        return await self._update(job_id, lambda store: store.fail_stage(job_id, body))

    async def retry(self, job_id: UUID, body: RetryStage) -> JobView:
        return await self._update(job_id, lambda store: store.retry_stage(job_id, body))

    async def cancel(self, job_id: UUID) -> JobView:
        return await self._update(job_id, lambda store: store.cancel(job_id))

    async def _update(
        self, job_id: UUID, operation: Callable[[JobRepository], Awaitable[None]]
    ) -> JobView:
        async with self._database.session() as store:
            await store.lock(job_id, wait=True)
            await operation(store)
            await self._advance(store, job_id)
            return await store.view(job_id)

    def _deadline(self) -> datetime:
        return self._clock().astimezone(UTC) + self._review_wait

    async def _advance(self, store: JobRepository, job_id: UUID) -> None:
        await store.advance_reviews(job_id, self._deadline())
        await self._graph.advance(store, job_id)

    async def expire_due(self) -> None:
        await self._reconcile(expiry=True)

    async def recover_pending(self) -> None:
        await self._reconcile(expiry=False)

    async def _reconcile(self, *, expiry: bool) -> None:
        async with self._database.session() as store:
            if expiry:
                jobs = await store.due_jobs(self._clock().astimezone(UTC), self._batch_size)
            else:
                jobs = await store.pending_jobs(self._batch_size)
        for job_id in jobs:
            try:
                async with self._database.session() as store:
                    if not await store.lock(job_id, wait=False):
                        continue
                    if expiry:
                        await store.expire(job_id, self._clock().astimezone(UTC))
                    else:
                        await self._advance(store, job_id)
            except Exception as error:
                _LOG.error(
                    "reconcile job_id=%s expiry=%s error=%s", job_id, expiry, type(error).__name__
                )
