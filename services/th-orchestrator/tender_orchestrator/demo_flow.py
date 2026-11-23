import asyncio
import json
import smtplib
from argparse import ArgumentParser
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zipfile import ZipFile

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest
from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.agent_task import AgentResult, AgentTask
from contracts.models import Artifact, Document, SolverResult
from tender_orchestrator.models import JobView


class DemoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", extra="ignore")
    smtp_host: str
    smtp_port: int
    recipient: str
    mail_url: HttpUrl
    mail_token: SecretStr
    kb_url: HttpUrl
    kb_version: str
    solver_url: HttpUrl
    agents_url: HttpUrl
    artifact_token: SecretStr
    orchestrator_url: HttpUrl
    service_token: SecretStr
    reviewer_tokens: dict[str, SecretStr]
    timeout_seconds: float = Field(gt=0, le=360)


def _identifier(owner: UUID, scope: str) -> str:
    return str(uuid5(owner, scope))


def _url(origin: HttpUrl, path: str) -> str:
    return str(origin).rstrip("/") + path


def _auth(token: SecretStr) -> dict[str, str]:
    return {"Authorization": "Bearer " + token.get_secret_value()}


def _ok(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code} {response.request.url.path}")
    return response


def _pdf() -> bytes:
    stream = b"BT /F1 12 Tf 30 150 Td (Synthetic delivery terms) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(data)


def send_mail(settings: DemoSettings, external_ref: str) -> None:
    message = EmailMessage()
    message["From"] = "synthetic-sender@example.test"
    message["To"] = settings.recipient
    message["Subject"] = "TenderHive synthetic delivery exercise"
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = external_ref
    message.set_content("Synthetic TenderHive demonstration.")
    message.add_attachment(_pdf(), maintype="application", subtype="pdf", filename="scenario.pdf")
    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.timeout_seconds
    ) as smtp:
        smtp.send_message(message)


async def find_job(http: httpx.AsyncClient, settings: DemoSettings, external_ref: str) -> JobView:
    async with asyncio.timeout(settings.timeout_seconds):
        while True:
            mail = _ok(
                await http.get(
                    _url(settings.mail_url, "/api/v1/jobs"),
                    params={"external_ref": external_ref},
                    headers=_auth(settings.mail_token),
                )
            ).json()
            jobs = [entry for entry in mail["jobs"] if entry["published"]]
            if len(jobs) > 1:
                raise RuntimeError("Duplicate jobs for the same demonstration email")
            if jobs:
                response = await http.get(
                    _url(settings.orchestrator_url, f"/api/v1/jobs/{jobs[0]['job_id']}"),
                    headers=_auth(settings.service_token),
                )
                if response.status_code != 404:
                    return JobView.model_validate_json(_ok(response).content)
            await asyncio.sleep(1)


