"""Check design schemas and examples; does not validate runtime business rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def main() -> None:
    directory = Path(__file__).resolve().parent
    schema = json.loads((directory / "diet-management.schema.json").read_text())
    examples = json.loads((directory / "diet-management.schema-cases.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    tools = schema["x-tools"]
    assert len(tools) == 10
    assert len({tool["name"] for tool in tools}) == 10
    assert sum(tool["access"] == "read" for tool in tools) == 6
    assert sum(tool["access"] == "write" for tool in tools) == 4

    resolved: dict[str, dict] = {}
    positive = negative = semantic = 0
    covered_tools: set[str] = set()
    covered_actions: dict[str, set[str]] = {}
    for case in examples["cases"]:
        assert case["id"] not in resolved, case["id"]
        assert ("input" in case) != ("base" in case), case["id"]
        payload = copy.deepcopy(case["input"] if "input" in case else resolved[case["base"]])
        for change in case.get("changes", []):
            path = change["path"]
            assert path, case["id"]
            parent = payload
            for key in path[:-1]:
                parent = parent[key]
            if change.get("remove"):
                del parent[path[-1]]
            else:
                parent[path[-1]] = copy.deepcopy(change["value"])
        errors = list(validator.iter_errors(payload))
        assert (not errors) == case["schema_valid"], (
            case["id"],
            [error.message for error in errors],
        )
        resolved[case["id"]] = payload
        if case["schema_valid"]:
            positive += 1
            covered_tools.add(payload["tool"])
            if "action" in payload["arguments"]:
                covered_actions.setdefault(payload["tool"], set()).add(
                    payload["arguments"]["action"]
                )
        else:
            negative += 1
        semantic += bool(case.get("semantic_expectation"))

    assert covered_tools == {tool["name"] for tool in tools}
    assert covered_actions == {
        "prepare_intake_draft": {"create", "revise", "discard"},
        "prepare_diet_log_change": {"correct", "delete", "restore"},
        "prepare_meal_plan_change": {"create", "update", "delete", "copy"},
        "prepare_target_change": {"set", "correct", "stop"},
    }
    print(f"Schema checks passed: {positive} accepted, {negative} rejected; 10 tools, 13 actions.")
    print(f"Semantic-only examples documented: {semantic}; runtime validation NOT checked.")


if __name__ == "__main__":
    main()
