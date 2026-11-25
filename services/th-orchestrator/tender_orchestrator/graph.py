import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from contracts.risk_task_v1 import HumanReason, RiskResultV1
from tender_orchestrator.models import Clock, Role
from tender_orchestrator.ports import AgentError, JobRepository, RiskAgent, agent_error

_LOG = logging.getLogger("uvicorn.error.orchestrator")

type Route = Literal["review", "finish", "fail"]


def _review_reason(result: RiskResultV1) -> HumanReason | None:
    return result.human_reason or ("unclear_requirements" if result.risk_level == "high" else None)


class RiskState(TypedDict):
    job_id: str
    route: NotRequired[Route]
    decision_id: NotRequired[str]


@dataclass(frozen=True, slots=True)
class GraphContext:
    store: JobRepository


class RiskGraph:
    def __init__(
        self,
        saver: BaseCheckpointSaver,
        agent: RiskAgent,
        clock: Clock,
        *,
        max_attempts: int,
        retry_delay_seconds: float,
        review_role: Role,
        review_timeout_seconds: float,
    ) -> None:
        self._agent = agent
        self._attempts = max_attempts
        self._retry_delay = retry_delay_seconds
        self._role: Role = review_role
        self._wait = timedelta(seconds=review_timeout_seconds)
        self._clock = clock

        builder = StateGraph(RiskState, context_schema=GraphContext)
        builder.add_node("assess", self._assess)
        builder.add_node("open_review", self._open_review)
        builder.add_node("await_decision", self._await_decision)
        builder.add_node("finish", self._finish)
        builder.add_edge(START, "assess")
        builder.add_conditional_edges(
            "assess", self._route, {"review": "open_review", "finish": "finish", "fail": "finish"}
        )
        builder.add_edge("open_review", "await_decision")
        builder.add_edge("await_decision", "finish")
        builder.add_edge("finish", END)
        self._graph = builder.compile(checkpointer=saver)

    async def advance(self, store: JobRepository, job_id: UUID) -> None:
        record = await store.job(job_id)
        if record.stage != "risk" or not record.needs_advance or record.risk is None:
            return
        thread_id = (
            str(job_id) if record.risk_generation == 0 else f"{job_id}:{record.risk_generation}"
        )
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        context = GraphContext(store)
        snapshot = await self._graph.aget_state(config)
        decision = await store.recorded_decision(job_id)
        if not snapshot.interrupts:
            if snapshot.next:
                await self._graph.ainvoke(None, config, context=context, durability="sync")
            elif not snapshot.values:
                await self._graph.ainvoke(
                    {"job_id": str(job_id)}, config, context=context, durability="sync"
                )
            if decision is not None:
                snapshot = await self._graph.aget_state(config)
        if snapshot.interrupts and decision is not None:
            await self._graph.ainvoke(
                Command(resume=str(decision.decision_id)),
                config,
                context=context,
                durability="sync",
            )
        await store.mark_advanced(job_id)

    async def _assess(self, state: RiskState, runtime: Runtime[GraphContext]) -> dict[str, Route]:
        store = runtime.context.store
        job_id = UUID(state["job_id"])
        record = await store.job(job_id)
        if record.risk_result is not None:
            reason = record.risk_review or _review_reason(record.risk_result)
            return {"route": "review" if reason else "finish"}
        failure = record.failure
        if record.risk is None:
            raise RuntimeError("risk input has not been recorded")
        if failure is not None and not failure.retryable:
            return {"route": "fail"}

        for attempt in range(record.risk_attempts + 1, self._attempts + 1):
            await store.reserve_attempt(job_id)
            try:
                result = await self._agent.assess(record.risk)
            except AgentError as error:
                await store.save_failure(job_id, error.failure)
                _LOG.info(
                    "agent_attempt job_id=%s attempt=%s outcome=%s",
                    job_id,
                    attempt,
                    error.failure.code,
                )
                if not error.failure.retryable or attempt == self._attempts:
                    return {"route": "fail"}
                await asyncio.sleep(self._retry_delay)
            else:
                await store.save_result(job_id, result)
                return {"route": "review" if _review_reason(result) else "finish"}

        if failure is None:
            await store.save_failure(
                job_id, agent_error("agent_attempt_interrupted", retryable=False).failure
            )
        return {"route": "fail"}

    @staticmethod
    def _route(state: RiskState) -> Route:
        route = state.get("route")
        if route is None:
            raise RuntimeError("risk assessment has no outcome")
        return route

    async def _open_review(
        self, state: RiskState, runtime: Runtime[GraphContext]
    ) -> dict[str, str]:
        job_id = UUID(state["job_id"])
        record = await runtime.context.store.job(job_id)
        risk = record.risk_result
        reason = record.risk_review or (_review_reason(risk) if risk is not None else None)
        if reason is None:
            raise RuntimeError("risk assessment has no review reason")
        await runtime.context.store.open_review(
            job_id, reason, self._role, self._clock().astimezone(UTC) + self._wait
        )
        return {}

    @staticmethod
    def _await_decision(state: RiskState) -> dict[str, str]:
        decision_id = interrupt({"job_id": state["job_id"], "step": "risk_review"})
        return {"decision_id": decision_id}

    async def _finish(self, state: RiskState, runtime: Runtime[GraphContext]) -> dict[str, str]:
        store = runtime.context.store
        job_id = UUID(state["job_id"])
        route = RiskGraph._route(state)
        if route == "review":
            decision = await store.recorded_decision(job_id)
            if decision is None or str(decision.decision_id) != state.get("decision_id"):
                raise RuntimeError("review decision was not recorded")
            if decision.action == "stop":
                await store.set_phase(job_id, "declined")
                return {}
            elif decision.action == "needs_info":
                await store.set_phase(job_id, "needs_clarification")
                return {}
        await store.finish_risk(
            job_id, self._clock().astimezone(UTC) + self._wait, failed=route == "fail"
        )
        return {}
