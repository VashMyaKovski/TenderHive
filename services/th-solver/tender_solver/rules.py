from collections import defaultdict
from decimal import Decimal
from operator import ge, gt, le, lt
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from contracts.models import (
    Compliance,
    EmptyResultPart,
    Estimate,
    Explanation,
    PreparationPlan,
    Recommendation,
    ResourceCheck,
    Risk,
    SolverResult,
    StopReason,
)
from pydantic import JsonValue, ValidationError

from tender_solver.models import Condition, Consequence, Fact, KnowledgeFact, Scalar, SolveRequest

_COMPARISONS = {"gt": gt, "gte": ge, "lt": lt, "lte": le}


def _same(left: Scalar, right: Scalar) -> bool:
    comparable = type(left) is type(right) or (
        type(left) in (int, float) and type(right) in (int, float)
    )
    return comparable and left == right


def _matches(actual: Scalar, condition: Condition) -> bool:
    expected = condition.value
    if condition.op in {"eq", "ne"}:
        return _same(actual, expected) == (condition.op == "eq")
    if not isinstance(actual, int | float) or not isinstance(expected, int | float):
        return False
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    return _COMPARISONS[condition.op](actual, expected)


def solve(
    request: SolveRequest, context: dict[str, JsonValue], margin_floor: Decimal
) -> SolverResult:
    reasons: set[StopReason] = set()
    trace: list[JsonValue] = []
    facts: dict[str, list[Fact]] = defaultdict(list)
    knowledge_ids: dict[UUID, str] = {}
    for raw in cast(list[JsonValue], context.get("facts", [])):
        try:
            item = KnowledgeFact.model_validate(raw)
        except ValidationError:
            continue
        if not isinstance(item.value, (str, int, float, bool)) and item.value is not None:
            continue
        try:
            fact_uuid = UUID(item.fact_id)
        except ValueError:
            fact_uuid = uuid5(NAMESPACE_URL, f"tenderhive:kb:{request.kb_version}:{item.fact_id}")
        knowledge_ids[fact_uuid] = item.fact_id
        facts[item.key or str(item.fact_id)].append(
            Fact(
                fact_id=fact_uuid,
                key=item.key or str(item.fact_id),
                source=item.source,
                value=item.value,
            )
        )
    for item in request.facts:
        facts[item.key].append(item)

    rules: dict[str, list[dict[str, JsonValue]]] = defaultdict(list)
    for raw in cast(list[dict[str, JsonValue]], context.get("rules", [])):
        scope = raw.get("scope")
        if isinstance(scope, dict):
            if any(
                scope.get(key) not in (None, value)
                for key, value in (
                    ("regime", request.profile.regime),
                    ("task_class", request.profile.task_class),
                )
            ):
                continue
            kind = str(scope.get("kind") or "*")
        else:
            kind = scope if isinstance(scope, str) else "*"
        rules[kind].append(raw)

    compliance: list[Compliance] = []
    for requirement in request.requirements:
        applicable: list[tuple[int, Consequence, Fact, str, JsonValue]] = []
        indeterminate = requirement.needs_review
        for rule in rules["*"] + rules[requirement.kind]:
            try:
                condition = Condition.model_validate(rule.get("condition"))
                consequence = Consequence.model_validate(rule.get("consequence"))
            except ValidationError:
                indeterminate = True
                continue
            candidates = facts[condition.field]
            if not candidates or any(
                not _same(item.value, candidates[0].value) for item in candidates
            ):
                indeterminate = True
                continue
            fact = candidates[0]
            if _matches(fact.value, condition):
                priority = rule.get("priority")
                applicable.append(
                    (
                        priority if isinstance(priority, int) else 0,
                        consequence,
                        fact,
                        str(rule["rule_id"]),
                        rule.get("version"),
                    )
                )
        applicable.sort(key=lambda outcome: (-outcome[0], outcome[3]))
        conflict = len({outcome[1].status for outcome in applicable}) > 1
        if indeterminate or not applicable or conflict:
            if requirement.mandatory or conflict:
                reasons.add("UNCLEAR_REQUIREMENTS")
            compliance.append(
                Compliance(
                    requirement_id=requirement.requirement_id,
                    status="NEEDS_REVIEW",
                    fact_ref=None,
                    rule_id="unresolved",
                    reason="Conflicting rules" if conflict else "Insufficient evidence",
                )
            )
        else:
            _, consequence, fact, rule_id, version = applicable[0]
            if consequence.status == "NEEDS_REVIEW" and requirement.mandatory:
                reasons.add("UNCLEAR_REQUIREMENTS")
            compliance.append(
                Compliance(
                    requirement_id=requirement.requirement_id,
                    status=consequence.status,
                    fact_ref=fact.fact_id,
                    rule_id=rule_id,
                    reason=consequence.reason,
                )
            )
            entry: dict[str, JsonValue] = {
                "rule_id": rule_id,
                "version": version,
                "source": fact.source,
            }
            if fact.fact_id in knowledge_ids:
                entry["fact_id"] = knowledge_ids[fact.fact_id]
            trace.append(entry)

    resources = [
        ResourceCheck(
            position_id=p.position_id,
            covered=p.available >= p.quantity,
            shortage=str(max(p.quantity - p.available, 0)),
            supply_deadline=p.supply_deadline,
            alternatives=p.alternatives,
        )
        for p in request.profile.positions
    ]
    if any(not row.covered for row in resources):
        reasons.add("RESOURCE_GAP")
    offer = sum((p.quantity * p.offer_unit_price for p in request.profile.positions), Decimal(0))
    cost = sum((p.quantity * p.unit_cost for p in request.profile.positions), Decimal(0))
    estimate: Estimate | EmptyResultPart = EmptyResultPart()
    if offer:
        margin = (offer - cost) * 100 / offer
        estimate = Estimate(
            positions=[p.model_dump(mode="json") for p in request.profile.positions],
            total_cost=cost,
            offer_price=offer,
            margin_percent=margin,
            threshold=margin_floor,
            threshold_source="SOLVER_MARGIN_THRESHOLD_PERCENT",
        )
        if margin < margin_floor:
            reasons.add("LOW_MARGIN")
    else:
        reasons.add("NO_SOLUTION")

    risks: list[Risk] = []
    for result in request.agent_results:
        trace.append(
            {
                "agent_role": result.agent_role,
                "task_id": str(result.task_id),
                "status": result.status,
                "evidence": [item for item in result.evidence],
            }
        )
        if result.agent_role != "risk":
            if result.status == "FAILED":
                reasons.add("UNCLEAR_REQUIREMENTS")
            elif result.status == "NEEDS_REVIEW":
                if result.agent_role in {"resource", "supply"}:
                    reasons.add("RESOURCE_GAP")
                elif result.agent_role == "estimate":
                    reasons.add("LOW_MARGIN")
                else:
                    reasons.add("UNCLEAR_REQUIREMENTS")
            continue
        if result.status == "FAILED":
            reasons.add("UNCLEAR_REQUIREMENTS")
            continue
        reason = result.result.get("human_reason")
        if reason == "penalty_above_threshold":
            reasons.add("PENALTY_ABOVE_THRESHOLD")
        elif result.status == "NEEDS_REVIEW":
            reasons.add("UNCLEAR_REQUIREMENTS")
        summary, level = result.result.get("summary"), result.result.get("risk_level")
        if isinstance(summary, str) and isinstance(level, str):
            risks.append(
                Risk(
                    risk_id=uuid5(NAMESPACE_URL, str(result.task_id)),
                    category="contract",
                    level=level,
                    basis=summary,
                    mitigation=None,
                    source_ref=[item for item in result.evidence],
                )
            )
    blocked = any(
        r.mandatory and item.status == "NOT_MET"
        for r, item in zip(request.requirements, compliance, strict=True)
    )
    if blocked:
        reasons.add("NO_SOLUTION")
    status = (
        "NO_SOLUTION"
        if "NO_SOLUTION" in reasons
        else ("INCOMPLETE" if "UNCLEAR_REQUIREMENTS" in reasons else "READY")
    )
    recommendation = (
        "NOT_PARTICIPATE"
        if status == "NO_SOLUTION"
        else ("PARTICIPATE_WITH_CONDITIONS" if reasons else "PARTICIPATE")
    )
    return SolverResult(
        job_id=request.job_id,
        kb_version=request.kb_version,
        status=status,
        requirements=request.requirements,
        compliance=compliance,
        resources=resources,
        estimate=estimate,
        risks=risks,
        recommendation=Recommendation(
            value=recommendation, conditions=[item for item in sorted(reasons)]
        ),
        plan=PreparationPlan(
            steps=["Confirm requirements", "Prepare reviewed offer"], deadlines=[], owners=[]
        ),
        explanation=Explanation(
            steps=["Evaluate rules", "Verify resources and margin"],
            rule_trace=trace,
            source_refs=cast(list[JsonValue], context.get("corpus_fragments", [])),
        ),
        artifacts=[],
        stop_reasons=sorted(reasons),
    )
