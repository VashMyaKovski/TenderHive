import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import Field, HttpUrl, JsonValue, SecretStr

from contracts.agent_task import AgentResult, ModelCall
from contracts.models import Artifact, Estimate, Job, Recommendation, SolverResult, StopReason
from contracts.risk_task_v1 import Percent, RiskResultV1
from tender_orchestrator.demo_flow import (
    DemoSettings,
    _auth,
    _ok,
    _post_job,
    _url,
    _verify_content,
    agent,
)
from tender_orchestrator.models import JobView, ReviewCard
from tender_orchestrator.workbench_inputs import (
    DEMO_MINIMUM_VALIDITY_WORKDAYS,
    DecisionInput,
    Facts,
    RunInput,
    Upload,
    decimal_text,
    has_readable_text,
)

_LOG = logging.getLogger("uvicorn.error.workbench")
_LABELS = {
    "extraction": "Проверка входных требований",
    "legal": "Проверка условий закупки",
    "resource": "Проверка наличия",
    "estimate": "Расчёт сметы",
    "document": "Подготовка комплекта",
}
_REASONS: dict[StopReason, str] = {
    "PENALTY_ABOVE_THRESHOLD": "Штраф превышает порог демонстрационного профиля.",
    "LOW_MARGIN": "Расчётная маржа ниже порога.",
    "RESOURCE_GAP": "Доступного количества не хватает.",
    "UNCLEAR_REQUIREMENTS": "Одно из требований нуждается в проверке.",
    "NO_SOLUTION": "Решатель не нашёл допустимый вариант.",
    "FINAL_APPROVAL": "Требуется итоговое согласование.",
}


class WorkbenchSettings(DemoSettings):
    web_url: HttpUrl
    web_port: Annotated[int, Field(ge=1, le=65535)]
    web_max_runs: Annotated[int, Field(ge=1, le=64)]
    web_margin_threshold_percent: Percent
    web_penalty_threshold_percent: Percent
    web_profile_ref: str
    web_service_token: Annotated[SecretStr, Field(min_length=32)] | None = None


@dataclass(slots=True)
class Run:
    id: UUID
    title: str
    facts: Facts
    context: str
    uploads: list[Upload]
    normalization: Upload
    view: JobView | None = None
    extraction: AgentResult | None = None
    task: asyncio.Task[None] | None = None
    error: str | None = None
    messages: list[dict[str, JsonValue]] = field(default_factory=list)
    artifacts: dict[str, bytes] = field(default_factory=dict)

    @property
    def card(self) -> ReviewCard | None:
        if self.view is None:
            return None
        return next((card for card in self.view.cards if card.state == "open"), None)

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def phase(self) -> str:
        if self.error is not None:
            return "failed"
        return self.view.phase if self.view is not None else "queued"

    def message(
        self,
        sender: str,
        recipient: str,
        status: str,
        text: str,
        details: dict[str, JsonValue] | None,
    ) -> None:
        self.messages.append(
            {
                "id": str(uuid4()),
                "agent": sender,
                "to": recipient,
                "status": status,
                "text": text,
                "at": datetime.now(UTC).isoformat(),
                "details": details,
            }
        )

    def snapshot(self) -> dict[str, object]:
        card = self.card
        view = self.view
        result = view.solver_result if view is not None else None
        failure = view.failure if view is not None else None
        return {
            "id": str(self.id),
            "title": self.title,
            "status": "ERROR" if self.error else (view.job.status if view else "ACCEPTED"),
            "phase": self.phase,
            "trace_id": str(view.job.trace_id) if view and view.job.trace_id else None,
            "documents": [
                {"name": upload.document.name, "size": len(upload.data)} for upload in self.uploads
            ],
            "messages": self.messages,
            "recommendation": _recommendation(result) if result is not None else None,
            "card": (
                {
                    "id": str(card.card_id),
                    "reason": card.reason_code,
                    "role": card.role,
                    "question": card.question,
                }
                if card and self.error is None and not self.is_running
                else None
            ),
            "artifacts": [
                {"name": name, "url": f"/api/workbench/runs/{self.id}/artifacts/{name}"}
                for name in self.artifacts
            ]
            if self.phase == "done"
            else [],
            "error": self.error or (f"Ошибка этапа ({failure.code})." if failure else None),
        }


