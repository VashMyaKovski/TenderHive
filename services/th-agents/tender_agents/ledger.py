from typing import Literal, Protocol, cast
from uuid import UUID

from redis.asyncio import Redis

type ClaimStatus = Literal["owned", "busy", "conflict", "recorded"]
type Claim = tuple[ClaimStatus, str]


class RiskLedger(Protocol):
    async def claim(self, task_id: UUID, fingerprint: str, owner: UUID) -> Claim: ...

    async def finish(self, task_id: UUID, owner: UUID, outcome: str) -> bool: ...

    async def ready(self) -> bool: ...


_CLAIM = """
local fingerprint = redis.call('HGET', KEYS[1], 'fingerprint')
if fingerprint and fingerprint ~= ARGV[1] then
    return {'conflict', ''}
end
local outcome = redis.call('HGET', KEYS[1], 'outcome')
if outcome then
    return {'recorded', outcome}
end
if not redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3], 'NX') then
    return {'busy', ''}
end
redis.call('HSET', KEYS[1], 'fingerprint', ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return {'owned', ''}
"""

_FINISH = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then
    return 0
end
if ARGV[2] ~= '' then
    redis.call('HSET', KEYS[1], 'outcome', ARGV[2])
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('DEL', KEYS[2])
return 1
"""


class RedisRiskLedger:
    def __init__(self, redis: Redis, ttl_seconds: int, lease_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._lease = lease_seconds

    @staticmethod
    def _keys(task_id: UUID) -> tuple[str, str]:
        prefix = f"th-agents:risk:{{{task_id}}}"
        return prefix, f"{prefix}:lease"

    async def claim(self, task_id: UUID, fingerprint: str, owner: UUID) -> Claim:
        result = await self._redis.execute_command(
            "EVAL", _CLAIM, 2, *self._keys(task_id), fingerprint, str(owner), self._lease, self._ttl
        )
        status, outcome = cast(tuple[ClaimStatus, str], result)
        return status, outcome

    async def finish(self, task_id: UUID, owner: UUID, outcome: str) -> bool:
        result = await self._redis.execute_command(
            "EVAL", _FINISH, 2, *self._keys(task_id), str(owner), outcome, self._ttl
        )
        return bool(result)

    async def ready(self) -> bool:
        return bool(await self._redis.execute_command("PING"))
