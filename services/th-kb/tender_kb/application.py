import hashlib
import hmac
import logging
from datetime import date
from typing import Protocol
from uuid import UUID, uuid4

from tender_kb.domain import (
    Conflict,
    Context,
    ContextProjection,
    ContextSelection,
    CorpusFragment,
    DependencyUnavailable,
    EvalRequest,
    EvalResult,
    GraphSlice,
    Head,
    IntegrityError,
    Issue,
    Period,
    Snapshot,
    VersionNotFound,
    VersionResult,
    VersionUpload,
    canonical_json,
    integrity_issues,
    matching_rules,
)

_LOG = logging.getLogger(__name__)


class Versions(Protocol):
    async def ready(self) -> None: ...
    async def head(self) -> Head: ...
    async def create(self, payload: VersionUpload) -> Snapshot: ...
    async def get(self, version: str | None) -> Snapshot: ...
    async def stage(self, version: str, projection: UUID) -> Snapshot: ...
    async def reject_draft(self, version: str, report: list[Issue]) -> Snapshot: ...
    async def finish_check(
        self, version: str, baseline: Head, passed: bool, report: list[Issue]
    ) -> tuple[Snapshot, UUID | None]: ...
    async def rollback(self, version: str) -> Snapshot: ...


class Graph(Protocol):
    async def ready(self) -> None: ...
    async def put(self, projection: UUID, graph: GraphSlice) -> None: ...
    async def delete(self, projection: UUID) -> None: ...
    async def slice(self, projection: UUID, lot_ids: tuple[str, ...]) -> GraphSlice: ...


class Corpus(Protocol):
    async def ready(self) -> None: ...
    async def put(self, projection: UUID, fragments: list[CorpusFragment]) -> None: ...
    async def delete(self, projection: UUID) -> None: ...
    async def search(
        self, projection: UUID, selection: ContextSelection, limit: int
    ) -> list[CorpusFragment]: ...


class Gate(Protocol):
    async def check(self, request: EvalRequest) -> EvalResult: ...


class ContextCache(Protocol):
    async def get(self, key: str) -> ContextProjection | None: ...
    async def put(self, key: str, context: ContextProjection) -> None: ...


class Knowledge:
    def __init__(
        self,
        versions: Versions,
        graph: Graph,
        corpus: Corpus,
        gate: Gate,
        cache: ContextCache | None,
        test_set_version: str,
        max_fragments: int,
    ) -> None:
        self._versions = versions
        self._graph = graph
        self._corpus = corpus
        self._gate = gate
        self._cache = cache
        self._test_set_version = test_set_version
        self._max_fragments = max_fragments

    async def ready(self) -> None:
        await self._versions.ready()
        await self._graph.ready()
        await self._corpus.ready()

    async def create(self, payload: VersionUpload) -> VersionResult:
        try:
            matches = hmac.compare_digest(payload.digest(), payload.checksum)
        except ValueError:
            raise IntegrityError("Knowledge snapshot must contain valid JSON values.") from None
        if not matches:
            raise IntegrityError("The canonical payload does not match the uploaded checksum.")
        return (await self._versions.create(payload)).result()

    async def activate(self, version: str) -> VersionResult:
        snapshot = await self._versions.get(version)
        if snapshot.status == "DRAFT":
            issues = integrity_issues(snapshot.payload)
            if issues:
                return (await self._versions.reject_draft(version, issues)).result()
            snapshot = await self._stage(snapshot.payload)
        if snapshot.status in ("ACTIVE", "REJECTED"):
            return snapshot.result()
        if snapshot.status != "CHECK":
            raise Conflict("Use rollback to restore a previously active version.")

        baseline = await self._versions.head()
        check = await self._gate.check(
            EvalRequest(
                candidate_version=version,
                baseline_version=baseline.version,
                test_set_version=self._test_set_version,
                test_cases=snapshot.payload.test_cases,
            )
        )
        report = check.report
        if not check.passed and all(issue.code != "REGRESSION_DETECTED" for issue in report):
            report = [
                Issue(
                    code="REGRESSION_DETECTED", message="The regression gate rejected the version."
                ),
                *report,
            ]
        updated, obsolete = await self._versions.finish_check(
            version, baseline, check.passed, report
        )
        if obsolete is not None:
            await self._discard_projection(obsolete)
        return updated.result()

    async def rollback(self, version: str) -> VersionResult:
        return (await self._versions.rollback(version)).result()

    async def context(
        self, version: str | None, selection: ContextSelection, *, allow_check: bool
    ) -> Context:
        snapshot = await self._versions.get(version)
        if snapshot.status not in ("ACTIVE", "OLD") and not (
            allow_check and snapshot.status == "CHECK"
        ):
            raise VersionNotFound("Knowledge version is unavailable.")
        projection = snapshot.projection
        if projection is None:
            raise DependencyUnavailable("Knowledge projection is unavailable.")

        key = _cache_key(projection, selection, self._max_fragments)
        cache = self._cache if snapshot.status == "ACTIVE" else None
        indexed = await cache.get(key) if cache is not None else None
        if indexed is None:
            indexed = ContextProjection(
                graph_slice=await self._graph.slice(projection, selection.lot_ids),
                corpus_fragments=(
                    await self._corpus.search(projection, selection, self._max_fragments)
                    if snapshot.payload.corpus_fragments
                    else []
                ),
            )
            if cache is not None:
                await cache.put(key, indexed)
        return Context(
            version=snapshot.payload.version,
            rules=[
                rule
                for rule in matching_rules(snapshot.payload.rules, selection)
                if _in_effect(rule, selection.as_of)
            ],
            facts=[fact for fact in snapshot.payload.facts if _in_effect(fact, selection.as_of)],
            reference_data=snapshot.payload.reference_data,
            graph_slice=indexed.graph_slice,
            corpus_fragments=indexed.corpus_fragments,
        )

    async def _stage(self, payload: VersionUpload) -> Snapshot:
        projection = uuid4()
        try:
            await self._graph.put(projection, payload.graph_slice)
            await self._corpus.put(projection, payload.corpus_fragments)
        except DependencyUnavailable:
            await self._discard_projection(projection)
            raise
        # If the commit result is unknown, this projection may already be referenced.
        snapshot = await self._versions.stage(payload.version, projection)
        if snapshot.projection != projection:
            await self._discard_projection(projection)
        return snapshot

    async def _discard_projection(self, projection: UUID) -> None:
        for backend in (self._graph, self._corpus):
            try:
                await backend.delete(projection)
            except DependencyUnavailable:
                _LOG.warning("Could not clear %s projection %s", type(backend).__name__, projection)


def _cache_key(projection: UUID, selection: ContextSelection, limit: int) -> str:
    digest = hashlib.sha256(canonical_json(selection.model_dump(mode="json"))).hexdigest()
    return f"th-kb:context:v1:{projection}:{limit}:{digest}"


def _in_effect(item: Period, as_of: date | None) -> bool:
    if as_of is None:
        return True
    return (item.valid_from is None or item.valid_from <= as_of) and (
        item.valid_until is None or as_of <= item.valid_until
    )
