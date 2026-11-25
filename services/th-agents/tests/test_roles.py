import asyncio
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from a2a.helpers.proto_helpers import get_data_parts
from a2a.types.a2a_pb2 import Task, TaskState
from docx import Document
from pypdf import PdfReader

from contracts.agent_task import AgentResult, AgentRole
from contracts.models import Artifact
from tender_agents.artifacts import MIME_TYPES
from tender_agents.demo import send
from tests.test_a2a import ORIGIN, TEST_ARTIFACT_TOKEN, MemoryLedger, agent

_SOURCE = {"url": "https://example.invalid/tenders/9", "locator": "Schedule A, row 2"}


def _gateway_unexpected(_: httpx.Request) -> httpx.Response:
    pytest.fail("deterministic agent roles must not contact the model gateway")


def build_task(role: AgentRole, data: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "job_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "agent_role": role,
        "kb_version": "kb-demo-v1",
        "input": data,
    }


def get_result(response: Task) -> AgentResult:
    if response.status.state == TaskState.TASK_STATE_FAILED:
        return AgentResult.model_validate(get_data_parts(response.status.message.parts)[0])
    assert response.status.state == TaskState.TASK_STATE_COMPLETED
    return AgentResult.model_validate(get_data_parts(response.artifacts[0].parts)[0])


def requirement() -> dict[str, object]:
    return {
        "requirement_id": str(uuid4()),
        "text": "Deliver 8 replacement valves",
        "kind": "resource",
        "mandatory": True,
        "value": 8,
        "unit": "valves",
        "source": {
            "file_id": str(uuid4()),
            "page": 2,
            "block_id": str(uuid4()),
            "bbox": [1, 2, 3, 4],
        },
        "confidence": 0.93,
        "needs_review": False,
        "template_set_version": "normalized-v1",
    }


@pytest.mark.parametrize(
    ("role", "data", "field", "status"),
    [
        ("extraction", {"requirements": [requirement()]}, "requirements", "DONE"),
        (
            "legal",
            {
                "regime": "own_funds",
                "regime_source": _SOURCE,
                "clauses": [
                    {"text": "Unclear payment terms", "source": _SOURCE, "requires_review": True}
                ],
            },
            "clauses",
            "NEEDS_REVIEW",
        ),
        (
            "resource",
            {
                "positions": [
                    {
                        "position_id": "valve",
                        "required_quantity": "8",
                        "available_quantity": "5",
                        "source": _SOURCE,
                    }
                ]
            },
            "resources",
            "NEEDS_REVIEW",
        ),
        (
            "estimate",
            {
                "currency": "BYN",
                "offer_price": "100",
                "margin_threshold_percent": "20",
                "threshold_source": _SOURCE,
                "positions": [
                    {"position_id": "valve", "quantity": "8", "unit_cost": "10", "source": _SOURCE}
                ],
            },
            "estimate",
            "DONE",
        ),
        (
            "supply",
            {
                "positions": [
                    {
                        "position_id": "valve",
                        "required_quantity": "8",
                        "delivery_deadline": "2026-12-01T00:00:00Z",
                        "source": _SOURCE,
                        "offers": [
                            {
                                "supplier": "Sample Vendor",
                                "available_quantity": "8",
                                "delivery_at": "2026-12-02T00:00:00Z",
                                "source": _SOURCE,
                            }
                        ],
                    }
                ]
            },
            "positions",
            "NEEDS_REVIEW",
        ),
    ],
)
async def test_role_evidence_and_shared_task_replay(
    role: AgentRole, data: dict[str, object], field: str, status: str
) -> None:
    payload = build_task(role, data)
    ledger = MemoryLedger()
    async with agent(_gateway_unexpected, ledger) as client:
        first, replay = await asyncio.gather(
            send(client, ORIGIN, payload),
            send(client, ORIGIN, {**payload, "trace_id": str(uuid4())}),
        )
        conflicting = await send(client, ORIGIN, {**payload, "kb_version": "other-kb"})
    result = get_result(first)
    assert result == get_result(replay)
    assert result.status == status
    assert result.evidence and result.model_calls == [] and result.result[field]
    assert len(ledger.completions) == 1 and ledger.completions[0].payload == result
    assert get_result(conflicting).result == {"code": "task_id_conflict", "retryable": False}
    value = result.result[field]
    if role == "extraction":
        assert value == data["requirements"]
    if role == "resource":
        assert isinstance(value, list) and isinstance(value[0], dict)
        assert value[0]["shortage"] == "3"
    if role == "estimate":
        assert isinstance(value, dict) and value["margin_percent"] == "20.0000"
    if role == "supply":
        assert isinstance(value, list) and isinstance(value[0], dict)
        assert value[0]["covered"] is False


