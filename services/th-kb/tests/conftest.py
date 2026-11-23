from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import JsonValue

from th_kb.config import Settings
from th_kb.errors import ErrorCode, KbError
from th_kb.models import (
    CorpusFragment,
    Fact,
    Rule,
    TestCase,
    VersionInfo,
    VersionPayload,
    VersionStatus,
    compute_checksum,
)


class MemoryRepository:
    def __init__(self) -> None:
        self.versions: dict[str, VersionPayload] = {}
        self.statuses: dict[str, VersionStatus] = {}
        self.active: str | None = None
        self.history: list[str] = []

    async def save(self, payload: VersionPayload) -> None:
        if payload.version in self.versions:
            raise KbError(ErrorCode.VERSION_EXISTS, "version exists")
        self.versions[payload.version] = payload
        self.statuses[payload.version] = VersionStatus.DRAFT

    async def get(self, version: str) -> VersionPayload | None:
        return self.versions.get(version)

    async def list_versions(self) -> list[VersionInfo]:
        return [
            VersionInfo(
                version=payload.version,
                status=self.statuses[payload.version],
                checksum=payload.checksum,
                author=payload.author,
                created_at=payload.created_at,
            )
            for payload in self.versions.values()
        ]

    async def active_version(self) -> str | None:
        return self.active

    async def activate(self, version: str) -> str | None:
        if version not in self.versions:
            raise KbError(ErrorCode.VERSION_NOT_FOUND, "version not found")
        previous = self.active
        self.active = version
        self.statuses[version] = VersionStatus.ACTIVE
        if previous is not None:
            self.statuses[previous] = VersionStatus.OLD
        self.history.append(version)
        return previous

    async def mark(self, version: str, status: VersionStatus) -> None:
        self.statuses[version] = status

    async def last_activated(self, excluded: str) -> str | None:
        for version in reversed(self.history):
            if version != excluded:
                return version
        return None


class FakeGraph:
    async def slice(self, lot_ids: list[str]) -> dict[str, JsonValue]:
        return (
            {"edges": [{"source": lot_ids[0], "target": "R-07", "kind": "APPLIES"}]}
            if lot_ids
            else {}
        )


class FakeVector:
    async def search(self, version: str, query: str, limit: int) -> list[CorpusFragment]:
        return [
            CorpusFragment(
                fragment_id=str(uuid4()),
                text=f"fragment for {query}",
                source="tender.pdf",
                date="2026-11-21",
                score=0.9,
            )
        ][:limit]


class FakeCache:
    def __init__(self) -> None:
        self.value: str | None = None

    async def get(self) -> str | None:
        return self.value

    async def set(self, version: str, ttl_seconds: int) -> None:
        self.value = version


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "postgres_dsn": "postgresql://localhost/test",
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "qdrant_url": "http://localhost:6333",
            "redis_url": "redis://localhost:6379/0",
            "modelgw_url": "http://localhost:8000",
            "active_version": "kb-v1",
            "test_set_version": "tests-v1",
            "prompt_version": "v1",
            "collection": "kb-v1",
            "cache_ttl_seconds": 30,
            "max_fragments": 8,
            "embed_timeout_seconds": 5,
            "qdrant_timeout_seconds": 5,
            "neo4j_timeout_seconds": 5,
            "bind_host": "127.0.0.1",
            "port": 8000,
        }
    )


def make_payload(
    version: str = "kb-v1",
    rules: list[Rule] | None = None,
    facts: list[Fact] | None = None,
    test_cases: list[TestCase] | None = None,
) -> VersionPayload:
    now = datetime(2026, 11, 21, tzinfo=UTC)
    rules = rules if rules is not None else [make_rule()]
    facts = facts if facts is not None else []
    test_cases = test_cases if test_cases is not None else []
    payload = VersionPayload(
        version=version,
        rules=rules,
        facts=facts,
        reference_data={},
        test_cases=test_cases,
        checksum="0" * 64,
        author="vsevolod.myalo",
        created_at=now,
    )
    return payload.model_copy(update={"checksum": compute_checksum(payload)})


def make_rule(rule_id: str = "R-07", consequence: str = "MET", priority: int = 50) -> Rule:
    return Rule(
        rule_id=rule_id,
        condition="margin_percent >= 10",
        consequence=consequence,
        priority=priority,
        scope="global",
        version=1,
    )


def make_fact(key: str = "margin_percent", value: int = 12) -> Fact:
    return Fact(
        fact_id=f"fact-{uuid4()}",
        subject="profile-1",
        key=key,
        value=value,
        source="1c-stub",
        valid_from=datetime(2026, 11, 21, tzinfo=UTC),
    )
