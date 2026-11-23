import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.agent import AgentClient
from tender_orchestrator.api import create_app
from tender_orchestrator.application import Orchestrator
from tender_orchestrator.config import ReviewerCredential, Settings
from tender_orchestrator.db import Database, JobStore, connection_info
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
from tender_orchestrator.ports import RiskAgent, agent_error
from tests.demo import JOBS_PATH, bearer

LAWYER = "local-orchestrator-lawyer-token"
LEAD = "local-orchestrator-leader-token"
OUTSIDE = "local-orchestrator-external-lawyer"
SUPPLY = "local-orchestrator-supply-token"
TENDER = "local-orchestrator-tender-token"


@pytest.fixture
def settings(request: pytest.FixtureRequest) -> Settings:
    if not request.config.getoption("--with-postgres"):
        pytest.skip("requires the migrated Compose PostgreSQL stack")
    base = Settings.model_validate(
        {
            "review_timeout_seconds": 30,
            "agent_max_attempts": 2,
            "agent_retry_delay_seconds": 0,
            "poll_interval_seconds": 0.05,
        }
    )
    extra = ReviewerCredential(
        actor_id=uuid4(),
        role="юрист",
        profile_refs=frozenset({"another-profile"}),
        token=SecretStr(OUTSIDE),
    )
    return base.model_copy(update={"reviewers": [*base.reviewers, extra]})


@dataclass(slots=True)
class ManualClock:
    time: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.time


class StubAgent:
    def __init__(self, reason: HumanReason | None, failures: tuple[bool, ...] = ()) -> None:
        self.reason: HumanReason | None = reason
        self.failures = failures
        self.calls = 0

    async def assess(self, request: RiskTaskV1) -> RiskResultV1:
        self.calls += 1
        if self.calls <= len(self.failures):
            retryable = self.failures[self.calls - 1]
            raise agent_error(
                "provider_unavailable" if retryable else "unsafe_context", retryable=retryable
            )
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
            summary="Public risk evidence",
            confidence=0.9,
            human_reason=self.reason,
            sources=[],
        )


class CountingAgent:
    def __init__(self, client: RiskAgent) -> None:
        self._client = client
        self.calls = 0

    async def assess(self, request: RiskTaskV1) -> RiskResultV1:
        self.calls += 1
        return await self._client.assess(request)


@dataclass(slots=True)
class API:
    http: httpx.AsyncClient
    service: Orchestrator

    async def get(self, job_id: UUID) -> JobView:
        response = await self.http.get(f"{JOBS_PATH}/{job_id}")
        return JobView.model_validate(response.raise_for_status().json())

    async def start(self, body: StartJob) -> JobView:
        response = await self.http.post(JOBS_PATH, json=body.model_dump(mode="json"))
        response.raise_for_status()
        phases = {"awaiting_review", "ready_for_next_stage", "failed"}
        return await self.wait_for(body.job.job_id, lambda job: job.phase in phases)

    async def wait_for(self, job_id: UUID, predicate: Callable[[JobView], bool]) -> JobView:
        async with asyncio.timeout(10):
            while True:
                state = await self.get(job_id)
                if predicate(state):
                    return state
                await asyncio.sleep(0.02)

    async def decision(
        self, job: JobView, token: str, body: dict[str, str], index: int = 0
    ) -> httpx.Response:
        card = job.cards[index]
        return await self.http.post(
            f"{JOBS_PATH}/{job.job.job_id}/cards/{card.card_id}/decision",
            headers=bearer(token),
            json=body,
        )


@asynccontextmanager
async def running(settings: Settings, clock: Clock, agent: RiskAgent) -> AsyncIterator[API]:
    app = create_app(settings, clock, agent)
    async with app.router.lifespan_context(app) as state:
        # ASGITransport doesn't run the ASGI lifespan or propagate its state.
        assert state is not None
        lifespan_state = dict(state)

        async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
            scope["state"] = lifespan_state.copy()
            await app(scope, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi, raise_app_exceptions=False),
            base_url="http://orchestrator.invalid",
            headers=bearer(settings.service_token.get_secret_value()),
        ) as http:
            yield API(http, state["orchestrator"])


def decision_payload() -> dict[str, str]:
    return {"decision_id": str(uuid4()), "action": "continue", "comment": "Риск рассмотрен"}


