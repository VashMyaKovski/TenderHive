import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, LiteralString, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, errors
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL
from psycopg.types.json import Jsonb

from contracts.models import (
    Artifact,
    Estimate,
    HumanDecision,
    Job,
    Recommendation,
    SolverResult,
    StopReason,
)
from contracts.risk_task_v1 import HumanReason, RiskResultV1, RiskTaskV1
from tender_orchestrator.config import Settings
from tender_orchestrator.models import (
    DECISIONS,
    JOB_STATUS,
    LEAD_ROLE,
    REVIEW_QUESTIONS,
    REVIEW_ROLES,
    RISK_REASONS,
    ArtifactReceipt,
    AssessmentFailure,
    CachedPosition,
    DecisionRequest,
    JobView,
    LegacyHumanDecision,
    OutboxEvent,
    Phase,
    RetryStage,
    ReviewCard,
    Reviewer,
    ReviewStep,
    RiskInput,
    Role,
    SolverHandoff,
    Stage,
    StageFailure,
    StartJob,
    WorkflowError,
)
from tender_orchestrator.ports import JobRecord, RecordedDecision

_SCHEMA_VERSION = 3

_RISK_STEPS = ("risk_review", "risk_escalation")
_ESCALATION_STEPS = ("risk_escalation", "review_escalation")


def connection_info(settings: Settings) -> str:
    return make_conninfo(
        host=settings.database_host,
        port=settings.database_port,
        dbname=settings.database_name,
        user=settings.database_user,
        password=settings.database_password.get_secret_value(),
        connect_timeout=settings.database_timeout_seconds,
        options=(
            "-csearch_path=th_orchestrator -ctimezone=UTC "
            f"-cstatement_timeout={settings.database_timeout_seconds * 1000}"
        ),
    )


class Database:
    def __init__(self, conninfo: str, max_connections: int) -> None:
        self._conninfo = conninfo
        self._slots = asyncio.Semaphore(max_connections)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        async with (
            self._slots,
            await AsyncConnection[DictRow].connect(
                self._conninfo, autocommit=True, row_factory=dict_row
            ) as connection,
        ):
            yield connection

    @asynccontextmanager
    async def session(self) -> AsyncIterator[JobStore]:
        async with self.connect() as connection:
            yield JobStore(connection)

    async def check_ready(self) -> None:
        async with self.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = %s) "
                    "AND EXISTS (SELECT 1 FROM checkpoint_migrations WHERE v = %s) AS ready",
                    (_SCHEMA_VERSION, len(AsyncPostgresSaver.MIGRATIONS) - 1),
                )
            ).fetchone()
            if row is None or not row["ready"]:
                raise errors.ObjectNotInPrerequisiteState("orchestrator migrations are required")


