from decimal import Decimal
from uuid import uuid4

import pytest

from tender_solver.config import Settings
from tender_solver.errors import ErrorCode, SolverError
from tender_solver.models import FactInput, SolveRequest
from tender_solver.solver import Solver
from tests.conftest import FakeKnowledge, FakeRisk, MemoryCache, context, requirement, rule


def build_solver(
    settings: Settings,
    knowledge: FakeKnowledge | None = None,
    risk: FakeRisk | None = None,
) -> tuple[Solver, FakeKnowledge, FakeRisk, MemoryCache]:
    knowledge = knowledge or FakeKnowledge(context())
    risk = risk or FakeRisk()
    cache = MemoryCache()
    return (
        Solver(settings, knowledge, risk, cache, settings.confidence_floor),
        knowledge,
        risk,
        cache,
    )


def request(**kwargs: object) -> SolveRequest:
    defaults = {
        "job_id": uuid4(),
        "kb_version": "kb-v1",
        "requirements": [requirement()],
        "facts": [FactInput(key="margin_percent", value=12)],
        "profile": {},
        "lot_ids": [],
        "trace_id": uuid4(),
        "regime": None,
        "task_class": None,
    }
    defaults.update(kwargs)
    return SolveRequest.model_validate(defaults)


async def test_ready_result_and_cache(settings: Settings) -> None:
    solver, knowledge, risk, cache = build_solver(settings, FakeKnowledge(context(rules=[rule()])))
    payload = request()
    result = await solver.solve(payload)

    assert result.status == "READY"
    assert result.compliance[0].status == "MET"
    assert result.recommendation == "PARTICIPATE"
    assert result.stop_reasons == []
    assert [artifact.kind for artifact in result.artifacts] == ["DOCX", "PDF", "XLSX"]
    assert cache.items
    assert risk.calls == 0

    cached = await solver.solve(payload)
    assert cached == result
    assert knowledge.calls == 1


async def test_low_margin_escalates(settings: Settings) -> None:
    always_matches = rule().model_copy(update={"condition": "margin_percent >= 0"})
    solver, _, _, _ = build_solver(settings, FakeKnowledge(context(rules=[always_matches])))
    result = await solver.solve(request(facts=[FactInput(key="margin_percent", value=8)]))

    assert result.status == "INCOMPLETE"
    assert result.recommendation == "PARTICIPATE_WITH_CONDITIONS"
    assert [stop.code for stop in result.stop_reasons] == ["LOW_MARGIN"]


async def test_conflicting_rules_are_not_resolved_silently(settings: Settings) -> None:
    solver, _, _, _ = build_solver(
        settings,
        FakeKnowledge(
            context(
                rules=[rule("R-12", "LOW_MARGIN"), rule("R-15", "MET")],
                facts=[FactInput(key="margin_percent", value=12)],
            )
        ),
    )
    result = await solver.solve(request())
    assert any(stop.code == "INCOMPATIBLE_RULES" for stop in result.stop_reasons)
    assert result.status == "INCOMPLETE"


async def test_penalty_risk_uses_gateway(settings: Settings) -> None:
    solver, _, risk, _ = build_solver(
        settings,
        FakeKnowledge(context(rules=[rule()])),
        FakeRisk(reason="penalty_above_threshold", level="high"),
    )
    result = await solver.solve(
        request(
            requirements=[requirement(kind="penalty", value="0,15 % в день")],
            facts=[FactInput(key="margin_percent", value=12)],
        )
    )
    assert risk.calls == 1
    assert result.risks[0].level == "high"
    assert any(stop.code == "UNCLEAR_REQUIREMENTS" for stop in result.stop_reasons)


async def test_docintel_requirement_contract_is_accepted(settings: Settings) -> None:
    penalty_rule = rule().model_copy(update={"scope": "penalty"})
    solver, _, risk, _ = build_solver(
        settings, FakeKnowledge(context(rules=[penalty_rule])), FakeRisk(level="low")
    )
    source_block_id = uuid4()
    docintel_requirement = {
        "requirement_id": str(uuid4()),
        "text": "штраф 0,15 % в день, но не более 10 %",
        "kind": "penalty",
        "mandatory": True,
        "value": "0.0015",
        "unit": "fraction_per_day",
        "source": {
            "file_id": str(uuid4()),
            "page": 7,
            "block_id": str(source_block_id),
            "bbox": [10.0, 20.0, 400.0, 35.0],
        },
        "confidence": 0.95,
        "needs_review": False,
        "template_set_version": "ts-2026-11-1",
    }

    result = await solver.solve(
        request(
            requirements=[docintel_requirement],
            facts=[FactInput(key="margin_percent", value=12)],
        )
    )

    assert result.status == "READY"
    assert risk.calls == 1
    assert risk.daily_percents == [Decimal("0.1500")]
    assert result.requirements[0].source_ref.endswith(f"block:{source_block_id}")


async def test_unknown_version_is_rejected(settings: Settings) -> None:
    solver, _, _, _ = build_solver(settings)
    with pytest.raises(SolverError) as error:
        await solver.solve(request(kb_version="kb-v9"))
    assert error.value.code is ErrorCode.KB_VERSION_NOT_FOUND


async def test_empty_input_is_no_solution(settings: Settings) -> None:
    solver, _, _, _ = build_solver(settings)
    with pytest.raises(SolverError) as error:
        await solver.solve(request(requirements=[], facts=[]))
    assert error.value.code is ErrorCode.NO_SOLUTION
