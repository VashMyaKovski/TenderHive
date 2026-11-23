from decimal import Decimal
from uuid import uuid4

import pytest

from tender_solver.models import SolveRequest
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


def test_no_positions_has_no_solution(request_data: SolveRequest) -> None:
    request_data.profile.positions.clear()
    result = solve(request_data, {"rules": [RULE]}, Decimal(10))
    assert result.status == "NO_SOLUTION"
    assert "NO_SOLUTION" in result.stop_reasons
