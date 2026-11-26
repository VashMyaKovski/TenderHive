from uuid import uuid4

import pytest
from pydantic import ValidationError

from contracts.docintel_v1 import ExtractResponse


def payload() -> dict[str, object]:
    return {
        "job_id": str(uuid4()),
        "requirements": [
            {
                "requirement_id": str(uuid4()),
                "text": "штраф 0,15 % в день",
                "kind": "penalty",
                "mandatory": True,
                "value": "0.0015",
                "unit": "fraction_per_day",
                "source": {
                    "file_id": str(uuid4()),
                    "page": 7,
                    "block_id": str(uuid4()),
                    "bbox": [10.0, 20.0, 400.0, 35.0],
                },
                "confidence": 0.95,
                "needs_review": False,
                "template_set_version": "ts-2026-11-1",
            }
        ],
        "numbers": [],
        "table": {"rows": [], "sum_check": None},
        "status": "OK",
        "status_reason": None,
        "warnings": [],
        "trace_id": str(uuid4()),
    }


def test_docintel_result_contract_round_trips_json() -> None:
    message = ExtractResponse.model_validate(payload())

    assert message.requirements[0].unit == "fraction_per_day"
    assert message.model_json_schema()["title"] == "ExtractResponse"
    assert ExtractResponse.model_validate_json(message.model_dump_json()) == message


def test_docintel_result_rejects_invalid_source_coordinates() -> None:
    invalid = payload()
    requirements = invalid["requirements"]
    assert isinstance(requirements, list)
    requirement = requirements[0]
    assert isinstance(requirement, dict)
    source = requirement["source"]
    assert isinstance(source, dict)
    source["bbox"] = [10.0, 20.0, 5.0, 35.0]

    with pytest.raises(ValidationError):
        ExtractResponse.model_validate(invalid)


def test_docintel_result_rejects_unknown_requirement_kind() -> None:
    invalid = payload()
    requirements = invalid["requirements"]
    assert isinstance(requirements, list)
    requirement = requirements[0]
    assert isinstance(requirement, dict)
    requirement["kind"] = "admit"

    with pytest.raises(ValidationError):
        ExtractResponse.model_validate(invalid)
