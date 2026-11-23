import asyncio
import sys
from datetime import UTC, datetime
from http.client import HTTPConnection

import uvicorn

from th_kb.adapters import (
    Neo4jGraph,
    PostgresRepository,
    QdrantVectorStore,
    RedisActiveVersionCache,
)
from th_kb.api import create_app
from th_kb.application import KnowledgeBase
from th_kb.config import Settings


def _host(settings: Settings) -> str:
    if not settings.bind_host.is_unspecified:
        return str(settings.bind_host)
    return "::1" if settings.bind_host.version == 6 else "127.0.0.1"


async def _serve(settings: Settings, host: str) -> None:
    repository = PostgresRepository(settings.postgres_dsn)
    graph = Neo4jGraph(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_timeout_seconds,
    )
    vector = QdrantVectorStore(
        settings.qdrant_url,
        settings.modelgw_url,
        settings.prompt_version,
        settings.embed_timeout_seconds,
        settings.qdrant_timeout_seconds,
    )
    cache = RedisActiveVersionCache(settings.redis_url, "kb:active_version")
    await repository.start()
    try:
        kb = KnowledgeBase(
            settings,
            repository,
            graph,
            vector,
            cache,
            lambda: datetime.now(UTC),
        )
        config = uvicorn.Config(create_app(kb), host=host, port=settings.port, access_log=False)
        await uvicorn.Server(config).serve()
    finally:
        await repository.close()
        await graph.close()
        await cache.close()


def main() -> None:
    settings = Settings.model_validate({})
    host = _host(settings)
    if sys.argv[1:] == ["healthcheck"]:
        connection = HTTPConnection(host, settings.port, timeout=settings.embed_timeout_seconds)
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
