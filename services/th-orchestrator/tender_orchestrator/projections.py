import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from pydantic import TypeAdapter
from redis.asyncio import Redis

from contracts.models import Job
from tender_orchestrator.application import Orchestrator
from tender_orchestrator.config import Settings
from tender_orchestrator.db import Database
from tender_orchestrator.models import (
    DecisionRequest,
    IncomingDecision,
    OutboxEvent,
    StartJob,
    WorkflowError,
)

_LOG = logging.getLogger("uvicorn.error.orchestrator")
_CREATED = "tender.job.created"
_DECIDED = "tender.human.decided"
_START = TypeAdapter(Job | StartJob)


class Projections:
    def __init__(self, database: Database, service: Orchestrator, settings: Settings) -> None:
        self._database = database
        self._service = service
        self._settings = settings

    async def cache(self) -> None:
        settings = self._settings
        async with Redis.from_url(
            str(settings.redis_url),
            socket_timeout=settings.io_timeout_seconds,
            socket_connect_timeout=settings.io_timeout_seconds,
        ) as redis:
            while True:
                async with self._database.session() as store:
                    if await store.try_worker_lock("cache"):
                        for position in await store.cache_batch(settings.poll_batch_size):
                            async with redis.pipeline(transaction=True) as pipeline:
                                pipeline.set(
                                    f"job:status:{position.job_id}",
                                    position.status,
                                    ex=settings.checkpoint_ttl_seconds,
                                )
                                pipeline.set(
                                    f"checkpoint:{position.job_id}",
                                    position.model_dump_json(),
                                    ex=settings.checkpoint_ttl_seconds,
                                )
                                await pipeline.execute()
                            await store.mark_cached(position)
                await asyncio.sleep(settings.poll_interval_seconds)

    async def publish(self) -> None:
        settings = self._settings
        async with _connected(
            AIOKafkaProducer(
                bootstrap_servers=",".join(settings.kafka_brokers),
                acks="all",
                request_timeout_ms=int(settings.io_timeout_seconds * 1000),
            )
        ) as producer:
            while True:
                async with self._database.session() as store:
                    if await store.try_worker_lock("outbox"):
                        for event in await store.outbox_batch(settings.poll_batch_size):
                            async with asyncio.timeout(settings.io_timeout_seconds):
                                await producer.send_and_wait(
                                    event.event_type,
                                    event.model_dump_json().encode(),
                                    key=str(event.job_id).encode(),
                                )
                            await store.mark_delivered(event.event_id)
                await asyncio.sleep(settings.poll_interval_seconds)

    async def consume(self) -> None:
        settings = self._settings
        async with _connected(
            AIOKafkaConsumer(
                _CREATED,
                _DECIDED,
                bootstrap_servers=",".join(settings.kafka_brokers),
                group_id=settings.kafka_group_id,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                request_timeout_ms=int(settings.io_timeout_seconds * 1000),
            )
        ) as consumer:
            async for message in consumer:
                try:
                    await self._receive(message.topic, message.key, message.value)
                except (ValueError, WorkflowError) as error:
                    reason = error.code if isinstance(error, WorkflowError) else "invalid_event"
                    async with self._database.session() as store:
                        await store.reject_event(
                            message.topic, message.partition, message.offset, reason
                        )
                    _LOG.warning(
                        "rejected event topic=%s partition=%s offset=%s reason=%s",
                        message.topic,
                        message.partition,
                        message.offset,
                        reason,
                    )
                await consumer.commit(
                    {TopicPartition(message.topic, message.partition): message.offset + 1}
                )

    async def _receive(self, topic: str, key: bytes | None, value: bytes | None) -> None:
        if key is None or value is None:
            raise ValueError("event requires a key and payload")
        job_id = UUID(key.decode())
        payload = json.loads(value)
        if isinstance(payload, dict) and "event_type" in payload:
            envelope = OutboxEvent.model_validate(payload)
            if envelope.event_type != topic or envelope.job_id != job_id:
                raise ValueError("misrouted event")
            payload = envelope.payload
        if topic == _CREATED:
            request = _START.validate_python(payload)
            incoming_id = request.job.job_id if isinstance(request, StartJob) else request.job_id
            if incoming_id != job_id:
                raise ValueError("job ID does not match the message key")
            await self._service.enqueue(request)
        else:
            event = IncomingDecision.model_validate(payload)
            if event.decision.job_id != job_id:
                raise ValueError("job ID does not match the message key")
            reviewer = next(
                (item for item in self._settings.reviewers if item.actor_id == event.actor_id), None
            )
            if reviewer is None:
                raise WorkflowError("access_denied")
            await self._service.decide(
                job_id,
                event.card_id,
                reviewer,
                DecisionRequest(
                    decision_id=event.decision.decision_id,
                    decision=event.decision.decision,
                    comment=event.decision.comment,
                ),
                source=event.decision,
            )


@asynccontextmanager
async def _connected[Client: (AIOKafkaConsumer, AIOKafkaProducer)](client: Client):
    try:
        await client.start()
        yield client
    finally:
        await client.stop()


async def run_projection(operation: Callable[[], Awaitable[None]], interval: float) -> None:
    while True:
        try:
            await operation()
        except Exception as error:
            _LOG.warning(
                "projection operation=%s error=%s", operation.__name__, type(error).__name__
            )
        await asyncio.sleep(interval)