async def test_public_tender_real_a2a_survives_new_instance(
    settings: Settings, request_body: StartJob
) -> None:
    clock = ManualClock()
    async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
        agent = CountingAgent(AgentClient(http, str(settings.agent_url), 10))
        async with running(settings, clock, agent) as first:
            pending = await first.start(request_body)

        assert pending.job.status == "ожидает решения человека"
        assert (
            pending.risk_result is not None
            and pending.risk_result.sources[0].source == request_body.risk.source
        )
        assert len(pending.cards) == 1 and pending.cards[0].reason_code == "penalty_above_threshold"
        assert pending.risk_attempts == agent.calls == 1
        database = Database(connection_info(settings), 1)
        async with database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT count(*) AS count FROM checkpoints WHERE thread_id = %s",
                    (str(pending.job.job_id),),
                )
            ).fetchone()
        assert row is not None and row["count"] > 0

        async with running(settings, clock, agent) as second:
            assert await second.get(pending.job.job_id) == pending
            response = await second.decision(pending, LAWYER, decision_payload())
            done = JobView.model_validate(response.raise_for_status().json())

        assert done.phase == "ready_for_next_stage"
        assert done.risk_result == pending.risk_result and agent.calls == 1
        assert done.cards[0].card_id == pending.cards[0].card_id
        assert done.cards[0].decision is not None and done.cards[0].decision.decision == "continue"


@pytest.mark.parametrize(
    ("reason", "failures", "phase", "attempts"),
    [
        (None, (), "ready_for_next_stage", 1),
        ("unclear_requirements", (), "awaiting_review", 1),
        (None, (True,), "ready_for_next_stage", 2),
        (None, (False,), "failed", 1),
        (None, (True, True), "failed", 2),
    ],
)
async def test_risk_branches_and_retry_budget(
    settings: Settings,
    request_body: StartJob,
    reason: HumanReason | None,
    failures: tuple[bool, ...],
    phase: str,
    attempts: int,
) -> None:
    agent = StubAgent(reason, failures)
    async with running(settings, ManualClock(), agent) as api:
        result = await api.start(request_body)
        assert await api.start(request_body) == result
    assert result.phase == phase and agent.calls == result.risk_attempts == attempts
    if reason is None:
        assert not result.cards
    else:
        assert [card.reason_code for card in result.cards] == [reason]
    assert (result.failure is not None) == (phase == "failed")
    if result.failure is not None:
        assert result.job.status == "ошибка" and result.risk_result is None


async def test_job_and_decision_conflicts_and_reviewer_access(
    settings: Settings, request_body: StartJob
) -> None:
    agent = StubAgent("penalty_above_threshold")
    async with running(settings, ManualClock(), agent) as api:
        assert (
            await api.http.post(
                JOBS_PATH, headers=bearer("invalid"), json=request_body.model_dump(mode="json")
            )
        ).status_code == 401
        pending = await api.start(request_body)
        wrong = request_body.model_dump(mode="json")
        wrong["risk"]["fragment_text"] = "An incompatible request for the same job"
        assert (await api.http.post(JOBS_PATH, json=wrong)).json() == {"code": "job_conflict"}
        wrong["job"]["job_id"] = wrong["risk"]["job_id"] = str(uuid4())
        assert (await api.http.post(JOBS_PATH, json=wrong)).status_code == 409

        decision = decision_payload()
        for token in (LEAD, OUTSIDE):
            assert (await api.decision(pending, token, decision)).status_code == 403
        service_token = settings.service_token.get_secret_value()
        assert (await api.decision(pending, service_token, decision)).status_code == 401
        foreign_url = f"{JOBS_PATH}/{uuid4()}/cards/{pending.cards[0].card_id}/decision"
        foreign = await api.http.post(foreign_url, headers=bearer(LAWYER), json=decision)
        assert foreign.status_code == 404 and foreign.json() == {"code": "card_not_found"}

        first = await api.decision(pending, LAWYER, decision)
        assert first.status_code == 200
        assert (await api.decision(pending, LAWYER, decision)).json() == first.json()
        changed = {**decision, "action": "stop", "comment": "Different decision"}
        assert (await api.decision(pending, LAWYER, changed)).status_code == 409
        assert (await api.decision(pending, LAWYER, decision_payload())).status_code == 409
    assert agent.calls == 1


async def test_concurrent_instances_share_one_assessment_and_one_decision(
    settings: Settings,
    request_body: StartJob,
) -> None:
    agent = StubAgent("penalty_above_threshold")
    clock = ManualClock()
    async with running(settings, clock, agent) as api, running(settings, clock, agent) as peer:
        left, right = await asyncio.gather(api.start(request_body), peer.start(request_body))
        assert left == right and left.risk_attempts == agent.calls == 1
        stop = {**decision_payload(), "action": "stop", "comment": "Неприемлемый риск"}
        replies = await asyncio.gather(
            api.decision(left, LAWYER, stop),
            peer.decision(right, LAWYER, decision_payload()),
        )
        assert sorted(response.status_code for response in replies) == [200, 409]
        accepted = next(response for response in replies if response.status_code == 200)
        final = await api.get(left.job.job_id)
        assert final.model_dump(mode="json") == accepted.json()
        assert final.phase in {"declined", "ready_for_next_stage"}
        assert len(final.cards) == 1 and final.cards[0].decision is not None


