import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from redis.asyncio import Redis
from starlette.types import Receive, Scope, Send

from contracts.models import Artifact, HumanDecision, SolverResult, StopReason
from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.agent import AgentClient
from tender_orchestrator.api import create_app
from tender_orchestrator.application import Orchestrator
from tender_orchestrator.config import ReviewerCredential, Settings
from tender_orchestrator.db import Database, JobStore, connection_info
from tender_orchestrator.models import (
    RISK_REASONS,
    CachedPosition,
    Clock,
    DecisionRequest,
    IncomingDecision,
    JobView,
    OutboxEvent,
    Reviewer,
    Role,
    StartJob,
)
from tender_orchestrator.ports import RiskAgent, agent_error
from tests.demo import JOBS_PATH, bearer

LAWYER = "local-orchestrator-lawyer-token"
LEAD = "local-orchestrator-leader-token"
OUTSIDE = "local-orchestrator-external-lawyer"
SPECIALIST = "local-orchestrator-specialist-token"
SUPPLY = "local-orchestrator-supply-token"


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
        role="LAWYER",
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

        assert pending.job.status == "WAITING_HUMAN"
        assert (
            pending.risk_result is not None
            and pending.risk_result.sources[0].source == request_body.risk.source
        )
        assert len(pending.cards) == 1 and pending.cards[0].reason_code == "PENALTY_ABOVE_THRESHOLD"
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
        assert done.cards[0].decision is not None and done.cards[0].decision.decision == "APPROVED"


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
        assert [card.reason_code for card in result.cards] == [RISK_REASONS[reason]]
    assert (result.failure is not None) == (phase == "failed")
    if result.failure is not None:
        assert result.job.status == "ERROR" and result.risk_result is None


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
        trace_id = str(uuid4())
        conflicting = await api.http.post(JOBS_PATH, json=wrong, headers={"X-Trace-Id": trace_id})
        assert conflicting.json() == {
            "code": "job_conflict",
            "message": "Job conflict",
            "trace_id": trace_id,
        }
        assert conflicting.headers["X-Trace-Id"] == trace_id
        wrong["job"]["job_id"] = wrong["risk"]["job_id"] = str(uuid4())
        assert (await api.http.post(JOBS_PATH, json=wrong)).status_code == 409

        decision = decision_payload()
        for token in (LEAD, OUTSIDE):
            assert (await api.decision(pending, token, decision)).status_code == 403
        service_token = settings.service_token.get_secret_value()
        assert (await api.decision(pending, service_token, decision)).status_code == 401
        foreign_url = f"{JOBS_PATH}/{uuid4()}/cards/{pending.cards[0].card_id}/decision"
        foreign = await api.http.post(foreign_url, headers=bearer(LAWYER), json=decision)
        assert foreign.status_code == 404 and foreign.json()["code"] == "card_not_found"

        first = await api.decision(pending, LAWYER, decision)
        assert first.status_code == 200
        assert (await api.decision(pending, LAWYER, decision)).json() == first.json()
        changed = {**decision, "action": "stop", "comment": "Different decision"}
        assert (await api.decision(pending, LAWYER, changed)).status_code == 409
        assert (await api.decision(pending, LAWYER, decision_payload())).status_code == 409
    assert agent.calls == 1


def package(job: JobView) -> list[dict[str, object]]:
    return [
        Artifact.model_validate(
            {
                "artifact_id": uuid4(),
                "kind": kind,
                "file_path": f"artifacts/{job.job.job_id}/proposal.{kind.lower()}",
                "version": "1",
                "kb_version": job.job.kb_version,
                "checksum": "ab" * 32,
            }
        ).model_dump(mode="json")
        for kind in ("DOCX", "PDF", "XLSX")
    ]


def solver_payload(
    job: JobView, *, reasons: list[StopReason], status: str = "READY"
) -> dict[str, object]:
    result = SolverResult.model_validate(
        {
            "job_id": job.job.job_id,
            "kb_version": job.job.kb_version,
            "status": status,
            "requirements": [],
            "compliance": [],
            "resources": [],
            "estimate": {},
            "risks": [],
            "recommendation": {"value": "PARTICIPATE", "conditions": []},
            "plan": {"steps": [], "deadlines": [], "owners": []},
            "explanation": {"steps": [], "rule_trace": [], "source_refs": []},
            "artifacts": [],
            "stop_reasons": reasons,
        }
    )
    return {"execution_id": str(job.execution_id), "result": result.model_dump(mode="json")}


async def stage(api: API, job: JobView, path: str, body: Mapping[str, object]) -> JobView:
    result = await api.http.post(f"{JOBS_PATH}/{job.job.job_id}/{path}", json=body)
    assert result.is_success, result.text
    return JobView.model_validate(result.json())


