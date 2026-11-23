from collections.abc import Callable
from datetime import datetime

from th_kb.config import Settings
from th_kb.errors import ErrorCode, KbError
from th_kb.models import (
    ActivationReport,
    CheckResult,
    Context,
    ContextRequest,
    VersionInfo,
    VersionPayload,
    VersionStatus,
)
from th_kb.ports import ActiveVersionCache, GraphStore, VectorStore, VersionRepository
from th_kb.validation import check_checksum, validate_for_activation


class KnowledgeBase:
    def __init__(
        self,
        settings: Settings,
        repository: VersionRepository,
        graph: GraphStore,
        vector: VectorStore,
        cache: ActiveVersionCache,
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._graph = graph
        self._vector = vector
        self._cache = cache
        self._clock = clock

    async def create_version(self, payload: VersionPayload) -> VersionInfo:
        check_checksum(payload, None)
        await self._repository.save(payload)
        return VersionInfo(
            version=payload.version,
            status=VersionStatus.DRAFT,
            checksum=payload.checksum,
            author=payload.author,
            created_at=payload.created_at,
        )

    async def activate(self, version: str) -> ActivationReport:
        payload = await self._repository.get(version)
        if payload is None:
            raise KbError(ErrorCode.VERSION_NOT_FOUND, f"version {version} not found")
        active = await self._repository.active_version()
        active_payload = None if active is None else await self._repository.get(active)
        try:
            report = validate_for_activation(payload, active_payload)
        except KbError as error:
            await self._repository.mark(version, VersionStatus.REJECTED)
            failed = CheckResult(code=str(error.code), passed=False, message=error.message)
            return ActivationReport(
                version=version, status="REJECTED", report=[*error.report, failed]
            )
        await self._repository.activate(version)
        await self._cache.set(version, self._settings.cache_ttl_seconds)
        return ActivationReport(version=version, status="ACTIVE", report=report)

    async def rollback(self, version: str) -> ActivationReport:
        payload = await self._repository.get(version)
        if payload is None:
            raise KbError(ErrorCode.VERSION_NOT_FOUND, f"version {version} not found")
        await self._repository.activate(version)
        await self._cache.set(version, self._settings.cache_ttl_seconds)
        return ActivationReport(
            version=version,
            status="ACTIVE",
            report=[CheckResult(code="ROLLBACK", passed=True, message="version restored")],
        )

    async def previous_version(self, version: str) -> str | None:
        return await self._repository.last_activated(version)

    async def get_context(self, request: ContextRequest) -> Context:
        version = (
            request.version or await self._cache.get() or await self._repository.active_version()
        )
        if version is None:
            version = self._settings.active_version
        payload = await self._repository.get(version)
        if payload is None:
            raise KbError(ErrorCode.VERSION_NOT_FOUND, f"version {version} not found")
        scopes = {scope for scope in (request.regime, request.task_class) if scope is not None}
        rules = [
            rule
            for rule in payload.rules
            if not scopes or rule.scope == "global" or rule.scope in scopes
        ]
        fragments = []
        if request.query is not None:
            fragments = await self._vector.search(
                version, request.query, self._settings.max_fragments
            )
        graph_slice = await self._graph.slice(list(request.lot_ids))
        return Context(
            version=version,
            rules=rules,
            facts=payload.facts,
            reference_data=payload.reference_data,
            graph_slice=graph_slice,
            corpus_fragments=fragments,
        )

    async def get_version(self, version: str) -> VersionInfo:
        infos = await self._repository.list_versions()
        for info in infos:
            if info.version == version:
                return info
        raise KbError(ErrorCode.VERSION_NOT_FOUND, f"version {version} not found")

    async def list_versions(self) -> list[VersionInfo]:
        return await self._repository.list_versions()