@pytest.mark.parametrize("elapsed", [False, True])
async def test_deadline_and_response_race(
    settings: Settings, request_body: StartJob, elapsed: bool
) -> None:
    agent = StubAgent("penalty_above_threshold")
    clock = ManualClock()
    async with running(settings, clock, agent) as api, running(settings, clock, agent) as peer:
        pending = await api.start(request_body)
        clock.time = pending.cards[0].deadline - timedelta(microseconds=0 if elapsed else 1)
        decision, _, _ = await asyncio.gather(
            api.decision(pending, LAWYER, decision_payload()),
            peer.service.expire_due(),
            api.service.expire_due(),
        )
        final = await api.get(pending.job.job_id)
    if elapsed:
        assert decision.status_code == 409
        assert final.phase == "awaiting_review"
        assert [(card.step, card.state) for card in final.cards] == [
            ("risk_review", "timed_out"),
            ("risk_escalation", "open"),
        ]
        assert all(card.timeout and card.decision is None for card in final.cards)
    else:
        assert decision.status_code == 200 and final.phase == "ready_for_next_stage"
        assert len(final.cards) == 1 and not final.cards[0].timeout


async def test_restart_escalates_to_lead_and_stop_ends_the_job(
    settings: Settings, request_body: StartJob
) -> None:
    clock = ManualClock()
    agent = StubAgent("unclear_requirements")
    async with running(settings, clock, agent) as first:
        pending = await first.start(request_body)

    clock.time = pending.cards[0].deadline
    async with running(settings, clock, agent) as second:
        review = await second.wait_for(pending.job.job_id, lambda job: len(job.cards) == 2)
        assert review.phase == "awaiting_review" and review.cards[0].state == "timed_out"
        assert (
            review.cards[1].role == "руководитель подразделения" and review.cards[1].state == "open"
        )
        request = {**decision_payload(), "action": "stop", "comment": "Условие неприемлемо"}
        assert (await second.decision(review, LAWYER, request, index=1)).status_code == 403
        response = await second.decision(review, LEAD, request, index=1)
        final = JobView.model_validate(response.raise_for_status().json())
        assert final.phase == "declined" and final.job.status == "завершено"
        decision = final.cards[1].decision
        assert decision is not None and decision.timeout and decision.comment == request["comment"]
        assert agent.calls == 1 and len(final.cards) == 2


