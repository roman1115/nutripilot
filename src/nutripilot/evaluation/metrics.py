"""Deterministic metrics used before any LLM Judge."""

from __future__ import annotations

from typing import Any


def expected_slots_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, list):
            if not set(expected_value).issubset(set(actual_value or [])):
                return False
        elif actual_value != expected_value:
            return False
    return True


def response_contains(response: dict[str, Any] | None, terms: list[str]) -> bool:
    if not terms:
        return True
    message = str((response or {}).get("message", ""))
    return all(term in message for term in terms)


def rate(values: list[bool]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0
