from typing import Literal, Protocol, cast
from uuid import UUID

from redis.asyncio import Redis

from contracts.agent_task import AgentCompleted

type ClaimStatus = Literal["owned", "busy", "conflict", "recorded"]
type Claim = tuple[ClaimStatus, str]
type CompletionLease = tuple[str, int]


class RiskLedger(Protocol):
    async def claim(self, task_id: UUID, fingerprint: str, owner: UUID) -> Claim: ...

    async def finish(
        self, task_id: UUID, owner: UUID, outcome: str, completion: AgentCompleted | None
    ) -> bool: ...

    async def ready(self) -> bool: ...


_OUTBOX = "th-agents:completion-outbox:v1"

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
if ARGV[3] ~= '' then
    redis.call('ZADD', KEYS[3], 0, ARGV[3])
    redis.call('PERSIST', KEYS[1])
else
    redis.call('EXPIRE', KEYS[1], ARGV[4])
end
redis.call('DEL', KEYS[2])
return 1
"""

_CLAIM_COMPLETION = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local items = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, 1)
if #items == 0 then
    return nil
end
local deadline = now + tonumber(ARGV[1])
redis.call('ZADD', KEYS[1], deadline, items[1])
return {items[1], deadline}
"""

_ACK_COMPLETION = """
local deadline = redis.call('ZSCORE', KEYS[1], ARGV[1])
if deadline and tonumber(deadline) == tonumber(ARGV[2]) then
    redis.call('ZREM', KEYS[1], ARGV[1])
    redis.call('EXPIRE', KEYS[2], ARGV[3])
    return 1
end
return 0
"""


class RedisRiskLedger:
    def __init__(self, redis: Redis, ttl_seconds: int, lease_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._lease = lease_seconds

    @staticmethod
    def _keys(task_id: UUID) -> tuple[str, str]:
        # Use the original key so legacy risk requests retain their idempotency history.
        prefix = f"th-agents:risk:{{{task_id}}}"
        return prefix, f"{prefix}:lease"

    async def claim(self, task_id: UUID, fingerprint: str, owner: UUID) -> Claim:
        result = await self._redis.execute_command(
            "EVAL", _CLAIM, 2, *self._keys(task_id), fingerprint, str(owner), self._lease, self._ttl
        )
        status, outcome = cast(tuple[ClaimStatus, str], result)
        return status, outcome

    async def finish(
        self, task_id: UUID, owner: UUID, outcome: str, completion: AgentCompleted | None
    ) -> bool:
        result = await self._redis.execute_command(
            "EVAL",
            _FINISH,
            3,
            *self._keys(task_id),
            _OUTBOX,
            str(owner),
            outcome,
            completion.model_dump_json() if completion is not None else "",
            self._ttl,
        )
        return bool(result)

    async def ready(self) -> bool:
        return bool(await self._redis.execute_command("PING"))

    async def claim_completion(self, lease_ms: int) -> CompletionLease | None:
        result = await self._redis.execute_command("EVAL", _CLAIM_COMPLETION, 1, _OUTBOX, lease_ms)
        return cast(CompletionLease | None, result)

    async def acknowledge_completion(self, lease: CompletionLease, task_id: UUID) -> bool:
        result = await self._redis.execute_command(
            "EVAL", _ACK_COMPLETION, 2, _OUTBOX, self._keys(task_id)[0], *lease, self._ttl
        )
        return bool(result)
