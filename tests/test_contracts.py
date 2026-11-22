import json

import pytest
from pydantic import BaseModel, ValidationError

from contracts.models import HumanDecision, Job, SolverResult

JOB_ID = "5a07395b-fe93-4f8e-85dd-723799ed45d0"

JOB = {
    "job_id": JOB_ID,
    "idempotency_key": "a" * 64,
    "source": "mail",
    "created_at": "2026-11-22T10:00:00Z",
    "status": "принято",
    "kb_version": "1",
    "lot_ids": ["lot-1"],
    "documents": [
        {
            "file_id": "18b3ac87-b2a9-438a-8a4c-e9eab76b5f46",
            "name": "specification.pdf",
            "source": "mail",
        }
    ],
    "profile_ref": "profile-1",
}

RESULT = {
    "job_id": JOB_ID,
    "kb_version": "1",
    "status": "готово",
    "requirements": [{"required": True}],
    "compliance": [],
    "resources": {"available": True},
    "estimate": {"currency": "BYN"},
    "risks": [],
    "recommendation": "участвовать",
    "explanation": {"steps": [], "sources": ["specification.pdf"]},
    "artifacts": [],
}

DECISION = {
    "decision_id": "8685bc47-d84c-49fe-8d53-6de770d23f6a",
    "job_id": JOB_ID,
    "reason_code": "final_approval",
    "role": "тендерный специалист",
    "deadline": "2026-11-22T12:00:00Z",
    "timeout": False,
    "decision": "утверждено",
    "comment": "Пакет проверен",
}


@pytest.mark.parametrize(
    ("model", "payload"),
    [(Job, JOB), (SolverResult, RESULT), (HumanDecision, DECISION)],
)
def test_public_contract_round_trips_json(
    model: type[BaseModel], payload: dict[str, object]
) -> None:
    message = model.model_validate_json(json.dumps(payload))

    assert json.loads(message.model_dump_json()) == payload
    assert model.model_json_schema()["title"] == model.__name__


@pytest.mark.parametrize(
    ("model", "payload", "field", "invalid_value"),
    [
        (Job, JOB, "created_at", "2026-11-22T10:00:00"),
        (Job, JOB, "source", "unknown"),
        (SolverResult, RESULT, "status", "unknown"),
        (HumanDecision, DECISION, "reason_code", "unknown"),
        (HumanDecision, DECISION, "deadline", "2026-11-22T12:00:00"),
    ],
)
def test_public_contract_rejects_invalid_field(
    model: type[BaseModel], payload: dict[str, object], field: str, invalid_value: str
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate({**payload, field: invalid_value})

    assert exc_info.value.errors()[0]["loc"] == (field,)


def test_job_rejects_document_without_id() -> None:
    documents = [{"name": "specification.pdf", "source": "mail"}]

    with pytest.raises(ValidationError) as exc_info:
        Job.model_validate({**JOB, "documents": documents})

    assert exc_info.value.errors()[0]["loc"] == ("documents", 0, "file_id")
