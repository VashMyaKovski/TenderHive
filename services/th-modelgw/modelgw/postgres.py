import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from cryptography.fernet import Fernet
from psycopg import AsyncConnection, Error
from psycopg.rows import TupleRow

from modelgw.context import Replacement
from modelgw.errors import Failure, GatewayError
from modelgw.journal import Invocation, InvocationResult
from modelgw.prompts import PROMPTS

_SCHEMA = (
    "CREATE SCHEMA IF NOT EXISTS modelgw",
    """
    CREATE TABLE IF NOT EXISTS modelgw.prompt_versions (
        version text PRIMARY KEY,
        templates jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS modelgw.invocations (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        job_id uuid NOT NULL,
        trace_id uuid NOT NULL,
        task text NOT NULL,
        prompt_version text NOT NULL,
        rules_version text,
        model text,
        request_hash text,
        started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at timestamptz,
        outcome text NOT NULL DEFAULT 'started',
        duration_ms integer,
        model_version text,
        cached boolean,
        confidence double precision,
        human_reason text,
        encrypted_replacements bytea
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS modelgw_invocations_job_idx
        ON modelgw.invocations (job_id, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS modelgw_invocations_trace_idx
        ON modelgw.invocations (trace_id)
    """,
)


class PostgresJournal:
    def __init__(self, dsn: str, timeout_seconds: int, cipher: Fernet | None) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds
        self._cipher = cipher

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection[TupleRow]]:
        try:
            async with (
                asyncio.timeout(self._timeout_seconds),
                await AsyncConnection.connect(
                    self._dsn, connect_timeout=self._timeout_seconds
                ) as connection,
            ):
                yield connection
        except Error, TimeoutError:
            raise GatewayError(Failure.STORAGE_UNAVAILABLE) from None

    async def migrate(self) -> None:
        async with self._connection() as connection:
            # Serialize migration and seeding when multiple app instances initialize together.
            await connection.execute("SELECT pg_advisory_xact_lock(748203131)")
            for statement in _SCHEMA:
                await connection.execute(statement)
            for version, templates in PROMPTS.items():
                await connection.execute(
                    """
                    INSERT INTO modelgw.prompt_versions (version, templates)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version, json.dumps(dict(templates))),
                )
                await self._verify_templates(connection, version)

    async def ready(self, version: str) -> None:
        async with self._connection() as connection:
            await self._verify_templates(connection, version)
            await connection.execute("SELECT id FROM modelgw.invocations LIMIT 0")

    @staticmethod
    async def _verify_templates(connection: AsyncConnection[TupleRow], version: str) -> None:
        cursor = await connection.execute(
            "SELECT templates FROM modelgw.prompt_versions WHERE version = %s", (version,)
        )
        row = await cursor.fetchone()
        if row is None or row[0] != dict(PROMPTS[version]):
            raise GatewayError(Failure.STORAGE_UNAVAILABLE)

    async def begin(self, invocation: Invocation, replacements: tuple[Replacement, ...]) -> int:
        encrypted = None
        if replacements:
            cipher = self._cipher
            if cipher is None:
                raise GatewayError(Failure.STORAGE_UNAVAILABLE)
            encrypted = cipher.encrypt(json.dumps([asdict(item) for item in replacements]).encode())
        async with self._connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO modelgw.invocations
                    (job_id, trace_id, task, prompt_version, rules_version,
                     model, request_hash, encrypted_replacements)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    invocation.job_id,
                    invocation.trace_id,
                    invocation.task,
                    invocation.prompt_version,
                    invocation.rules_version,
                    invocation.model,
                    invocation.request_hash,
                    encrypted,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise GatewayError(Failure.STORAGE_UNAVAILABLE)
            return row[0]

    async def finish(self, attempt: int, result: InvocationResult) -> None:
        async with self._connection() as connection:
            await connection.execute(
                """
                UPDATE modelgw.invocations SET
                    finished_at = CURRENT_TIMESTAMP, outcome = %s, duration_ms = %s,
                    model_version = %s, cached = %s, confidence = %s, human_reason = %s
                WHERE id = %s
                """,
                (
                    result.outcome,
                    result.duration_ms,
                    result.model_version,
                    result.cached,
                    result.confidence,
                    result.human_reason,
                    attempt,
                ),
            )
