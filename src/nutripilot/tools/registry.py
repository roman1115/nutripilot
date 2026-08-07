from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nutripilot.tools.models import ToolDefinition


class ToolRegistry:
    """Validated allowlist used by PlannerAgent and ToolExecutor."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition, *, replace: bool = False) -> None:
        if definition.name in self._definitions and not replace:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def register_many(
        self, definitions: Iterable[ToolDefinition], *, replace: bool = False
    ) -> None:
        for definition in definitions:
            self.register(definition, replace=replace)

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def require(self, name: str) -> ToolDefinition:
        definition = self.get(name)
        if definition is None:
            raise KeyError(f"Unknown tool: {name}")
        return definition

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def planner_schemas(self, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for definition in self.definitions():
            if allowed_names is not None and definition.name not in allowed_names:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": definition.json_schema(),
                    },
                    "metadata": {
                        "access": definition.access.value,
                        "source": definition.source,
                        "schema_version": definition.schema_version,
                        "implementation_version": definition.implementation_version,
                        "requires_confirmation": definition.requires_confirmation,
                    },
                }
            )
        return schemas

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)
