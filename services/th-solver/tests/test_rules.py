from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from contracts.agent_task import AgentResult
from contracts.docintel_v1 import ExtractedRequirement
from contracts.models import Estimate, Recommendation
from pydantic import JsonValue

from tender_solver.models import SolveRequest, SupplyConfirmation
from tender_solver.rules import solve


@pytest.fixture
def request_data() -> SolveRequest:
    return SolveRequest.model_validate(
        {
            "job_id": str(uuid4()),
            "kb_version": "kb-test-1",
            "trace_id": str(uuid4()),
            "requirements": [
                {
                    "requirement_id": str(uuid4()),
                    "text": "Five years of experience",
                    "kind": "qualification",
                    "mandatory": True,
                    "value": 5,
                    "unit": "years",
                    "source": {
                        "file_id": str(uuid4()),
                        "page": 1,
                        "block_id": str(uuid4()),
                        "bbox": [0, 0, 1, 1],
                    },
                    "confidence": 1,
                    "needs_review": False,
                    "template_set_version": "v1",
                }
            ],
            "facts": [
                {"fact_id": str(uuid4()), "key": "experience", "value": 6, "source": "profile"}
            ],
            "profile": {
                "regime": "commercial",
                "task_class": "supply",
                "positions": [
                    {
                        "position_id": "p1",
                        "description": "Consumables",
                        "quantity": "10",
                        "available": "10",
                        "unit_cost": "90",
                        "offer_unit_price": "100",
                        "supply_deadline": None,
                        "alternatives": [],
                    }
                ],
            },
            "lot_ids": [],
        }
    )


RULE = {
    "rule_id": "R-07",
    "version": 3,
    "priority": 10,
    "scope": {"kind": "qualification"},
    "condition": {"field": "experience", "op": "gte", "value": 5},
    "consequence": {"status": "MET", "reason": "Experience confirmed"},
}


@pytest.mark.parametrize(
    ("text", "unit", "value", "expected"),
    [
        ("Срок действия предложения не менее 120 рабочих дней", "business_days", "120", "NOT_MET"),
        ("Срок действия предложения не менее 90 рабочих дней", "business_days", "90", "MET"),
        ("Срок поставки не менее 90 рабочих дней", "business_days", "90", "NEEDS_REVIEW"),
        (
            "Срок действия предложения не менее 90 календарных дней",
            "calendar_days",
            "90",
            "NEEDS_REVIEW",
        ),
        ("Дата окончания подачи 14.10.2026", "date", "2026-10-14", "NEEDS_REVIEW"),
    ],
)
def test_extracted_deadline_matches_only_its_own_facts(
    request_data: SolveRequest, text: str, unit: str, value: str, expected: str
) -> None:
    payload = request_data.model_dump(mode="json")
    source = request_data.requirements[0].source.model_dump(mode="json")
    requirement = ExtractedRequirement.model_validate(
        {
            "requirement_id": uuid4(),
            "text": text,
            "kind": "deadline",
            "mandatory": True,
            "value": value,
            "unit": unit,
            "source": source,
            "confidence": 1,
            "needs_review": False,
            "template_set_version": "ts-2026-11-1",
        }
    )
    payload["requirements"] = [requirement.model_dump(mode="json")]
    payload["facts"] = [
        {"fact_id": str(uuid4()), "key": "offer_validity_workdays", "value": 90, "source": "offer"}
    ]
    rule = {
        **RULE,
        "rule_id": "R-DEMO-OFFER-VALIDITY",
        "scope": {"kind": "deadline"},
        "condition": {"field": "offer_validity_workdays", "op": "gte", "value": 60},
    }

    result = solve(SolveRequest.model_validate(payload), {"rules": [rule]}, Decimal(10))

    assert result.compliance[0].status == expected
    assert result.requirements[0].source.model_dump(mode="json") == source


def test_ready_with_threshold_equality(request_data: SolveRequest) -> None:
    result = solve(request_data, {"rules": [RULE]}, Decimal(10))
    assert result.status == "READY"
    assert result.compliance[0].status == "MET"
    assert result.explanation.rule_trace == [{"rule_id": "R-07", "version": 3, "source": "profile"}]
    assert result.stop_reasons == []