async def test_recovery_after_card_commit_before_interrupt(
    settings: Settings,
    request_body: StartJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    agent = StubAgent("penalty_above_threshold")
    original = JobStore.open_review

    async def interrupt_after_commit(
        self: JobStore,
        job_id: UUID,
        reason: HumanReason,
        role: Role,
        deadline: datetime,
        *,
        step: ReviewStep = "risk_review",
    ) -> None:
        await original(self, job_id, reason, role, deadline, step=step)
        if job_id == request_body.job.job_id:
            raise RuntimeError("simulate termination after the review card commit")

    with monkeypatch.context() as patch:
        patch.setattr(JobStore, "open_review", interrupt_after_commit)
        slow_poll = settings.model_copy(update={"poll_interval_seconds": 60})
        async with running(slow_poll, clock, agent) as first:
            await asyncio.sleep(0.1)
            response = await first.http.post(JOBS_PATH, json=request_body.model_dump(mode="json"))
            assert response.status_code == 500
            saved = await first.get(request_body.job.job_id)
            assert len(saved.cards) == 1 and saved.risk_result is not None

    # Also model the decision being persisted while the interrupt is still missing.
    database = Database(connection_info(settings), 1)
    actor: Reviewer = next(item for item in settings.reviewers if item.role == "юрист")
    async with database.session() as store:
        await store.lock(saved.job.job_id, wait=True)
        await store.record_decision(
            saved.job.job_id,
            saved.cards[0].card_id,
            actor,
            DecisionRequest.model_validate(decision_payload()),
            clock(),
        )
    async with running(settings, clock, agent) as second:
        final = await second.wait_for(
            saved.job.job_id, lambda job: job.phase == "ready_for_next_stage"
        )
    assert final.risk_result == saved.risk_result and agent.calls == 1
    assert len(final.cards) == 1 and final.cards[0].card_id == saved.cards[0].card_id


async def test_recovery_after_decision_commit_before_resume(
    settings: Settings,
    request_body: StartJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    agent = StubAgent("penalty_above_threshold")
    original = JobStore.record_decision

    async def interrupt_after_commit(
        self: JobStore,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
        now: datetime,
    ) -> None:
        await original(self, job_id, card_id, actor, request, now)
        raise RuntimeError("simulate termination after the decision commit")

    slow_poll = settings.model_copy(update={"poll_interval_seconds": 60})
    async with running(slow_poll, clock, agent) as first:
        pending = await first.start(request_body)
        body = decision_payload()
        with monkeypatch.context() as patch:
            patch.setattr(JobStore, "record_decision", interrupt_after_commit)
            assert (await first.decision(pending, LAWYER, body)).status_code == 500
        saved = await first.get(pending.job.job_id)
        assert saved.phase == "awaiting_review" and saved.cards[0].decision is not None

    async with running(settings, clock, agent) as second:
        resumed = await second.wait_for(
            pending.job.job_id, lambda job: job.phase == "ready_for_next_stage"
        )
        assert (await second.decision(pending, LAWYER, body)).json() == resumed.model_dump(
            mode="json"
        )
    assert agent.calls == 1


def with_reviews(request_body: StartJob, *reasons: str) -> StartJob:
    return request_body.model_copy(
        update={"reviews": [ReviewPoint(reason_code=reason) for reason in reasons]}  # type: ignore[arg-type]
    )


def card_for(job: JobView, step: ReviewStep):
    return next(card for card in job.cards if card.step == step)


async def decide_card(
    api: API, job: JobView, token: str, step: ReviewStep, action: str = "continue"
) -> JobView:
    card = card_for(job, step)
    response = await api.http.post(
        f"{JOBS_PATH}/{job.job.job_id}/cards/{card.card_id}/decision",
        headers=bearer(token),
        json={"decision_id": str(uuid4()), "action": action, "comment": "Reviewed"},
    )
    assert response.status_code == 200, response.text
    return JobView.model_validate(response.json())


async def test_four_additional_hitl_points_run_in_order(
    settings: Settings, request_body: StartJob
) -> None:
    agent = StubAgent(None)
    body = with_reviews(request_body, "low_margin", "resource_gap", "no_solution", "final_approval")
    async with running(settings, ManualClock(), agent) as api:
        job = await api.start(body)
        assert job.phase == "awaiting_review"
        assert [card.step for card in job.cards] == ["margin_review"]
        assert job.cards[0].role == "руководитель подразделения"

        job = await decide_card(api, job, LEAD, "margin_review")
        assert [card.step for card in job.cards if card.state == "open"] == ["resource_review"]
        assert card_for(job, "resource_review").role == "служба снабжения"

        job = await decide_card(api, job, SUPPLY, "resource_review")
        assert [card.step for card in job.cards if card.state == "open"] == ["no_solution_review"]

        job = await decide_card(api, job, LEAD, "no_solution_review")
        assert [card.step for card in job.cards if card.state == "open"] == [
            "final_approval_review"
        ]

        job = await decide_card(api, job, TENDER, "final_approval_review")
        assert job.phase == "ready_for_next_stage"
        assert all(card.state == "decided" for card in job.cards)


async def test_stop_on_additional_point_declins_job(
    settings: Settings, request_body: StartJob
) -> None:
    async with running(settings, ManualClock(), StubAgent(None)) as api:
        job = await api.start(with_reviews(request_body, "low_margin"))
        response = await api.http.post(
            f"{JOBS_PATH}/{job.job.job_id}/cards/{card_for(job, 'margin_review').card_id}/decision",
            headers=bearer(LEAD),
            json={"decision_id": str(uuid4()), "action": "stop", "comment": "Margin too low"},
        )
        assert response.status_code == 200
        assert JobView.model_validate(response.json()).phase == "declined"


async def test_additional_point_timeout_escalates_to_leader(
    settings: Settings, request_body: StartJob
) -> None:
    clock = ManualClock()
    async with running(settings, clock, StubAgent(None)) as api:
        job = await api.start(with_reviews(request_body, "resource_gap"))
        clock.time += timedelta(seconds=settings.review_timeout_seconds + 1)
        await api.service.expire_due()
        state = await api.wait_for(job.job.job_id, lambda item: len(item.cards) == 2)
        assert [card.step for card in state.cards] == ["resource_review", "resource_escalation"]
        assert state.cards[0].state == "timed_out" and state.cards[0].timeout
        assert state.cards[1].role == "руководитель подразделения"
        decided = await decide_card(api, state, LEAD, "resource_escalation")
        assert decided.cards[1].decision is not None and decided.cards[1].decision.timeout
