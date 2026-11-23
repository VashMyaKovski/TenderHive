from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider

from contracts.models import Job
from tender_integrations.application import (
    IntakePolicy,
    MailIntake,
    OutboxRecord,
    OutboxRelay,
    RegisteredJob,
)
from tender_integrations.domain import IncomingMail
from tests.test_mail import message_bytes


class MailboxFixture:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.messages = {"1": message_bytes((b"%PDF-1.4 fixture", "application", "pdf", "bid.pdf"))}

    @asynccontextmanager
    async def open(self) -> AsyncIterator[MailboxFixture]:
        yield self

    async def unread(self) -> AsyncIterator[tuple[str, bytes]]:
        for source_id, raw in list(self.messages.items()):
            yield source_id, raw

    async def mark_processed(self, source_id: str) -> None:
        self.events.append(f"marked:{source_id}")
        self.messages.pop(source_id)


class StoreFixture:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.record: OutboxRecord | None = None
        self.published = False

    async def find(self, job_id: UUID, key: str) -> RegisteredJob | None:
        if (
            self.record
            and self.record.job.job_id == job_id
            and self.record.job.idempotency_key == key
        ):
            return RegisteredJob(job_id, self.published)
        return None

    async def register(
        self, job: Job, mail: IncomingMail, headers: dict[str, str]
    ) -> RegisteredJob:
        if self.record is None:
            self.events.append("registered")
            self.record = OutboxRecord(job, uuid4(), headers)
        return RegisteredJob(self.record.job.job_id, self.published)

    async def claim(self) -> OutboxRecord | None:
        return self.record if not self.published else None

    async def confirm(self, record: OutboxRecord) -> bool:
        self.events.append("confirmed")
        assert self.record == record
        self.published = True
        return True


class MemoryCache:
    def __init__(self) -> None:
        self.keys: dict[str, UUID] = {}

    async def get(self, key: str) -> UUID | None:
        return self.keys.get(key)

    async def remember(self, key: str, job_id: UUID) -> None:
        self.keys[key] = job_id


class PublisherFixture:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failed = True

    async def publish(self, record: OutboxRecord) -> None:
        if self.failed:
            raise TimeoutError
        self.events.append("delivered")


async def test_ack_after_kafka_and_durable_dedup_without_redis() -> None:
    events: list[str] = []
    source = MailboxFixture(events)
    duplicate = source.messages["1"]
    store = StoreFixture(events)
    cache = MemoryCache()
    broker = PublisherFixture(events)
    tracer = TracerProvider().get_tracer(__name__)
    intake = MailIntake(
        source,
        store,
        cache,
        IntakePolicy("demo", "kb-demo", "demo-profile", "http://integration", 1024),
        tracer,
    )
    relay = OutboxRelay(store, broker, 5, tracer)

    await intake.poll()
    assert events == ["registered"]
    assert store.record is not None
    assert store.record.job.source == "mail"
    assert store.record.job.external_ref == "<fixture-01@example.test>"
    assert store.record.job.trace_id is not None
    assert store.record.headers["traceparent"].split("-")[1] == store.record.job.trace_id.hex
    with pytest.raises(TimeoutError):
        await relay.flush()
    await intake.poll()
    assert list(source.messages) == ["1"]

    broker.failed = False
    await relay.flush()
    await intake.poll()
    assert events == ["registered", "delivered", "confirmed", "marked:1"]
    assert cache.keys == {store.record.job.idempotency_key: store.record.job.job_id}

    cache.keys.clear()
    source.messages["2"] = duplicate
    await intake.poll()
    await relay.flush()
    assert events == ["registered", "delivered", "confirmed", "marked:1", "marked:2"]