def _recommendation(result: SolverResult) -> dict[str, object]:
    recommendation = result.recommendation
    reasons = [_REASONS[reason] for reason in result.stop_reasons]
    for item in result.compliance:
        if item.status == "MET":
            reasons.append("Правило о сроке действия предложения выполнено.")
        else:
            reasons.append(item.reason)
    for item in result.resources:
        if item.covered:
            reasons.append("Доступного количества достаточно для заявки.")
        else:
            reasons.append(f"Недостающее количество: {item.shortage}.")
    if isinstance(result.estimate, Estimate):
        reasons.append(
            f"Расчётная маржа {result.estimate.margin_percent:.2f}%, "
            f"порог {result.estimate.threshold:g}%."
        )
    reasons.extend(item.basis for item in result.risks)
    if not reasons:
        reasons.append("В отчёте решателя нет дополнительных пояснений.")
    return {
        "value": recommendation.value
        if isinstance(recommendation, Recommendation)
        else result.status,
        "status": result.status,
        "reasons": list(dict.fromkeys(reasons)),
        "conditions": recommendation.conditions
        if isinstance(recommendation, Recommendation)
        else [],
    }


def _source(run: Run) -> dict[str, str]:
    return {
        "url": run.normalization.document.storage_path,
        "locator": "Открытый фрагмент и сведения, предоставленные оператором",
    }


def _requirement(run: Run) -> dict[str, JsonValue]:
    return {
        "requirement_id": str(uuid5(run.id, "validity")),
        "text": (
            f"Срок действия предложения от {DEMO_MINIMUM_VALIDITY_WORKDAYS} рабочих дней "
            "по демонстрационному правилу"
        ),
        "kind": "deadline",
        "mandatory": True,
        "value": DEMO_MINIMUM_VALIDITY_WORKDAYS,
        "unit": "рабочих дней",
        "source": {
            "file_id": str(run.normalization.document.file_id),
            "page": 1,
            "block_id": str(uuid5(run.id, "normalized-input")),
            "bbox": [0, 0, 0, 0],
        },
        "confidence": 1,
        "needs_review": False,
        "template_set_version": "demo-v1",
    }


async def _ask(
    http: httpx.AsyncClient,
    settings: WorkbenchSettings,
    run: Run,
    view: JobView,
    role: str,
    payload: dict[str, object],
) -> AgentResult:
    label = _LABELS[role]
    run.message("orchestrator", role, "SENT", label, None)
    result = await agent(http, settings, view, role, payload)
    run.message(
        role,
        "orchestrator",
        result.status,
        label,
        {
            "task_id": str(result.task_id),
            "result": result.result,
            "evidence": [item for item in result.evidence],
        },
    )
    if result.status == "FAILED":
        raise RuntimeError(f"Agent {role} reported failure")
    return result


def _risk_result(risk: RiskResultV1, reviewed: bool) -> AgentResult:
    return AgentResult(
        task_id=risk.task_id,
        job_id=risk.job_id,
        agent_role="risk",
        status="DONE" if reviewed or risk.human_reason is None else "NEEDS_REVIEW",
        result={"summary": risk.summary, "risk_level": risk.risk_level},
        evidence=[item.model_dump(mode="json") for item in risk.sources],
        model_calls=[
            ModelCall(
                trace_id=risk.trace_id,
                model_version=risk.model_version,
                prompt_version=risk.prompt_version,
            )
        ],
        trace_id=risk.trace_id,
    )