def test_conflicting_rules_need_review(request_data: SolveRequest) -> None:
    opposing = {
        **RULE,
        "rule_id": "R-08",
        "consequence": {"status": "NOT_MET", "reason": "Conflict"},
    }
    result = solve(request_data, {"rules": [RULE, opposing]}, Decimal(10))
    assert result.status == "INCOMPLETE"
    assert result.stop_reasons == ["UNCLEAR_REQUIREMENTS"]


def test_resource_gap_and_low_margin(request_data: SolveRequest) -> None:
    request_data.profile.positions[0].available = Decimal(2)
    result = solve(request_data, {"rules": [RULE]}, Decimal(11))
    assert result.resources[0].shortage == "8"
    assert result.stop_reasons == ["LOW_MARGIN", "RESOURCE_GAP"]


def test_loss_is_an_automatic_refusal(request_data: SolveRequest) -> None:
    request_data.profile.positions[0].unit_cost = Decimal(105)
    request_data.agent_results = [
        AgentResult(
            task_id=uuid4(),
            job_id=request_data.job_id,
            agent_role="estimate",
            status="NEEDS_REVIEW",
            result={},
            evidence=[],
            model_calls=[],
            trace_id=request_data.trace_id,
        )
    ]

    result = solve(request_data, {"rules": [RULE]}, Decimal(10))

    assert result.status == "NO_SOLUTION"
    assert result.stop_reasons == ["NO_SOLUTION"]
    assert isinstance(result.recommendation, Recommendation)
    assert result.recommendation.value == "NOT_PARTICIPATE"
    assert isinstance(result.estimate, Estimate)
    assert result.estimate.total_cost - result.estimate.offer_price == Decimal(50)


def test_exact_total_offer_takes_precedence_over_a_rounded_unit_price(
    request_data: SolveRequest,
) -> None:
    position = request_data.profile.positions[0]
    position.quantity = Decimal(3)
    position.unit_cost = Decimal("33.33333333333333333333333334")
    position.offer_unit_price = Decimal(100) / position.quantity
    position.offer_total_price = Decimal(100)
    assert position.offer_unit_price * position.quantity < 100

    exact = solve(request_data, {"rules": [RULE]}, Decimal(10))
    position.offer_total_price = None
    rounded = solve(request_data, {"rules": [RULE]}, Decimal(10))

    assert isinstance(exact.estimate, Estimate)
    assert exact.estimate.offer_price == Decimal(100)
    assert exact.estimate.margin_percent == 0
    assert exact.stop_reasons == ["LOW_MARGIN"]
    assert rounded.status == "NO_SOLUTION"


@pytest.mark.parametrize(
    ("quantity", "delivery", "cost", "shortage", "total_cost", "status", "reasons"),
    [
        (8, date(2026, 12, 2), 20, "0", 920, "READY", ["LOW_MARGIN"]),
        (8, date(2026, 12, 3), 20, "8", 900, "NO_SOLUTION", ["NO_SOLUTION"]),
        (7, date(2026, 12, 1), 20, "1", 920, "NO_SOLUTION", ["LOW_MARGIN", "NO_SOLUTION"]),
        (8, date(2026, 12, 1), 101, "0", 1001, "NO_SOLUTION", ["NO_SOLUTION"]),
    ],
)
def test_confirmed_supply_changes_coverage_and_margin(
    request_data: SolveRequest,
    quantity: int,
    delivery: date,
    cost: int,
    shortage: str,
    total_cost: int,
    status: str,
    reasons: list[str],
) -> None:
    position = request_data.profile.positions[0]
    position.available = Decimal(2)
    position.supply_deadline = datetime(2026, 12, 2, tzinfo=UTC)
    position.supply_confirmation = SupplyConfirmation(
        quantity=Decimal(quantity), delivery_date=delivery, additional_cost=Decimal(cost)
    )
    request_data.agent_results = [
        AgentResult(
            task_id=uuid4(),
            job_id=request_data.job_id,
            agent_role="resource",
            status="NEEDS_REVIEW",
            result={},
            evidence=[],
            model_calls=[],
            trace_id=request_data.trace_id,
        )
    ]

    result = solve(request_data, {"rules": [RULE]}, Decimal(10))

    assert result.resources[0].covered is (shortage == "0")
    assert result.resources[0].shortage == shortage
    assert isinstance(result.estimate, Estimate)
    assert result.estimate.total_cost == total_cost
    row = result.estimate.positions[0]
    assert isinstance(row, dict)
    assert row["cost"] == str(total_cost)
    assert result.status == status
    assert result.stop_reasons == reasons


