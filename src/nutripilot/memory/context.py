"""Context engineering helpers kept independent from model providers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AnyMessage, convert_to_messages

if TYPE_CHECKING:
    from nutripilot.agent.state import AgentState


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


def ordered_messages(state: AgentState) -> list[AnyMessage]:
    messages = cast("list[AnyMessage]", convert_to_messages(state.get("messages", [])))
    return [
        item for _, item in sorted(enumerate(messages), key=lambda pair: (
            int(pair[1].additional_kwargs.get("position") or 2**63), pair[0],
        ))
    ]


def conversation_context(state: AgentState) -> dict[str, Any]:
    """Project shared conversation data without changing stored originals."""
    snapshot = state.get("context_snapshot") or {}
    if snapshot:
        return {
            "recent_messages": snapshot["recent_messages"],
            "conversation_summary": snapshot["conversation_summary"],
            "history_recall": snapshot.get("history_recall", []),
            "recent_objects": snapshot.get("recent_objects", []),
        }
    if state.get("messages") or state.get("memory_summary"):
        recent = [
            {
                "role": "assistant" if message.type == "ai" else "user",
                "content": message.content,
            }
            for message in ordered_messages(state)
            if message.id != f"{state.get('turn_id')}:user"
        ]
        summary = state.get("memory_summary") or {}
        return {
            "recent_messages": recent,
            "conversation_summary": (
                json.dumps({
                    "entries": summary["entries"], "objects": summary.get("objects", []),
                }, ensure_ascii=False)
                if summary.get("entries") else None
            ),
            "history_recall": [],
            "recent_objects": [
                {
                    "source_message_id": message.id,
                    "displayed_meals": message.additional_kwargs["displayed_meals"],
                }
                for message in ordered_messages(state)
                if message.additional_kwargs.get("displayed_meals")
            ],
        }
    # Old checkpoints and isolated agent callers retain a read-only compatibility path.
    return {
        "recent_messages": state.get("recent_messages", []),
        "conversation_summary": state.get("conversation_summary"),
        "history_recall": [],
        "recent_objects": [],
    }


def constraint_sources(state: AgentState, slots: dict[str, Any]) -> dict[str, Any]:
    sources = dict(state.get("active_constraints", {}))
    for field, value in slots.items():
        if value != state.get("slots", {}).get(field) or field not in sources:
            sources[field] = {
                "value": value,
                "operation": "clear" if value is None else "set",
                "source_message_id": f"{state['turn_id']}:user",
                "user_id": state["user_id"],
                "date": state["current_date"],
                "scope": "conversation",
            }
    for field in sources.keys() - slots.keys():
        sources[field] = {
            **sources[field], "value": None, "operation": "clear",
            "source_message_id": f"{state['turn_id']}:user",
        }
    return sources
