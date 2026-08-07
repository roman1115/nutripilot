from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nutripilot.tools import ToolDefinition, ToolRegistry


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


async def _echo(arguments: BaseModel, context: Any) -> dict[str, str]:
    return {"text": EchoArgs.model_validate(arguments).text}


def test_registry_exports_openai_compatible_schema() -> None:
    registry = ToolRegistry([ToolDefinition("echo", "Echo validated text", EchoArgs, _echo)])

    schema = registry.planner_schemas()[0]

    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"]["additionalProperties"] is False
    assert schema["metadata"]["access"] == "read"


def test_registry_rejects_duplicate_names() -> None:
    definition = ToolDefinition("echo", "Echo validated text", EchoArgs, _echo)
    registry = ToolRegistry([definition])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