async def agent(
    http: httpx.AsyncClient,
    settings: DemoSettings,
    view: JobView,
    role: str,
    payload: dict[str, object],
) -> AgentResult:
    job = view.job
    task_id = uuid5(job.job_id, f"{view.execution_id}:{role}")
    trace = job.trace_id or uuid5(job.job_id, "trace")
    task = AgentTask.model_validate(
        {
            "task_id": task_id,
            "job_id": job.job_id,
            "agent_role": role,
            "kb_version": job.kb_version,
            "input": payload,
            "trace_id": trace,
        }
    )
    if role == "document":
        async with httpx.AsyncClient(
            headers=_auth(settings.artifact_token),
            timeout=settings.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as authorized:
            return await _send_agent(authorized, settings.agents_url, task)
    return await _send_agent(http, settings.agents_url, task)


async def _send_agent(http: httpx.AsyncClient, origin: HttpUrl, task: AgentTask) -> AgentResult:
    factory = ClientFactory(ClientConfig(httpx_client=http, streaming=False))
    client = await factory.create_from_url(str(origin))
    try:
        command = SendMessageRequest(
            message=new_data_message(
                task.model_dump(mode="json"), media_type="application/json", role=Role.ROLE_USER
            )
        )
        async for response in client.send_message(command):
            if response.HasField("task"):
                for artifact in response.task.artifacts:
                    for part in get_data_parts(artifact.parts):
                        result = AgentResult.model_validate(part)
                        if result.task_id == task.task_id and result.job_id == task.job_id:
                            return result
    finally:
        await client.close()
    raise RuntimeError(f"Agent {task.agent_role} did not return a correlated result")


def _inputs(view: JobView) -> dict[str, object]:
    job = view.job
    document = next(item for item in job.documents if isinstance(item, Document))
    item = {
        "requirement_id": _identifier(job.job_id, "requirement"),
        "text": "At least five years of delivery experience",
        "kind": "qualification",
        "mandatory": True,
        "value": 5,
        "unit": "years",
        "source": {
            "file_id": str(document.file_id),
            "page": 1,
            "block_id": _identifier(job.job_id, "block"),
            "bbox": [0, 0, 1, 1],
        },
        "confidence": 1,
        "needs_review": False,
        "template_set_version": "demo-v1",
    }
    return {
        "job_id": str(job.job_id),
        "kb_version": job.kb_version,
        "trace_id": str(job.trace_id or uuid5(job.job_id, "trace")),
        "as_of": job.created_at.date().isoformat(),
        "lot_ids": job.lot_ids,
        "requirements": [item],
        "facts": [
            {
                "fact_id": _identifier(job.job_id, "fact"),
                "key": "experience",
                "value": 6,
                "source": document.storage_path,
            }
        ],
        "profile": {
            "regime": "commercial",
            "task_class": "supply",
            "positions": [
                {
                    "position_id": "paper",
                    "description": "Printer paper",
                    "quantity": "10",
                    "available": "12",
                    "unit_cost": "8",
                    "offer_unit_price": "10",
                    "supply_deadline": None,
                    "alternatives": [],
                }
            ],
        },
    }


async def _post_job(
    http: httpx.AsyncClient,
    settings: DemoSettings,
    view: JobView,
    route: str,
    payload: dict[str, object],
    token: SecretStr | None = None,
) -> JobView:
    response = await http.post(
        _url(settings.orchestrator_url, f"/api/v1/jobs/{view.job.job_id}/{route}"),
        headers=_auth(token or settings.service_token),
        json=payload,
    )
    return JobView.model_validate_json(_ok(response).content)


async def advance(
    http: httpx.AsyncClient,
    settings: DemoSettings,
    view: JobView,
    approve: bool,
    output: Path,
) -> JobView:
    job = view.job
    document = next(item for item in job.documents if isinstance(item, Document))
    trace = job.trace_id or uuid5(job.job_id, "trace")
    source = {"url": document.storage_path, "locator": "Synthetic normalized text, page 1"}
    inputs = _inputs(view)
    if view.stage == "extract" and view.phase == "accepted":
        attachment = _ok(
            await http.get(
                _url(settings.mail_url, f"/api/v1/documents/{document.file_id}"),
                headers=_auth(settings.mail_token),
            )
        ).content
        if sha256(attachment).hexdigest() != document.checksum or attachment != _pdf():
            raise RuntimeError("The prepared fixture must match the received synthetic PDF")
        extracted = await agent(
            http, settings, view, "extraction", {"requirements": inputs["requirements"]}
        )
        if extracted.status != "DONE":
            raise RuntimeError("Prepared requirements could not be validated")
        view = await _post_job(
            http,
            settings,
            view,
            "risk-input",
            {
                "execution_id": str(view.execution_id),
                "risk": {
                    "kind": "risk_task.v1",
                    "task_id": _identifier(job.job_id, "risk"),
                    "job_id": str(job.job_id),
                    "fragment_id": _identifier(job.job_id, "contract"),
                    "source": source,
                    "fragment_text": "The fictional contract includes a daily late delivery penalty.",
                    "fragment_type": "synthetic_text",
                    "rules_version": job.kb_version,
                    "penalty_daily_percent": "0.25",
                    "penalty_daily_threshold_percent": "0.1",
                    "prompt_version": "v1",
                    "trace_id": str(trace),
                },
            },
        )
    for _ in range(10):
        card = next((item for item in view.cards if item.state == "open"), None)
        if card is not None:
            if not approve:
                return view
            view = await _post_job(
                http,
                settings,
                view,
                f"cards/{card.card_id}/decision",
                {
                    "decision_id": _identifier(card.card_id, "demo-approval"),
                    "decision": "APPROVED",
                    "comment": "Approved in synthetic demo.",
                },
                settings.reviewer_tokens[card.role],
            )
        elif view.stage == "solve" and view.phase == "ready_for_next_stage":
            more = await asyncio.gather(
                agent(
                    http,
                    settings,
                    view,
                    "legal",
                    {
                        "regime": "commercial",
                        "regime_source": source,
                        "clauses": [
                            {
                                "text": "Daily delay penalty",
                                "source": source,
                                "requires_review": False,
                            }
                        ],
                    },
                ),
                agent(
                    http,
                    settings,
                    view,
                    "resource",
                    {
                        "positions": [
                            {
                                "position_id": "paper",
                                "required_quantity": "10",
                                "available_quantity": "12",
                                "source": source,
                            }
                        ],
                    },
                ),
                agent(
                    http,
                    settings,
                    view,
                    "estimate",
                    {
                        "currency": "BYN",
                        "offer_price": "100",
                        "margin_threshold_percent": "10",
                        "threshold_source": source,
                        "positions": [
                            {
                                "position_id": "paper",
                                "quantity": "10",
                                "unit_cost": "8",
                                "source": source,
                            }
                        ],
                    },
                ),
                agent(
                    http,
                    settings,
                    view,
                    "supply",
                    {
                        "positions": [
                            {
                                "position_id": "paper",
                                "required_quantity": "10",
                                "delivery_deadline": "2099-12-20T00:00:00Z",
                                "source": source,
                                "offers": [
                                    {
                                        "supplier": "Synthetic supplier",
                                        "available_quantity": "12",
                                        "delivery_at": "2099-12-10T00:00:00Z",
                                        "source": source,
                                    }
                                ],
                            }
                        ],
                    },
                ),
            )
            if any(item.status == "FAILED" for item in more):
                raise RuntimeError("An A2A specialist failed")
            outcomes = [item.model_dump(mode="json") for item in more]
            previous_risk = view.risk_result
            if previous_risk is not None:
                outcomes.append(
                    {
                        "task_id": str(previous_risk.task_id),
                        "job_id": str(job.job_id),
                        "agent_role": "risk",
                        "status": "DONE",
                        "trace_id": str(previous_risk.trace_id),
                        "result": {
                            "summary": previous_risk.summary,
                            "risk_level": previous_risk.risk_level,
                            "reviewed_reason": previous_risk.human_reason,
                        },
                        "evidence": [
                            item.model_dump(mode="json") for item in previous_risk.sources
                        ],
                        "model_calls": [
                            {
                                "trace_id": str(previous_risk.trace_id),
                                "model_version": previous_risk.model_version,
                                "prompt_version": previous_risk.prompt_version,
                            }
                        ],
                    }
                )
            inputs["agent_results"] = outcomes
            answer = _ok(
                await http.post(
                    _url(settings.solver_url, "/api/v1/solve"),
                    json=inputs,
                    headers={"X-Trace-Id": str(trace)},
                )
            )
            result = SolverResult.model_validate_json(answer.content)
            view = await _post_job(
                http,
                settings,
                view,
                "solver-result",
                {
                    "execution_id": str(view.execution_id),
                    "result": result.model_dump(mode="json"),
                },
            )
        elif view.phase == "awaiting_artifacts" and view.solver_result is not None:
            approval = next(
                item.decision
                for item in reversed(view.cards)
                if item.reason_code == "FINAL_APPROVAL" and item.decision is not None
            )
            pack = await agent(
                http,
                settings,
                view,
                "document",
                {
                    "solver_result": view.solver_result.model_dump(mode="json"),
                    "approval": approval.model_dump(mode="json"),
                },
            )
            files = pack.result.get("artifacts")
            if pack.status != "DONE" or not isinstance(files, list):
                raise RuntimeError("Document agent failed")
            for raw in files:
                item = Artifact.model_validate(raw)
                response = _ok(
                    await http.get(
                        _url(settings.agents_url, item.file_path),
                        headers=_auth(settings.artifact_token),
                    )
                )
                _verify_content(item, response.content)
                await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    (output / f"offer.{item.kind.lower()}").write_bytes, response.content
                )
            view = await _post_job(
                http,
                settings,
                view,
                "artifacts",
                {
                    "execution_id": str(view.execution_id),
                    "artifacts": files,
                },
            )
        else:
            return view
    raise RuntimeError("Unexpected number of workflow transitions")