async def test_canonical_review_and_artifact_gate_survive_restart_and_replays(
    settings: Settings,
    request_body: StartJob,
) -> None:
    clock = ManualClock()
    agent = StubAgent(None)
    roles: list[tuple[StopReason, str]] = [
        ("PENALTY_ABOVE_THRESHOLD", LAWYER),
        ("LOW_MARGIN", LEAD),
        ("RESOURCE_GAP", SUPPLY),
        ("UNCLEAR_REQUIREMENTS", SPECIALIST),
        ("FINAL_APPROVAL", SPECIALIST),
    ]
    async with running(settings, clock, agent) as api:
        started = await api.start(request_body)
        body = solver_payload(started, reasons=[reason for reason, _ in roles])
        artifacts = package(started)
        result = body["result"]
        assert isinstance(result, dict)
        result["artifacts"] = artifacts
        review = await stage(api, started, "solver-result", body)
        assert review.solver_result is not None and not review.solver_result.artifacts
        artifact_url = f"{JOBS_PATH}/{started.job.job_id}/artifacts"
        premature = {"execution_id": str(review.execution_id), "artifacts": artifacts}
        assert (await api.http.post(artifact_url, json=premature)).status_code == 409
        assert await stage(api, started, "solver-result", body) == review
        for index, (reason, role) in enumerate(roles[:-1]):
            assert review.job.status == "WAITING_HUMAN"
            assert review.cards[index].reason_code == reason
            response = await api.decision(review, role, decision_payload(), index=index)
            review = JobView.model_validate(response.raise_for_status().json())
            recorded = review.cards[index].decision
            assert isinstance(recorded, HumanDecision)
            assert recorded.decided_at == clock.time

    async with running(settings, clock, agent) as restarted:
        assert await restarted.get(started.job.job_id) == review
        final = review.cards[-1]
        assert final.reason_code == "FINAL_APPROVAL" and final.role == "TENDER_SPECIALIST"
        response = await restarted.decision(review, LAWYER, decision_payload(), index=4)
        assert response.status_code == 403
        approved = await restarted.decision(review, SPECIALIST, decision_payload(), index=4)
        waiting = JobView.model_validate(approved.raise_for_status().json())
        assert waiting.phase == "awaiting_artifacts" and waiting.job.status == "PROCESSING"
        assert (await restarted.http.post(artifact_url, json=premature)).status_code == 409
        receipt = {"execution_id": str(waiting.execution_id), "artifacts": artifacts}
        invalid = {
            **receipt,
            "artifacts": [{**artifacts[0], "checksum": "cd" * 32}, *artifacts[1:]],
        }
        assert (await restarted.http.post(artifact_url, json=invalid)).status_code == 422
        assert (
            await restarted.http.post(artifact_url, json={**receipt, "artifacts": artifacts[:2]})
        ).status_code == 422
        done = await stage(restarted, waiting, "artifacts", receipt)
        assert done.job.status == "DONE" and done.solver_result is not None
        assert done.artifacts == done.solver_result.artifacts and len(done.artifacts) == 3
        assert (
            await stage(restarted, waiting, "artifacts", {**receipt, "artifacts": artifacts[::-1]})
            == done
        )
        assert agent.calls == 1


async def test_no_solution_escalates_and_clarifies_before_new_final_approval(
    settings: Settings,
    request_body: StartJob,
) -> None:
    clock = ManualClock()
    agent = StubAgent(None)
    async with running(settings, clock, agent) as api:
        started = await api.start(request_body)
        unsolved = await stage(
            api, started, "solver-result", solver_payload(started, reasons=[], status="NO_SOLUTION")
        )
        assert (
            unsolved.cards[0].reason_code == "NO_SOLUTION"
            and unsolved.cards[0].role == "DIVISION_HEAD"
        )
        clock.time = unsolved.cards[0].deadline
        late = await api.decision(unsolved, LEAD, decision_payload())
        assert late.status_code == 409
        escalated = await api.get(unsolved.job.job_id)
        assert [(item.state, item.timeout) for item in escalated.cards] == [
            ("timed_out", True),
            ("open", True),
        ]
        answer = {
            "decision_id": str(uuid4()),
            "decision": "NEEDS_INFO",
            "comment": "Уточните условия",
        }
        response = await api.decision(escalated, LEAD, answer, index=1)
        clarification = JobView.model_validate(response.raise_for_status().json())
        assert clarification.job.status == "NEEDS_CLARIFICATION"
        assert clarification.execution_id != unsolved.execution_id
        execution_id = str(clarification.execution_id)
        failed = await stage(
            api,
            clarification,
            "stage-failure",
            {
                "execution_id": execution_id,
                "stage": "solve",
                "failure": {
                    "code": "solver_unavailable",
                    "retryable": True,
                    "a2a_task_id": None,
                    "trace_id": None,
                },
            },
        )
        assert failed.job.status == "ERROR"
        restarted = await stage(
            api, failed, "retry-stage", {"execution_id": execution_id, "stage": "solve"}
        )
        corrected = await stage(
            api, restarted, "solver-result", solver_payload(restarted, reasons=[])
        )
        assert (
            corrected.job.status == "WAITING_HUMAN"
            and corrected.cards[-1].reason_code == "FINAL_APPROVAL"
        )
        declined = {**answer, "decision_id": str(uuid4()), "decision": "REJECTED"}
        response = await api.decision(corrected, SPECIALIST, declined, index=2)
        assert JobView.model_validate(response.raise_for_status().json()).job.status == "ERROR"
        assert agent.calls == 1


