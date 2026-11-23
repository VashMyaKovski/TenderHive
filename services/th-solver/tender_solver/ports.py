from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from tender_solver.models import KnowledgeContext, SolverResult


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    summary: str
    confidence: float
    human_reason: str | None
    rule_id: str
    trace_id: UUID


class KnowledgeClient(Protocol):
    async def context(
        self,
        version: str,
        regime: str | None,
        task_class: str | None,
        lot_ids: list[str],
        query: str | None,
    ) -> KnowledgeContext: ...


class RiskClient(Protocol):
    async def assess(
        self,
        fragment: str,
        kb_version: str,
        prompt_version: str,
        daily_percent: Decimal,
        threshold: Decimal,
    ) -> RiskAssessment: ...


class ResultCache(Protocol):
    async def get(self, key: str) -> SolverResult | None: ...

    async def set(self, key: str, result: SolverResult, ttl_seconds: int) -> None: ...
