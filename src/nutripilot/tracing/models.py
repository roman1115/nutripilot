"""Trace contracts independent of any external observability vendor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    trace_id: str
    thread_id: str
    turn_id: str
    user_id: int
    event_type: str
    component: str
    step_order: int = Field(ge=1)
    phase_before: str | None = None
    phase_after: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