def test_mandatory_requirement_refuses_even_when_profitable(request_data: SolveRequest) -> None:
    failure = {**RULE, "consequence": {"status": "NOT_MET", "reason": "Mandatory rule"}}

    result = solve(request_data, {"rules": [failure]}, Decimal(10))

    assert result.compliance[0].status == "NOT_MET"
    assert result.status == "NO_SOLUTION"
    assert isinstance(result.recommendation, Recommendation)
    assert result.recommendation.value == "NOT_PARTICIPATE"


def test_approved_high_risk_remains_in_explanation_without_blocking(
    request_data: SolveRequest,
) -> None:
    request_data.agent_results = [
        AgentResult(
            task_id=uuid4(),
            job_id=request_data.job_id,
            agent_role="risk",
            status="DONE",
            result={
                "summary": "Penalty above profile threshold",
                "risk_level": "high",
                "human_reason": "penalty_above_threshold",
            },
            evidence=[{"decision": "APPROVED", "comment": "Capped at the contract value"}],
            model_calls=[],
            trace_id=request_data.trace_id,
        )
    ]

    result = solve(request_data, {"rules": [RULE]}, Decimal(10))

    assert isinstance(result.recommendation, Recommendation)
    assert result.recommendation.value == "PARTICIPATE"
    assert result.risks[0].level == "high"
    assert result.risks[0].source_ref == request_data.agent_results[0].evidence
    assert result.stop_reasons == []


def test_no_positions_has_no_solution(request_data: SolveRequest) -> None:
    request_data.profile.positions.clear()
    result = solve(request_data, {"rules": [RULE]}, Decimal(10))
    assert result.status == "NO_SOLUTION"
    assert "NO_SOLUTION" in result.stop_reasons


def test_regime_scope_and_stable_knowledge_fact_reference(request_data: SolveRequest) -> None:
    request_data.facts.clear()
    rule = {**RULE, "scope": {"kind": None, "regime": "commercial", "task_class": None}}
    context: dict[str, JsonValue] = {
        "rules": [rule],
        "facts": [
            {
                "fact_id": "registry-experience",
                "key": "experience",
                "value": 6,
                "value_type": "number",
                "source": "registry",
                "valid_from": "2026-01-01",
                "valid_until": "2026-12-31",
            }
        ],
        "corpus_fragments": [
            {
                "fragment_id": "experience-entry",
                "text": "Confirmed six years of experience",
                "source": "registry",
                "date": "2026-11-25",
                "lot_ids": [],
            }
        ],
    }
    result = solve(request_data, context, Decimal(10))
    assert result.status == "READY"
    assert result.compliance[0].fact_ref is not None
    assert result.explanation.rule_trace == [
        {"rule_id": "R-07", "version": 3, "source": "registry", "fact_id": "registry-experience"},
    ]
    assert result.explanation.source_refs == context["corpus_fragments"]


def test_number_and_numeric_string_do_not_match_same_condition(request_data: SolveRequest) -> None:
    string_rule = {
        **RULE,
        "rule_id": "R-string",
        "condition": {"field": "experience", "op": "eq", "value": "6"},
        "consequence": {"status": "NOT_MET", "reason": "This value is a string."},
    }
    result = solve(request_data, {"rules": [RULE, string_rule]}, Decimal(10))
    assert result.status == "READY"
