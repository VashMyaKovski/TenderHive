import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import LiteralString, cast
from uuid import UUID

import psycopg
from contracts.models import SolverResult
from psycopg.sql import SQL


class TraceStore:
    def __init__(self, dsn: str, timeout: int) -> None:
        self._dsn = dsn
        self._timeout = timeout

    @asynccontextmanager
    async def _connection(self):
        async with (
            asyncio.timeout(self._timeout),
            await psycopg.AsyncConnection.connect(self._dsn, connect_timeout=self._timeout) as conn,
        ):
            yield conn

    async def migrate(self) -> None:
        script = Path(__file__).with_name("migrations") / "001_trace.sql"
        sql = await asyncio.to_thread(script.read_text, encoding="utf-8")
        async with self._connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(447209143)")
            await conn.execute(SQL(cast(LiteralString, sql)))

    async def ready(self) -> None:
        async with self._connection() as conn:
            await conn.execute("SELECT trace_id FROM th_solver.traces LIMIT 0")

    async def record(self, trace_id: UUID, result: SolverResult, cached: bool) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                INSERT INTO th_solver.traces
                    (job_id, trace_id, kb_version, status, stop_reasons, cached)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    result.job_id,
                    trace_id,
                    result.kb_version,
                    result.status,
                    result.stop_reasons,
                    cached,
                ),
            )
