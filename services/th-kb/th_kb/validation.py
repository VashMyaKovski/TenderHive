from collections import defaultdict
from collections.abc import Callable

from th_kb.conditions import evaluate
from th_kb.errors import ErrorCode, KbError
from th_kb.models import CheckResult, Rule, TestCase, VersionPayload, compute_checksum

Check = Callable[[VersionPayload, VersionPayload | None], CheckResult]


def _passed(code: str, message: str) -> CheckResult:
    return CheckResult(code=code, passed=True, message=message)


def check_checksum(payload: VersionPayload, _active: VersionPayload | None) -> CheckResult:
    if compute_checksum(payload) != payload.checksum:
        raise KbError(ErrorCode.CHECKSUM_MISMATCH, "checksum does not match the version payload")
    return _passed("CHECKSUM", "checksum verified")


def check_integrity(payload: VersionPayload, _active: VersionPayload | None) -> CheckResult:
    rules = [rule.rule_id for rule in payload.rules]
    facts = [fact.fact_id for fact in payload.facts]
    cases = [case.case_id for case in payload.test_cases]
    for name, identifiers in (("rule", rules), ("fact", facts), ("test case", cases)):
        if len(identifiers) != len(set(identifiers)):
            raise KbError(ErrorCode.INTEGRITY_ERROR, f"duplicate {name} identifier")
    known_rules = set(rules)
    for case in payload.test_cases:
        if case.expected_rule_id is not None and case.expected_rule_id not in known_rules:
            raise KbError(
                ErrorCode.INTEGRITY_ERROR,
                f"test case {case.case_id} references unknown rule {case.expected_rule_id}",
            )
    return _passed("INTEGRITY", "identifiers and references are consistent")


def check_types(payload: VersionPayload, _active: VersionPayload | None) -> CheckResult:
    signatures: dict[tuple[str, str], tuple[str, str | None]] = {}
    for fact in payload.facts:
        signature = (type(fact.value).__name__, fact.unit)
        key = (fact.subject, fact.key)
        if key in signatures and signatures[key] != signature:
            raise KbError(ErrorCode.INTEGRITY_ERROR, f"inconsistent value type for fact {key}")
        signatures[key] = signature
    return _passed("TYPES", "fact types and units are consistent")


def check_periods(payload: VersionPayload, _active: VersionPayload | None) -> CheckResult:
    for rule in payload.rules:
        if rule.effective_from and rule.effective_to and rule.effective_from >= rule.effective_to:
            raise KbError(ErrorCode.INTEGRITY_ERROR, f"invalid period for rule {rule.rule_id}")
    for fact in payload.facts:
        if fact.valid_from and fact.valid_to and fact.valid_from >= fact.valid_to:
            raise KbError(ErrorCode.INTEGRITY_ERROR, f"invalid period for fact {fact.fact_id}")
    return _passed("PERIODS", "effective periods are ordered")


def _overlaps(left: Rule, right: Rule) -> bool:
    left_ends_before_right = (
        left.effective_to is not None
        and right.effective_from is not None
        and left.effective_to <= right.effective_from
    )
    right_ends_before_left = (
        right.effective_to is not None
        and left.effective_from is not None
        and right.effective_to <= left.effective_from
    )
    return not (left_ends_before_right or right_ends_before_left)


def check_compatibility(payload: VersionPayload, _active: VersionPayload | None) -> CheckResult:
    by_scope: dict[str, list[Rule]] = defaultdict(list)
    for rule in payload.rules:
        by_scope[rule.scope].append(rule)
    for rules in by_scope.values():
        for index, left in enumerate(rules):
            for right in rules[index + 1 :]:
                if left.consequence != right.consequence and _overlaps(left, right):
                    raise KbError(
                        ErrorCode.INCOMPATIBLE_RULES,
                        f"rules {left.rule_id} and {right.rule_id} conflict",
                    )
    return _passed("COMPATIBILITY", "no incompatible rules in overlapping scopes")


def _passed_cases(payload: VersionPayload, test_cases: list[TestCase]) -> int:
    count = 0
    for case in test_cases:
        matched = [rule for rule in payload.rules if evaluate(rule.condition, case.input_facts)]
        matched.sort(key=lambda rule: (-rule.priority, rule.rule_id))
        winner = matched[0] if matched else None
        if case.expected_rule_id is not None:
            count += int(winner is not None and winner.rule_id == case.expected_rule_id)
        elif winner is not None:
            count += int(winner.consequence == case.expected_consequence)
    return count


def check_tests(payload: VersionPayload, active: VersionPayload | None) -> CheckResult:
    current = _passed_cases(payload, list(payload.test_cases))
    if active is None:
        return _passed("TESTS", f"{current}/{len(payload.test_cases)} cases passed")
    previous = _passed_cases(active, list(payload.test_cases))
    if current < previous:
        raise KbError(
            ErrorCode.REGRESSION_DETECTED,
            f"test results regressed: {previous} -> {current} passed cases",
        )
    return _passed("TESTS", f"{current}/{len(payload.test_cases)} cases passed")


CHECKS: tuple[Check, ...] = (
    check_checksum,
    check_integrity,
    check_types,
    check_periods,
    check_compatibility,
    check_tests,
)


def validate_for_activation(
    payload: VersionPayload, active: VersionPayload | None
) -> list[CheckResult]:
    """Run pre-activation checks in order and collect successful results.

    A failing check raises :class:`KbError` carrying the results collected so far, so
    the API can return a rejected activation report instead of a partial activation.
    """
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check(payload, active))
        except KbError as error:
            error.report = results
            raise
    return results
