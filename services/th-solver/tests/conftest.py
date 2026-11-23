from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from tender_solver.config import Settings
from tender_solver.models import (
    FactInput,
    KnowledgeContext,
    Requirement,
    Rule,
    SolverResult,
)
from tender_solver.ports import RiskAssessment


class FakeKnowledge:
    def __init__(self, context: KnowledgeContext) -> None:
        self.context_data = context
        self.calls = 0

    async def context(
        self,
        version: str,
        regime: str | None,
        task_class: str | None,
        lot_ids: list[str],
        query: str | None,
    ) -> KnowledgeContext:
        self.calls += 1
        return self.context_data


class FakeRisk:
    def __init__(self, reason: str | None = None, level: str = "high") -> None:
        self.reason = reason
        self.level = level
        self.calls = 0

    async def assess(
        self,
        fragment: str,
        kb_version: str,
        prompt_version: str,
        daily_percent: Decimal,
        threshold: Decimal,
    ) -> RiskAssessment:
        self.calls += 1
        return RiskAssessment(
            level=self.level,
            summary=f"penalty {daily_percent}% versus {threshold}%",
            confidence=0.9,
            human_reason=self.reason,
            rule_id="R-RISK-04",
            trace_id=uuid4(),
        )


class MemoryCache:
    def __init__(self) -> None:
        self.items: dict[str, SolverResult] = {}

    async def get(self, key: str) -> SolverResult | None:
        return self.items.get(key)

    async def set(self, key: str, result: SolverResult, ttl_seconds: int) -> None:
        self.items[key] = result


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "kb_url": "http://localhost:8000",
            "modelgw_url": "http://localhost:8001",
            "redis_url": "redis://localhost:6379/1",
            "active_version": "kb-v1",
            "prompt_version": "v1",
            "margin_threshold_percent": "10",
            "penalty_threshold_percent": "0.10",
            "confidence_floor": 0.6,
            "cache_ttl_seconds": 60,
            "request_timeout_seconds": 5,
            "bind_host": "127.0.0.1",
            "port": 8000,
        }
    )


def context(
    rules: list[Rule] | None = None, facts: list[FactInput] | None = None
) -> KnowledgeContext:
    return KnowledgeContext(
        version="kb-v1",
        rules=rules if rules is not None else [],
        facts=facts if facts is not None else [],
        reference_data={},
        graph_slice={},
        corpus_fragments=[],
    )


def rule(rule_id: str = "R-07", consequence: str = "MET") -> Rule:
    return Rule(
        rule_id=rule_id,
        condition="margin_percent >= 10",
        consequence=consequence,
        priority=50,
        scope="qualification",
        version=1,
    )


def requirement(
    requirement_id: UUID | None = None,
    kind: str = "qualification",
    value: str | None = None,
    confidence: float = 0.9,
) -> Requirement:
    return Requirement(
        requirement_id=requirement_id or uuid4(),
        text="The supplier must hold a valid licence.",
        kind=kind,  # type: ignore[arg-type]
        value=value,
        mandatory=True,
        confidence=confidence,
        source_ref="tender.pdf#p7",
    )


def now() -> datetime:
    return datetime(2026, 11, 23, tzinfo=UTC)
