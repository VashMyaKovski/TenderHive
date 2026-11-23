import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from tender_integrations.config import Settings
from tender_integrations.errors import ErrorCode, IntegrationError
from tender_integrations.models import Job, MailMessage
from tender_integrations.ports import (
    EventPublisher,
    FileStore,
    IdempotencyStore,
    JobStore,
    MailSource,
)

_SUPPORTED = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}


class MailProcessor:
    def __init__(
        self,
        settings: Settings,
        source: MailSource,
        idempotency: IdempotencyStore,
        files: FileStore,
        jobs: JobStore,
        publisher: EventPublisher,
        clock: Callable[[], datetime],
        trace_factory: Callable[[], UUID],
    ) -> None:
        self._settings = settings
        self._source = source
        self._idempotency = idempotency
        self._files = files
        self._jobs = jobs
        self._publisher = publisher
        self._clock = clock
        self._trace_factory = trace_factory

    def idempotency_key(self, message: MailMessage) -> str:
        hashes = sorted(
            hashlib.sha256(attachment.content).hexdigest() for attachment in message.attachments
        )
        source = "|".join([self._settings.mailbox, message.message_id, *hashes])
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    async def poll(self) -> tuple[list[Job], list[str]]:
        messages = await self._source.fetch()
        created: list[Job] = []
        errors: list[str] = []
        for message in messages:
            try:
                job = await self.process(message)
            except IntegrationError as error:
                errors.append(str(error.code))
                continue
            created.append(job)
        return created, errors

    async def process(self, message: MailMessage) -> Job:
        attachments = [
            attachment
            for attachment in message.attachments
            if Path(attachment.name).suffix.lower() in _SUPPORTED
        ]
        if not attachments:
            raise IntegrationError(
                ErrorCode.UNSUPPORTED_ATTACHMENT, "message has no supported attachments"
            )
        key = self.idempotency_key(message)
        if not await self._idempotency.claim(key, self._settings.idempotency_ttl_seconds):
            raise IntegrationError(ErrorCode.DUPLICATE_MESSAGE, "message already processed")

        job_id = uuid4()
        try:
            documents = [await self._files.save(item, job_id) for item in attachments]
            job = Job(
                job_id=job_id,
                idempotency_key=key,
                external_ref=message.message_id,
                created_at=self._clock(),
                kb_version=self._settings.kb_version,
                documents=documents,
                profile_ref=self._settings.profile_ref,
                trace_id=self._trace_factory(),
            )
            await self._jobs.save(job)
            await self._publisher.publish(job)
        except Exception:
            await self._idempotency.release(key)
            raise
        await self._source.mark_processed(message)
        return job
