from decimal import Decimal, localcontext
from typing import Literal

from pydantic import JsonValue

from contracts.agent_task import (
    AgentResult,
    DocumentAgentTask,
    EstimateAgentTask,
    EstimateInput,
    ExtractionAgentTask,
    ExtractionInput,
    LegalAgentTask,
    LegalInput,
    ResourceAgentTask,
    ResourceInput,
    SupplyAgentTask,
    SupplyInput,
    TypedAgentTask,
)

type Evidence = list[dict[str, JsonValue]]
type Findings = tuple[dict[str, JsonValue], Evidence, bool]
type AnalysisTask = (
    ExtractionAgentTask | LegalAgentTask | ResourceAgentTask | EstimateAgentTask | SupplyAgentTask
)


def finding_result(
    task: TypedAgentTask,
    result: dict[str, JsonValue],
    evidence: Evidence,
    status: Literal["DONE", "NEEDS_REVIEW", "FAILED"],
) -> AgentResult:
    return AgentResult(
        task_id=task.task_id,
        job_id=task.job_id,
        agent_role=task.agent_role,
        status=status,
        result=result,
        evidence=evidence,
        model_calls=[],
        trace_id=task.trace_id,
    )


def analyze(task: AnalysisTask) -> AgentResult:
    match task:
        case ExtractionAgentTask(input=data):
            result, evidence, review = _extract(data)
        case LegalAgentTask(input=data):
            result, evidence, review = _legal(data)
        case ResourceAgentTask(input=data):
            result, evidence, review = _resource(data)
        case EstimateAgentTask(input=data):
            result, evidence, review = _estimate(data)
        case SupplyAgentTask(input=data):
            result, evidence, review = _supply(data)
    return finding_result(task, result, evidence, "NEEDS_REVIEW" if review else "DONE")


def _extract(data: ExtractionInput) -> Findings:
    requirements: list[JsonValue] = [item.model_dump(mode="json") for item in data.requirements]
    review_ids: list[JsonValue] = [
        str(item.requirement_id) for item in data.requirements if item.needs_review
    ]
    return (
        {"requirements": requirements, "review_requirement_ids": review_ids},
        [
            {
                "requirement_id": str(item.requirement_id),
                "source": item.source.model_dump(mode="json"),
                "confidence": item.confidence,
            }
            for item in data.requirements
        ],
        not requirements or bool(review_ids),
    )


def _legal(data: LegalInput) -> Findings:
    clauses: list[JsonValue] = [clause.model_dump(mode="json") for clause in data.clauses]
    review = not clauses or any(clause.requires_review for clause in data.clauses)
    return (
        {"regime": data.regime, "clauses": clauses},
        [
            {"kind": "regime", "source": data.regime_source.model_dump(mode="json")},
            *(
                {"kind": "clause", "source": clause.source.model_dump(mode="json")}
                for clause in data.clauses
            ),
        ],
        review,
    )


def _resource(data: ResourceInput) -> Findings:
    positions: list[JsonValue] = []
    evidence: Evidence = []
    review = False
    for position in data.positions:
        shortage = max(Decimal(0), position.required_quantity - position.available_quantity)
        positions.append(
            {
                "position_id": position.position_id,
                "required_quantity": str(position.required_quantity),
                "available_quantity": str(position.available_quantity),
                "covered": shortage == 0,
                "shortage": str(shortage),
                "supply_deadline": None,
                "alternatives": [],
            }
        )
        review |= shortage > 0
        evidence.append(
            {"position_id": position.position_id, "source": position.source.model_dump(mode="json")}
        )
    return {"resources": positions}, evidence, review


def _estimate(data: EstimateInput) -> Findings:
    lines: list[JsonValue] = []
    evidence: Evidence = []
    with localcontext() as context:
        context.prec = 40
        total_cost = Decimal(0)
        for position in data.positions:
            cost = position.quantity * position.unit_cost
            total_cost += cost
            lines.append(
                {
                    "position_id": position.position_id,
                    "quantity": str(position.quantity),
                    "unit_cost": str(position.unit_cost),
                    "cost": str(cost),
                }
            )
            evidence.append(
                {
                    "position_id": position.position_id,
                    "source": position.source.model_dump(mode="json"),
                }
            )
        margin = (data.offer_price - total_cost) * 100 / data.offer_price
        below_threshold = (data.offer_price - total_cost) * 100 < (
            data.margin_threshold_percent * data.offer_price
        )
        display_margin = str(margin.quantize(Decimal("0.0001")))
    evidence.append(
        {"kind": "margin_threshold", "source": data.threshold_source.model_dump(mode="json")}
    )
    return (
        {
            "estimate": {
                "positions": lines,
                "total_cost": str(total_cost),
                "offer_price": str(data.offer_price),
                "margin_percent": display_margin,
                "threshold": str(data.margin_threshold_percent),
                "threshold_source": data.threshold_source.model_dump(mode="json"),
            },
            "currency": data.currency,
            "below_threshold": below_threshold,
        },
        evidence,
        below_threshold,
    )


def _supply(data: SupplyInput) -> Findings:
    positions: list[JsonValue] = []
    evidence: Evidence = []
    review = False
    for position in data.positions:
        alternatives: list[JsonValue] = []
        covered = False
        for offer in sorted(position.offers, key=lambda item: (item.delivery_at, item.supplier)):
            viable = (
                offer.available_quantity >= position.required_quantity
                and offer.delivery_at <= position.delivery_deadline
            )
            covered |= viable
            alternatives.append({**offer.model_dump(mode="json"), "eligible": viable})
            evidence.append(
                {
                    "position_id": position.position_id,
                    "supplier": offer.supplier,
                    "source": offer.source.model_dump(mode="json"),
                }
            )
        review |= not covered
        positions.append(
            {
                "position_id": position.position_id,
                "required_quantity": str(position.required_quantity),
                "delivery_deadline": position.delivery_deadline.isoformat(),
                "covered": covered,
                "alternatives": alternatives,
            }
        )
        evidence.append(
            {"position_id": position.position_id, "source": position.source.model_dump(mode="json")}
        )
    return {"positions": positions}, evidence, review


def approved_handoff(task: DocumentAgentTask) -> bool:
    result = task.input.solver_result
    approval = task.input.approval
    return (
        result.job_id == approval.job_id == task.job_id
        and result.kb_version == task.kb_version
        and result.status == "READY"
        and approval.reason_code == "FINAL_APPROVAL"
        and approval.decision == "APPROVED"
        and not approval.timeout
    )
