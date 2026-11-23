import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

_LOG = logging.getLogger("uvicorn.error.modelgw")


class RedisCache:
    def __init__(self, client: Redis, ttl_seconds: int, timeout_seconds: float) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds

    async def get(self, key: str) -> str | None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                value = await self._client.get(key)
        except RedisError, TimeoutError, UnicodeError:
            _LOG.warning("model_cache operation=read outcome=unavailable")
            return None
        return value if isinstance(value, str) else None

    async def set(self, key: str, value: str) -> None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._client.set(key, value, ex=self._ttl_seconds)
        except RedisError, TimeoutError:
            _LOG.warning("model_cache operation=write outcome=unavailable")
