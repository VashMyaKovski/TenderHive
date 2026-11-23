from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from tender_integrations.config import Settings
from tender_integrations.errors import ErrorCode, IntegrationError
from tender_integrations.models import Attachment, Job, MailMessage, StoredDocument


class FakeMailSource:
    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages
        self.marked: list[str] = []

    async def fetch(self) -> list[MailMessage]:
        return list(self.messages)

    async def mark_processed(self, message: MailMessage) -> None:
        self.marked.append(message.message_id)


class MemoryIdempotency:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def release(self, key: str) -> None:
        self.keys.discard(key)


class MemoryFiles:
    async def save(self, attachment: Attachment, job_id: UUID) -> StoredDocument:
        return StoredDocument(
            file_id=uuid4(),
            name=attachment.name,
            mime=attachment.mime,
            checksum="a" * 64,
            storage_path=f"/tmp/{job_id}/{attachment.name}",
        )


class MemoryJobs:
    def __init__(self) -> None:
        self.saved: list[Job] = []

    async def save(self, job: Job) -> None:
        self.saved.append(job)


class MemoryPublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[Job] = []

    async def publish(self, job: Job) -> None:
        if self.fail:
            raise IntegrationError(ErrorCode.MAIL_UNAVAILABLE, "broker unavailable")
        self.published.append(job)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "imap_host": "imap.local",
            "imap_port": 993,
            "imap_user": "user",
            "imap_password": "secret",
            "mailbox": "INBOX",
            "poll_seconds": 60,
            "storage_path": "/tmp/tenderhive",
            "postgres_dsn": "postgresql://localhost/test",
            "redis_url": "redis://localhost:6379/2",
            "kafka_brokers": "kafka:29092",
            "kafka_topic": "tender.job.created",
            "idempotency_ttl_seconds": 604800,
            "profile_ref": "profile-1",
            "kb_version": "kb-v1",
            "counterparty_name": "Supplier",
            "bind_host": "127.0.0.1",
            "port": 8000,
        }
    )


def message(attachments: tuple[Attachment, ...] | None = None) -> MailMessage:
    return MailMessage(
        message_id="<msg-1@example.local>",
        sender="tender@example.local",
        subject="Tender documents",
        received_at=datetime(2026, 11, 21, tzinfo=UTC),
        attachments=attachments
        if attachments is not None
        else (Attachment("docs.pdf", b"pdf-content", "application/pdf"),),
        uid="1",
    )