def document_task() -> dict[str, object]:
    task = build_task("document", {})
    requirement_data = requirement()
    task["input"] = {
        "solver_result": {
            "job_id": task["job_id"],
            "kb_version": task["kb_version"],
            "status": "READY",
            "requirements": [requirement_data],
            "compliance": [
                {
                    "requirement_id": requirement_data["requirement_id"],
                    "status": "MET",
                    "fact_ref": None,
                    "rule_id": "check-coverage",
                    "reason": "Stock verified",
                }
            ],
            "resources": [],
            "estimate": {
                "positions": [
                    {"position_id": "=2+2", "quantity": "8", "unit_cost": "4", "cost": "32"}
                ],
                "total_cost": "32",
                "offer_price": "40",
                "margin_percent": "20",
                "threshold": "10",
                "threshold_source": _SOURCE,
            },
            "risks": [
                {
                    "risk_id": str(uuid4()),
                    "category": "Delivery",
                    "level": "medium",
                    "basis": "Short lead time",
                    "mitigation": "Reserve a second vehicle",
                    "source_ref": _SOURCE,
                }
            ],
            "recommendation": {"value": "PARTICIPATE", "conditions": []},
            "plan": {"steps": ["Confirm stock"], "deadlines": [], "owners": []},
            "explanation": {
                "steps": [],
                "rule_trace": ["lead-time-check"],
                "source_refs": [_SOURCE],
            },
            "artifacts": [],
            "stop_reasons": [],
        },
        "approval": {
            "decision_id": str(uuid4()),
            "job_id": task["job_id"],
            "reason_code": "FINAL_APPROVAL",
            "role": "TENDER_SPECIALIST",
            "question": "Approve the proposal?",
            "deadline": "2026-11-30T18:00:00Z",
            "timeout": False,
            "decision": "APPROVED",
            "comment": "",
            "decided_at": "2026-11-30T17:00:00Z",
        },
    }
    return task


async def test_document_requires_trusted_final_approval_and_renders_valid_files() -> None:
    payload = document_task()
    ledger = MemoryLedger()
    async with agent(_gateway_unexpected, ledger) as client:
        unauthenticated = get_result(await send(client, ORIGIN, payload))
        assert unauthenticated.result["code"] == "document_unauthorized"
        assert ledger.completions == []
        client.headers["Authorization"] = f"Bearer {TEST_ARTIFACT_TOKEN}"

        invalid_payload = {**payload, "task_id": str(uuid4())}
        data = payload["input"]
        assert isinstance(data, dict)
        invalid_payload["input"] = {
            **data,
            "approval": {**data["approval"], "decision": "REJECTED"},
        }
        rejected = get_result(await send(client, ORIGIN, invalid_payload))
        assert rejected.result["code"] == "approval_required"

        completed = get_result(await send(client, ORIGIN, payload))
        duplicate = get_result(await send(client, ORIGIN, payload))
        assert completed == duplicate and completed.status == "DONE"
        assert len(ledger.completions) == 2
        assert ledger.completions[1].payload == completed

        references = completed.result["artifacts"]
        assert isinstance(references, list) and len(references) == 3
        expected_mime = {
            "DOCX": MIME_TYPES["docx"],
            "PDF": MIME_TYPES["pdf"],
            "XLSX": MIME_TYPES["xlsx"],
        }
        for reference in references:
            artifact = Artifact.model_validate(reference)
            response = await client.get(ORIGIN + artifact.file_path)
            assert response.status_code == 200
            assert response.headers["Content-Type"] == expected_mime[artifact.kind]
            assert sha256(response.content).hexdigest() == artifact.checksum
            assert artifact.kb_version == payload["kb_version"]
            if artifact.kind == "DOCX":
                doc = Document(BytesIO(response.content))
                assert doc.tables[0].rows[1].cells[0].text == "Deliver 8 replacement valves"
            elif artifact.kind == "PDF":
                report = PdfReader(BytesIO(response.content))
                assert "Short lead time" in report.pages[0].extract_text()
            else:
                with ZipFile(BytesIO(response.content)) as workbook:
                    assert b"=2+2" in workbook.read("xl/sharedStrings.xml")
                    sheet = workbook.read("xl/worksheets/sheet1.xml")
                    assert b"<f>" not in sheet
                    assert b"<v>32</v>" in sheet
        client.headers.clear()
        blocked = await client.get(ORIGIN + Artifact.model_validate(references[0]).file_path)
        assert blocked.status_code == 401
