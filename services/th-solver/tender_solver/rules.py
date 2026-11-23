from collections import defaultdict

from pydantic import JsonValue

from tender_solver.conditions import matches
from tender_solver.models import FactInput, Rule


def fact_map(facts: list[FactInput]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for fact in facts:
        key = fact.key
        if key in result:
            raise ValueError(f"duplicate fact key: {key}")
        result[key] = fact.value
    return result


def apply_rules(
    rules: list[Rule], facts: dict[str, JsonValue]
) -> tuple[list[Rule], list[tuple[Rule, Rule]]]:
    matched = [rule for rule in rules if matches(rule.condition, facts)]
    by_scope: dict[str, list[Rule]] = defaultdict(list)
    for rule in matched:
        by_scope[rule.scope].append(rule)
    conflicts: list[tuple[Rule, Rule]] = []
    for group in by_scope.values():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if left.consequence != right.consequence and left.priority == right.priority:
                    conflicts.append((left, right))
    matched.sort(key=lambda rule: (-rule.priority, rule.rule_id))
    return matched, conflicts
