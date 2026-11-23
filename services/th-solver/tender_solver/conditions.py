import operator
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import JsonValue

_CLAUSE = re.compile(r"^([A-Za-z0-9_.:/@+-]+)\s*(==|!=|>=|<=|>|<)\s*(.+)$")
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation, ValueError:
        return None


def _literal(raw: str) -> Any:
    text = raw.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    number = _number(text)
    return text if number is None else number


def matches(condition: str, facts: Mapping[str, JsonValue]) -> bool:
    """Evaluate ``field operator value [and ...]`` conditions without ``eval``."""
    clauses = [part.strip() for part in condition.split(" and ") if part.strip()]
    if not clauses:
        return False
    for clause in clauses:
        parsed = _CLAUSE.fullmatch(clause)
        if parsed is None:
            return False
        key, symbol, raw = parsed.groups()
        if key not in facts:
            return False
        left, right = facts[key], _literal(raw)
        left_number, right_number = _number(left), _number(right)
        if left_number is not None and right_number is not None:
            left, right = left_number, right_number
        else:
            left, right = str(left), str(right)
        if not _OPERATORS[symbol](left, right):
            return False
    return True
