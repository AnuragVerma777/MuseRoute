"""Answer checks for the auto-checkable workload dataset."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


_CHECKS = {"exact", "numeric", "json_fields", "contains"}
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def _parse_number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if not isinstance(value, str):
        return None
    cleaned = value.replace(",", "")
    matches = _NUMBER_RE.findall(cleaned)
    if len(matches) != 1:
        return None
    try:
        parsed = Decimal(matches[0])
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def score(answer: str, gold: Any, check: str) -> bool:
    """Return whether an answer satisfies its declared check against ``gold``.

    ``json_fields`` compares every key/value in the gold mapping, allowing
    additional keys in the answer object. JSON is parsed with ``json.loads``;
    Python expressions and malformed JSON are never evaluated.
    """
    if check not in _CHECKS or not isinstance(answer, str):
        return False
    if check == "exact":
        return isinstance(gold, str) and _normalise(answer) == _normalise(gold)
    if check == "contains":
        return isinstance(gold, str) and _normalise(gold) in _normalise(answer)
    if check == "numeric":
        answer_number = _parse_number(answer)
        gold_number = _parse_number(gold)
        return answer_number is not None and gold_number is not None and answer_number == gold_number
    if check == "json_fields":
        if not isinstance(gold, dict):
            return False
        try:
            parsed = json.loads(answer)
        except (json.JSONDecodeError, TypeError):
            return False
        return isinstance(parsed, dict) and all(key in parsed and parsed[key] == value for key, value in gold.items())
    return False