async def _solve(
    http: httpx.AsyncClient, settings: WorkbenchSettings, run: Run, view: JobView
) -> JobView:
    facts = run.facts
    source = _source(run)
    position = {
        "position_id": "uploaded-lot",
        "required_quantity": decimal_text(facts.quantity),
        "available_quantity": decimal_text(facts.available_quantity),
        "source": source,
    }
    specialists = await asyncio.gather(
        _ask(
            http,
            settings,
            run,
            view,
            "legal",
            {
                "regime": "commercial",
                "regime_source": source,
                "clauses": [{"text": run.context, "source": source, "requires_review": False}],
            },
        ),
        _ask(http, settings, run, view, "resource", {"positions": [position]}),
        _ask(
            http,
            settings,
            run,
            view,
            "estimate",
            {
                "currency": "BYN",
                "offer_price": decimal_text(facts.offer_price),
                "margin_threshold_percent": decimal_text(settings.web_margin_threshold_percent),
                "threshold_source": source,
                "positions": [
                    {
                        "position_id": position["position_id"],
                        "quantity": position["required_quantity"],
                        "unit_cost": decimal_text(facts.unit_cost),
                        "source": source,
                    }
                ],
            },
        ),
    )
    if run.extraction is None:
        raise RuntimeError("Missing validated requirements")
    results = [*specialists, run.extraction]
    if view.risk_result is not None:
        results.append(
            _risk_result(
                view.risk_result, any(card.step.startswith("risk_") for card in view.cards)
            )
        )
    run.message("orchestrator", "solver", "SENT", "Проверка правил и расчёт рекомендации", None)
    trace = str(view.job.trace_id)
    response = _ok(
        await http.post(
            _url(settings.solver_url, "/api/v1/solve"),
            headers={"X-Trace-Id": trace},
            json={
                "job_id": str(run.id),
                "kb_version": view.job.kb_version,
                "trace_id": trace,
                "as_of": view.job.created_at.date().isoformat(),
                "lot_ids": view.job.lot_ids,
                "requirements": run.extraction.result.get("requirements"),
                "facts": [
                    {
                        "fact_id": str(uuid5(run.id, "validity-fact")),
                        "key": "offer_validity_workdays",
                        "value": facts.offer_validity_workdays,
                        "source": run.normalization.document.storage_path,
                    }
                ],
                "profile": {
                    "regime": "commercial",
                    "task_class": "supply",
                    "positions": [
                        {
                            "position_id": position["position_id"],
                            "description": run.title,
                            "quantity": position["required_quantity"],
                            "available": position["available_quantity"],
                            "unit_cost": decimal_text(facts.unit_cost),
                            "offer_unit_price": decimal_text(facts.offer_price / facts.quantity),
                            "supply_deadline": None,
                            "alternatives": [],
                        }
                    ],
                },
                "agent_results": [item.model_dump(mode="json") for item in results],
            },
        )
    )
    result = SolverResult.model_validate_json(response.content)
    run.message(
        "solver",
        "orchestrator",
        result.status,
        "Рекомендация рассчитана",
        result.recommendation.model_dump(mode="json"),
    )
    return await _post_job(
        http,
        settings,
        view,
        "solver-result",
        {"execution_id": str(view.execution_id), "result": result.model_dump(mode="json")},
    )


async def _generate(
    http: httpx.AsyncClient, settings: WorkbenchSettings, run: Run, view: JobView
) -> JobView:
    approval = next(
        card.decision
        for card in reversed(view.cards)
        if card.reason_code == "FINAL_APPROVAL" and card.decision is not None
    )
    assert approval is not None and view.solver_result is not None
    result = await _ask(
        http,
        settings,
        run,
        view,
        "document",
        {
            "solver_result": view.solver_result.model_dump(mode="json"),
            "approval": approval.model_dump(mode="json"),
        },
    )
    raw = result.result.get("artifacts")
    if not isinstance(raw, list):
        raise RuntimeError("Missing generated artifacts")
    artifacts: dict[str, bytes] = {}
    for item in raw:
        artifact = Artifact.model_validate(item)
        reply = _ok(
            await http.get(
                _url(settings.agents_url, artifact.file_path),
                headers=_auth(settings.artifact_token),
            )
        )
        _verify_content(artifact, reply.content)
        artifacts[f"offer.{artifact.kind.lower()}"] = reply.content
    view = await _post_job(
        http,
        settings,
        view,
        "artifacts",
        {"execution_id": str(view.execution_id), "artifacts": raw},
    )
    if view.phase == "done":
        run.artifacts.update(artifacts)
    return view


