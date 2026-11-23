import json

import pytest
from pydantic import BaseModel, ValidationError

from contracts.models import HumanDecision, Job, SolverResult

JOB_ID = "5a07395b-fe93-4f8e-85dd-723799ed45d0"
TRACE_ID = "461360b7-7315-4855-ab7b-e58886161f64"

JOB = {
    "job_id": JOB_ID,
    "idempotency_key": "a" * 64,
    "source": "mail",
    "external_ref": "test-message-id",
    "created_at": "2026-11-22T10:00:00Z",
    "updated_at": "2026-11-22T10:01:00Z",
    "status": "ACCEPTED",
    "kb_version": "1",
    "lot_ids": ["lot-1"],
    "documents": [
        {
            "file_id": "18b3ac87-b2a9-438a-8a4c-e9eab76b5f46",
            "name": "specification.pdf",
            "mime": "application/pdf",
            "checksum": "d" * 64,
            "storage_path": "uploads/specification.pdf",
        }
    ],
    "profile_ref": "profile-1",
    "trace_id": TRACE_ID,
}

RESULT = {
    "job_id": JOB_ID,
    "kb_version": "1",
    "status": "READY",
    "requirements": [],
    "compliance": [],
    "resources": [],
    "estimate": {
        "positions": [],
        "total_cost": "90",
        "offer_price": "100",
        "margin_percent": "10",
        "threshold": "8",
        "threshold_source": "R-12",
    },
    "risks": [],
    "recommendation": {"value": "PARTICIPATE", "conditions": []},
    "plan": {"steps": [], "deadlines": [], "owners": []},
    "explanation": {"steps": [], "rule_trace": [], "source_refs": ["specification.pdf"]},
    "artifacts": [],
    "stop_reasons": ["FINAL_APPROVAL"],
}

DECISION = {
    "decision_id": "8685bc47-d84c-49fe-8d53-6de770d23f6a",
    "job_id": JOB_ID,
    "reason_code": "FINAL_APPROVAL",
    "role": "TENDER_SPECIALIST",
    "question": "Утвердить проект предложения?",
    "deadline": "2026-11-22T12:00:00Z",
    "timeout": False,
    "decision": "APPROVED",
    "comment": "Пакет проверен",
    "decided_at": "2026-11-22T11:00:00Z",
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
        (HumanDecision, DECISION, "decision", "PARTICIPATE"),
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

    locations = {error["loc"] for error in exc_info.value.errors()}
    assert ("documents", 0, "Document", "file_id") in locations
    assert ("documents", 0, "LegacyDocument", "file_id") in locations


def test_job_normalizes_old_status_and_timestamps() -> None:
    job = Job.model_validate(
        {**JOB, "status": "принято", "created_at": "2026-11-22T13:00:00+03:00"}
    )

    assert job.status == "ACCEPTED"
    assert job.model_dump(mode="json")["created_at"] == JOB["created_at"]


def test_solver_rejects_invalid_nonempty_recommendation() -> None:
    with pytest.raises(ValidationError):
        SolverResult.model_validate(
            {**RESULT, "recommendation": {"value": "APPROVED", "conditions": []}}
        )
