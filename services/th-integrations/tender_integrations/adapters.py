import asyncio
import hashlib
import imaplib
import json
from datetime import UTC
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from aiokafka import AIOKafkaProducer
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from tender_integrations.config import Settings
from tender_integrations.errors import ErrorCode, IntegrationError
from tender_integrations.models import (
    Attachment,
    Job,
    MailMessage,
    McpResult,
    StoredDocument,
)


def parse_message(raw: bytes, uid: str = "") -> MailMessage:
    message = BytesParser(policy=email_policy).parsebytes(raw)
    received = parsedate_to_datetime(str(message.get("Date", "")))
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    attachments: list[Attachment] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_filename()
        if name is None:
            continue
        attachments.append(
            Attachment(
                name=str(name),
                content=cast(bytes, part.get_payload(decode=True) or b""),
                mime=str(part.get_content_type()),
            )
        )
    return MailMessage(
        message_id=str(message.get("Message-ID", "")),
        sender=str(message.get("From", "")),
        subject=str(message.get("Subject", "")),
        received_at=received,
        attachments=tuple(attachments),
        uid=uid,
    )


class ImapMailSource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timeout = float(settings.poll_seconds)

    def _connect(self) -> imaplib.IMAP4_SSL:
        client = imaplib.IMAP4_SSL(
            self._settings.imap_host, self._settings.imap_port, timeout=self._timeout
        )
        client.login(self._settings.imap_user, self._settings.imap_password)
        client.select(self._settings.mailbox)
        return client

    async def fetch(self) -> list[MailMessage]:
        def read() -> list[MailMessage]:
            client = self._connect()
            try:
                _status, items = client.search(None, "UNSEEN")
                messages: list[MailMessage] = []
                for raw_uid in (items[0] or b"").split():
                    uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
                    _result, data = client.fetch(uid, "(RFC822)")
                    if data and isinstance(data[0], tuple):
                        messages.append(parse_message(cast(bytes, data[0][1]), uid))
                return messages
            finally:
                client.logout()

        try:
            return await asyncio.to_thread(read)
        except (imaplib.IMAP4.error, OSError) as error:
            raise IntegrationError(ErrorCode.MAIL_UNAVAILABLE, str(error)) from error

    async def mark_processed(self, message: MailMessage) -> None:
        def mark() -> None:
            client = self._connect()
            try:
                client.store(message.uid, "+FLAGS", "\\Seen")
            finally:
                client.logout()

        await asyncio.to_thread(mark)


class RedisIdempotencyStore:
    def __init__(self, url: str, prefix: str) -> None:
        self._client = Redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        return bool(await self._client.set(f"{self._prefix}{key}", "1", ex=ttl_seconds, nx=True))

    async def release(self, key: str) -> None:
        await self._client.delete(f"{self._prefix}{key}")

    async def close(self) -> None:
        await self._client.aclose()


class LocalFileStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    async def save(self, attachment: Attachment, job_id: UUID) -> StoredDocument:
        directory = self._root / str(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / attachment.name
        path.write_bytes(attachment.content)
        return StoredDocument(
            file_id=uuid4(),
            name=attachment.name,
            mime=attachment.mime,
            checksum=hashlib.sha256(attachment.content).hexdigest(),
            storage_path=str(path),
        )


class PostgresJobStore:
    def __init__(self, dsn: str, schema: str) -> None:
        self._schema = schema
        self._pool = AsyncConnectionPool(conninfo=dsn, open=False)

    async def start(self) -> None:
        await self._pool.open()
        await self._pool.wait()

    async def close(self) -> None:
        await self._pool.close()

    async def save(self, job: Job) -> None:
        query = sql.SQL(
            "INSERT INTO {}.jobs (job_id, idempotency_key, payload) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING"
        ).format(sql.Identifier(self._schema))
        async with self._pool.connection() as connection:
            await connection.execute(
                query,
                (job.job_id, job.idempotency_key, job.model_dump_json()),
            )


class KafkaEventPublisher:
    def __init__(self, brokers: str, topic: str) -> None:
        self._topic = topic
        self._producer = AIOKafkaProducer(bootstrap_servers=brokers)

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        await self._producer.stop()

    async def publish(self, job: Job) -> None:
        await self._producer.send_and_wait(
            self._topic,
            key=str(job.job_id).encode("utf-8"),
            value=json.dumps(
                {"event_type": "tender.job.created", "payload": job.model_dump(mode="json")}
            ).encode("utf-8"),
        )


def mcp_result(system: str, method: str, settings: Settings) -> McpResult:
    payloads = {
        ("1c", "getOrganizationProfile"): {
            "profile_ref": settings.profile_ref,
            "licenses": [],
            "resources": [],
            "prices": [],
        },
        ("edo", "getDocumentStatus"): {"status": "SENT", "document_id": "stub"},
        ("registry", "checkCounterparty"): {
            "name": settings.counterparty_name,
            "active": True,
        },
    }
    payload = payloads.get((system, method))
    if payload is None:
        raise IntegrationError(
            ErrorCode.MCP_STUB_ERROR, f"unsupported stub call: {system}.{method}"
        )
    return McpResult.model_validate({"system": system, "method": method, "payload": payload})
