from typing import Protocol

from pydantic import JsonValue

from th_kb.models import CorpusFragment, VersionInfo, VersionPayload, VersionStatus


class VersionRepository(Protocol):
    async def save(self, payload: VersionPayload) -> None: ...

    async def get(self, version: str) -> VersionPayload | None: ...

    async def list_versions(self) -> list[VersionInfo]: ...

    async def active_version(self) -> str | None: ...

    async def activate(self, version: str) -> str | None:
        """Atomically activate a version and return the previously active one."""

    async def mark(self, version: str, status: VersionStatus) -> None: ...

    async def last_activated(self, excluded: str) -> str | None: ...


class GraphStore(Protocol):
    async def slice(self, lot_ids: list[str]) -> dict[str, JsonValue]: ...


class VectorStore(Protocol):
    async def search(self, version: str, query: str, limit: int) -> list[CorpusFragment]: ...


class ActiveVersionCache(Protocol):
    async def get(self) -> str | None: ...

    async def set(self, version: str, ttl_seconds: int) -> None: ...
