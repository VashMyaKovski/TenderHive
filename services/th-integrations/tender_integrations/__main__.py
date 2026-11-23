import asyncio
import sys
from datetime import UTC, datetime
from http.client import HTTPConnection
from uuid import uuid4

import uvicorn

from tender_integrations.adapters import (
    ImapMailSource,
    KafkaEventPublisher,
    LocalFileStore,
    PostgresJobStore,
    RedisIdempotencyStore,
)
from tender_integrations.api import create_app
from tender_integrations.config import Settings
from tender_integrations.processor import MailProcessor


def _host(settings: Settings) -> str:
    if not settings.bind_host.is_unspecified:
        return str(settings.bind_host)
    return "::1" if settings.bind_host.version == 6 else "127.0.0.1"


async def _serve(settings: Settings, url_host: str) -> None:
    source = ImapMailSource(settings)
    idempotency = RedisIdempotencyStore(settings.redis_url, "job:idempotency:")
    files = LocalFileStore(settings.storage_path)
    jobs = PostgresJobStore(settings.postgres_dsn, "integrations")
    publisher = KafkaEventPublisher(settings.kafka_brokers, settings.kafka_topic)
    await jobs.start()
    await publisher.start()
    try:
        processor = MailProcessor(
            settings,
            source,
            idempotency,
            files,
            jobs,
            publisher,
            lambda: datetime.now(UTC),
            uuid4,
        )
        config = uvicorn.Config(
            create_app(processor, settings), host=url_host, port=settings.port, access_log=False
        )
        await uvicorn.Server(config).serve()
    finally:
        await publisher.stop()
        await jobs.close()
        await idempotency.close()


def main() -> None:
    settings = Settings.model_validate({})
    host = _host(settings)
    if sys.argv[1:] == ["healthcheck"]:
        connection = HTTPConnection(host, settings.port, timeout=settings.poll_seconds)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status != 200:
                raise SystemExit(1)
        finally:
            connection.close()
        return
    asyncio.run(_serve(settings, str(settings.bind_host)))


if __name__ == "__main__":
    main()
