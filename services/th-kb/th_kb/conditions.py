import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import JsonValue

_CONDITION = re.compile(r"^([A-Za-z0-9_.:/@+-]+)\s*(==|!=|>=|<=|>|<)\s*(.+)$")
_OPERATORS = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    ">": lambda left, right: left > right,
    ">=": lambda left, right: left >= right,
    "<": lambda left, right: left < right,
    "<=": lambda left, right: left <= right,
}


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _literal(raw: str) -> Any:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    number = _decimal(text)
    return text if number is None else number


def _compare(left: JsonValue, operator: str, right: Any) -> bool:
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is not None and right_number is not None:
        return bool(_OPERATORS[operator](left_number, right_number))
    return bool(_OPERATORS[operator](str(left), str(right)))


def evaluate(condition: str, facts: Mapping[str, JsonValue]) -> bool:
    """Evaluate a simple ``field operator value [and ...]`` condition without ``eval``."""
    clauses = [part.strip() for part in condition.split(" and ") if part.strip()]
    if not clauses:
        return False
    for clause in clauses:
        match = _CONDITION.fullmatch(clause)
        if match is None:
            raise ValueError(f"invalid condition clause: {clause}")
        key, operator, raw = match.groups()
        if key not in facts:
            return False
        if not _compare(facts[key], operator, _literal(raw)):
            return False
    return True
