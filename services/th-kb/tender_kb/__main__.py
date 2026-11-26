import argparse
import asyncio
import sys
from contextlib import AsyncExitStack

import httpx
import uvicorn
from neo4j import AsyncGraphDatabase
from pydantic import ValidationError
from redis.asyncio import Redis

from tender_kb.api import create_app
from tender_kb.application import Knowledge
from tender_kb.config import Settings
from tender_kb.dependencies import (
    Embeddings,
    FixtureEmbeddings,
    GatewayEmbeddings,
    HttpGate,
    RedisContextCache,
)
from tender_kb.domain import DependencyUnavailable
from tender_kb.postgres import Postgres
from tender_kb.projections import Neo4jGraph, QdrantCorpus


async def run(command: str, settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=settings.io_timeout_seconds, trust_env=False, follow_redirects=False
            )
        )
        if command == "healthcheck":
            host = settings.bind_host
            address = "127.0.0.1" if host.is_unspecified else str(host)
            if host.version == 6:
                address = "[::1]" if host.is_unspecified else f"[{host}]"
            response = await client.get(
                f"http://{address}:{settings.port}/healthz",
                timeout=settings.database_timeout_seconds + 2 * settings.io_timeout_seconds,
            )
            response.raise_for_status()
            return

        driver = await stack.enter_async_context(
            AsyncGraphDatabase.driver(
                str(settings.neo4j_uri),
                auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
                connection_timeout=settings.io_timeout_seconds,
                connection_acquisition_timeout=settings.io_timeout_seconds,
                max_transaction_retry_time=settings.io_timeout_seconds,
            )
        )
        graph = Neo4jGraph(driver, settings.neo4j_database, settings.io_timeout_seconds)
        embeddings: Embeddings
        if settings.embedding_mode == "fixture":
            embeddings = FixtureEmbeddings(settings.vector_size)
        else:
            embeddings = GatewayEmbeddings(
                client,
                str(settings.model_gw_url),
                settings.model_gw_prompt_version,
                settings.vector_size,
            )
        corpus = QdrantCorpus(
            client,
            str(settings.qdrant_url),
            settings.qdrant_collection,
            settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
            embeddings,
            settings.vector_size,
        )
        postgres = Postgres(str(settings.postgres_dsn), settings.database_timeout_seconds)
        if command == "migrate":
            await postgres.migrate()
            await graph.migrate()
            await corpus.migrate()
            return

        cache = None
        if settings.redis_url is not None:
            redis = Redis.from_url(
                str(settings.redis_url),
                socket_timeout=settings.io_timeout_seconds,
                socket_connect_timeout=settings.io_timeout_seconds,
            )
            stack.push_async_callback(redis.aclose)
            cache = RedisContextCache(redis, settings.cache_ttl_seconds)
        gate = HttpGate(
            client,
            str(settings.eval_url),
            settings.eval_token.get_secret_value(),
            settings.eval_timeout_seconds,
        )
        knowledge = Knowledge(
            postgres, graph, corpus, gate, cache, settings.test_set_version, settings.max_fragments
        )
        app = create_app(knowledge, settings.eval_token.get_secret_value())
        server = uvicorn.Server(
            uvicorn.Config(app, host=str(settings.bind_host), port=settings.port, access_log=False)
        )
        await server.serve()


def main() -> int:
    parser = argparse.ArgumentParser(prog="th-kb")
    parser.add_argument("command", choices=["serve", "migrate", "healthcheck"])
    args = parser.parse_args()
    try:
        settings = Settings.model_validate({})
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        asyncio.run(run(args.command, settings), loop_factory=loop_factory)
    except ValidationError as error:
        fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        print(f"th-kb configuration is invalid for: {fields}", file=sys.stderr)
        return 2
    except (DependencyUnavailable, httpx.HTTPError) as error:
        print(f"th-kb dependency check failed ({type(error).__name__})", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
