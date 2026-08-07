from __future__ import annotations

from typing import Any


class ToolRuntimeError(Exception):
    """Expected tool failure with a user/model-safe message."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.details = details or {}


class ToolUnavailableError(ToolRuntimeError):
    def __init__(
        self, safe_message: str = "The requested tool is temporarily unavailable."
    ) -> None:
        super().__init__("TOOL_UNAVAILABLE", safe_message, retryable=True)
