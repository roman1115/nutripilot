"""Conversation read models, independent of graph checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trace_id: str | None = None
    response: dict[str, Any] | None = None


class ConversationSummary(BaseModel):
    thread_id: str
    title: str
    updated_at: datetime
    message_count: int


class ConversationStore(Protocol):
    def append(self, user_id: int, thread_id: str, message: ConversationMessage) -> None: ...

    def list_threads(
        self, user_id: int, query: str, limit: int, offset: int
    ) -> list[ConversationSummary]: ...

    def messages(
        self, user_id: int, thread_id: str, limit: int, offset: int
    ) -> list[ConversationMessage]: ...
