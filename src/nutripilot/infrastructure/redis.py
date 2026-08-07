from __future__ import annotations

import json
from typing import Any


class RedisTTLStore:
    """Optional Redis adapter with the same API as InMemoryTTLStore."""

    def __init__(self, client: Any, *, key_prefix: str = "nutripilot") -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

    @classmethod
    def from_url(cls, url: str, *, key_prefix: str = "nutripilot") -> RedisTTLStore:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError("redis is required to use RedisTTLStore") from exc
        return cls(Redis.from_url(url, decode_responses=True), key_prefix=key_prefix)

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self.client.get(self._key(key))
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        await self.client.set(
            self._key(key),
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
            ex=ttl_seconds,
        )

    async def add_if_absent(self, key: str, value: Any, ttl_seconds: int) -> bool:
        result = await self.client.set(
            self._key(key),
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
            ex=ttl_seconds,
            nx=True,
        )
        return bool(result)

    async def delete(self, key: str) -> None:
        await self.client.delete(self._key(key))

    async def close(self) -> None:
        await self.client.aclose()
