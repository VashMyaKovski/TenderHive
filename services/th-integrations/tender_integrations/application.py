import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Tracer

from contracts.models import Document, Job, LegacyDocument
from tender_integrations.domain import IncomingMail, MailRejected, MailUnavailable, parse_message

_LOG = logging.getLogger(__name__)
JOB_CREATED = "tender.job.created"


@dataclass(frozen=True, slots=True)
class RegisteredJob:
    job_id: UUID
    published: bool


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    job: Job
    token: UUID
    headers: dict[str, str]


class MailSession(Protocol):
    def unread(self) -> AsyncIterator[tuple[str, bytes]]: ...

    async def mark_processed(self, source_id: str) -> None: ...


class Mailbox(Protocol):
    def open(self) -> AbstractAsyncContextManager[MailSession]: ...


class MailJobs(Protocol):
    async def find(self, job_id: UUID, key: str) -> RegisteredJob | None: ...

    async def register(
        self, job: Job, mail: IncomingMail, headers: dict[str, str]
    ) -> RegisteredJob: ...

    async def claim(self) -> OutboxRecord | None: ...

    async def confirm(self, record: OutboxRecord) -> bool: ...


class DedupCache(Protocol):
    async def get(self, key: str) -> UUID | None: ...

    async def remember(self, key: str, job_id: UUID) -> None: ...


class Publisher(Protocol):
    async def publish(self, record: OutboxRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    mailbox_id: str
    kb_version: str
    profile_ref: str
    document_base_url: str
    max_attachment_bytes: int


class MailIntake:
    def __init__(
        self,
        mailbox: Mailbox,
        jobs: MailJobs,
        cache: DedupCache,
        policy: IntakePolicy,
        tracer: Tracer,
    ) -> None:
        self._mailbox = mailbox
        self._jobs = jobs
        self._cache = cache
        self._policy = policy
        self._tracer = tracer

    async def poll(self) -> None:
        async with self._mailbox.open() as session:
            async for source_id, raw in session.unread():
                with self._tracer.start_as_current_span(
                    "mail.receive", record_exception=False
                ) as span:
                    try:
                        mail = await asyncio.to_thread(
                            parse_message, source_id, raw, self._policy.max_attachment_bytes
                        )
                    except MailRejected as error:
                        _LOG.warning("mail skipped code=%s", error)
                        continue

                    trace = span.get_span_context()
                    result = await self._register(mail, UUID(int=trace.trace_id or uuid4().int))
                    if result.published:
                        await self._cache.remember(
                            mail.idempotency_key(self._policy.mailbox_id), result.job_id
                        )
                        await session.mark_processed(mail.source_id)
                        _LOG.info("mail acknowledged job_id=%s", result.job_id)

    async def _register(self, mail: IncomingMail, trace_id: UUID) -> RegisteredJob:
        key = mail.idempotency_key(self._policy.mailbox_id)
        if (cached_id := await self._cache.get(key)) and (
            known := await self._jobs.find(cached_id, key)
        ):
            _LOG.debug("mail deduplicated code=DUPLICATE_MESSAGE job_id=%s", known.job_id)
            return known
        created_at = datetime.now(UTC)
        origin = self._policy.document_base_url.rstrip("/")
        documents: list[Document | LegacyDocument] = []
        for attachment in mail.attachments:
            file_id = uuid4()
            documents.append(
                Document(
                    file_id=file_id,
                    name=attachment.name,
                    mime=attachment.mime,
                    checksum=attachment.checksum,
                    storage_path=f"{origin}/api/v1/documents/{file_id}",
                )
            )
        job = Job(
            job_id=uuid4(),
            idempotency_key=key,
            source="mail",
            external_ref=mail.message_id,
            created_at=created_at,
            updated_at=created_at,
            status="ACCEPTED",
            kb_version=self._policy.kb_version,
            lot_ids=[],
            documents=documents,
            profile_ref=self._policy.profile_ref,
            trace_id=trace_id,
        )
        headers: dict[str, str] = {}
        inject(headers)
        registered = await self._jobs.register(job, mail, headers)
        if registered.job_id != job.job_id:
            _LOG.debug("mail deduplicated code=DUPLICATE_MESSAGE job_id=%s", registered.job_id)
        return registered


class OutboxRelay:
    def __init__(
        self, jobs: MailJobs, publisher: Publisher, batch_size: int, tracer: Tracer
    ) -> None:
        self._jobs = jobs
        self._publisher = publisher
        self._batch_size = batch_size
        self._tracer = tracer

    async def flush(self) -> None:
        for _ in range(self._batch_size):
            record = await self._jobs.claim()
            if record is None:
                return
            with self._tracer.start_as_current_span(
                "mail.publish",
                context=extract(record.headers),
                record_exception=False,
                set_status_on_exception=False,
            ):
                await self._publisher.publish(record)
                if await self._jobs.confirm(record):
                    _LOG.info("job published job_id=%s", record.job.job_id)


async def repeat(operation: Callable[[], Awaitable[None]], interval: float) -> None:
    while True:
        try:
            await operation()
        except MailUnavailable:
            _LOG.warning("worker operation=%s code=MAIL_UNAVAILABLE", operation.__name__)
        except Exception as error:
            _LOG.warning("worker operation=%s error=%s", operation.__name__, type(error).__name__)
        await asyncio.sleep(interval)
