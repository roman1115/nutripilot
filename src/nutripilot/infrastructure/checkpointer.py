"""Fence checkpoint writes against an obsolete durable task owner."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from nutripilot.domain.execution import execution_fence


class FencedCheckpointer(BaseCheckpointSaver[Any]):
    def __init__(self, delegate: Any) -> None:
        super().__init__(serde=delegate.serde)
        self.delegate = delegate

    def get_next_version(self, current: Any, channel: None) -> Any:
        return self.delegate.get_next_version(current, channel)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await self.delegate.aget_tuple(config)  # type: ignore[no-any-return]

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for checkpoint in self.delegate.alist(
            config, filter=filter, before=before, limit=limit
        ):
            yield checkpoint

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        async with execution_fence():
            return await self.delegate.aput(  # type: ignore[no-any-return]
                config, checkpoint, metadata, new_versions
            )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        async with execution_fence():
            await self.delegate.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await self.delegate.adelete_thread(thread_id)
