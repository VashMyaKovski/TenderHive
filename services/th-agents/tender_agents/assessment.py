import asyncio
import json
from hashlib import sha256
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, ValidationError
from redis.exceptions import RedisError
from reportlab.platypus.doctemplate import LayoutError
from xlsxwriter.exceptions import XlsxWriterException

from contracts.agent_task import (
    AgentResult,
    DocumentAgentTask,
    ModelCall,
    RiskAgentTask,
    TypedAgentTask,
)
from contracts.risk_task_v1 import RiskFailureV1, RiskResultV1, RiskTaskV1
from tender_agents.artifacts import ArtifactConflict, ArtifactStore
from tender_agents.completions import completion_event
from tender_agents.gateway import GatewayFailure, ModelGateway
from tender_agents.ledger import Claim, RiskLedger
from tender_agents.roles import AnalysisTask, Evidence, analyze, approved_handoff, finding_result

_RISK_OUTCOME = TypeAdapter(Annotated[RiskResultV1 | RiskFailureV1, Field(discriminator="kind")])

type Request = RiskTaskV1 | TypedAgentTask
type Execution = RiskTaskV1 | DocumentAgentTask | AnalysisTask
type Outcome = RiskResultV1 | RiskFailureV1 | AgentResult


def _risk_task(task: RiskAgentTask) -> RiskTaskV1:
    return RiskTaskV1.model_validate(
        {
            **task.input.model_dump(mode="json"),
            "kind": "risk_task.v1",
            "task_id": task.task_id,
            "job_id": task.job_id,
            "rules_version": task.kb_version,
            "trace_id": task.trace_id,
        }
    )


def _risk_event(result: RiskResultV1 | RiskFailureV1, task: RiskTaskV1) -> AgentResult:
    if isinstance(result, RiskFailureV1):
        return AgentResult(
            task_id=task.task_id,
            job_id=task.job_id,
            agent_role="risk",
            status="FAILED",
            result={"code": result.code, "retryable": False},
            evidence=[],
            model_calls=[],
            trace_id=result.trace_id or uuid4(),
        )
    return AgentResult(
        task_id=task.task_id,
        job_id=task.job_id,
        agent_role="risk",
        status="NEEDS_REVIEW" if result.human_reason else "DONE",
        result={
            "summary": result.summary,
            "risk_level": result.risk_level,
            "confidence": result.confidence,
            "human_reason": result.human_reason,
            "rules_version": result.rules_version,
        },
        evidence=[source.model_dump(mode="json") for source in result.sources],
        model_calls=[
            ModelCall(
                trace_id=result.trace_id,
                model_version=result.model_version,
                prompt_version=result.prompt_version,
            )
        ],
        trace_id=result.trace_id,
    )


class Assessment:
    def __init__(
        self,
        gateway: ModelGateway,
        ledger: RiskLedger,
        artifacts: ArtifactStore,
        wait_seconds: float,
        poll_seconds: float,
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self._artifacts = artifacts
        self._wait = wait_seconds
        self._poll = poll_seconds

    async def _claim(self, task: Execution, fingerprint: str, owner: UUID) -> Claim:
        async with asyncio.timeout(self._wait):
            while True:
                claim = await self._ledger.claim(task.task_id, fingerprint, owner)
                if claim[0] != "busy":
                    return claim
                await asyncio.sleep(self._poll)

    async def assess(self, request: Request, a2a_task_id: str) -> RiskResultV1 | AgentResult:
        task = _risk_task(request) if isinstance(request, RiskAgentTask) else request
        if isinstance(task, RiskTaskV1):
            canonical = task.model_dump_json(exclude={"trace_id"})
        else:
            canonical = json.dumps(
                task.model_dump(mode="json", exclude={"trace_id"}), sort_keys=True
            )
        fingerprint = sha256(canonical.encode()).hexdigest()
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
            outcome = self._recorded(task, stored)
        else:
            outcome = await self._assess_owned(task, a2a_task_id, fingerprint, owner)

        if isinstance(outcome, RiskFailureV1):
            raise GatewayFailure(outcome.code, outcome.trace_id)
        if isinstance(outcome, RiskResultV1):
            if isinstance(request, RiskTaskV1):
                return outcome.model_copy(update={"a2a_task_id": a2a_task_id})
            assert isinstance(task, RiskTaskV1)
            return _risk_event(outcome, task)
        return outcome

    @staticmethod
    def _recorded(task: Execution, stored: str) -> Outcome:
        try:
            outcome = (
                _RISK_OUTCOME.validate_json(stored)
                if isinstance(task, RiskTaskV1)
                else AgentResult.model_validate_json(stored)
            )
        except ValidationError:
            raise GatewayFailure("task_store_unavailable", task.trace_id) from None
        if (outcome.task_id, outcome.job_id) != (task.task_id, task.job_id):
            raise GatewayFailure("task_store_unavailable", task.trace_id)
        return outcome

    async def _assess_owned(
        self, task: Execution, a2a_task_id: str, fingerprint: str, owner: UUID
    ) -> Outcome:
        try:
            outcome = await self._execute(task, a2a_task_id, fingerprint)
        except GatewayFailure as error:
            if error.retryable:
                await self._save(task, owner, None)
                raise
            if isinstance(task, RiskTaskV1):
                outcome = RiskFailureV1(
                    kind="risk_failure.v1",
                    task_id=task.task_id,
                    job_id=task.job_id,
                    a2a_task_id=a2a_task_id,
                    trace_id=error.trace_id,
                    code=error.code,
                    retryable=False,
                )
            else:
                outcome = finding_result(
                    task, {"code": error.code, "retryable": False}, [], "FAILED"
                )
        await self._save(task, owner, outcome)
        return outcome

    async def _execute(
        self, task: Execution, a2a_task_id: str, fingerprint: str
    ) -> RiskResultV1 | AgentResult:
        if isinstance(task, RiskTaskV1):
            return await self._gateway.assess(task, a2a_task_id)
        if not isinstance(task, DocumentAgentTask):
            return analyze(task)
        if not approved_handoff(task):
            raise GatewayFailure("approval_required", task.trace_id)
        try:
            artifacts = await asyncio.to_thread(self._artifacts.render, task, fingerprint)
        except ArtifactConflict:
            raise GatewayFailure("task_id_conflict", task.trace_id) from None
        except OSError:
            raise GatewayFailure("artifact_storage_unavailable", task.trace_id) from None
        except LayoutError, XlsxWriterException:
            raise GatewayFailure("artifact_generation_failed", task.trace_id) from None
        evidence: Evidence = [
            {
                "decision_id": str(task.input.approval.decision_id),
                "job_id": str(task.job_id),
                "reason_code": task.input.approval.reason_code,
            }
        ]
        return finding_result(
            task,
            {"artifacts": [artifact.model_dump(mode="json") for artifact in artifacts]},
            evidence,
            "DONE",
        )

    async def _save(self, task: Execution, owner: UUID, outcome: Outcome | None) -> None:
        event = None
        if isinstance(outcome, AgentResult):
            event = completion_event(outcome)
        elif outcome is not None and isinstance(task, RiskTaskV1):
            event = completion_event(_risk_event(outcome, task))
        try:
            saved = await self._ledger.finish(
                task.task_id, owner, outcome.model_dump_json() if outcome is not None else "", event
            )
        except RedisError:
            raise GatewayFailure("task_store_unavailable", task.trace_id) from None
        if not saved:
            raise GatewayFailure("task_store_unavailable", task.trace_id)
