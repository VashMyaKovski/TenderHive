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

from contracts.risk_task_v1 import HumanReason
from tender_orchestrator.models import HITL_POINTS, Clock, ReviewStep, Role
from tender_orchestrator.ports import AgentError, JobRepository, RiskAgent, agent_error

_LOG = logging.getLogger("uvicorn.error.orchestrator")

type Route = Literal["review", "next", "finish", "fail", "declined"]


class RiskState(TypedDict):
    job_id: str
    route: NotRequired[Route]
    step: NotRequired[ReviewStep]
    reason: NotRequired[HumanReason]
    index: NotRequired[int]
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
        builder.add_node("next_review", self._next_review)
        builder.add_node("open_review", self._open_review)
        builder.add_node("await_decision", self._await_decision)
        builder.add_node("after_decision", self._after_decision)
        builder.add_node("finish", self._finish)
        builder.add_edge(START, "assess")
        builder.add_conditional_edges(
            "assess",
            self._route,
            {"review": "open_review", "next": "next_review", "fail": "finish"},
        )
        builder.add_conditional_edges(
            "next_review", self._route, {"review": "open_review", "finish": "finish"}
        )
        builder.add_edge("open_review", "await_decision")
        builder.add_edge("await_decision", "after_decision")
        builder.add_conditional_edges(
            "after_decision", self._route, {"next": "next_review", "declined": "finish"}
        )
        builder.add_edge("finish", END)
        self._graph = builder.compile(checkpointer=saver)

    async def advance(self, store: JobRepository, job_id: UUID) -> None:
        if not (await store.job(job_id)).needs_advance:
            return
        config: RunnableConfig = {"configurable": {"thread_id": str(job_id)}}
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
            await store.mark_decision_applied(decision.decision_id)
        await store.mark_advanced(job_id)

    @staticmethod
    def _review(reason: HumanReason) -> dict[str, object]:
        return {"route": "review", "step": HITL_POINTS[reason][0], "reason": reason}

    async def _assess(self, state: RiskState, runtime: Runtime[GraphContext]) -> dict[str, object]:
        store = runtime.context.store
        job_id = UUID(state["job_id"])
        record = await store.job(job_id)
        requested = {point.reason_code for point in record.request.reviews}
        if record.risk_result is not None:
            reason = record.risk_result.human_reason
            if reason is not None:
                return self._review(reason)
            return {"route": "next", "index": 0}
        failure = record.failure
        if failure is not None and not failure.retryable:
            if "no_solution" in requested:
                return self._review("no_solution")
            return {"route": "fail"}

        for attempt in range(record.risk_attempts + 1, self._attempts + 1):
            await store.reserve_attempt(job_id)
            try:
                result = await self._agent.assess(record.request.risk)
            except AgentError as error:
                await store.save_failure(job_id, error.failure)
                _LOG.info(
                    "agent_attempt job_id=%s attempt=%s outcome=%s",
                    job_id,
                    attempt,
                    error.failure.code,
                )
                if not error.failure.retryable or attempt == self._attempts:
                    if "no_solution" in requested:
                        return self._review("no_solution")
                    return {"route": "fail"}
                await asyncio.sleep(self._retry_delay)
            else:
                await store.save_result(job_id, result)
                if result.human_reason is not None:
                    return self._review(result.human_reason)
                return {"route": "next", "index": 0}

        if failure is None:
            await store.save_failure(
                job_id, agent_error("agent_attempt_interrupted", retryable=False).failure
            )
        if "no_solution" in requested:
            return self._review("no_solution")
        return {"route": "fail"}

    async def _next_review(
        self, state: RiskState, runtime: Runtime[GraphContext]
    ) -> dict[str, object]:
        record = await runtime.context.store.job(UUID(state["job_id"]))
        index = state.get("index", 0)
        if index >= len(record.request.reviews):
            return {"route": "finish"}
        reason = record.request.reviews[index].reason_code
        return {
            "route": "review",
            "step": HITL_POINTS[reason][0],
            "reason": reason,
            "index": index + 1,
        }

    @staticmethod
    def _route(state: RiskState) -> Route:
        route = state.get("route")
        if route is None:
            raise RuntimeError("risk assessment has no outcome")
        return route

    async def _open_review(
        self, state: RiskState, runtime: Runtime[GraphContext]
    ) -> dict[str, str]:
        step = state.get("step")
        reason = state.get("reason")
        if step is None or reason is None:
            raise RuntimeError("review point has no step or reason")
        job_id = UUID(state["job_id"])
        role = self._role if step == "risk_review" else HITL_POINTS[reason][1]
        deadline = self._clock().astimezone(UTC) + self._wait
        await runtime.context.store.open_review(job_id, reason, role, deadline, step=step)
        return {}

    @staticmethod
    def _await_decision(state: RiskState) -> dict[str, str]:
        decision_id = interrupt(
            {"job_id": state["job_id"], "step": state.get("step", "risk_review")}
        )
        return {"decision_id": decision_id}

    @staticmethod
    async def _after_decision(state: RiskState, runtime: Runtime[GraphContext]) -> dict[str, Route]:
        decision_id = state.get("decision_id")
        if decision_id is None:
            raise RuntimeError("review decision was not recorded")
        decision = await runtime.context.store.decision_by_id(
            UUID(state["job_id"]), UUID(decision_id)
        )
        if decision is None:
            raise RuntimeError("review decision was not recorded")
        return {"route": "declined" if decision.action == "stop" else "next"}

    @staticmethod
    async def _finish(state: RiskState, runtime: Runtime[GraphContext]) -> dict[str, str]:
        store = runtime.context.store
        job_id = UUID(state["job_id"])
        route = RiskGraph._route(state)
        if route == "fail":
            await store.set_phase(job_id, "failed")
        elif route == "declined":
            await store.set_phase(job_id, "declined")
        else:
            await store.set_phase(job_id, "ready_for_next_stage")
        return {}
