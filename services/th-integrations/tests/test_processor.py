from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tender_integrations.adapters import LocalFileStore, parse_message
from tender_integrations.config import Settings
from tender_integrations.errors import ErrorCode, IntegrationError
from tender_integrations.models import Attachment
from tender_integrations.processor import MailProcessor
from tests.conftest import (
    FakeMailSource,
    MemoryFiles,
    MemoryIdempotency,
    MemoryJobs,
    MemoryPublisher,
    message,
)


def build(
    settings: Settings, publisher: MemoryPublisher | None = None
) -> tuple[MailProcessor, FakeMailSource]:
    source = FakeMailSource([message()])
    processor = MailProcessor(
        settings,
        source,
        MemoryIdempotency(),
        MemoryFiles(),
        MemoryJobs(),
        publisher or MemoryPublisher(),
        lambda: datetime(2026, 11, 23, tzinfo=UTC),
        uuid4,
    )
    return processor, source


def test_parse_message_extracts_attachments() -> None:
    raw = (
        b"Message-ID: <abc@example.local>\r\n"
        b"From: sender@example.local\r\n"
        b"Subject: Tender\r\n"
        b"Date: Sat, 21 Nov 2026 10:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUND\r\n\r\n"
        b"--BOUND\r\nContent-Type: text/plain\r\n\r\nHello\r\n"
        b"--BOUND\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=docs.pdf\r\n\r\nPDFDATA\r\n--BOUND--\r\n"
    )
    parsed = parse_message(raw, uid="7")
    assert parsed.message_id == "<abc@example.local>"
    assert parsed.attachments[0].name == "docs.pdf"
    assert parsed.attachments[0].content == b"PDFDATA"
    assert parsed.received_at.tzinfo is not None


async def test_poll_creates_job_and_marks_after_publish(settings: Settings) -> None:
    processor, source = build(settings)
    jobs, errors = await processor.poll()
    assert errors == []
    assert len(jobs) == 1
    assert jobs[0].source == "mail"
    assert jobs[0].idempotency_key == processor.idempotency_key(message())
    assert len(jobs[0].documents) == 1
    assert source.marked == ["<msg-1@example.local>"]


async def test_duplicate_message_is_not_processed_twice(settings: Settings) -> None:
    processor, _ = build(settings)
    first = await processor.poll()
    second = await processor.poll()
    assert len(first[0]) == 1
    assert second[1] == ["DUPLICATE_MESSAGE"]


async def test_unsupported_attachment_is_reported(settings: Settings) -> None:
    processor, _ = build(settings)
    unsupported = message((Attachment("notes.txt", b"text", "text/plain"),))
    with pytest.raises(IntegrationError) as error:
        await processor.process(unsupported)
    assert error.value.code is ErrorCode.UNSUPPORTED_ATTACHMENT


async def test_failed_publish_releases_key_and_keeps_message_unseen(settings: Settings) -> None:
    publisher = MemoryPublisher(fail=True)
    processor, source = build(settings, publisher)
    with pytest.raises(IntegrationError):
        await processor.process(message())
    assert source.marked == []


async def test_attachment_name_cannot_escape_storage(tmp_path: Path) -> None:
    store = LocalFileStore(str(tmp_path))
    document = await store.save(Attachment("../../evil.pdf", b"data", "application/pdf"), uuid4())
    assert document.name == "evil.pdf"
    assert Path(document.storage_path).parent.parent == tmp_path
