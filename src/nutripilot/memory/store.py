"""Replaceable in-memory semantic-memory boundary for offline execution."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: int
    kind: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InMemoryLongTermMemory:
    def __init__(self) -> None:
        self._records: dict[int, list[MemoryRecord]] = defaultdict(list)

    async def add(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.user_id].append(record)
        return record

    async def search(self, user_id: int, query: str, limit: int = 5) -> list[MemoryRecord]:
        terms = {term.casefold() for term in query.split() if term}
        records = self._records.get(user_id, [])
        ranked = sorted(
            records,
            key=lambda item: (
                -len(terms & {term.casefold() for term in item.content.split()}),
                -item.created_at.timestamp(),
            ),
        )
        return ranked[:limit]

    async def delete(self, user_id: int, memory_id: str) -> bool:
        records = self._records.get(user_id, [])
        kept = [record for record in records if record.id != memory_id]
        self._records[user_id] = kept
        return len(kept) != len(records)
