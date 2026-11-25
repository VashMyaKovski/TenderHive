import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from psycopg import AsyncConnection

from modelgw.context import Replacement
from modelgw.errors import Failure, GatewayError
from modelgw.journal import Invocation, InvocationResult
from modelgw.postgres import PostgresJournal


async def test_journal_requires_a_cipher_to_persist_replacements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_connection(_: PostgresJournal) -> None:
        pytest.fail("replacement data must be rejected before a database connection is opened")

    monkeypatch.setattr(PostgresJournal, "_connection", unexpected_connection)
    journal = PostgresJournal("postgresql://127.0.0.1:1/unused", 2, None)
    invocation = Invocation(uuid4(), uuid4(), "vision", "v1", None, None, None)
    with pytest.raises(GatewayError) as error:
        await journal.begin(invocation, (Replacement(1, "redacted", "private value"),))
    assert error.value.code == Failure.STORAGE_UNAVAILABLE


def test_migration_journal_and_encrypted_replacements() -> None:
    dsn = os.getenv("MODEL_GW_TEST_POSTGRES_DSN")
    if dsn is None:
        pytest.skip("requires a disposable PostgreSQL instance")

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(_verify_journal(dsn), loop_factory=loop_factory)


async def _verify_journal(dsn: str) -> None:
    cipher = Fernet(Fernet.generate_key())
    journal = PostgresJournal(dsn, 5, cipher)
    await asyncio.gather(journal.migrate(), journal.migrate())
    await journal.ready("v1")
    await journal.ready("v3")

    job_id, trace_id = uuid4(), uuid4()
    invocation = Invocation(
        job_id, trace_id, "risk_assessment", "v1", "rules-v2", "test/chat", "test-hash"
    )
    private_text = "customer@example.invalid"
    replacement = Replacement(1, "[REDACTED_PERSONAL]", private_text)
    first, second = await asyncio.gather(
        journal.begin(invocation, (replacement,)),
        journal.begin(invocation, (replacement,)),
    )
    assert first != second
    await journal.finish(first, InvocationResult("ok", 12, "test/chat-1", True, 0.7, None))
    await journal.finish(second, InvocationResult("provider_timeout", 24, None, False, None, None))

    async with await AsyncConnection.connect(dsn) as connection:
        cursor = await connection.execute(
            """
            SELECT id, trace_id, rules_version, outcome, cached, encrypted_replacements
            FROM modelgw.invocations WHERE job_id = %s
            """,
            (job_id,),
        )
        rows = {row[0]: row[1:] for row in await cursor.fetchall()}

    assert rows[first][:-1] == (trace_id, "rules-v2", "ok", True)
    assert rows[second][:-1] == (trace_id, "rules-v2", "provider_timeout", False)
    assert rows[first][-1] != rows[second][-1]
    for row in rows.values():
        assert private_text.encode() not in row[-1]
        assert json.loads(cipher.decrypt(row[-1])) == [
            {"fragment": 1, "token": "[REDACTED_PERSONAL]", "value": private_text}
        ]
