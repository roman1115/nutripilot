"""Replay factual-review failures and valid answers against the configured real model."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.nutrition_mcp.service import NutritionMCPService
from scripts.verify_showcase import probe_model

from nutripilot.agent.agents.recommendation import RecommendationAgent
from nutripilot.agent.answers import (
    answer_review_issues,
    answer_review_records,
    answer_review_units,
    canonical_answer,
    compact_answer,
    guard_answer_facts,
)
from nutripilot.agent.contracts import AnswerReview, RecommendationDraft
from nutripilot.agent.state import AgentState, new_agent_state
from nutripilot.config import Settings
from nutripilot.model_gateway import OpenAICompatibleModelGateway
from nutripilot.model_gateway.budget import execution_budget
from nutripilot.paths import project_root
from nutripilot.safety import OutputGuard
from nutripilot.tools.mcp.models import GetNutritionMCPArgs


async def verify(output: Path) -> dict[str, Any]:
    settings = Settings(model_provider="openai")
    probe = await probe_model(settings)
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(), "probe": probe,
        "prompt_version": RecommendationAgent.prompt_version, "cases": [], "passed": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if probe["status"] != "passed":
        await asyncio.to_thread(
            output.write_text, json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return report
    service = NutritionMCPService.from_seed_data(project_root() / "data")
    recipe = (await service.get_recipe(GetNutritionMCPArgs(meal_id=1034))).model_dump(mode="json")
    meal = await service.store.get_meal(1034)
    assert meal is not None
    state = new_agent_state(message="为什么推荐第一道？", user_id=1)
    state.update({
        "intent": "FOOD_QUESTION",
        "slots": {"required_ingredients": ["鸡胸肉"]},
        "discussed_meals": [{"meal_id": 1034, "name": meal.name, "reason": "含鸡胸肉"}],
        "conversation_evidence": [{
            "tool_name": "search_meals", "status": "success",
            "data": {"hits": [meal.model_dump(mode="json")]},
        }],
    })
    recipe_state: AgentState = {
        **state, "tool_results": [
            {"tool_name": "get_recipe", "status": "success", "data": recipe},
        ],
    }
    candidates = [meal]
    for meal_id in (1067, 1061):
        candidate = await service.store.get_meal(meal_id)
        assert candidate is not None
        candidates.append(candidate)
    single_card_state: AgentState = {
        **state, "intent": "MEAL_RECOMMENDATION", "discussed_meals": [],
        "candidate_ids": [item.id for item in candidates],
        "conversation_evidence": [],
        "tool_results": [{
            "tool_name": "search_meals", "status": "success",
            "data": {"hits": [item.model_dump(mode="json") for item in candidates]},
        }],
    }
    three_displayed_state: AgentState = {
        **state,
        "discussed_meals": [
            {"meal_id": item.id, "name": item.name, "reason": ""} for item in candidates
        ],
        "conversation_evidence": single_card_state["tool_results"],
    }
    cases: list[tuple[str, bool, AgentState, str, str]] = [
        ("ranking", False, state, "为什么推荐第一道？",
         "第一道被优先推荐，因为最大程度保留了鸡胸肉的嫩滑口感，比其他候选更适合你。"),
        ("health_effect", False, state, "为什么推荐它？",
         "这道菜的食材搭配能延缓餐后血糖波动，适合控制血糖。"),
        ("condition_dropped", False, recipe_state, "下次怎么煮？",
         "严格控时，非冷冻鸡胸肉水沸后中火煮5分钟即关火，这样就熟了。"),
        ("recipe_inferred_from_name", False, state, "为什么推荐第一道？",
         "凉拌鸡丝制作简单，煮熟撕丝拌汁即可，是最省时间的选择。"),
        ("matched_ingredient", True, state, "为什么推荐第一道？",
         "凉拌鸡丝的配方包含你想吃的鸡胸肉，因此列在候选中。展示顺序不代表排名。"),
        ("general_rescue", True, state, "已经做柴了怎么补救？",
         "如果已经熟透但发柴，可以撕细后拌些酱汁改善入口感受，但不能恢复已经流失的水分。"),
        ("conditions_kept", True, recipe_state, "煮多久？",
         "原配方列出非冷冻鸡胸肉煮5分钟、冷冻的煮7分钟；大小会影响成熟时间，"
         "不能仅凭到时关火判断熟透，未熟时还需延长煮制。"),
        ("count_mismatch", False, state, "想吃鸡胸肉",
         "这两道菜都含鸡胸肉：凉拌鸡丝。"),
        ("plural_mismatch", False, state, "想吃鸡胸肉",
         "这几道菜的配方里都有鸡胸肉：凉拌鸡丝。"),
        ("single_match", True, state, "想吃鸡胸肉",
         "可以考虑凉拌鸡丝，配方里有鸡胸肉。"),
        ("general_rescue_variant", True, state, "肉柴了怎么办",
         "已经熟透的话，可以切成小块拌进汤面里，入口时不那么干；不必反复加热。"),
        ("contradictory_rescue", False, state, "已经做柴了怎么补救",
         "可以把熟鸡胸肉再蒸一下。\n\n注意完全不要再加热。"),
        ("qualified_rescue", True, state, "已经做柴了怎么补救",
         "如果已经熟透，可以撕细拌酱改善入口感受；避免反复长时间加热。"),
        ("mixed_health_claim", False, state, "想吃鸡胸肉",
         "凉拌鸡丝含鸡胸肉，而且能稳定餐后血糖。"),
        ("single_card_plural", False, single_card_state, "推荐一道含鸡胸肉的菜",
         "这几道菜里都有鸡胸肉，可以选凉拌鸡丝。"),
        ("single_card_valid", True, single_card_state, "推荐一道含鸡胸肉的菜",
         "可以选凉拌鸡丝，它的配方包含鸡胸肉。"),
        ("three_objects_valid", True, three_displayed_state, "这几道有什么共同点",
         "这三道菜的配方都含有鸡胸肉：凉拌鸡丝、蛋包饭、汤面。"),
        ("singular_subset_valid", True, three_displayed_state, "为什么第一道",
         "凉拌鸡丝这道菜的配方包含鸡胸肉，符合你想吃的食材。"),
        ("three_card_inferred_methods", False, single_card_state, "想吃鸡胸肉",
         "这三道菜都含鸡胸肉：凉拌鸡丝是清爽开胃的冷盘；"
         "蛋包饭把鸡胸肉切丁混入蛋液和米饭中，口感软嫩；"
         "汤面用鸡胸肉片煮进清汤，暖身又轻盈。"),
        ("three_card_ingredient_match", True, single_card_state, "想吃鸡胸肉",
         "凉拌鸡丝、蛋包饭、汤面的收录配方都包含鸡胸肉，可以从这三道中选择。"),
    ]
    gateway = OpenAICompatibleModelGateway(
        api_key=settings.model_api_key or "", base_url=settings.model_base_url,
        model=settings.model_name, max_retries=settings.model_max_retries,
        timeout_seconds=settings.model_timeout_seconds,
    )
    try:
        for name, expected, context, question, text in cases:
            current = context.copy()
            current["user_message"] = question
            selected = candidates if name.startswith("three_card_") else [meal]
            draft = RecommendationDraft(message=text, recommendations=(
                [{"meal_id": item.id, "name": item.name} for item in selected]
                if current["intent"] == "MEAL_RECOMMENDATION" else []
            ))
            units = answer_review_units(draft)
            canonical, _, _ = guard_answer_facts(
                canonical_answer(current, draft), current, OutputGuard(),
            )
            records = answer_review_records(current, draft, canonical)
            objects = {
                "recommendations": [item.meal_id for item in canonical.recommendations],
                "display": [item["meal_id"] for item in current.get("discussed_meals", [])],
            }
            payload = {
                "user_message": question, "intent": current["intent"],
                "review_units": units, "evidence_records": records,
                "verified_facts": compact_answer(canonical),
                "objects": objects,
            }
            try:
                with execution_budget(
                    seconds=settings.agent_deadline_seconds, tokens=settings.agent_token_budget,
                ):
                    result = await gateway.generate_structured(
                        worker="answer_review", system_prompt=RecommendationAgent.review_prompt,
                        payload=payload, response_model=AnswerReview,
                        prompt_version=RecommendationAgent.prompt_version,
                    )
                issues = answer_review_issues(result.output, units, records, objects)
                accepted = not issues
                semantic_accepted = (
                    result.output.supported and result.output.coherent and not result.output.issues
                    and bool(result.output.checks)
                    and all(
                        check.supported and check.conditions_preserved
                        for check in result.output.checks
                    )
                )
                item = {
                    "name": name, "expected_accepted": expected, "accepted": accepted,
                    "semantic_accepted": semantic_accepted,
                    "passed": accepted == expected and (
                        semantic_accepted == expected or (
                            not expected and any(
                                "OBJECT_COUNT_MISMATCH" in issue for issue in issues
                            )
                        )
                    ),
                    "question": question, "draft": text,
                    "review": result.output.model_dump(mode="json"),
                    "validation_issues": issues,
                    "usage": result.usage.model_dump(), "model": result.model,
                    "used_fallback": result.used_fallback, "input": payload,
                }
            except Exception as exc:
                item = {
                    "name": name, "passed": False, "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None),
                    "validation_error": str(exc.__cause__)[:1800] if exc.__cause__ else None,
                }
            report["cases"].append(item)
            await asyncio.to_thread(
                output.write_text, json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({
                key: item[key]
                for key in ("name", "passed", "accepted", "semantic_accepted", "error_type")
                if key in item
            }), flush=True)
    finally:
        await gateway.client.close()
    report["passed"] = all(item["passed"] for item in report["cases"])
    await asyncio.to_thread(
        output.write_text, json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return 0 if asyncio.run(verify(args.output))["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
