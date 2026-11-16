"""Trusted diet approval: no model-supplied commit call or private tool parameters."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nutripilot.agent.state import AgentState
from nutripilot.domain.diet import DietError
from nutripilot.domain.diet_records import DietChangePreview, IntakeDraft
from nutripilot.domain.execution import execution_fence
from nutripilot.domain.models import DietMemoryReference, FinalResponse

if TYPE_CHECKING:
    from nutripilot.application.diet_management import DietManagement


def preview_reference(preview: IntakeDraft | DietChangePreview) -> dict[str, Any]:
    return {
        "id": str(preview.id),
        "version": preview.version,
        "hash": preview.preview_hash if isinstance(preview, IntakeDraft) else preview.content_hash,
        "presented_at": datetime.now(UTC).isoformat(),
    }


def confirmation_id(reference: dict[str, Any]) -> str:
    return f"diet:{reference['id']}:{reference['version']}"


class DietCommitNode:
    def __init__(self, diet: DietManagement) -> None:
        self.diet = diet

    async def validate(
        self,
        state: AgentState,
        *,
        preview_version: int | None,
        content_hash: str | None,
    ) -> IntakeDraft | DietChangePreview:
        ref = state.get("pending_diet_preview_ref")
        if not ref or preview_version != ref["version"] or content_hash != ref["hash"]:
            raise DietError("VERSION_CONFLICT", "请核对当前饮食预览及版本后确认。")
        preview = await self.diet.preview(state["user_id"], UUID(ref["id"]))
        if preview.thread_id != state["thread_id"]:
            raise DietError("NOT_FOUND", "Preview not found in this conversation.")
        actual_hash = (
            preview.preview_hash if isinstance(preview, IntakeDraft) else preview.content_hash
        )
        if (
            content_hash is None
            or preview.version != preview_version
            or not hmac.compare_digest(actual_hash, content_hash)
        ):
            raise DietError("VERSION_CONFLICT", "预览已修改，请重新核对。")
        if preview.state not in ("ready", "committed"):
            raise DietError("INVALID_PREVIEW_STATE", "预览不可确认，请重新读取。")
        return preview

    async def run(self, state: AgentState) -> dict[str, Any]:
        ref = state.get("pending_diet_preview_ref")
        if ref is None:
            raise DietError("INVALID_PREVIEW_STATE", "No pending diet preview.")
        try:
            await self.validate(state, preview_version=ref["version"], content_hash=ref["hash"])
            async with execution_fence():
                receipt = await self.diet.confirm(
                    state["user_id"],
                    UUID(ref["id"]),
                    version=ref["version"],
                    approved_hash=ref["hash"],
                )
            message = {
                "committed": "已保存，本次确认不会重复写入。",
                "deleted": "原记录已删除，本次确认不会重新创建。",
                "purged": "原数据已清除，本次确认不会恢复它。",
            }.get(receipt.result_status, "操作已处理。")
            return {
                "phase": "COMPLETED",
                "requires_confirmation": False,
                "pending_tool_calls": [],
                "pending_diet_preview_ref": None,
                "final_response": FinalResponse(
                    status="completed",
                    message=message,
                    diet_context=DietMemoryReference(
                        state=receipt.result_status,
                        object_ids=[str(item.id) for item in receipt.result_refs],
                    ),
                ).model_dump(mode="json"),
            }
        except DietError as exc:
            return {
                "phase": "FAILED",
                "requires_confirmation": False,
                "pending_tool_calls": [],
                "pending_diet_preview_ref": None,
                "error": {"code": exc.code, "retryable": False},
                "final_response": FinalResponse(
                    status="degraded",
                    message=f"未完成本次提交：{exc}",
                    diet_context=DietMemoryReference(state="failed", object_ids=[ref["id"]]),
                ).model_dump(mode="json"),
            }
