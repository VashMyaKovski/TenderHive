import json
from typing import Any, cast
from uuid import uuid4

import httpx
from neo4j import AsyncGraphDatabase
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue
from redis.asyncio import Redis

from th_kb.errors import ErrorCode, KbError
from th_kb.models import (
    CorpusFragment,
    VersionInfo,
    VersionPayload,
    VersionStatus,
)


class PostgresRepository:
    def __init__(self, dsn: str) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    async def start(self) -> None:
        await self._pool.open()
        await self._pool.wait()

    async def close(self) -> None:
        await self._pool.close()

    async def save(self, payload: VersionPayload) -> None:
        async with self._pool.connection() as connection, connection.transaction():
            row = await connection.execute(
                """
                    INSERT INTO kb.versions (version, status, payload, checksum, author, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (version) DO NOTHING
                    RETURNING version
                    """,
                (
                    payload.version,
                    VersionStatus.DRAFT.value,
                    json.dumps(payload.model_dump(mode="json")),
                    payload.checksum,
                    payload.author,
                    payload.created_at,
                ),
            )
            if await row.fetchone() is None:
                raise KbError(ErrorCode.VERSION_EXISTS, f"version {payload.version} exists")

    async def get(self, version: str) -> VersionPayload | None:
        async with self._pool.connection() as connection:
            row = await connection.execute(
                "SELECT payload FROM kb.versions WHERE version = %s", (version,)
            )
            record = await row.fetchone()
        return None if record is None else VersionPayload.model_validate(record["payload"])

    async def list_versions(self) -> list[VersionInfo]:
        async with self._pool.connection() as connection:
            rows = await connection.execute(
                """
                SELECT version, status, checksum, author, created_at, activated_at
                FROM kb.versions ORDER BY created_at DESC
                """
            )
            records = await rows.fetchall()
        return [VersionInfo.model_validate(record) for record in records]

    async def active_version(self) -> str | None:
        async with self._pool.connection() as connection:
            row = await connection.execute(
                "SELECT version FROM kb.versions WHERE status = %s", (VersionStatus.ACTIVE.value,)
            )
            record = await row.fetchone()
        return None if record is None else str(record["version"])

    async def activate(self, version: str) -> str | None:
        async with self._pool.connection() as connection, connection.transaction():
            current = await connection.execute(
                "SELECT version FROM kb.versions WHERE status = %s FOR UPDATE",
                (VersionStatus.ACTIVE.value,),
            )
            record = await current.fetchone()
            previous = None if record is None else str(record["version"])
            if previous == version:
                return previous
            await connection.execute(
                "UPDATE kb.versions SET status = %s WHERE status = %s",
                (VersionStatus.OLD.value, VersionStatus.ACTIVE.value),
            )
            updated = await connection.execute(
                """
                    UPDATE kb.versions
                    SET status = %s, activated_at = CURRENT_TIMESTAMP
                    WHERE version = %s
                    RETURNING author
                    """,
                (VersionStatus.ACTIVE.value, version),
            )
            target = await updated.fetchone()
            if target is None:
                raise KbError(ErrorCode.VERSION_NOT_FOUND, f"version {version} not found")
            await connection.execute(
                "INSERT INTO kb.activation_log (version, actor) VALUES (%s, %s)",
                (version, str(target["author"])),
            )
        return previous

    async def mark(self, version: str, status: VersionStatus) -> None:
        async with self._pool.connection() as connection:
            await connection.execute(
                "UPDATE kb.versions SET status = %s WHERE version = %s",
                (status.value, version),
            )

    async def last_activated(self, excluded: str) -> str | None:
        async with self._pool.connection() as connection:
            row = await connection.execute(
                """
                SELECT version FROM kb.activation_log
                WHERE version <> %s ORDER BY id DESC LIMIT 1
                """,
                (excluded,),
            )
            record = await row.fetchone()
        return None if record is None else str(record["version"])


class RedisActiveVersionCache:
    def __init__(self, url: str, key: str) -> None:
        self._client = Redis.from_url(url, decode_responses=True)
        self._key = key

    async def get(self) -> str | None:
        try:
            value = await self._client.get(self._key)
        except Exception:
            return None
        return None if value is None else str(value)

    async def set(self, version: str, ttl_seconds: int) -> None:
        try:
            await self._client.set(self._key, version, ex=ttl_seconds)
        except Exception:
            return

    async def close(self) -> None:
        await self._client.aclose()


class Neo4jGraph:
    def __init__(self, uri: str, user: str, password: str, timeout: float) -> None:
        self._driver = AsyncGraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=timeout
        )

    async def close(self) -> None:
        await self._driver.close()

    async def slice(self, lot_ids: list[str]) -> dict[str, JsonValue]:
        if not lot_ids:
            return {"nodes": [], "edges": []}
        query = """
            MATCH (requirement:Requirement)-[relation]-(related)
            WHERE requirement.lot_id IN $lot_ids
            RETURN requirement.id AS source, related.id AS target, type(relation) AS kind
            LIMIT 100
        """
        async with self._driver.session() as session:
            result = await session.run(query, lot_ids=lot_ids)
            records = await result.data()
        edges: list[JsonValue] = [
            {"source": record["source"], "target": record["target"], "kind": record["kind"]}
            for record in records
        ]
        return cast(dict[str, JsonValue], {"edges": edges})


class QdrantVectorStore:
    def __init__(
        self,
        qdrant_url: str,
        modelgw_url: str,
        prompt_version: str,
        embed_timeout: float,
        query_timeout: float,
    ) -> None:
        self._qdrant_url = qdrant_url.rstrip("/")
        self._modelgw_url = modelgw_url.rstrip("/")
        self._prompt_version = prompt_version
        self._embed_timeout = embed_timeout
        self._query_timeout = query_timeout

    async def _embedding(self, text: str) -> list[float]:
        payload = {
            "job_id": str(uuid4()),
            "task": "embeddings",
            "context": [{"type": "synthetic_text", "text": text}],
            "prompt_version": self._prompt_version,
            "mask": True,
        }
        async with httpx.AsyncClient(timeout=self._embed_timeout) as client:
            response = await client.post(f"{self._modelgw_url}/api/v1/inference", json=payload)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        vectors = data["answer"]["vectors"]
        if not vectors:
            raise KbError(ErrorCode.STORAGE_UNAVAILABLE, "embedding service returned no vector")
        return [float(value) for value in vectors[0]]

    async def search(self, version: str, query: str, limit: int) -> list[CorpusFragment]:
        vector = await self._embedding(query)
        payload = {
            "query": vector,
            "limit": limit,
            "with_payload": True,
            "filter": {"must": [{"key": "version", "match": {"value": version}}]},
        }
        url = f"{self._qdrant_url}/collections/{version}/points/search"
        async with httpx.AsyncClient(timeout=self._query_timeout) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        points: list[dict[str, Any]] = response.json()["result"]["points"]
        return [
            CorpusFragment.model_validate(
                {
                    "fragment_id": point["payload"]["fragment_id"],
                    "text": point["payload"]["text"],
                    "source": point["payload"]["source"],
                    "date": point["payload"]["date"],
                    "score": point["score"],
                }
            )
            for point in points
        ]
