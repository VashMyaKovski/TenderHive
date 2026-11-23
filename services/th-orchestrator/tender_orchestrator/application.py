import logging
from datetime import UTC
from uuid import UUID

from tender_orchestrator.graph import RiskGraph
from tender_orchestrator.models import (
    Clock,
    DecisionRequest,
    JobView,
    Reviewer,
    StartJob,
    WorkflowError,
)
from tender_orchestrator.ports import JobDatabase

_LOG = logging.getLogger("uvicorn.error.orchestrator")


class Orchestrator:
    def __init__(
        self, database: JobDatabase, graph: RiskGraph, clock: Clock, batch_size: int
    ) -> None:
        self._database = database
        self._graph = graph
        self._clock = clock
        self._batch_size = batch_size

    async def start(self, request: StartJob) -> JobView:
        job_id = request.job.job_id
        async with self._database.session() as store:
            await store.register(request)
            if await store.lock(job_id, wait=False):
                await self._graph.advance(store, job_id)
            return await store.view(job_id)

    async def status(self, job_id: UUID) -> JobView:
        async with self._database.session() as store:
            return await store.view(job_id)

    async def decide(
        self,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
    ) -> JobView:
        async with self._database.session() as store:
            await store.lock(job_id, wait=True)
            role = await store.card_role(job_id, card_id)
            job = await store.job(job_id)
            if role != actor.role or job.request.job.profile_ref not in actor.profile_refs:
                raise WorkflowError("access_denied")
            now = self._clock().astimezone(UTC)
            await store.expire(job_id, now)
            await store.record_decision(job_id, card_id, actor, request, now)
            await self._graph.advance(store, job_id)
            return await store.view(job_id)

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
                        await self._graph.advance(store, job_id)
            except Exception as error:
                _LOG.error(
                    "reconcile job_id=%s expiry=%s error=%s", job_id, expiry, type(error).__name__
                )