class JobStore:
    def __init__(self, connection: AsyncConnection[DictRow]) -> None:
        self._connection = connection

    async def lock(self, job_id: UUID, *, wait: bool) -> bool:
        # The dedicated autocommit session releases its lock on close, including after a crash.
        key = f"th_orchestrator:job:{job_id}"
        if wait:
            await self._connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,)
            )
            return True
        row = await (
            await self._connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", (key,)
            )
        ).fetchone()
        return bool(row and row["locked"])

    async def try_worker_lock(self, name: Literal["outbox", "cache"]) -> bool:
        row = await (
            await self._connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                (f"th_orchestrator:worker:{name}",),
            )
        ).fetchone()
        return bool(row and row["locked"])

    async def outbox_batch(self, limit: int) -> list[OutboxEvent]:
        rows = await (
            await self._connection.execute(
                "SELECT event_id, job_id, topic AS event_type, occurred_at, payload FROM event_outbox "
                "WHERE NOT delivered ORDER BY sequence LIMIT %s",
                (limit,),
            )
        ).fetchall()
        return [OutboxEvent.model_validate(row) for row in rows]

    async def mark_delivered(self, event_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE event_outbox SET delivered = true WHERE event_id = %s", (event_id,)
        )

    async def cache_batch(self, limit: int) -> list[CachedPosition]:
        rows = await (
            await self._connection.execute(
                """
                (SELECT job_id, phase, stage, execution_id, status_version AS version FROM jobs
                 WHERE status_synced_version < status_version ORDER BY job_id LIMIT %s)
                UNION ALL
                (SELECT job_id, phase, stage, execution_id, status_version AS version FROM jobs
                 WHERE status_synced_version = status_version AND phase NOT IN ('done', 'declined', 'failed')
                 ORDER BY cache_synced_at NULLS FIRST, job_id LIMIT %s)
                """,
                (limit, limit),
            )
        ).fetchall()
        return [
            CachedPosition.model_validate({**row, "status": JOB_STATUS[row["phase"]]})
            for row in rows
        ]

    async def mark_cached(self, position: CachedPosition) -> None:
        await self._connection.execute(
            "UPDATE jobs SET status_synced_version = %s, cache_synced_at = clock_timestamp() "
            "WHERE job_id = %s AND status_version = %s",
            (position.version, position.job_id, position.version),
        )

    async def reject_event(self, topic: str, partition: int, offset: int, reason: str) -> None:
        await self._connection.execute(
            "INSERT INTO rejected_events (topic, partition_id, broker_offset, reason) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (topic, partition, offset, reason),
        )

    async def register(self, request: Job | StartJob) -> None:
        risk_start = isinstance(request, StartJob)
        job = request.job if risk_start else request
        if job.status != "ACCEPTED":
            raise WorkflowError("job_conflict")
        payload = (
            request.model_dump(mode="json") if risk_start else {"job": job.model_dump(mode="json")}
        )
        cursor = await self._connection.execute(
            "INSERT INTO jobs (job_id, idempotency_key, request, stage, needs_advance) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                job.job_id,
                job.idempotency_key,
                Jsonb(payload),
                "risk" if risk_start else "extract",
                risk_start,
            ),
        )
        if cursor.rowcount == 0:
            existing = await self._job_row(job.job_id, error="job_conflict")
            previous = existing["request"]
            if risk_start:
                matches = "risk" in previous and StartJob.model_validate(previous) == request
            else:
                matches = "risk" not in previous and Job.model_validate(previous["job"]) == job
            if not matches:
                raise WorkflowError("job_conflict")

    async def _find_job(self, job_id: UUID) -> DictRow | None:
        return await (
            await self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s",
                (job_id,),
            )
        ).fetchone()

    async def job(self, job_id: UUID) -> JobRecord:
        row = await self._job_row(job_id)
        risk = row["risk_input"] or row["request"].get("risk")
        risk_review = None
        if row["phase"] == "awaiting_review" and row["stage"] == "risk":
            card = await (
                await self._connection.execute(
                    """
                    SELECT lower(reason_code) AS reason FROM review_cards
                    WHERE job_id = %s AND step = 'risk_review' AND review_round = %s
                    """,
                    (job_id, row["risk_generation"]),
                )
            ).fetchone()
            risk_review = card["reason"] if card else None
        return JobRecord(
            job=Job.model_validate(row["request"]["job"]),
            risk=RiskTaskV1.model_validate(risk) if risk is not None else None,
            needs_advance=row["needs_advance"],
            risk_generation=row["risk_generation"],
            risk_attempts=row["risk_attempts"],
            risk_result=RiskResultV1.model_validate(row["risk_result"])
            if row["risk_result"] is not None
            else None,
            failure=AssessmentFailure.model_validate(row["failure"])
            if row["failure"] is not None
            else None,
            stage=row["stage"],
            risk_review=risk_review,
        )

    async def _job_row(
        self, job_id: UUID, *, error: Literal["job_not_found", "job_conflict"] = "job_not_found"
    ) -> DictRow:
        row = await self._find_job(job_id)
        if row is None:
            raise WorkflowError(error)
        return row

    async def view(self, job_id: UUID) -> JobView:
        row = await (
            await self._connection.execute(
                """
            SELECT jobs.*, COALESCE(
                (SELECT jsonb_agg(to_jsonb(review_cards) ORDER BY opened_at, step DESC, card_id) FROM review_cards
                 WHERE review_cards.job_id = jobs.job_id), '[]'::jsonb
            ) AS cards FROM jobs WHERE job_id = %s
            """,
                (job_id,),
            )
        ).fetchone()
        if row is None:
            raise WorkflowError("job_not_found")
        phase: Phase = row["phase"]
        job = Job.model_validate(row["request"]["job"])
        updated_at = max(job.created_at, row["updated_at"]) if row["updated_at"] else job.updated_at
        solver = row["solver_result"]
        return JobView.model_validate(
            {
                "job": job.model_copy(
                    update={"status": JOB_STATUS[phase], "updated_at": updated_at}
                ),
                "phase": phase,
                "risk_attempts": row["risk_attempts"],
                "risk_result": row["risk_result"],
                "failure": row["failure"],
                "cards": [_public_card(card) for card in row["cards"]],
                "stage": row["stage"],
                "execution_id": row["execution_id"],
                "solver_result": {**solver, "artifacts": row["artifacts"] or []}
                if solver
                else None,
                "artifacts": row["artifacts"] or [],
            }
        )

    async def reserve_attempt(self, job_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE jobs SET risk_attempts = risk_attempts + 1, phase = 'assessing', failure = NULL, "
            "updated_at = clock_timestamp(), status_version = status_version + 1 WHERE job_id = %s",
            (job_id,),
        )

    async def save_result(self, job_id: UUID, result: RiskResultV1) -> None:
        await self._connection.execute(
            "UPDATE jobs SET risk_result = %s, failure = NULL WHERE job_id = %s",
            (Jsonb(result.model_dump(mode="json")), job_id),
        )

    async def save_failure(self, job_id: UUID, failure: AssessmentFailure) -> None:
        await self._connection.execute(
            "UPDATE jobs SET failure = %s WHERE job_id = %s",
            (Jsonb(failure.model_dump(mode="json")), job_id),
        )

    async def open_review(
        self,
        job_id: UUID,
        reason: HumanReason,
        role: Role,
        deadline: datetime,
    ) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            existing = await self._connection.execute(
                "SELECT 1 FROM review_cards WHERE job_id = %s AND step = 'risk_review' AND review_round = %s",
                (job_id, row["risk_generation"]),
            )
            if await existing.fetchone() is None:
                await self._open_card(
                    job_id,
                    "risk_review",
                    RISK_REASONS[reason],
                    role,
                    deadline,
                    row["risk_generation"],
                )
                await self._transition(job_id, "awaiting_review", "risk")
                await self._stage_event(job_id, "risk", "WAITING_HUMAN")

    async def set_phase(self, job_id: UUID, phase: Phase) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            if row["phase"] == phase:
                return
            if phase == "ready_for_next_stage":
                await self._transition(job_id, phase, "solve", execution_id=uuid4())
                await self._stage_event(job_id, "risk", "DONE")
            else:
                await self._transition(
                    job_id,
                    phase,
                    "risk",
                    execution_id=uuid4() if phase == "needs_clarification" else None,
                )
                if phase in {"failed", "declined"}:
                    await self._stage_event(job_id, "risk", "FAILED")
            await self.mark_advanced(job_id)

    async def mark_advanced(self, job_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE jobs SET needs_advance = false WHERE job_id = %s", (job_id,)
        )

    async def finish_risk(self, job_id: UUID, deadline: datetime, *, failed: bool) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            if row["stage"] != "risk":
                return
            legacy = row["request"].get("reviews")
            if legacy:
                request = StartJob.model_validate(row["request"])
                completed = await (
                    await self._connection.execute(
                        "SELECT reason_code FROM review_cards WHERE job_id = %s AND state = 'decided'",
                        (job_id,),
                    )
                ).fetchall()
                reviewed = {_reason(card["reason_code"]) for card in completed}
                reasons: list[StopReason] = [
                    point.reason_code
                    for point in request.reviews
                    if point.reason_code not in reviewed
                    and (not failed or point.reason_code == "NO_SOLUTION")
                ]
                if reasons:
                    round_id = row["review_round"] + 1
                    await self._connection.execute(
                        "UPDATE jobs SET pending_reasons = %s, review_round = %s, "
                        "needs_advance = false WHERE job_id = %s",
                        (reasons, round_id, job_id),
                    )
                    await self._transition(job_id, "awaiting_review", "review")
                    await self._open_card(
                        job_id, "review", reasons[0], REVIEW_ROLES[reasons[0]], deadline, round_id
                    )
                    return
            await self.set_phase(job_id, "failed" if failed else "ready_for_next_stage")

    async def review_card(self, job_id: UUID, card_id: UUID) -> ReviewCard:
        return _public_card(await self._card(job_id, card_id))

    async def _card(self, job_id: UUID, card_id: UUID) -> DictRow:
        row = await (
            await self._connection.execute(
                "SELECT * FROM review_cards WHERE job_id = %s AND card_id = %s", (job_id, card_id)
            )
        ).fetchone()
        if row is None:
            raise WorkflowError("card_not_found")
        return row

    async def expire(self, job_id: UUID, now: datetime) -> None:
        async with self._connection.transaction():
            card = await (
                await self._connection.execute(
                    """
                UPDATE review_cards SET state = 'timed_out'
                WHERE job_id = %s AND step IN ('risk_review', 'review') AND state = 'open' AND deadline <= %s
                RETURNING reason_code, deadline, step, review_round
                """,
                    (job_id, now),
                )
            ).fetchone()
            if card is not None:
                await self._open_card(
                    job_id,
                    "risk_escalation" if card["step"] == "risk_review" else "review_escalation",
                    _reason(card["reason_code"]),
                    LEAD_ROLE,
                    card["deadline"],
                    card["review_round"],
                )

    async def record_decision(
        self,
        job_id: UUID,
        card_id: UUID,
        actor: Reviewer,
        request: DecisionRequest,
        now: datetime,
    ) -> None:
        card = await self._card(job_id, card_id)
        confirmation = (
            request.supply_confirmation.model_dump(mode="json")
            if request.supply_confirmation
            else None
        )
        if card["state"] != "open":
            if (
                card["decision_id"] == request.decision_id
                and card["actor_id"] == actor.actor_id
                and card["action"] == request.action
                and card["comment"] == request.comment
                and card["supply_confirmation"] == confirmation
            ):
                return
            raise WorkflowError("decision_conflict")
        try:
            async with self._connection.transaction():
                cursor = await self._connection.execute(
                    """
                    UPDATE review_cards SET state = 'decided', decision_id = %s, actor_id = %s,
                        action = %s, comment = %s, decided_at = %s, decided_role = %s,
                        question = COALESCE(question, %s), supply_confirmation = %s
                    WHERE job_id = %s AND card_id = %s AND state = 'open'
                        AND (step IN ('risk_escalation', 'review_escalation') OR deadline > %s)
                    """,
                    (
                        request.decision_id,
                        actor.actor_id,
                        request.action,
                        request.comment,
                        now,
                        actor.role,
                        REVIEW_QUESTIONS[_reason(card["reason_code"])],
                        Jsonb(confirmation) if confirmation is not None else None,
                        job_id,
                        card_id,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WorkflowError("decision_conflict")
                await self._connection.execute(
                    "UPDATE jobs SET needs_advance = true WHERE job_id = %s", (job_id,)
                )
        except errors.UniqueViolation:
            raise WorkflowError("decision_conflict") from None

    async def recorded_decision(self, job_id: UUID) -> RecordedDecision | None:
        row = await (
            await self._connection.execute(
                """
                SELECT decision_id, action FROM review_cards JOIN jobs USING (job_id)
                WHERE job_id = %s AND state = 'decided' AND step IN ('risk_review', 'risk_escalation')
                    AND review_cards.review_round = jobs.risk_generation
                """,
                (job_id,),
            )
        ).fetchone()
        return RecordedDecision(row["decision_id"], row["action"]) if row is not None else None

    async def pending_jobs(self, limit: int) -> list[UUID]:
        rows = await (
            await self._connection.execute(
                "SELECT job_id FROM jobs WHERE needs_advance ORDER BY job_id LIMIT %s",
                (limit,),
            )
        ).fetchall()
        return [row["job_id"] for row in rows]

    async def due_jobs(self, now: datetime, limit: int) -> list[UUID]:
        rows = await (
            await self._connection.execute(
                """
            SELECT job_id FROM review_cards WHERE step IN ('risk_review', 'review') AND state = 'open'
                AND deadline <= %s ORDER BY deadline, job_id LIMIT %s
            """,
                (now, limit),
            )
        ).fetchall()
        return [row["job_id"] for row in rows]

    async def accept_risk_input(self, job_id: UUID, body: RiskInput) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            data = body.model_dump(mode="json")
            if await self._received(job_id, body.execution_id, "risk_input", data):
                return
            job = Job.model_validate(row["request"]["job"])
            if body.risk.job_id != job_id or body.risk.rules_version != job.kb_version:
                raise WorkflowError("result_mismatch")
            if (row["stage"], row["phase"]) not in {
                ("extract", "accepted"),
                ("risk", "needs_clarification"),
            } or row["execution_id"] != body.execution_id:
                raise WorkflowError("stage_conflict")
            if row["stage"] == "extract":
                await self._stage_event(job_id, "extract", "DONE")
            await self._connection.execute(
                "UPDATE jobs SET risk_input = %s WHERE job_id = %s",
                (Jsonb(body.risk.model_dump(mode="json")), job_id),
            )
            await self._reset_risk(job_id)
            await self._save_receipt(job_id, body.execution_id, "risk_input", data)

    async def accept_solver_result(
        self, job_id: UUID, body: SolverHandoff, deadline: datetime
    ) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            data = body.model_dump(mode="json")
            if await self._received(job_id, body.execution_id, "solver", data):
                return
            result = body.result
            negative_margin = (
                isinstance(result.estimate, Estimate) and result.estimate.margin_percent < 0
            )
            declined_result = (
                result.status == "NO_SOLUTION"
                and isinstance(result.recommendation, Recommendation)
                and result.recommendation.value == "NOT_PARTICIPATE"
            )
            early_loss = (
                (row["stage"], row["phase"]) == ("extract", "accepted")
                and negative_margin
                and declined_result
            )
            solving = row["stage"] == "solve" and row["phase"] in {
                "ready_for_next_stage",
                "needs_clarification",
            }
            if not (early_loss or solving) or row["execution_id"] != body.execution_id:
                raise WorkflowError("stage_conflict")
            job = Job.model_validate(row["request"]["job"])
            if result.job_id != job_id or result.kb_version != job.kb_version:
                raise WorkflowError("result_mismatch")
            if any(item.kb_version != job.kb_version for item in result.artifacts):
                raise WorkflowError("result_mismatch")
            mandatory = {item.requirement_id for item in result.requirements if item.mandatory}
            unmet = any(
                item.requirement_id in mandatory and item.status == "NOT_MET"
                for item in result.compliance
            )
            uncovered = not result.resources or any(not item.covered for item in result.resources)
            supply_unresolved = False
            if not early_loss and uncovered:
                confirmation = await (
                    await self._connection.execute(
                        """
                        SELECT 1 FROM review_cards WHERE job_id = %s
                        AND reason_code = 'RESOURCE_GAP' AND state = 'decided'
                        AND action = 'continue' AND supply_confirmation IS NOT NULL LIMIT 1
                        """,
                        (job_id,),
                    )
                ).fetchone()
                supply_unresolved = confirmation is not None
            if negative_margin or unmet or supply_unresolved:
                if not declined_result or result.artifacts:
                    raise WorkflowError("result_mismatch")
                await self._connection.execute(
                    "UPDATE jobs SET solver_result = %s, pending_reasons = '{}' WHERE job_id = %s",
                    (Jsonb(result.model_dump(mode="json")), job_id),
                )
                await self._save_receipt(job_id, body.execution_id, "solver", data)
                if early_loss:
                    await self._stage_event(job_id, "extract", "DONE")
                await self._transition(job_id, "declined", "solve")
                await self._stage_event(job_id, "solve", "DONE")
                return
            reasons: list[StopReason] = list(
                dict.fromkeys(
                    reason for reason in result.stop_reasons if reason != "FINAL_APPROVAL"
                )
            )
            if result.status == "READY":
                if "NO_SOLUTION" in reasons:
                    raise WorkflowError("result_mismatch")
                reasons.append("FINAL_APPROVAL")
            elif result.status == "NO_SOLUTION" and "NO_SOLUTION" not in reasons:
                reasons.append("NO_SOLUTION")
            elif not reasons:
                reasons.append("UNCLEAR_REQUIREMENTS")
            if "RESOURCE_GAP" in reasons:
                reasons.remove("RESOURCE_GAP")
                reasons.insert(0, "RESOURCE_GAP")

            review_round = row["review_round"] + 1
            await self._connection.execute(
                "UPDATE jobs SET solver_result = %s, pending_reasons = %s, review_round = %s WHERE job_id = %s",
                (Jsonb(result.model_dump(mode="json")), reasons, review_round, job_id),
            )
            await self._save_receipt(job_id, body.execution_id, "solver", data)
            await self._stage_event(job_id, "solve", "DONE")
            await self._transition(job_id, "awaiting_review", "review")
            await self._open_card(
                job_id, "review", reasons[0], REVIEW_ROLES[reasons[0]], deadline, review_round
            )
            await self._stage_event(job_id, "review", "WAITING_HUMAN")

    async def advance_reviews(self, job_id: UUID, deadline: datetime) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            if row["stage"] != "review" or not row["needs_advance"]:
                return
            reasons: list[StopReason] = row["pending_reasons"]
            card = await (
                await self._connection.execute(
                    """
                SELECT action FROM review_cards WHERE job_id = %s AND step IN ('review', 'review_escalation')
                    AND review_round = %s AND reason_code = %s AND state = 'decided'
                """,
                    (job_id, row["review_round"], reasons[0]),
                )
            ).fetchone()
            if card is None:
                raise RuntimeError("review decision was not recorded")
            remaining = reasons[1:]
            await self._connection.execute(
                "UPDATE jobs SET pending_reasons = %s, needs_advance = false WHERE job_id = %s",
                (remaining, job_id),
            )
            if card["action"] == "stop":
                await self._transition(job_id, "declined", "review")
                await self._stage_event(job_id, "review", "FAILED")
            elif (
                card["action"] == "continue"
                and reasons[0] == "RESOURCE_GAP"
                and row["solver_result"]
            ):
                await self._connection.execute(
                    "UPDATE jobs SET pending_reasons = '{}', solver_result = NULL WHERE job_id = %s",
                    (job_id,),
                )
                await self._transition(
                    job_id, "ready_for_next_stage", "solve", execution_id=uuid4()
                )
                await self._stage_event(job_id, "review", "DONE")
            elif card["action"] == "continue" and not remaining and row["solver_result"] is None:
                await self._transition(
                    job_id, "ready_for_next_stage", "solve", execution_id=uuid4()
                )
                await self._stage_event(job_id, "review", "DONE")
            elif card["action"] == "needs_info" or (
                not remaining and reasons[0] != "FINAL_APPROVAL"
            ):
                await self._transition(job_id, "needs_clarification", "solve", execution_id=uuid4())
            elif remaining:
                reason = remaining[0]
                await self._open_card(
                    job_id, "review", reason, REVIEW_ROLES[reason], deadline, row["review_round"]
                )
                await self._stage_event(job_id, "review", "WAITING_HUMAN")
            else:
                await self._transition(
                    job_id, "awaiting_artifacts", "artifacts", execution_id=uuid4()
                )
                await self._stage_event(job_id, "review", "DONE")

    async def accept_artifacts(self, job_id: UUID, body: ArtifactReceipt) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            artifacts = sorted(body.artifacts, key=lambda item: item.artifact_id)
            data = body.model_copy(update={"artifacts": artifacts}).model_dump(mode="json")
            if await self._received(job_id, body.execution_id, "artifacts", data):
                return
            if (row["stage"], row["phase"]) != ("artifacts", "awaiting_artifacts") or row[
                "execution_id"
            ] != body.execution_id:
                raise WorkflowError("stage_conflict")
            approval = await (
                await self._connection.execute(
                    """
                SELECT 1 FROM review_cards WHERE job_id = %s AND review_round = %s
                    AND reason_code = 'FINAL_APPROVAL' AND action = 'continue' AND state = 'decided'
                """,
                    (job_id, row["review_round"]),
                )
            ).fetchone()
            result = SolverResult.model_validate(row["solver_result"])
            if approval is None or result.status != "READY":
                raise WorkflowError("stage_conflict")
            if any(item.kb_version != result.kb_version for item in artifacts):
                raise WorkflowError("result_mismatch")
            expected: dict[UUID, Artifact] = {item.artifact_id: item for item in result.artifacts}
            if expected and expected != {item.artifact_id: item for item in artifacts}:
                raise WorkflowError("result_mismatch")
            await self._connection.execute(
                "UPDATE jobs SET artifacts = %s WHERE job_id = %s",
                (Jsonb(data["artifacts"]), job_id),
            )
            await self._transition(job_id, "done", "artifacts")
            await self._save_receipt(job_id, body.execution_id, "artifacts", data)
            await self._stage_event(job_id, "artifacts", "DONE")
            await self._outbox(
                job_id,
                "tender.artifacts.ready",
                {"job_id": str(job_id), "artifacts": data["artifacts"]},
            )

    async def fail_stage(self, job_id: UUID, body: StageFailure) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            data = body.model_dump(mode="json")
            if await self._received(job_id, body.execution_id, "failure", data):
                return
            expected_phases = {
                "extract": {"accepted"},
                "solve": {"ready_for_next_stage", "needs_clarification"},
                "artifacts": {"awaiting_artifacts"},
            }
            if (
                row["stage"] != body.stage
                or row["phase"] not in expected_phases[body.stage]
                or row["execution_id"] != body.execution_id
            ):
                raise WorkflowError("stage_conflict")
            await self.save_failure(job_id, body.failure)
            await self._transition(job_id, "failed", body.stage)
            await self._save_receipt(job_id, body.execution_id, "failure", data)
            await self._stage_event(job_id, body.stage, "FAILED")

    async def retry_stage(self, job_id: UUID, body: RetryStage) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            data = body.model_dump(mode="json")
            if await self._received(job_id, body.execution_id, "retry", data):
                return
            if (
                row["stage"] != body.stage
                or row["phase"] != "failed"
                or row["execution_id"] != body.execution_id
                or row["failure"] is None
                or not row["failure"]["retryable"]
            ):
                raise WorkflowError("stage_conflict")
            if body.stage == "risk":
                await self._reset_risk(job_id)
            else:
                phases: dict[str, Phase] = {
                    "extract": "accepted",
                    "solve": "ready_for_next_stage",
                    "artifacts": "awaiting_artifacts",
                }
                await self._connection.execute(
                    "UPDATE jobs SET failure = NULL WHERE job_id = %s", (job_id,)
                )
                await self._transition(job_id, phases[body.stage], body.stage, execution_id=uuid4())
            await self._save_receipt(job_id, body.execution_id, "retry", data)

    async def cancel(self, job_id: UUID) -> None:
        async with self._connection.transaction():
            row = await self._job_row(job_id)
            if row["phase"] == "declined" and (row["failure"] or {}).get("code") == "job_cancelled":
                return
            if row["phase"] in {"done", "declined"}:
                raise WorkflowError("stage_conflict")
            await self._connection.execute(
                "UPDATE review_cards SET state = 'cancelled' WHERE job_id = %s AND state = 'open'",
                (job_id,),
            )
            await self.save_failure(
                job_id,
                AssessmentFailure(
                    code="job_cancelled", retryable=False, a2a_task_id=None, trace_id=None
                ),
            )
            await self.mark_advanced(job_id)
            await self._transition(job_id, "declined", row["stage"])
            await self._stage_event(job_id, row["stage"], "FAILED")

    async def _reset_risk(self, job_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE jobs SET risk_generation = risk_generation + 1, risk_attempts = 0, risk_result = NULL, "
            "failure = NULL, needs_advance = true WHERE job_id = %s",
            (job_id,),
        )
        await self._transition(job_id, "accepted", "risk", execution_id=uuid4())

    async def _transition(
        self, job_id: UUID, phase: Phase, stage: Stage, *, execution_id: UUID | None = None
    ) -> None:
        await self._connection.execute(
            "UPDATE jobs SET phase = %s, stage = %s, execution_id = COALESCE(%s, execution_id), "
            "updated_at = clock_timestamp(), status_version = status_version + 1 WHERE job_id = %s",
            (phase, stage, execution_id, job_id),
        )

    async def _received(
        self, job_id: UUID, execution_id: UUID, kind: str, payload: dict[str, object]
    ) -> bool:
        row = await (
            await self._connection.execute(
                "SELECT payload FROM stage_receipts WHERE job_id = %s AND execution_id = %s AND kind = %s",
                (job_id, execution_id, kind),
            )
        ).fetchone()
        if row is not None and row["payload"] != payload:
            raise WorkflowError("stage_conflict")
        return row is not None

    async def _save_receipt(
        self, job_id: UUID, execution_id: UUID, kind: str, payload: dict[str, object]
    ) -> None:
        await self._connection.execute(
            "INSERT INTO stage_receipts (job_id, execution_id, kind, payload) VALUES (%s, %s, %s, %s)",
            (job_id, execution_id, kind, Jsonb(payload)),
        )

    async def _open_card(
        self,
        job_id: UUID,
        step: ReviewStep,
        reason: StopReason,
        role: Role,
        deadline: datetime,
        review_round: int,
    ) -> None:
        card_id = uuid4()
        question = REVIEW_QUESTIONS[reason]
        await self._connection.execute(
            "INSERT INTO review_cards (card_id, job_id, step, reason_code, role, question, deadline, review_round) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (card_id, job_id, step, reason, role, question, deadline, review_round),
        )
        await self._outbox(
            job_id,
            "tender.human.required",
            {
                "card_id": str(card_id),
                "job_id": str(job_id),
                "step": step,
                "reason_code": reason,
                "role": role,
                "question": question,
                "deadline": deadline.isoformat(),
                "timeout": step in _ESCALATION_STEPS,
            },
        )

    async def _stage_event(
        self, job_id: UUID, stage: Stage, status: Literal["DONE", "WAITING_HUMAN", "FAILED"]
    ) -> None:
        row = await self._job_row(job_id)
        job = Job.model_validate(row["request"]["job"])
        trace_id = (
            (row["failure"] or {}).get("trace_id")
            or (row["risk_result"] or {}).get("trace_id")
            or str(job.trace_id or job_id)
        )
        await self._outbox(
            job_id,
            "tender.stage.done",
            {
                "job_id": str(job_id),
                "stage": stage,
                "status": status,
                "ts": datetime.now(UTC).isoformat(),
                "trace_id": trace_id,
            },
        )

    async def _outbox(self, job_id: UUID, topic: str, payload: dict[str, object]) -> None:
        await self._connection.execute(
            "INSERT INTO event_outbox (event_id, job_id, topic, occurred_at, payload) VALUES (%s, %s, %s, clock_timestamp(), %s)",
            (uuid4(), job_id, topic, Jsonb(payload)),
        )


def _reason(value: str) -> StopReason:
    return cast(StopReason, value.upper())


def _public_card(row: DictRow) -> ReviewCard:
    expired = row["state"] == "timed_out" or row["step"] in _ESCALATION_STEPS
    reason = _reason(row["reason_code"])
    question = row["question"] or REVIEW_QUESTIONS[reason]
    decision = None
    if row["decision_id"] is not None:
        data = {
            "decision_id": row["decision_id"],
            "job_id": row["job_id"],
            "reason_code": row["reason_code"],
            "role": row["role"],
            "deadline": row["deadline"],
            "timeout": expired,
            "decision": row["action"],
            "comment": row["comment"],
        }
        if row["decided_at"] is None:
            decision = LegacyHumanDecision.model_validate(data)
        else:
            decision = HumanDecision.model_validate(
                {
                    **data,
                    "reason_code": reason,
                    "role": row["decided_role"],
                    "question": question,
                    "decision": DECISIONS[row["action"]],
                    "decided_at": row["decided_at"],
                }
            )
    return ReviewCard.model_validate(
        {
            "card_id": row["card_id"],
            "job_id": row["job_id"],
            "step": row["step"],
            "reason_code": reason,
            "role": row["role"],
            "question": question,
            "deadline": row["deadline"],
            "state": row["state"],
            "timeout": expired,
            "decision": decision,
            "supply_confirmation": row["supply_confirmation"],
        }
    )


def migration_scripts() -> list[tuple[int, str]]:
    directory = Path(__file__).with_name("migrations")
    return [
        (int(path.stem.split("_")[0]), path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("[0-9]*_*.sql"))
    ]


async def migrate(database: Database, scripts: list[tuple[int, str]]) -> None:
    async with database.connect() as connection:
        await connection.execute(
            "SELECT pg_advisory_lock(hashtextextended('th_orchestrator:migrate', 0))"
        )
        await connection.execute("CREATE SCHEMA IF NOT EXISTS th_orchestrator")
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY)"
        )
        applied = await (
            await connection.execute("SELECT version FROM schema_migrations")
        ).fetchall()
        versions = {row["version"] for row in applied}
        for version, script in scripts:
            if version not in versions:
                async with connection.transaction():
                    await connection.execute(SQL(cast(LiteralString, script)), prepare=False)
                    await connection.execute(
                        "INSERT INTO schema_migrations VALUES (%s)", (version,)
                    )
        await AsyncPostgresSaver(connection).setup()
