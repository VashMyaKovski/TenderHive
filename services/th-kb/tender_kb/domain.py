import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from operator import eq, ge, gt, le, lt
from typing import Annotated, Literal, TypeGuard
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


type Identifier = Annotated[str, StringConstraints(pattern=_IDENTIFIER_PATTERN)]
type Text = Annotated[str, StringConstraints(min_length=1, max_length=12_000, pattern=r"\S")]
type Status = Literal["DRAFT", "CHECK", "ACTIVE", "REJECTED", "OLD"]
type Scalar = str | bool | int | float | None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Period(Contract):
    valid_from: date | None = None
    valid_until: date | None = None


class Scope(Contract):
    kind: Identifier | None = None
    regime: Identifier | None = None
    task_class: Identifier | None = None


class Condition(Contract):
    field: Identifier
    op: Literal["eq", "gte", "lte", "gt", "lt", "ne"]
    value: Scalar


class Consequence(Contract):
    status: Literal["MET", "NOT_MET", "NEEDS_REVIEW"]
    reason: Text


class Rule(Period):
    rule_id: Identifier
    condition: Text | Condition
    consequence: Text | Consequence
    priority: Annotated[int, Field(strict=True)]
    scope: Text | Scope
    version: Annotated[int, Field(strict=True, gt=0)]


class Fact(Period):
    fact_id: Identifier
    key: Identifier | None = None
    value: JsonValue
    source: Text
    value_type: Literal["string", "number", "boolean", "object", "array", "null"] | None = None


class GraphNode(Contract):
    node_id: Identifier
    kind: Identifier
    lot_id: Identifier | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class GraphEdge(Contract):
    source: Identifier
    target: Identifier
    relation: Identifier
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class GraphSlice(Contract):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class CorpusFragment(Contract):
    fragment_id: Identifier
    text: Text
    source: Text
    date: date
    lot_ids: list[Identifier] = Field(default_factory=list)


class Case(Contract):
    case_id: Identifier
    input: dict[str, JsonValue]
    expected: dict[str, JsonValue]


class VersionUpload(Contract):
    version: Identifier
    rules: list[Rule]
    facts: list[Fact]
    reference_data: dict[str, JsonValue]
    test_cases: list[Case]
    checksum: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    author: Text | None = None
    graph_slice: GraphSlice = Field(default_factory=GraphSlice)
    corpus_fragments: list[CorpusFragment] = Field(default_factory=list)

    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_unset=True, exclude={"checksum"})
        return hashlib.sha256(canonical_json(payload)).hexdigest()


class Issue(Contract):
    code: Identifier
    message: Text


class VersionResult(Contract):
    version: Identifier
    status: Status
    report: list[Issue]


class EvalRequest(Contract):
    candidate_version: Identifier
    baseline_version: Identifier | None
    test_set_version: Identifier
    test_cases: list[Case]


class EvalResult(Contract):
    passed: Annotated[bool, Field(strict=True)]
    report: list[Issue]


class ContextProjection(Contract):
    graph_slice: GraphSlice
    corpus_fragments: list[CorpusFragment]


class Context(Contract):
    version: Identifier
    rules: list[Rule]
    facts: list[Fact]
    reference_data: dict[str, JsonValue]
    graph_slice: GraphSlice
    corpus_fragments: list[CorpusFragment]


class ContextSelection(Contract):
    lot_ids: tuple[Identifier, ...]
    regime: Identifier | None
    task_class: Identifier | None
    query: Text | None
    as_of: date | None


@dataclass(frozen=True)
class Snapshot:
    payload: VersionUpload
    status: Status
    projection: UUID | None
    report: list[Issue]

    def result(self) -> VersionResult:
        return VersionResult(version=self.payload.version, status=self.status, report=self.report)


@dataclass(frozen=True)
class Head:
    version: str | None
    revision: int


class Conflict(Exception):
    """The workflow changed concurrently or the requested state change is invalid."""


class VersionNotFound(Exception):
    """The requested snapshot is unavailable."""


class IntegrityError(Exception):
    """The uploaded checksum does not describe a canonical snapshot."""


