import asyncio
import imaplib
import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from aiokafka import AIOKafkaConsumer
from mcp import Client
from redis.asyncio import Redis

from contracts.models import Document, Job
from tender_integrations.application import JOB_CREATED
from tender_integrations.config import ImapConfig, Settings

pytestmark = pytest.mark.skipif(
    "TH_INTEGRATIONS_TEST_SMTP_PORT" not in os.environ,
    reason="requires the isolated Compose integrations sandbox",
)


def send_fixture(config: ImapConfig, raw: bytes, smtp_port: int) -> None:
    with smtplib.SMTP(config.host, smtp_port, timeout=5) as smtp:
        smtp.sendmail("supplier@example.test", [config.user], raw)


def mailbox_has_acknowledged(config: ImapConfig, message_id: str, count: int) -> bool:
    with imaplib.IMAP4(config.host, config.port, timeout=5) as mailbox:
        mailbox.login(config.user, config.password.get_secret_value())
        mailbox.select(config.folder, readonly=True)
        status, received = mailbox.uid("SEARCH", "HEADER", "Message-ID", f'"{message_id}"')
        if status != "OK" or not isinstance(received[0], bytes):
            return False
        status, unread = mailbox.uid("SEARCH", "UNSEEN", "HEADER", "Message-ID", f'"{message_id}"')
        return len(received[0].split()) == count and status == "OK" and unread == [b""]


async def wait_for_ack(config: ImapConfig, message_id: str, count: int) -> None:
    async with asyncio.timeout(20):
        while True:
            if await asyncio.to_thread(mailbox_has_acknowledged, config, message_id, count):
                return
            await asyncio.sleep(0.5)


async def test_real_imap_postgres_redis_kafka_and_mcp() -> None:
    settings = Settings.model_validate({})
    assert settings.imap is not None
    config = settings.imap
    smtp_port = int(os.environ["TH_INTEGRATIONS_TEST_SMTP_PORT"])
    message_id = f"<integration-{uuid4()}@example.test>"
    message = EmailMessage()
    message["From"] = "supplier@example.test"
    message["To"] = config.user
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Subject"] = "Synthetic tender document"
    message["Message-ID"] = message_id
    message.set_content("Sandbox test")
    attachment = b"%PDF-1.7\n% Integration test data\n%%EOF\n"
    message.add_attachment(attachment, maintype="application", subtype="pdf", filename="tender.pdf")
    raw = message.as_bytes()
    api = str(settings.document_base_url).rstrip("/")
    token = settings.document_token.get_secret_value()
    async with (
        AIOKafkaConsumer(
            JOB_CREATED,
            bootstrap_servers=",".join(settings.kafka_brokers),
            group_id=f"integrations-test-{uuid4()}",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        ) as consumer,
        httpx.AsyncClient(base_url=api, headers={"Authorization": f"Bearer {token}"}) as http,
        Redis.from_url(str(settings.redis_url), decode_responses=True) as redis,
    ):
        await asyncio.to_thread(send_fixture, config, raw, smtp_port)
        async with asyncio.timeout(25):
            while True:
                event = await consumer.getone()
                assert event.value is not None
                job = Job.model_validate_json(event.value)
                if job.external_ref == message_id:
                    break
        assert event.key == str(job.job_id).encode()
        assert job.source == "mail"
        assert tuple(name for name, _ in event.headers) == ("traceparent",)
        document = job.documents[0]
        assert isinstance(document, Document)
        assert urlsplit(document.storage_path).path == f"/api/v1/documents/{document.file_id}"
        assert (await http.get(document.storage_path)).content == attachment
        unauthorized = await http.get(
            document.storage_path, headers={"Authorization": "Bearer invalid"}
        )
        assert unauthorized.status_code == 401
        await wait_for_ack(config, message_id, 1)
        cache_key = f"job:idempotency:{job.idempotency_key}"
        assert await redis.get(cache_key) == str(job.job_id)
        assert 0 < await redis.ttl(cache_key) <= 604800
        await redis.delete(cache_key)

        await asyncio.to_thread(send_fixture, config, raw, smtp_port)
        await wait_for_ack(config, message_id, 2)
        response = await http.get("/api/v1/jobs", params={"external_ref": message_id})
        assert response.status_code == 200
        assert response.json() == {"jobs": [{"job_id": str(job.job_id), "published": True}]}
        for messages in (await consumer.getmany(timeout_ms=1500)).values():
            assert all(event.key != str(job.job_id).encode() for event in messages)

    async with Client(f"{api}/api/v1/mcp") as client:
        result = await client.call_tool("getDocumentStatus", {"document_ref": "fixture"})
        assert result.structured_content == {
            "document_ref": "fixture",
            "status": "received",
            "simulated": True,
        }
