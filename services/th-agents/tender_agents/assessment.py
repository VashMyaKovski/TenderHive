import asyncio
from hashlib import sha256
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError
from redis.exceptions import RedisError

from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1
from tender_agents.gateway import GatewayFailure, ModelGateway
from tender_agents.ledger import Claim, RiskLedger

_OUTCOME = TypeAdapter(Annotated[RiskResultV1 | RiskFailureV1, Field(discriminator="kind")])


class RiskAssessment:
    def __init__(
        self,
        gateway: ModelGateway,
        ledger: RiskLedger,
        wait_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self._wait = wait_seconds
        self._poll = poll_seconds

    async def _claim(self, task: RiskTaskV1, fingerprint: str, owner: UUID) -> Claim:
        async with asyncio.timeout(self._wait):
            while True:
                claim = await self._ledger.claim(task.task_id, fingerprint, owner)
                if claim[0] != "busy":
                    return claim
                await asyncio.sleep(self._poll)

    async def assess(self, task: RiskTaskV1, a2a_task_id: str) -> RiskResultV1:
        fingerprint = sha256(task.model_dump_json(exclude={"trace_id"}).encode()).hexdigest()
        owner = uuid4()
        try:
            status, stored = await self._claim(task, fingerprint, owner)
        except TimeoutError:
            raise GatewayFailure("task_in_progress", task.trace_id) from None
        except RedisError:
            raise GatewayFailure("task_store_unavailable", task.trace_id) from None
        if status == "conflict":
            raise GatewayFailure("task_id_conflict", task.trace_id)
        if status == "recorded":
            try:
                outcome = _OUTCOME.validate_json(stored)
            except ValidationError:
                raise GatewayFailure("task_store_unavailable", task.trace_id) from None
            if (outcome.task_id, outcome.job_id) != (task.task_id, task.job_id):
                raise GatewayFailure("task_store_unavailable", task.trace_id)
            if isinstance(outcome, RiskFailureV1):
                raise GatewayFailure(outcome.code, outcome.trace_id)
            return outcome.model_copy(update={"a2a_task_id": a2a_task_id})

        try:
            result = await self._gateway.assess(task, a2a_task_id)
        except GatewayFailure as error:
            terminal = ""
            if not error.retryable:
                terminal = RiskFailureV1(
                    kind="risk_failure.v1",
                    task_id=task.task_id,
                    job_id=task.job_id,
                    a2a_task_id=a2a_task_id,
                    trace_id=error.trace_id,
                    code=error.code,
                    retryable=False,
                ).model_dump_json()
            await self._save(task, owner, terminal)
            raise
        await self._save(task, owner, result.model_dump_json())
        return result

    async def _save(self, task: RiskTaskV1, owner: UUID, outcome: str) -> None:
        try:
            saved = await self._ledger.finish(task.task_id, owner, outcome)
        except RedisError:
            raise GatewayFailure("task_store_unavailable", task.trace_id) from None
        if not saved:
            raise GatewayFailure("task_store_unavailable", task.trace_id)
