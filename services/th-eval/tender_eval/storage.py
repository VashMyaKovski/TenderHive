import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL
from psycopg.types.json import Jsonb

from tender_eval.cases import Dataset, DatasetError, EvalCase
from tender_eval.run_models import RunRequest, RunResult
from tender_eval.runner import Observation

SCHEMA_VERSION = 1


class RunInProgress(Exception):
    """The run ID is already locked by another request."""


class RunStore:
    def __init__(self, connection: AsyncConnection[DictRow]) -> None:
        self._connection = connection

    async def get(self, run_id: UUID) -> tuple[RunRequest, RunResult] | None:
        cursor = await self._connection.execute(
            "SELECT request, result FROM th_eval_runs WHERE run_id = %s", (run_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return RunRequest.model_validate(row["request"]), RunResult.model_validate(row["result"])

    async def dataset(self, suite: str, kb_version: str) -> Dataset | None:
        cursor = await self._connection.execute(
            """SELECT d.digest, c.definition
               FROM th_eval_datasets AS d JOIN th_eval_cases AS c USING (suite, kb_version)
               WHERE d.suite = %s AND d.kb_version = %s ORDER BY c.position""",
            (suite, kb_version),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        return Dataset(
            suite,
            rows[0]["digest"],
            tuple(EvalCase.model_validate(row["definition"]) for row in rows),
        )

    async def save(
        self, request: RunRequest, result: RunResult, observations: tuple[Observation, ...]
    ) -> None:
        await self._connection.execute(
            """INSERT INTO th_eval_runs (run_id, suite, kb_version, request, result, observations)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                request.run_id,
                request.suite,
                request.kb_version,
                Jsonb(request.model_dump(mode="json")),
                Jsonb(result.model_dump(mode="json")),
                Jsonb([item.model_dump(mode="json") for item in observations]),
            ),
        )


class Store:
    def __init__(self, postgres_dsn: str, timeout_seconds: int) -> None:
        self._postgres_dsn = postgres_dsn
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        async with asyncio.timeout(self._timeout_seconds):
            connection = await AsyncConnection[DictRow].connect(
                self._postgres_dsn,
                connect_timeout=self._timeout_seconds,
                autocommit=True,
                row_factory=dict_row,
                options=f"-c statement_timeout={self._timeout_seconds * 1000}",
            )
        async with connection:
            yield connection

    @asynccontextmanager
    async def read(self) -> AsyncIterator[RunStore]:
        async with self._connection() as connection:
            yield RunStore(connection)

    @asynccontextmanager
    async def claim(self, run_id: UUID) -> AsyncIterator[RunStore]:
        # Closing the session releases its lock, including after cancellation or a process crash.
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (f"th-eval:run:{run_id}",),
            )
            row = await cursor.fetchone()
            if not row or not row["acquired"]:
                raise RunInProgress
            yield RunStore(connection)

    async def ready(self) -> bool:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM th_eval_schema_migrations WHERE version = %s", (SCHEMA_VERSION,)
            )
            return await cursor.fetchone() is not None

    async def migrate(self, dataset: Dataset, kb_version: str) -> None:
        _validate_knowledge_version(dataset, kb_version)
        migration = Path(__file__).with_name("migrations") / "001_evaluations.sql"
        script = await asyncio.to_thread(migration.read_text, encoding="utf-8")
        async with self._connection() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('th-eval:migrate', 0))"
            )
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS th_eval_schema_migrations (version integer PRIMARY KEY)"
            )
            cursor = await connection.execute("SELECT version FROM th_eval_schema_migrations")
            versions = {row["version"] for row in await cursor.fetchall()}
            if not versions:
                await connection.execute(SQL(cast(LiteralString, script)), prepare=False)
                await connection.execute(
                    "INSERT INTO th_eval_schema_migrations VALUES (%s)", (SCHEMA_VERSION,)
                )
            elif versions != {SCHEMA_VERSION}:
                raise DatasetError("unsupported evaluation database schema version")
            await _seed_dataset(connection, dataset, kb_version)


def _validate_knowledge_version(dataset: Dataset, kb_version: str) -> None:
    for case in dataset.cases:
        try:
            _, knowledge, _ = case.risk_context()
        except ValueError:
            # Contract-negative fixture cases may deliberately contain invalid context.
            continue
        if knowledge.rules_version != kb_version:
            raise DatasetError("dataset knowledge version does not match TH_EVAL_KB_VERSION")


async def _seed_dataset(
    connection: AsyncConnection[DictRow], dataset: Dataset, kb_version: str
) -> None:
    cursor = await connection.execute(
        "SELECT digest FROM th_eval_datasets WHERE suite = %s AND kb_version = %s",
        (dataset.version, kb_version),
    )
    previous = await cursor.fetchone()
    if previous is not None:
        if previous["digest"] != dataset.digest:
            raise DatasetError(
                "a different dataset is already stored under this suite and KB version"
            )
        return
    await connection.execute(
        "INSERT INTO th_eval_datasets (suite, kb_version, digest) VALUES (%s, %s, %s)",
        (dataset.version, kb_version, dataset.digest),
    )
    async with connection.cursor() as cases:
        await cases.executemany(
            """INSERT INTO th_eval_cases (suite, kb_version, case_id, position, definition)
               VALUES (%s, %s, %s, %s, %s)""",
            [
                (
                    dataset.version,
                    kb_version,
                    case.case_id,
                    position,
                    Jsonb(case.model_dump(mode="json", exclude_unset=True)),
                )
                for position, case in enumerate(dataset.cases)
            ],
        )