def _verify_content(artifact: Artifact, body: bytes) -> None:
    if sha256(body).hexdigest() != artifact.checksum:
        raise ValueError("Artifact checksum mismatch")
    if artifact.kind == "PDF":
        if not body.startswith(b"%PDF-") or not body.rstrip().endswith(b"%%EOF"):
            raise ValueError("Invalid PDF")
    else:
        expected = "word/document.xml" if artifact.kind == "DOCX" else "xl/workbook.xml"
        with ZipFile(BytesIO(body)) as packaged:
            if packaged.testzip() is not None or not packaged.read(expected):
                raise ValueError("Invalid Open XML artifact")


async def seed(http: httpx.AsyncClient, settings: DemoSettings) -> None:
    version = settings.kb_version
    context = _url(settings.kb_url, "/api/v1/context")
    if (await http.get(context, params={"version": version})).status_code == 200:
        return
    identifier = uuid5(NAMESPACE_URL, f"tenderhive-demo:{version}")
    job = {
        "job_id": str(identifier),
        "idempotency_key": str(identifier),
        "source": "mail",
        "created_at": "2026-01-01T00:00:00Z",
        "status": "ACCEPTED",
        "kb_version": version,
        "lot_ids": [],
        "profile_ref": "demo-profile",
        "documents": [
            {
                "file_id": _identifier(identifier, "seed-document"),
                "name": "scenario.pdf",
                "mime": "application/pdf",
                "checksum": sha256(_pdf()).hexdigest(),
                "storage_path": _url(
                    settings.mail_url,
                    "/api/v1/documents/" + _identifier(identifier, "seed-document"),
                ),
            }
        ],
    }
    view = JobView.model_validate(
        {
            "job": job,
            "phase": "accepted",
            "risk_attempts": 0,
            "risk_result": None,
            "failure": None,
            "cards": [],
        }
    )
    body = {
        "version": version,
        "rules": [
            {
                "rule_id": "R-DEMO-EXPERIENCE",
                "version": 1,
                "priority": 50,
                "condition": {"field": "experience", "op": "gte", "value": 5},
                "consequence": {
                    "status": "MET",
                    "reason": "Experience confirmed by the synthetic profile.",
                },
                "scope": {"kind": "qualification", "regime": "commercial", "task_class": "supply"},
            }
        ],
        "facts": [],
        "reference_data": {},
        "graph_slice": {"nodes": [{"node_id": "demo-lot", "kind": "lot"}], "edges": []},
        "corpus_fragments": [
            {
                "fragment_id": "demo-corpus",
                "text": "Qualification requires verified delivery experience.",
                "source": "Synthetic procurement brief",
                "date": "2026-01-01",
            }
        ],
        "test_cases": [
            {
                "case_id": "qualification",
                "input": _inputs(view),
                "expected": {"status": "READY", "stop_reasons": [], "compliance": ["MET"]},
            }
        ],
    }
    digest = sha256(
        json.dumps(
            body,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    _ok(
        await http.post(
            _url(settings.kb_url, "/api/v1/versions"), json={**body, "checksum": digest}
        )
    )
    activation = _ok(
        await http.post(_url(settings.kb_url, f"/api/v1/versions/{version}/activate"))
    ).json()
    if activation["status"] != "ACTIVE":
        raise RuntimeError("Synthetic knowledge version did not pass evaluation")


if __name__ == "__main__":
    parser = ArgumentParser(description="Drive the synthetic mail demonstration")
    parser.add_argument("--external-ref", default=None)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--resend", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-kb", action="store_true")
    args = parser.parse_args()
    external_ref = args.external_ref or f"<{uuid4()}@example.test>"
    settings = DemoSettings.model_validate({})

    async def main() -> None:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False) as http:
            if args.seed_kb:
                await seed(http, settings)
                print(json.dumps({"kb_version": settings.kb_version, "status": "ACTIVE"}))
                return
            if args.output is None:
                parser.error("--output is required for a workflow run")
            if args.external_ref is None or args.resend:
                await asyncio.to_thread(send_mail, settings, external_ref)
            view = await find_job(http, settings, external_ref)
            final = await advance(http, settings, view, args.approve, args.output)
        print(
            json.dumps(
                {
                    "external_ref": external_ref,
                    "job_id": str(final.job.job_id),
                    "trace_id": str(final.job.trace_id),
                    "status": final.job.status,
                    "phase": final.phase,
                    "kb_version": final.job.kb_version,
                    "checksums": [item.checksum for item in final.artifacts],
                }
            )
        )

    asyncio.run(main())
