"""Context engineering helpers kept independent from model providers."""

from __future__ import annotations

from typing import Any


def merge_slots(existing: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    """Merge new explicit facts while ignoring empty model output."""

    merged = dict(existing)
    for key, value in extracted.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            prior = merged.get(key, [])
            prior_items = prior if isinstance(prior, list) else []
            merged[key] = list(dict.fromkeys([*prior_items, *value]))
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def compact_messages(messages: list[dict[str, str]], *, max_characters: int = 2_000) -> str:
    """Create a deterministic local summary when a remote summarizer is unavailable."""

    lines = [
        f"{item.get('role', 'unknown')}: {item.get('content', '').strip()}"
        for item in messages[-12:]
        if item.get("content", "").strip()
    ]
    text = "\n".join(lines)
    if len(text) <= max_characters:
        return text
    return "...[earlier context omitted]\n" + text[-max_characters:]
