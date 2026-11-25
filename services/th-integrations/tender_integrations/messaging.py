import asyncio
import logging
from uuid import UUID

from aiokafka import AIOKafkaProducer
from opentelemetry.propagate import inject
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tender_integrations.application import JOB_CREATED, OutboxRecord

_LOG = logging.getLogger(__name__)
_DEDUP_SECONDS = 7 * 24 * 60 * 60


class RedisDedup:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> UUID | None:
        try:
            value = await self._redis.get(f"job:idempotency:{key}")
            return UUID(value) if isinstance(value, str) else None
        except (RedisError, ValueError) as error:
            _LOG.warning("dedup cache read error=%s", type(error).__name__)
            return None

    async def remember(self, key: str, job_id: UUID) -> None:
        try:
            await self._redis.set(f"job:idempotency:{key}", str(job_id), ex=_DEDUP_SECONDS)
        except RedisError as error:
            _LOG.warning("dedup cache write error=%s", type(error).__name__)


class KafkaPublisher:
    def __init__(self, brokers: list[str], timeout: float) -> None:
        self._brokers = brokers
        self._timeout = timeout
        self._producer: AIOKafkaProducer | None = None

    async def publish(self, record: OutboxRecord) -> None:
        async with asyncio.timeout(self._timeout):
            producer = await self._connect()
            headers = dict(record.headers)
            inject(headers)
            await producer.send_and_wait(
                JOB_CREATED,
                record.job.model_dump_json().encode(),
                key=str(record.job.job_id).encode(),
                headers=[(name, value.encode()) for name, value in headers.items()],
            )

    async def _connect(self) -> AIOKafkaProducer:
        if self._producer is None:
            producer = AIOKafkaProducer(
                bootstrap_servers=",".join(self._brokers),
                request_timeout_ms=int(self._timeout * 1000),
                enable_idempotence=True,
                acks="all",
            )
            try:
                await producer.start()
            except Exception, asyncio.CancelledError:
                await producer.stop()
                raise
            self._producer = producer
        return self._producer

    async def close(self) -> None:
        if self._producer is not None:
            try:
                async with asyncio.timeout(self._timeout):
                    await self._producer.stop()
            except TimeoutError:
                _LOG.warning("Kafka producer close timed out")
