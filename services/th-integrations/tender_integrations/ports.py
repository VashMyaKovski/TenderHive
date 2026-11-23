from typing import Protocol
from uuid import UUID

from tender_integrations.models import Attachment, Job, MailMessage, StoredDocument


class MailSource(Protocol):
    async def fetch(self) -> list[MailMessage]: ...

    async def mark_processed(self, message: MailMessage) -> None: ...


class IdempotencyStore(Protocol):
    async def claim(self, key: str, ttl_seconds: int) -> bool: ...

    async def release(self, key: str) -> None: ...


class JobStore(Protocol):
    async def save(self, job: Job) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, job: Job) -> None: ...


class FileStore(Protocol):
    async def save(self, attachment: Attachment, job_id: UUID) -> StoredDocument: ...
