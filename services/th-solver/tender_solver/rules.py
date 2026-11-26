import re
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
    Requirement,
    ResourceCheck,
    Risk,
    SolverResult,
    StopReason,
)
from pydantic import JsonValue, ValidationError

from tender_solver.models import (
    Condition,
    Consequence,
    Fact,
    KnowledgeFact,
    Position,
    Scalar,
    SolveRequest,
)

_COMPARISONS = {"gt": gt, "gte": ge, "lt": lt, "lte": le}
_OFFER_VALIDITY_FIELD = "offer_validity_workdays"
_DEMO_OFFER_VALIDITY_RULE = "R-DEMO-OFFER-VALIDITY"
_OFFER_VALIDITY = re.compile(
    r"\b(?:срок действия (?:(?:коммерческого|ценового|тендерного) )?"
    r"(?:предложения|оферты|заявки)|offer validity|bid validity)\b"
)
_MINIMUM = re.compile(
    r"\b(?:не менее(?: чем)?|не короче|минимум|от|at least|minimum(?: of)?) "
    r"([0-9]{1,4})(?![0-9,.])"
)


def _required_workdays(requirement: Requirement, normalized_text: str) -> int | None:
    if requirement.unit not in {"business_days", "рабочих дней", "working days"}:
        return None
    value = requirement.value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value):
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    minima = {int(match.group(1)) for match in _MINIMUM.finditer(normalized_text)}
    return value if minima == {value} else None


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


def _check_resources(
    positions: list[Position],
) -> tuple[list[ResourceCheck], list[Decimal], bool]:
    resources: list[ResourceCheck] = []
    costs: list[Decimal] = []
    confirmation_failed = False
    for position in positions:
        available = position.available
        cost = position.quantity * position.unit_cost
        confirmation = position.supply_confirmation
        if (
            confirmation is not None
            and position.supply_deadline is not None
            and confirmation.delivery_date <= position.supply_deadline.date()
        ):
            available += confirmation.quantity
            cost += confirmation.additional_cost
        costs.append(cost)
        shortage = max(position.quantity - available, Decimal(0))
        if shortage and confirmation is not None:
            confirmation_failed = True
        resources.append(
            ResourceCheck(
                position_id=position.position_id,
                covered=shortage == 0,
                shortage=str(shortage),
                supply_deadline=position.supply_deadline,
                alternatives=position.alternatives,
            )
        )
    return resources, costs, confirmation_failed


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
        requirement_text = " ".join(requirement.text.casefold().split())
        for rule in rules["*"] + rules[requirement.kind]:
            try:
                condition = Condition.model_validate(rule.get("condition"))
                consequence = Consequence.model_validate(rule.get("consequence"))
            except ValidationError:
                indeterminate = True
                continue
            minimum_workdays = None
            if condition.field == _OFFER_VALIDITY_FIELD:
                if requirement.kind != "deadline" or not _OFFER_VALIDITY.search(requirement_text):
                    continue
                if rule["rule_id"] == _DEMO_OFFER_VALIDITY_RULE:
                    minimum_workdays = _required_workdays(requirement, requirement_text)
                    if minimum_workdays is None:
                        indeterminate = True
                        continue
                    condition = Condition(field=condition.field, op="gte", value=minimum_workdays)
            candidates = facts[condition.field]
            if not candidates or any(
                not _same(item.value, candidates[0].value) for item in candidates
            ):
                indeterminate = True
                continue
            fact = candidates[0]
            matched = _matches(fact.value, condition)
            if minimum_workdays is not None:
                if not isinstance(fact.value, int) or isinstance(fact.value, bool):
                    indeterminate = True
                    continue
                consequence = Consequence(
                    status="MET" if matched else "NOT_MET",
                    reason=(
                        f"Предложение действительно {fact.value} раб. дн., "
                        f"документация требует не менее {minimum_workdays} раб. дн."
                    ),
                )
            if matched or minimum_workdays is not None:
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

    positions = request.profile.positions
    resources, costs, confirmation_failed = _check_resources(positions)
    shortage = any(not row.covered for row in resources)
    if confirmation_failed:
        reasons.add("NO_SOLUTION")
    elif shortage:
        reasons.add("RESOURCE_GAP")
    offer = sum(
        (
            position.offer_total_price
            if position.offer_total_price is not None
            else position.quantity * position.offer_unit_price
            for position in positions
        ),
        Decimal(0),
    )
    cost = sum(costs, Decimal(0))
    loss_making = cost > offer
    estimate: Estimate | EmptyResultPart = EmptyResultPart()
    if offer:
        margin_numerator = (offer - cost) * 100
        estimate = Estimate(
            positions=[
                {**position.model_dump(mode="json"), "cost": str(row_cost)}
                for position, row_cost in zip(positions, costs, strict=True)
            ],
            total_cost=cost,
            offer_price=offer,
            margin_percent=margin_numerator / offer,
            threshold=margin_floor,
            threshold_source="SOLVER_MARGIN_THRESHOLD_PERCENT",
        )
        if loss_making:
            reasons.add("NO_SOLUTION")
        elif margin_numerator < margin_floor * offer:
            reasons.add("LOW_MARGIN")
    else:
        reasons.add("NO_SOLUTION")

    risks: list[Risk] = []
    has_supply_confirmation = any(
        position.supply_confirmation is not None for position in positions
    )
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
                    if not has_supply_confirmation:
                        reasons.add("RESOURCE_GAP")
                elif result.agent_role == "estimate":
                    if not loss_making:
                        reasons.add("LOW_MARGIN")
                else:
                    reasons.add("UNCLEAR_REQUIREMENTS")
            continue
        if result.status == "FAILED":
            reasons.add("UNCLEAR_REQUIREMENTS")
            continue
        reason = result.result.get("human_reason")
        if result.status == "NEEDS_REVIEW":
            reasons.add(
                "PENALTY_ABOVE_THRESHOLD"
                if reason == "penalty_above_threshold"
                else "UNCLEAR_REQUIREMENTS"
            )
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
