"""Trace recorder implementations used by the core and tests."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any, Protocol

from nutripilot.tracing.models import TraceEvent

_SENSITIVE_KEYS = {"api_key", "authorization", "password", "token", "secret"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***" if str(key).lower() in _SENSITIVE_KEYS else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 2_000:
        return value[:2_000] + "...[truncated]"
    return value


class TraceRecorder(Protocol):
    def record(
        self,
        *,
        trace_id: str,
        thread_id: str,
        turn_id: str,
        user_id: int,
        event_type: str,
        component: str,
        phase_before: str | None = None,
        phase_after: str | None = None,
        payload: dict[str, Any] | None = None,
        duration_ms: float = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> TraceEvent: ...

    def get_trace(self, trace_id: str) -> list[TraceEvent]: ...


class InMemoryTraceRecorder:
    """Thread-safe recorder for local development and deterministic tests."""

    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = defaultdict(list)
        self._lock = Lock()

    def record(
        self,
        *,
        trace_id: str,
        thread_id: str,
        turn_id: str,
        user_id: int,
        event_type: str,
        component: str,
        phase_before: str | None = None,
        phase_after: str | None = None,
        payload: dict[str, Any] | None = None,
        duration_ms: float = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> TraceEvent:
        with self._lock:
            event = TraceEvent(
                trace_id=trace_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                event_type=event_type,
                component=component,
                step_order=len(self._events[trace_id]) + 1,
                phase_before=phase_before,
                phase_after=phase_after,
                payload=_sanitize(payload or {}),
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            self._events[trace_id].append(event)
            return event

    def get_trace(self, trace_id: str) -> list[TraceEvent]:
        with self._lock:
            return list(self._events.get(trace_id, []))


class NullTraceRecorder:
    def record(self, **kwargs: Any) -> TraceEvent:
        return TraceEvent(step_order=1, payload={}, **kwargs)

    def get_trace(self, trace_id: str) -> list[TraceEvent]:
        return []