async def test_durable_extraction_handoff_and_stage_retry_only_repeat_failed_stage(
    settings: Settings,
    request_body: StartJob,
) -> None:
    agent = StubAgent(None)
    async with running(settings, ManualClock(), agent) as api:
        response = await api.http.post(JOBS_PATH, json=request_body.job.model_dump(mode="json"))
        registered = JobView.model_validate(response.raise_for_status().json())
        assert (
            registered.job.status == "ACCEPTED"
            and registered.stage == "extract"
            and agent.calls == 0
        )
        ready = await stage(
            api,
            registered,
            "risk-input",
            {
                "execution_id": str(registered.execution_id),
                "risk": request_body.risk.model_dump(mode="json"),
            },
        )
        assert ready.stage == "solve" and agent.calls == 1
        failure = {
            "execution_id": str(ready.execution_id),
            "stage": "solve",
            "failure": {
                "code": "solver_unavailable",
                "retryable": True,
                "a2a_task_id": None,
                "trace_id": None,
            },
        }
        failed = await stage(api, ready, "stage-failure", failure)
        assert failed.job.status == "ERROR"
        retry = {"execution_id": str(ready.execution_id), "stage": "solve"}
        retried = await stage(api, failed, "retry-stage", retry)
        assert retried.execution_id != ready.execution_id and retried.stage == "solve"
        assert await stage(api, failed, "retry-stage", retry) == retried
        assert await stage(api, failed, "stage-failure", failure) == retried
        invalid = await api.http.post(
            f"{JOBS_PATH}/{ready.job.job_id}/solver-result", json=solver_payload(ready, reasons=[])
        )
        assert invalid.status_code == 409
        reviewed = await stage(api, retried, "solver-result", solver_payload(retried, reasons=[]))
        assert reviewed.job.status == "WAITING_HUMAN" and agent.calls == 1


async def test_legacy_job_replay_and_decision_with_unknown_timestamp(
    settings: Settings,
    request_body: StartJob,
) -> None:
    database = Database(connection_info(settings), 1)
    job_id = request_body.job.job_id
    old = request_body.model_dump(mode="json", exclude_none=True)
    old["job"]["status"] = "принято"
    async with database.connect() as connection:
        await connection.execute(
            "INSERT INTO jobs (job_id, idempotency_key, request, phase, stage, needs_advance) VALUES (%s, %s, %s, 'ready_for_next_stage', 'solve', false)",
            (job_id, request_body.job.idempotency_key, Jsonb(old)),
        )
        await connection.execute(
            """
            INSERT INTO review_cards (card_id, job_id, step, reason_code, role, deadline, state, decision_id, actor_id, action, comment)
            VALUES (%s, %s, 'risk_review', 'penalty_above_threshold', 'юрист', %s, 'decided', %s, %s, 'continue', '')
            """,
            (uuid4(), job_id, ManualClock().time, uuid4(), uuid4()),
        )
    agent = StubAgent(None)
    async with running(settings, ManualClock(), agent) as api:
        assert await api.start(request_body) == await api.get(job_id)
        result = await api.get(job_id)
        assert result.cards[0].reason_code == "PENALTY_ABOVE_THRESHOLD"
        assert result.cards[0].decision is not None
        assert "decided_at" not in result.cards[0].decision.model_dump()
        assert agent.calls == 0