async def _advance(http: httpx.AsyncClient, settings: WorkbenchSettings, run: Run) -> None:
    assert run.view is not None
    view = run.view
    if view.phase == "assessing":
        async with asyncio.timeout(settings.timeout_seconds):
            while view.phase == "assessing":
                await asyncio.sleep(0.5)
                reply = _ok(
                    await http.get(
                        _url(settings.orchestrator_url, f"/api/v1/jobs/{run.id}"),
                        headers=_auth(settings.service_token),
                    )
                )
                view = JobView.model_validate_json(reply.content)
        run.view = view
    if view.risk_result is not None and not any(m["agent"] == "risk" for m in run.messages):
        result = view.risk_result
        run.message(
            "risk",
            "orchestrator",
            "NEEDS_REVIEW" if result.human_reason else "DONE",
            result.summary,
            {
                "model": result.model_version,
                "confidence": result.confidence,
                "source": [item.model_dump(mode="json") for item in result.sources],
            },
        )
    if view.phase == "ready_for_next_stage" and view.stage == "solve":
        view = await _solve(http, settings, run, view)
        run.view = view
    if view.phase == "awaiting_artifacts":
        run.view = await _generate(http, settings, run, view)


async def _start(http: httpx.AsyncClient, settings: WorkbenchSettings, run: Run) -> None:
    readable = await asyncio.to_thread(lambda: sum(has_readable_text(file) for file in run.uploads))
    run.message(
        "workbench",
        "orchestrator",
        "DONE",
        f"Файлов принято: {len(run.uploads)}. С доступным текстом: {readable}.",
        None,
    )
    job = Job(
        job_id=run.id,
        idempotency_key=str(run.id),
        source="web",
        created_at=datetime.now(UTC),
        status="ACCEPTED",
        kb_version=settings.kb_version,
        lot_ids=["uploaded-lot"],
        documents=[item.document for item in [*run.uploads, run.normalization]],
        profile_ref=settings.web_profile_ref,
        trace_id=uuid5(run.id, "trace"),
    )
    response = _ok(
        await http.post(
            _url(settings.orchestrator_url, "/api/v1/jobs"),
            headers=_auth(settings.service_token),
            json=job.model_dump(mode="json"),
        )
    )
    view = JobView.model_validate_json(response.content)
    run.view = view
    run.extraction = await _ask(
        http, settings, run, view, "extraction", {"requirements": [_requirement(run)]}
    )
    run.message("orchestrator", "risk", "SENT", "Оценка открытого фрагмента", None)
    run.view = await _post_job(
        http,
        settings,
        view,
        "risk-input",
        {
            "execution_id": str(view.execution_id),
            "risk": {
                "kind": "risk_task.v1",
                "task_id": str(uuid5(run.id, "risk")),
                "job_id": str(run.id),
                "fragment_id": str(uuid5(run.id, "contract")),
                "source": _source(run),
                "fragment_text": run.context,
                "fragment_type": "public_text",
                "rules_version": settings.kb_version,
                "penalty_daily_percent": decimal_text(
                    run.facts.penalty_daily_percent or Decimal(0)
                ),
                "penalty_daily_threshold_percent": decimal_text(
                    settings.web_penalty_threshold_percent
                ),
                "prompt_version": "v1",
                "trace_id": str(job.trace_id),
            },
        },
    )
    await _advance(http, settings, run)


async def work_run(
    settings: WorkbenchSettings, run: Run, decision: tuple[ReviewCard, DecisionInput] | None
) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=settings.timeout_seconds, trust_env=False, follow_redirects=False
        ) as http:
            if decision is None:
                await _start(http, settings, run)
                return
            card, body = decision
            assert run.view is not None
            run.view = await _post_job(
                http,
                settings,
                run.view,
                f"cards/{card.card_id}/decision",
                {
                    "decision_id": str(uuid5(card.card_id, "web-decision")),
                    "decision": body.decision,
                    "comment": body.comment,
                },
                settings.reviewer_tokens[card.role],
            )
            run.message("operator", "orchestrator", body.decision, card.question, None)
            await _advance(http, settings, run)
    except Exception as error:
        _LOG.warning("workbench run_id=%s failure=%s", run.id, type(error).__name__)
        run.error = "Не удалось завершить этап. Проверьте документы и готовность сервисов."


def new_run(run_id: UUID, incoming: RunInput, uploads: list[Upload], normalization: Upload) -> Run:
    return Run(run_id, incoming.title, incoming.facts, incoming.context, uploads, normalization)
