from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.graph import RiskGraph
from tender_orchestrator.models import (
    Clock,
    DecisionRequest,
    JobView,
    Reviewer,
    ReviewPoint,
    ReviewStep,
    Role,
    StartJob,
)
from tender_orchestrator.ports import JobRecord, RecordedDecision, agent_error
from tests.demo import demo_request

JOB_ID = UUID("751da55e-033e-4ad7-9993-858d44d52eb1")


def make_request() -> StartJob:
    service = Path(__file__).resolve().parents[1]
    fixture = service / "examples/minsk-supply-risk.json"
    if not fixture.is_file():
        fixture = service.parent / "th-agents/examples/minsk-supply-risk.json"
    return demo_request(uuid4(), fixture)


class ManualClock:
    def __init__(self) -> None:
        self.time = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.time


class StubAgent:
    def __init__(self, reason: HumanReason | None = None, fail: bool = False) -> None:
        self.reason = reason
        self.fail = fail
        self.calls = 0

    async def assess(self, request: RiskTaskV1) -> RiskResultV1:
        self.calls += 1
        if self.fail:
            raise agent_error("unsafe_context", retryable=False)
        return RiskResultV1(
            kind="risk_result.v1",
            task_id=request.task_id,
            job_id=request.job_id,
            a2a_task_id=str(uuid4()),
            trace_id=uuid4(),
            rules_version=request.rules_version,
            model_version="fixture/model",
            prompt_version=request.prompt_version,
            risk_level="high",
            summary="Public evidence",
            confidence=0.9,
            human_reason=cast(HumanReason | None, self.reason),
            sources=[],
        )


@dataclass
class Card:
    card_id: UUID
    step: ReviewStep
    reason: HumanReason
    role: Role
    state: str = "open"
    decision_id: UUID | None = None
    action: str | None = None
    applied: bool = False


class FakeStore:
    def __init__(self, request: StartJob) -> None:
        self.record = JobRecord(request, True, 0, None, None)
        self.cards: list[Card] = []
        self.phase = "accepted"

    async def job(self, job_id: UUID) -> JobRecord:
        return self.record

    async def lock(self, job_id: UUID, *, wait: bool) -> bool:
        raise NotImplementedError

    async def register(self, request: StartJob) -> None:
        raise NotImplementedError

    async def view(self, job_id: UUID) -> JobView:
        raise NotImplementedError

    async def card_role(self, job_id: UUID, card_id: UUID) -> Role:
        raise NotImplementedError

    async def expire(self, job_id: UUID, now: datetime) -> None:
        raise NotImplementedError

    async def record_decision(
        self,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
        now: datetime,
    ) -> None:
        raise NotImplementedError

    async def pending_jobs(self, limit: int) -> list[UUID]:
        raise NotImplementedError

    async def due_jobs(self, now: datetime, limit: int) -> list[UUID]:
        raise NotImplementedError

    async def reserve_attempt(self, job_id: UUID) -> None:
        self.record = replace(self.record, risk_attempts=self.record.risk_attempts + 1)

    async def save_result(self, job_id: UUID, result: RiskResultV1) -> None:
        self.record = replace(self.record, risk_result=result)

    async def save_failure(self, job_id: UUID, failure: object) -> None:
        self.record = replace(self.record, failure=failure)  # type: ignore[arg-type]

    async def open_review(
        self,
        job_id: UUID,
        reason: HumanReason,
        role: Role,
        deadline: datetime,
        *,
        step: ReviewStep = "risk_review",
    ) -> None:
        if any(card.step == step for card in self.cards):
            return
        self.cards.append(Card(uuid4(), step, reason, role))
        self.phase = "awaiting_review"

    async def set_phase(self, job_id: UUID, phase: str) -> None:
        self.phase = phase

    async def mark_advanced(self, job_id: UUID) -> None:
        self.record = replace(self.record, needs_advance=False)

    async def recorded_decision(self, job_id: UUID) -> RecordedDecision | None:
        for card in self.cards:
            if card.state == "decided" and not card.applied and card.decision_id is not None:
                return RecordedDecision(card.decision_id, card.action or "continue")  # type: ignore[arg-type]
        return None

    async def decision_by_id(self, job_id: UUID, decision_id: UUID) -> RecordedDecision | None:
        for card in self.cards:
            if card.decision_id == decision_id:
                return RecordedDecision(decision_id, card.action or "continue")  # type: ignore[arg-type]
        return None

    async def mark_decision_applied(self, decision_id: UUID) -> None:
        for card in self.cards:
            if card.decision_id == decision_id:
                card.applied = True

    async def decide(self, step: ReviewStep, action: str = "continue") -> None:
        card = next(item for item in self.cards if item.step == step and item.state == "open")
        card.state = "decided"
        card.decision_id = uuid4()
        card.action = action
        self.record = replace(self.record, needs_advance=True)


def build_graph(agent: StubAgent, clock: Clock | None = None) -> RiskGraph:
    return RiskGraph(
        InMemorySaver(),
        agent,
        clock or ManualClock(),
        max_attempts=2,
        retry_delay_seconds=0,
        review_role="юрист",
        review_timeout_seconds=60,
    )


def with_reviews(request: StartJob, *reasons: str) -> StartJob:
    return request.model_copy(
        update={
            "reviews": [
                ReviewPoint(reason_code=reason)  # type: ignore[arg-type]
                for reason in reasons
            ]
        }
    )


async def test_additional_points_open_one_card_at_a_time() -> None:
    store = FakeStore(with_reviews(make_request(), "low_margin", "resource_gap", "final_approval"))
    graph = build_graph(StubAgent())

    await graph.advance(store, JOB_ID)
    assert [card.step for card in store.cards] == ["margin_review"]

    await store.decide("margin_review")
    await graph.advance(store, JOB_ID)
    assert [card.step for card in store.cards] == ["margin_review", "resource_review"]

    await store.decide("resource_review")
    await graph.advance(store, JOB_ID)
    assert [card.step for card in store.cards][-1] == "final_approval_review"

    await store.decide("final_approval_review")
    await graph.advance(store, JOB_ID)
    assert store.phase == "ready_for_next_stage"


async def test_stop_on_additional_point_declines() -> None:
    store = FakeStore(with_reviews(make_request(), "low_margin"))
    graph = build_graph(StubAgent())
    await graph.advance(store, JOB_ID)
    await store.decide("margin_review", "stop")
    await graph.advance(store, JOB_ID)
    assert store.phase == "declined"


async def test_no_solution_point_replaces_failure() -> None:
    store = FakeStore(with_reviews(make_request(), "no_solution"))
    graph = build_graph(StubAgent(fail=True))
    await graph.advance(store, JOB_ID)
    assert [card.step for card in store.cards] == ["no_solution_review"]
    assert store.phase == "awaiting_review"


async def test_failure_without_no_solution_point_stays_failed() -> None:
    store = FakeStore(make_request())
    graph = build_graph(StubAgent(fail=True))
    await graph.advance(store, JOB_ID)
    assert store.cards == []
    assert store.phase == "failed"