class DependencyUnavailable(Exception):
    """An external dependency did not return the data required for a safe operation."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def matching_rules(rules: list[Rule], selection: ContextSelection) -> list[Rule]:
    def applicable(rule: Rule) -> bool:
        if isinstance(rule.scope, str):
            return True
        return (
            selection.regime is None
            or rule.scope.regime is None
            or selection.regime == rule.scope.regime
        ) and (
            selection.task_class is None
            or rule.scope.task_class is None
            or selection.task_class == rule.scope.task_class
        )

    return sorted(
        (rule for rule in rules if applicable(rule)),
        key=lambda rule: (-rule.priority, rule.rule_id, -rule.version),
    )


def integrity_issues(payload: VersionUpload) -> list[Issue]:
    issues: list[Issue] = []
    graph = payload.graph_slice
    node_ids = {node.node_id for node in graph.nodes}
    lot_ids = {node.node_id for node in graph.nodes if node.kind == "lot"}

    for label, identifiers in (
        ("node", [node.node_id for node in graph.nodes]),
        ("fragment", [fragment.fragment_id for fragment in payload.corpus_fragments]),
        ("test case", [case.case_id for case in payload.test_cases]),
        ("rule revision", [(rule.rule_id, rule.version) for rule in payload.rules]),
        ("graph edge", [(edge.source, edge.target, edge.relation) for edge in graph.edges]),
    ):
        if len(identifiers) != len(set(identifiers)):
            issues.append(_integrity(f"Duplicate {label} identifier."))

    if any(edge.source not in node_ids or edge.target not in node_ids for edge in graph.edges):
        issues.append(_integrity("A graph edge references an unknown node."))
    referenced_lots = {node.lot_id for node in graph.nodes if node.lot_id is not None}
    referenced_lots.update(lot for fragment in payload.corpus_fragments for lot in fragment.lot_ids)
    if referenced_lots - lot_ids:
        issues.append(_integrity("A graph node or corpus fragment references an unknown lot."))

    for fact in payload.facts:
        if not _valid_value_type(fact):
            issues.append(
                _integrity(f"Fact {fact.fact_id} disagrees with its declared value type.")
            )
    for rule in payload.rules:
        condition = rule.condition
        if (
            isinstance(condition, Condition)
            and condition.op not in ("eq", "ne")
            and not _is_number(condition.value)
        ):
            issues.append(_integrity(f"Rule {rule.rule_id} requires a numeric comparison value."))
    for item in [*payload.rules, *payload.facts]:
        if item.valid_from and item.valid_until and item.valid_from > item.valid_until:
            issues.append(_integrity("An effective date range is inverted."))
    for left, right in combinations(payload.facts, 2):
        if (left.key or left.fact_id) == (right.key or right.fact_id) and _periods_overlap(
            left, right
        ):
            issues.append(_integrity(f"Fact {left.fact_id} has overlapping effective periods."))
    for left, right in combinations(payload.rules, 2):
        if not _periods_overlap(left, right):
            continue
        if left.rule_id == right.rule_id:
            issues.append(_integrity(f"Rule {left.rule_id} has overlapping effective periods."))
        elif (
            left.consequence != right.consequence
            and _scopes_overlap(left.scope, right.scope)
            and _conditions_overlap(left.condition, right.condition)
        ):
            issues.append(
                Issue(
                    code="INCOMPATIBLE_RULES",
                    message=f"Rules {left.rule_id} and {right.rule_id} apply with different consequences.",
                )
            )
    return issues


def _integrity(message: str) -> Issue:
    return Issue(code="INTEGRITY_ERROR", message=message)


def _valid_value_type(fact: Fact) -> bool:
    match fact.value_type:
        case None:
            return True
        case "null":
            return fact.value is None
        case "number":
            return type(fact.value) in (int, float)
        case "boolean":
            return isinstance(fact.value, bool)
        case "string":
            return isinstance(fact.value, str)
        case "object":
            return isinstance(fact.value, dict)
        case "array":
            return isinstance(fact.value, list)


def _periods_overlap(left: Period, right: Period) -> bool:
    return max(left.valid_from or date.min, right.valid_from or date.min) <= min(
        left.valid_until or date.max, right.valid_until or date.max
    )


def _scopes_overlap(left: Text | Scope, right: Text | Scope) -> bool:
    if left == "*" or right == "*":
        return True
    first = (
        Scope(kind=left)
        if isinstance(left, str) and re.fullmatch(_IDENTIFIER_PATTERN, left)
        else left
    )
    second = (
        Scope(kind=right)
        if isinstance(right, str) and re.fullmatch(_IDENTIFIER_PATTERN, right)
        else right
    )
    if isinstance(first, str) or isinstance(second, str):
        return first == second
    return all(
        one is None or two is None or one == two
        for one, two in zip(
            (first.kind, first.regime, first.task_class),
            (second.kind, second.regime, second.task_class),
            strict=True,
        )
    )


def _is_number(value: Scalar) -> TypeGuard[int | float]:
    return type(value) in (int, float)


def _conditions_overlap(left: Text | Condition, right: Text | Condition) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    if left.field != right.field:
        return False
    if left.op == "eq" or right.op == "eq":
        exact, other = (left, right) if left.op == "eq" else (right, left)
        return _matches(exact.value, other)
    if left.op == "ne" or right.op == "ne":
        return True
    if not _is_number(left.value) or not _is_number(right.value):
        return left == right
    left_lower = left.op in ("gt", "gte")
    right_lower = right.op in ("gt", "gte")
    if left_lower == right_lower:
        return True
    lower, upper = (left, right) if left_lower else (right, left)
    lo, hi = lower.value, upper.value
    assert isinstance(lo, int | float) and isinstance(hi, int | float)
    return lo < hi or (lo == hi and lower.op == "gte" and upper.op == "lte")


def _matches(value: Scalar, condition: Condition) -> bool:
    if condition.op in ("eq", "ne"):
        same_type = type(value) is type(condition.value) or (
            type(value) in (int, float) and type(condition.value) in (int, float)
        )
        return (same_type and eq(value, condition.value)) == (condition.op == "eq")
    if not _is_number(value) or not _is_number(condition.value):
        return False
    operation = {"gte": ge, "gt": gt, "lte": le, "lt": lt}[condition.op]
    return operation(value, condition.value)
