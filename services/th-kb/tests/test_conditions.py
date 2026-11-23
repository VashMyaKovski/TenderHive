from decimal import Decimal

from th_kb.conditions import evaluate


def test_compares_numbers_and_strings() -> None:
    facts = {"margin": Decimal("8"), "scope": "state"}
    assert evaluate("margin < 10 and scope == state", facts)
    assert not evaluate("margin > 10", facts)
    assert evaluate("scope != market", facts)


def test_missing_fact_is_not_a_match() -> None:
    assert not evaluate("unknown >= 1", {"margin": 2})


def test_invalid_clause_is_rejected() -> None:
    try:
        evaluate("margin ~ 1", {"margin": 2})
    except ValueError:
        return
    raise AssertionError("invalid condition must fail")
