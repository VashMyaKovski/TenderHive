import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg.sql import SQL
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from tender_kb.domain import (
    Conflict,
    DependencyUnavailable,
    Head,
    Issue,
    Snapshot,
    Status,
    VersionNotFound,
    VersionUpload,
)

_ISSUES = TypeAdapter(list[Issue])


class Postgres:
    def __init__(self, dsn: str, timeout_seconds: int) -> None:
        self._dsn = dsn
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[AsyncConnection[DictRow]]:
        try:
            async with asyncio.timeout(self._timeout):
                connection = await AsyncConnection[DictRow].connect(
                    self._dsn,
                    row_factory=dict_row,
                    autocommit=True,
                    connect_timeout=self._timeout,
                    options=f"-c statement_timeout={self._timeout * 1000}",
                )
            async with connection:
                yield connection
        except (psycopg.Error, TimeoutError) as error:
            raise DependencyUnavailable("PostgreSQL is unavailable.") from error

    async def migrate(self) -> None:
        script = Path(__file__).with_name("migrations") / "001_knowledge.sql"
        ddl = await asyncio.to_thread(script.read_text, encoding="utf-8")
        async with self._connect() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(60290741703459)")
            # The versioned migration is bundled with the service image.
            await connection.execute(SQL(cast(LiteralString, ddl)))

    async def ready(self) -> None:
        await self.head()

    async def head(self) -> Head:
        async with self._connect() as connection:
            return await _head(connection, for_update=False)

    async def create(self, payload: VersionUpload) -> Snapshot:
        async with self._connect() as connection:
            await connection.execute(
                """
                INSERT INTO th_kb_versions (version, checksum, payload, status)
                VALUES (%s, %s, %s, 'DRAFT') ON CONFLICT (version) DO NOTHING
                """,
                (
                    payload.version,
                    payload.checksum,
                    Jsonb(payload.model_dump(mode="json", exclude_unset=True)),
                ),
            )
            snapshot = await _snapshot(connection, payload.version)
        if snapshot.payload.checksum != payload.checksum:
            raise Conflict("The version identifier already belongs to a different snapshot.")
        return snapshot

    async def get(self, version: str | None) -> Snapshot:
        async with self._connect() as connection:
            return await _snapshot(connection, version)

    async def stage(self, version: str, projection: UUID) -> Snapshot:
        async with self._connect() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE th_kb_versions SET status = 'CHECK', projection = %s
                WHERE version = %s AND status = 'DRAFT'
                """,
                (projection, version),
            )
            return await _snapshot(connection, version)

    async def reject_draft(self, version: str, report: list[Issue]) -> Snapshot:
        async with self._connect() as connection, connection.transaction():
            head = await _head(connection, for_update=True)
            current = await _snapshot(connection, version, for_update=True)
            if current.status == "DRAFT":
                await _reject(connection, current, head.version, report)
            return await _snapshot(connection, version)

    async def finish_check(
        self, version: str, baseline: Head, passed: bool, report: list[Issue]
    ) -> tuple[Snapshot, UUID | None]:
        async with self._connect() as connection, connection.transaction():
            head = await _head(connection, for_update=True)
            current = await _snapshot(connection, version, for_update=True)
            if current.status in ("ACTIVE", "REJECTED"):
                return current, None
            if head != baseline or current.status != "CHECK":
                raise Conflict("The active version changed during evaluation. Retry activation.")
            if passed:
                await _switch(connection, head, version, "ACTIVATE", report)
                obsolete = None
            else:
                await _reject(connection, current, head.version, report)
                obsolete = current.projection
            return await _snapshot(connection, version), obsolete

    async def rollback(self, version: str) -> Snapshot:
        async with self._connect() as connection, connection.transaction():
            head = await _head(connection, for_update=True)
            current = await _snapshot(connection, version, for_update=True)
            if current.status == "ACTIVE":
                return current
            if current.status != "OLD":
                raise Conflict("Rollback requires a previously active version.")
            await _switch(connection, head, version, "ROLLBACK", current.report)
            return await _snapshot(connection, version)


async def _head(connection: AsyncConnection[DictRow], *, for_update: bool) -> Head:
    query = (
        "SELECT version, revision FROM th_kb_head WHERE singleton FOR UPDATE"
        if for_update
        else "SELECT version, revision FROM th_kb_head WHERE singleton"
    )
    cursor = await connection.execute(query)
    row = await cursor.fetchone()
    if row is None:
        raise DependencyUnavailable("PostgreSQL migration is missing.")
    return Head(row["version"], row["revision"])


async def _snapshot(
    connection: AsyncConnection[DictRow], version: str | None, *, for_update: bool = False
) -> Snapshot:
    if version is None:
        cursor = await connection.execute(
            """
            SELECT v.payload, v.status, v.projection, v.report
            FROM th_kb_head h JOIN th_kb_versions v ON v.version = h.version
            WHERE h.singleton
            """
        )
    else:
        query = (
            "SELECT payload, status, projection, report FROM th_kb_versions "
            "WHERE version = %s FOR UPDATE"
            if for_update
            else "SELECT payload, status, projection, report FROM th_kb_versions WHERE version = %s"
        )
        cursor = await connection.execute(query, (version,))
    row = await cursor.fetchone()
    if row is None:
        raise VersionNotFound("Knowledge version is unavailable.")
    return Snapshot(
        VersionUpload.model_validate(row["payload"]),
        cast(Status, row["status"]),
        row["projection"],
        _ISSUES.validate_python(row["report"]),
    )


async def _reject(
    connection: AsyncConnection[DictRow],
    snapshot: Snapshot,
    previous: str | None,
    report: list[Issue],
) -> None:
    version = snapshot.payload.version
    await connection.execute(
        """
        UPDATE th_kb_versions SET status = 'REJECTED', projection = NULL, report = %s
        WHERE version = %s
        """,
        (_json_report(report), version),
    )
    await _journal(connection, version, previous, "REJECT", report)


async def _switch(
    connection: AsyncConnection[DictRow],
    head: Head,
    version: str,
    action: str,
    report: list[Issue],
) -> None:
    if head.version is not None:
        await connection.execute(
            "UPDATE th_kb_versions SET status = 'OLD' WHERE version = %s", (head.version,)
        )
    await connection.execute(
        "UPDATE th_kb_versions SET status = 'ACTIVE', report = %s WHERE version = %s",
        (_json_report(report), version),
    )
    await connection.execute(
        "UPDATE th_kb_head SET version = %s, revision = revision + 1 WHERE singleton", (version,)
    )
    await _journal(connection, version, head.version, action, report)


async def _journal(
    connection: AsyncConnection[DictRow],
    version: str,
    previous: str | None,
    action: str,
    report: list[Issue],
) -> None:
    await connection.execute(
        """
        INSERT INTO th_kb_activation_journal (version, previous_version, action, report)
        VALUES (%s, %s, %s, %s)
        """,
        (version, previous, action, _json_report(report)),
    )


def _json_report(report: list[Issue]) -> Jsonb:
    return Jsonb([item.model_dump(mode="json") for item in report])
