import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from contracts.models import Job
from tender_integrations.application import OutboxRecord, RegisteredJob
from tender_integrations.config import Settings
from tender_integrations.domain import IncomingMail


@dataclass(frozen=True, slots=True)
class StoredDocument:
    name: str
    mime: str
    content: bytes


class Postgres:
    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.database_timeout_seconds
        self._lease = settings.outbox_lease_seconds
        self._pool = AsyncConnectionPool(
            str(settings.database_dsn),
            min_size=1,
            max_size=settings.database_max_connections,
            timeout=self._timeout,
            open=False,
            kwargs={
                "connect_timeout": self._timeout,
                "options": f"-c statement_timeout={self._timeout * 1000}",
            },
        )

    async def open(self) -> None:
        await self._pool.open(wait=True, timeout=self._timeout)

    async def close(self) -> None:
        await self._pool.close(timeout=self._timeout)

    async def migrate(self) -> None:
        path = Path(__file__).with_name("migrations") / "001_mail.sql"
        sql = await asyncio.to_thread(path.read_bytes)
        async with self._pool.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('tender_integrations:migrate'))"
            )
            await connection.execute(sql, prepare=False)

    async def ready(self) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT to_regclass('tender_integrations.outbox') IS NOT NULL"
            )
            row = await cursor.fetchone()
            return bool(row and row[0])

    async def find(self, job_id: UUID, key: str) -> RegisteredJob | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT job.job_id, outbox.published_at IS NOT NULL
                FROM tender_integrations.mail_job AS job
                JOIN tender_integrations.outbox AS outbox USING (job_id)
                WHERE job.job_id = %s AND job.idempotency_key = %s
                """,
                (job_id, key),
            )
            row = await cursor.fetchone()
            return RegisteredJob(*row) if row is not None else None

    async def register(
        self, job: Job, mail: IncomingMail, headers: dict[str, str]
    ) -> RegisteredJob:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO tender_integrations.mail_job
                    (job_id, idempotency_key, payload, sent_at, sender, subject)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING job_id
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    Jsonb(job.model_dump(mode="json")),
                    mail.sent_at,
                    mail.sender,
                    mail.subject,
                ),
            )
            if await cursor.fetchone() is None:
                # A new READ COMMITTED statement sees the winning concurrent transaction.
                cursor = await connection.execute(
                    """
                    SELECT job.job_id, outbox.published_at IS NOT NULL
                    FROM tender_integrations.mail_job AS job
                    JOIN tender_integrations.outbox AS outbox USING (job_id)
                    WHERE job.idempotency_key = %s
                    """,
                    (job.idempotency_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("registered mail job has no outbox row")
                return RegisteredJob(*row)

            async with connection.cursor() as document_cursor:
                await document_cursor.executemany(
                    """
                    INSERT INTO tender_integrations.document (file_id, job_id, name, mime, content)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            document.file_id,
                            job.job_id,
                            attachment.name,
                            attachment.mime,
                            attachment.content,
                        )
                        for document, attachment in zip(
                            job.documents, mail.attachments, strict=True
                        )
                    ],
                )
            await connection.execute(
                "INSERT INTO tender_integrations.outbox (job_id, headers) VALUES (%s, %s)",
                (job.job_id, Jsonb(headers)),
            )
            return RegisteredJob(job.job_id, False)

    async def claim(self) -> OutboxRecord | None:
        token = uuid4()
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                WITH pending AS (
                    SELECT job_id
                    FROM tender_integrations.outbox
                    WHERE published_at IS NULL
                        AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                    ORDER BY queued_at, job_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE tender_integrations.outbox AS outbox
                SET lease_token = %s, lease_until = clock_timestamp() + make_interval(secs => %s)
                FROM pending JOIN tender_integrations.mail_job AS job USING (job_id)
                WHERE outbox.job_id = pending.job_id
                RETURNING job.payload, outbox.headers
                """,
                (token, self._lease),
            )
            row = await cursor.fetchone()
            return OutboxRecord(Job.model_validate(row[0]), token, row[1]) if row else None

    async def confirm(self, record: OutboxRecord) -> bool:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE tender_integrations.outbox
                SET published_at = clock_timestamp(), lease_token = NULL, lease_until = NULL
                WHERE job_id = %s AND lease_token = %s AND published_at IS NULL
                """,
                (record.job.job_id, record.token),
            )
            return cursor.rowcount == 1

    async def lookup(self, external_ref: str) -> list[RegisteredJob]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT job.job_id, outbox.published_at IS NOT NULL
                FROM tender_integrations.mail_job AS job
                JOIN tender_integrations.outbox AS outbox USING (job_id)
                WHERE job.payload->>'external_ref' = %s
                ORDER BY outbox.queued_at, job.job_id
                """,
                (external_ref,),
            )
            return [RegisteredJob(*row) for row in await cursor.fetchall()]

    async def document(self, file_id: UUID) -> StoredDocument | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT name, mime, content FROM tender_integrations.document WHERE file_id = %s",
                (file_id,),
            )
            row = await cursor.fetchone()
            return StoredDocument(*row) if row else None