async def test_kafka_intake_decision_outbox_and_redis_checkpoint(
    settings: Settings,
    request_body: StartJob,
) -> None:
    def clock() -> datetime:
        return datetime.now(UTC)

    agent = StubAgent(None)
    job_id = request_body.job.job_id
    key = str(job_id).encode()
    brokers = ",".join(settings.kafka_brokers)
    async with (
        running(settings, clock, agent) as api,
        AIOKafkaProducer(bootstrap_servers=brokers, acks="all") as producer,
        AIOKafkaConsumer(
            "tender.human.required",
            "tender.artifacts.ready",
            bootstrap_servers=brokers,
            auto_offset_reset="earliest",
        ) as observer,
        Redis.from_url(str(settings.redis_url)) as redis,
        asyncio.timeout(20),
    ):
        await producer.send_and_wait("tender.job.created", b"not JSON", key=key)
        body = request_body.job.model_dump_json().encode()
        await producer.send_and_wait("tender.job.created", body, key=key)
        await producer.send_and_wait("tender.job.created", body, key=key)
        while True:
            response = await api.http.get(f"{JOBS_PATH}/{job_id}")
            if response.status_code == 200:
                registered = JobView.model_validate(response.json())
                break
            assert response.status_code == 404
            await asyncio.sleep(0.02)
        assert registered.job.status == "ACCEPTED" and registered.stage == "extract"
        ready = await stage(
            api,
            registered,
            "risk-input",
            {
                "execution_id": str(registered.execution_id),
                "risk": request_body.risk.model_dump(mode="json"),
            },
        )
        review = await stage(api, ready, "solver-result", solver_payload(ready, reasons=[]))
        async for message in observer:
            if message.topic == "tender.human.required" and message.key == key:
                assert isinstance(message.value, bytes)
                event = OutboxEvent.model_validate_json(message.value)
                assert event.payload["reason_code"] == "FINAL_APPROVAL"
                break
        card = review.cards[0]
        status_key = f"job:status:{job_id}"
        checkpoint_key = f"checkpoint:{job_id}"
        while True:
            checkpoint = await redis.get(checkpoint_key)
            if (
                isinstance(checkpoint, bytes)
                and CachedPosition.model_validate_json(checkpoint).phase == "awaiting_review"
            ):
                break
            await asyncio.sleep(0.02)
        assert await redis.delete(status_key, checkpoint_key) == 2
        while True:
            restored = await redis.get(checkpoint_key)
            if restored is not None:
                break
            await asyncio.sleep(0.02)
        assert restored == checkpoint and await redis.get(status_key) == b"WAITING_HUMAN"
        reviewer = next(item for item in settings.reviewers if item.role == "TENDER_SPECIALIST")
        answer = (
            IncomingDecision(
                card_id=card.card_id,
                actor_id=reviewer.actor_id,
                decision=HumanDecision(
                    decision_id=uuid4(),
                    job_id=job_id,
                    reason_code=card.reason_code,
                    role=reviewer.role,
                    question=card.question,
                    deadline=card.deadline,
                    timeout=False,
                    decision="APPROVED",
                    comment="Утверждено",
                    decided_at=clock(),
                ),
            )
            .model_dump_json()
            .encode()
        )
        await producer.send_and_wait("tender.human.decided", answer, key=key)
        await producer.send_and_wait("tender.human.decided", answer, key=key)
        approved = await api.wait_for(job_id, lambda view: view.phase == "awaiting_artifacts")
        await stage(
            api,
            approved,
            "artifacts",
            {"execution_id": str(approved.execution_id), "artifacts": package(approved)},
        )
        async for message in observer:
            if message.topic == "tender.artifacts.ready" and message.key == key:
                assert isinstance(message.value, bytes)
                event = OutboxEvent.model_validate_json(message.value)
                artifacts = event.payload["artifacts"]
                assert isinstance(artifacts, list) and len(artifacts) == 3
                break
        while True:
            if await redis.get(status_key) == b"DONE":
                break
            await asyncio.sleep(0.02)
        checkpoint = await redis.get(checkpoint_key)
        assert isinstance(checkpoint, bytes)
        position = CachedPosition.model_validate_json(checkpoint)
        assert position.status == "DONE" and position.execution_id == approved.execution_id
        assert await redis.ttl(checkpoint_key) > 0
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
        assert review.cards[1].role == "DIVISION_HEAD" and review.cards[1].state == "open"
        request = {**decision_payload(), "action": "stop", "comment": "Условие неприемлемо"}
        assert (await second.decision(review, LAWYER, request, index=1)).status_code == 403
        response = await second.decision(review, LEAD, request, index=1)
        final = JobView.model_validate(response.raise_for_status().json())
        assert final.phase == "declined" and final.job.status == "ERROR"
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
    ) -> None:
        await original(self, job_id, reason, role, deadline)
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
    actor: Reviewer = next(item for item in settings.reviewers if item.role == "LAWYER")
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
