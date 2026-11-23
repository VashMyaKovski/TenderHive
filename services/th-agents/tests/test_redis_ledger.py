from os import getenv
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from tender_agents.ledger import RedisRiskLedger


async def test_reservations_are_shared_and_only_owner_can_record() -> None:
    url = getenv("TH_AGENTS_TEST_REDIS_URL")
    if not url:
        pytest.skip("set TH_AGENTS_TEST_REDIS_URL to an isolated Redis instance")

    task, owner, other = uuid4(), uuid4(), uuid4()
    async with (
        Redis.from_url(url, decode_responses=True) as redis_one,
        Redis.from_url(url, decode_responses=True) as redis_two,
    ):
        first = RedisRiskLedger(redis_one, 30, 10)
        second = RedisRiskLedger(redis_two, 30, 10)
        assert await first.ready()
        assert await first.claim(task, "first", owner) == ("owned", "")
        assert await second.claim(task, "first", other) == ("busy", "")
        assert await second.claim(task, "different", other) == ("conflict", "")
        assert not await second.finish(task, other, "unowned")
        assert await first.finish(task, owner, "")
        assert await second.claim(task, "first", other) == ("owned", "")
        assert await second.finish(task, other, "checked")
        assert await first.claim(task, "first", owner) == ("recorded", "checked")
