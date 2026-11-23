import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, errors
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL
from psycopg.types.json import Jsonb

from contracts.models import HumanDecision
from contracts.risk_task_v1 import HumanReason, RiskResultV1
from tender_orchestrator.config import Settings
from tender_orchestrator.models import (
    ESCALATION_STEPS,
    JOB_STATUS,
    LEAD_ROLE,
    AssessmentFailure,
    DecisionRequest,
    JobView,
    Phase,
    ReviewCard,
    Reviewer,
    ReviewStep,
    Role,
    StartJob,
    WorkflowError,
)
from tender_orchestrator.ports import JobRecord, RecordedDecision

_SCHEMA_VERSION = 2


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

    async def register(self, request: StartJob) -> None:
        payload = request.model_dump(mode="json")
        cursor = await self._connection.execute(
            "INSERT INTO jobs (job_id, idempotency_key, request) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (request.job.job_id, request.job.idempotency_key, Jsonb(payload)),
        )
        if cursor.rowcount == 0:
            existing = await self._find_job(request.job.job_id)
            if existing is None or existing["request"] != payload:
                raise WorkflowError("job_conflict")

    async def _find_job(self, job_id: UUID) -> DictRow | None:
        return await (
            await self._connection.execute(
                "SELECT * FROM jobs WHERE job_id = %s",
                (job_id,),
            )
        ).fetchone()

    async def job(self, job_id: UUID) -> JobRecord:
        row = await self._find_job(job_id)
        if row is None:
            raise WorkflowError("job_not_found")
        return JobRecord(
            request=StartJob.model_validate(row["request"]),
            needs_advance=row["needs_advance"],
            risk_attempts=row["risk_attempts"],
            risk_result=RiskResultV1.model_validate(row["risk_result"])
            if row["risk_result"] is not None
            else None,
            failure=AssessmentFailure.model_validate(row["failure"])
            if row["failure"] is not None
            else None,
        )

    async def view(self, job_id: UUID) -> JobView:
        row = await (
            await self._connection.execute(
                """
            SELECT jobs.*, COALESCE(
                (SELECT jsonb_agg(to_jsonb(review_cards) ORDER BY step DESC) FROM review_cards
                 WHERE review_cards.job_id = jobs.job_id), '[]'::jsonb
            ) AS cards FROM jobs WHERE job_id = %s
            """,
                (job_id,),
            )
        ).fetchone()
        if row is None:
            raise WorkflowError("job_not_found")
        phase: Phase = row["phase"]
        return JobView.model_validate(
            {
                "job": {**row["request"]["job"], "status": JOB_STATUS[phase]},
                "phase": phase,
                "risk_attempts": row["risk_attempts"],
                "risk_result": row["risk_result"],
                "failure": row["failure"],
                "cards": [_public_card(card) for card in row["cards"]],
            }
        )

    async def reserve_attempt(self, job_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE jobs SET risk_attempts = risk_attempts + 1, phase = 'assessing', failure = NULL WHERE job_id = %s",
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
        *,
        step: ReviewStep = "risk_review",
    ) -> None:
        async with self._connection.transaction():
            await self._connection.execute(
                """
                INSERT INTO review_cards (card_id, job_id, step, reason_code, role, deadline)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (job_id, step) DO NOTHING
                """,
                (uuid4(), job_id, step, reason, role, deadline),
            )
            await self.set_phase(job_id, "awaiting_review")

    async def set_phase(self, job_id: UUID, phase: Phase) -> None:
        await self._connection.execute(
            "UPDATE jobs SET phase = %s WHERE job_id = %s", (phase, job_id)
        )

    async def mark_advanced(self, job_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE jobs SET needs_advance = false WHERE job_id = %s", (job_id,)
        )

    async def card_role(self, job_id: UUID, card_id: UUID) -> Role:
        return (await self._card(job_id, card_id))["role"]

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
            cards = await (
                await self._connection.execute(
                    """
                UPDATE review_cards SET state = 'timed_out'
                WHERE job_id = %s AND state = 'open' AND deadline <= %s
                    AND step NOT LIKE %s
                RETURNING step, reason_code, deadline
                """,
                    (job_id, now, "%escalation"),
                )
            ).fetchall()
            for card in cards:
                await self._connection.execute(
                    """
                    INSERT INTO review_cards (card_id, job_id, step, reason_code, role, deadline)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, step) DO NOTHING
                    """,
                    (
                        uuid4(),
                        job_id,
                        ESCALATION_STEPS[card["step"]],
                        card["reason_code"],
                        LEAD_ROLE,
                        card["deadline"],
                    ),
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
        if card["state"] != "open":
            if (
                card["decision_id"] == request.decision_id
                and card["actor_id"] == actor.actor_id
                and card["action"] == request.action
                and card["comment"] == request.comment
            ):
                return
            raise WorkflowError("decision_conflict")
        try:
            async with self._connection.transaction():
                cursor = await self._connection.execute(
                    """
                    UPDATE review_cards SET state = 'decided', decision_id = %s, actor_id = %s,
                        action = %s, comment = %s, decided_at = %s, applied = false
                    WHERE job_id = %s AND card_id = %s AND state = 'open'
                        AND (step LIKE '%escalation' OR deadline > %s)
                    """,
                    (
                        request.decision_id,
                        actor.actor_id,
                        request.action,
                        request.comment,
                        now,
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
                SELECT decision_id, action FROM review_cards
                WHERE job_id = %s AND state = 'decided' AND NOT applied
                ORDER BY decided_at, card_id LIMIT 1
                """,
                (job_id,),
            )
        ).fetchone()
        return RecordedDecision(row["decision_id"], row["action"]) if row is not None else None

    async def decision_by_id(self, job_id: UUID, decision_id: UUID) -> RecordedDecision | None:
        row = await (
            await self._connection.execute(
                """
                SELECT decision_id, action FROM review_cards
                WHERE job_id = %s AND decision_id = %s AND state = 'decided'
                """,
                (job_id, decision_id),
            )
        ).fetchone()
        return RecordedDecision(row["decision_id"], row["action"]) if row is not None else None

    async def mark_decision_applied(self, decision_id: UUID) -> None:
        await self._connection.execute(
            "UPDATE review_cards SET applied = true WHERE decision_id = %s", (decision_id,)
        )

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
            SELECT job_id FROM review_cards WHERE state = 'open'
                AND step NOT LIKE %s AND deadline <= %s ORDER BY deadline, job_id LIMIT %s
            """,
                ("%escalation", now, limit),
            )
        ).fetchall()
        return [row["job_id"] for row in rows]


def _public_card(row: DictRow) -> ReviewCard:
    expired = row["state"] == "timed_out" or str(row["step"]).endswith("escalation")
    decision = None
    if row["decision_id"] is not None:
        decision = HumanDecision.model_validate(
            {
                "decision_id": row["decision_id"],
                "job_id": row["job_id"],
                "reason_code": row["reason_code"],
                "role": row["role"],
                "deadline": row["deadline"],
                "timeout": expired,
                "decision": row["action"],
                "comment": row["comment"],
            }
        )
    return ReviewCard.model_validate(
        {
            "card_id": row["card_id"],
            "job_id": row["job_id"],
            "step": row["step"],
            "reason_code": row["reason_code"],
            "role": row["role"],
            "deadline": row["deadline"],
            "state": row["state"],
            "timeout": expired,
            "decision": decision,
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
