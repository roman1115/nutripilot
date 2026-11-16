"""The frozen design examples are also the executable tool boundary contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from nutripilot.tools.local.diet_models import DIET_ARGUMENT_MODELS

CONTRACT = Path(__file__).resolve().parents[2] / "docs/specs/diet-management.schema-cases.json"


def contract_cases() -> list[tuple[str, dict[str, Any], bool]]:
    resolved: dict[str, dict[str, Any]] = {}
    result = []
    for case in json.loads(CONTRACT.read_text())["cases"]:
        payload = copy.deepcopy(case["input"] if "input" in case else resolved[case["base"]])
        for change in case.get("changes", []):
            parent = payload
            for key in change["path"][:-1]:
                parent = parent[key]
            key = change["path"][-1]
            if change.get("remove"):
                del parent[key]
            else:
                parent[key] = copy.deepcopy(change["value"])
        resolved[case["id"]] = payload
        # Ownership is an application check; all other documented semantics are local rules.
        valid = case["schema_valid"] and (
            not case.get("semantic_expectation") or case["id"] == "semantic-unowned-reference"
        )
        result.append((case["id"], payload, valid))
    return result


@pytest.mark.parametrize(
    "case_id,payload,valid",
    contract_cases(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_frozen_examples(case_id: str, payload: dict[str, Any], valid: bool) -> None:
    model = DIET_ARGUMENT_MODELS.get(payload["tool"])
    if model is None:
        assert not valid, case_id
        return
    if not valid:
        with pytest.raises(ValidationError):
            model.model_validate(payload["arguments"])
    else:
        parsed = model.model_validate(payload["arguments"])
        restored = model.model_validate_json(parsed.model_dump_json(exclude_unset=True))
        assert restored == parsed


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "food", "limit": True},
        {"query": "food", "limit": "10"},
        {"query": "  "},
        {"query": "food", "food_kind": "sql"},
    ],
)
def test_query_strict_boundary(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DIET_ARGUMENT_MODELS["search_foods"].model_validate(arguments)


@pytest.mark.parametrize(
    "case_id,payload,valid",
    contract_cases(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_generated_tool_schema_rejects_the_same_structural_examples(
    case_id: str,
    payload: dict[str, Any],
    valid: bool,
) -> None:
    model = DIET_ARGUMENT_MODELS.get(payload["tool"])
    if model is None:
        return
    case = next(case for case in json.loads(CONTRACT.read_text())["cases"] if case["id"] == case_id)
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    actual = Draft202012Validator(schema).is_valid(payload["arguments"])
    # Runtime validators cover order/ownership/empty-patch semantics beyond JSON Schema.
    if case["schema_valid"]:
        assert actual
    elif actual:
        with pytest.raises(ValidationError):
            model.model_validate(payload["arguments"])
