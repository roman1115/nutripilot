"""Deterministic model gateway used without credentials and in tests."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import suppress
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from nutripilot.agent.contracts import PlanSpec
from nutripilot.agent.grounding import ground_recommendation_facts
from nutripilot.agent.planning import PlanBuilder, PlanBuildError, candidate_meals_from_results
from nutripilot.agent.slots import (
    explicit_food_request,
    is_negative_ingredient,
    is_recipe_request,
    resolve_recipe_name,
)
from nutripilot.domain.enums import Intent
from nutripilot.domain.models import FinalResponse
from nutripilot.model_gateway.base import GatewayResult, ModelOutputT, ModelUsage
from nutripilot.safety import extract_declared_allergens, merge_dietary_terms

FakeHandler = Callable[[dict[str, Any]], BaseModel | dict[str, Any]]


class FakeModelGateway:
    """A reproducible substitute for a chat model.

    It exercises the same Pydantic validation boundary as a remote model. Tests can
    queue exact payloads or exceptions to cover malformed-output and outage paths.
    """

    model_name = "fake-nutripilot-v1"

    def __init__(self) -> None:
        self._queued: dict[str, deque[BaseModel | dict[str, Any] | BaseException]] = defaultdict(
            deque
        )
        self._handlers: dict[str, FakeHandler] = {
            "intent": self._intent,
            "planner": self._planner,
            "recommendation": self._recommendation,
            "answer_boundary": lambda payload: {
                "facts_match": True, "objects_match": True, "portion_basis_preserved": True,
                "cooking_conditions_preserved": True, "issues": [],
            },
            "answer_review": self._answer_review,
            "evaluation": self._evaluation,
            "memory_summary": self._memory_summary,
        }
        self.call_history: list[dict[str, Any]] = []

    def queue_response(
        self, worker: str, response: BaseModel | dict[str, Any] | BaseException
    ) -> None:
        self._queued[worker].append(response)

    def register_handler(self, worker: str, handler: FakeHandler) -> None:
        self._handlers[worker] = handler

    async def generate_structured(
        self,
        *,
        worker: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[ModelOutputT],
        prompt_version: str,
    ) -> GatewayResult[ModelOutputT]:
        self.call_history.append(
            {
                "worker": worker,
                "payload": payload,
                "prompt_version": prompt_version,
                "system_prompt": system_prompt,
            }
        )
        if self._queued[worker]:
            raw = self._queued[worker].popleft()
            if isinstance(raw, BaseException):
                raise raw
        else:
            handler = self._handlers.get(worker)
            if handler is None:
                raise ValueError(f"No fake handler registered for worker: {worker}")
            raw = handler(payload)

        output = raw if isinstance(raw, response_model) else response_model.model_validate(raw)
        input_tokens = max(1, len(str(payload)) // 4)
        output_tokens = max(1, len(output.model_dump_json()) // 4)
        return GatewayResult(
            output=output,
            model=self.model_name,
            usage=ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )

    @staticmethod
    def _memory_summary(payload: dict[str, Any]) -> dict[str, Any]:
        # This deterministic projection tests the contract, not semantic summarization.
        entries = [
            *payload.get("old_summary", []),
            *[
                {
                    "kind": "assistant" if message["role"] == "ai" else "user",
                    "text": str(message["content"])[:100] or "(empty)",
                    "source_message_ids": [message["id"]],
                }
                for message in payload["messages"]
            ],
        ]
        return {"entries": entries[-24:]}

    @staticmethod
    def _answer_review(payload: dict[str, Any]) -> dict[str, Any]:
        # Fake checks exercise the contract only, not semantic correctness.
        return {
            "supported": True, "issues": [], "coherent": True,
            "checks": [
                {
                    "unit_id": unit_id, "kind": "general_advice", "supported": True,
                    "has_object_count": False, "object_counts": [],
                    "conditions_preserved": True, "conditions": [], "evidence": [],
                    "explanation": "Fake contract check; not a factual assessment.",
                }
                for unit_id in payload["review_units"]
            ],
        }

    @staticmethod
    def _intent(payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message", "")).strip()
        slots = dict(payload.get("slots") or {})
        current_date = (
            date.fromisoformat(str(payload["current_date"]))
            if payload.get("current_date")
            else date.today()
        )
        slots.update(_extract_slots(message, current_date=current_date))
        for field in slots.get("clear_constraints", []):
            slots.pop(field, None)
        slots.pop("required_ingredients", None)
        slots.pop("ingredient_any_groups", None)
        slots.pop("ingredient_action", None)
        requested = explicit_food_request(message)
        # The fake only recognises these regression ingredients; the real model extracts them.
        ingredients = [
            term for term in (
                "鸡胸肉", "鸡翅", "番茄", "鸡蛋", "西红柿", "豆腐", "土豆", "马铃薯",
                "花菜", "西兰花", "香菇", "香菜", "牛肉", "龙肉",
            )
            if term in message
        ]
        if ingredients:
            slots["required_ingredients"] = ingredients
            if "或" in message and len(ingredients) > 1:
                slots["ingredient_any_groups"] = [ingredients]
                slots.pop("required_ingredients")
        if any(term in message for term in ("不限食材", "食材不限", "取消食材限制")):
            slots["ingredient_action"] = "clear"
        elif any(term in message for term in ("再加", "加上", "还要")):
            slots["ingredient_action"] = "append"
        elif any(term in message for term in ("改成", "换成")):
            slots["ingredient_action"] = "replace"
        excluded = [term for term in ingredients if is_negative_ingredient(message, term)]
        if excluded:
            slots["excluded_ingredients"] = merge_dietary_terms(
                slots.get("excluded_ingredients"), excluded
            )

        if any(word in message for word in ("诊断", "处方", "药物", "催吐", "绝食")):
            intent = Intent.HEALTH_RISK
        elif any(
            word in message
            for word in (
                "一周",
                "一星期",
                "七天",
                "7天",
                "饮食计划",
                "餐单",
                "一天",
                "一日",
            )
        ):
            intent = Intent.MEAL_PLAN
            slots["plan_range"] = (
                "one_day"
                if any(word in message for word in ("一天", "一日", "今天", "今日"))
                else "one_week"
            )
        elif message.strip("，,。！!？? ") in {
            "谢谢", "好的，谢谢", "好的", "明白了", "你好", "谢谢你", "不客气",
        }:
            intent = Intent.CONVERSATION
        elif any(term in message for term in (
            "为什么", "区别", "比较", "怎么做才", "烹饪技巧", "失败补救",
        )) and (
            payload.get("discussed_meals")
            or any(term in message for term in (
                "鸡胸肉", "做菜", "烹饪", "食材", "第二个", "前两个",
            ))
        ):
            intent = Intent.FOOD_QUESTION
        elif is_recipe_request(message):
            intent = Intent.RECIPE_QUERY
        elif slots.get("clear_constraints") or any(word in message for word in (
            "换一个", "换一批", "不想吃这个", "再来", "改成", "换成", "再加", "加上", "还要",
            "不限食材", "食材不限", "取消食材限制",
        )):
            intent = Intent.MEAL_ADJUSTMENT
        elif any(word in message for word in ("热量", "蛋白质", "营养", "脂肪", "碳水")):
            intent = Intent.NUTRITION_QUERY
        elif any(word in message for word in ("记住", "更新偏好", "以后不要", "我喜欢")):
            intent = Intent.PROFILE_UPDATE
        elif requested or slots.get("ingredient_any_groups") or any(
            word in message for word in ("吃什么", "推荐", "早餐", "午餐", "晚餐", "夜宵")
        ):
            intent = Intent.MEAL_RECOMMENDATION
        else:
            intent = Intent.OTHER

        missing: list[str] = []
        if intent is Intent.MEAL_RECOMMENDATION and "meal_type" not in slots:
            missing.append("meal_type")
        if (
            intent is Intent.OTHER
            and payload.get("clarification_intent") in {
                Intent.NUTRITION_QUERY.value, Intent.RECIPE_QUERY.value
            }
        ):
            intent = Intent(payload["clarification_intent"])
            slots["food_name"] = message.strip("，。？? ")
        elif (
            intent is Intent.OTHER and payload.get("clarification_intent") == Intent.MEAL_PLAN.value
        ):
            intent = Intent.MEAL_PLAN
        if intent is Intent.NUTRITION_QUERY:
            food_name = _extract_food_name(message)
            if food_name:
                slots["food_name"] = food_name
            elif not slots.get("food_name"):
                missing.append("food_name")
        if intent is Intent.RECIPE_QUERY:
            name = resolve_recipe_name(
                message,
                message if (
                    payload.get("clarification_intent") == intent.value
                    and not is_recipe_request(message)
                ) else None,
                payload.get("discussed_meals") or [],
            )
            slots.pop("food_name", None)
            if name:
                slots["food_name"] = name
            else:
                missing.append("food_name")
        if intent is Intent.PROFILE_UPDATE and not any(
            key in slots for key in ("allergens", "preferences", "excluded_ingredients")
        ):
            missing.append("preference")

        return {
            "intent": intent.value,
            "confidence": 0.96 if intent is not Intent.OTHER else 0.65,
            "extracted_slots": slots,
            "missing_slots": missing,
            "answer_task": {
                "objective": message,
                "context": "general" if intent is Intent.FOOD_QUESTION and (
                    "柴" in message or "补救" in message
                ) else "auto",
                "recommendation_count": 1 if any(
                    term in message for term in ("一份", "一道", "换一个")
                ) else None,
                "needs_nutrition": any(term in message for term in ("蛋白", "脂肪", "营养")),
            },
            "notes": "deterministic fake classification",
        }

    @staticmethod
    def _planner(payload: dict[str, Any]) -> dict[str, Any]:
        intent = str(payload.get("intent") or Intent.OTHER.value)
        slots = dict(payload.get("slots") or {})
        current_date = (
            date.fromisoformat(str(payload["current_date"]))
            if payload.get("current_date")
            else date.today()
        )
        results = list(payload.get("tool_results") or [])
        called = [str(item.get("tool_name", "")) for item in results]
        turn_id = str(payload.get("turn_id", "turn"))

        next_tool: str | None = None
        arguments: dict[str, Any] = {}
        if intent in {
            Intent.MEAL_RECOMMENDATION.value,
            Intent.MEAL_ADJUSTMENT.value,
            Intent.MEAL_PLAN.value,
        }:
            if "get_user_profile" not in called:
                next_tool = "get_user_profile"
            elif "get_user_feedback" not in called:
                next_tool = "get_user_feedback"
            elif "get_weather_context" not in called:
                next_tool = "get_weather_context"
                arguments = {"location": slots.get("location", "北京")}
            elif "search_meals" not in called:
                profile_allergens = _profile_list(results, "allergens")
                profile_dislikes = _profile_list(results, "dislikes")
                next_tool = "search_meals"
                arguments = {
                    "query": str(payload.get("user_message", "个性化饮食推荐")),
                    "meal_type": slots.get("meal_type"),
                    "tastes": slots.get("tastes", []),
                    "health_goals": slots.get("health_goals", []),
                    "dietary_tags": slots.get("dietary_tags", []),
                    "required_ingredients": slots.get("required_ingredients", []),
                    "ingredient_any_groups": slots.get("ingredient_any_groups", []),
                    "excluded_allergens": merge_dietary_terms(
                        profile_allergens,
                        slots.get("allergens"),
                    ),
                    "excluded_ingredients": merge_dietary_terms(
                        profile_dislikes,
                        profile_allergens,
                        slots.get("dislikes"),
                        slots.get("excluded_ingredients"),
                        slots.get("allergens"),
                    ),
                    "excluded_meal_ids": sorted(
                        {
                            *payload.get("candidate_ids", []),
                            *_feedback_excluded_ids(results),
                        }
                    )
                    if intent == Intent.MEAL_ADJUSTMENT.value and any(
                        term in str(payload.get("user_message", ""))
                        for term in ("换一批", "换一个", "不想吃这个", "再来", "换几道")
                    )
                    else _feedback_excluded_ids(results),
                    "max_calories_kcal": slots.get("max_calories_kcal"),
                    "max_price_cny": slots.get("max_price_cny"),
                    "limit": 20 if intent == Intent.MEAL_PLAN.value else 3,
                }
            elif intent == Intent.MEAL_PLAN.value and "create_meal_plan" not in called:
                if payload.get("verified_plan"):
                    return {
                        "complete": False,
                        "tool_calls": [{
                            "call_id": f"{turn_id}:{len(results) + 1}:create_meal_plan",
                            "name": "create_meal_plan", "arguments": payload["verified_plan"],
                        }],
                        "plan_summary": "保存已验证餐单",
                    }
                try:
                    plan = PlanBuilder().build(
                        PlanSpec.from_slots(
                            slots,
                            start_date=current_date,
                            profile=next(
                                (
                                    result["data"]
                                    for result in results
                                    if result.get("tool_name") == "get_user_profile"
                                    and result.get("status") == "success"
                                ),
                                {},
                            ),
                        ),
                        candidate_meals_from_results(results),
                    )
                except PlanBuildError:
                    plan = None
                if plan is not None:
                    next_tool = "create_meal_plan"
                    arguments = plan.model_dump(mode="json")
        elif intent == Intent.NUTRITION_QUERY.value and "get_nutrition" not in called:
            next_tool = "get_nutrition"
            arguments = {"name": slots.get("food_name", "")}
        elif intent == Intent.RECIPE_QUERY.value and "get_recipe" not in called:
            next_tool = "get_recipe"
            arguments = {"name": slots.get("food_name", "")}
        elif intent == Intent.PROFILE_UPDATE.value and "update_user_profile" not in called:
            next_tool = "update_user_profile"
            arguments = {
                key: slots[key]
                for key in ("allergens", "preferences", "dislikes", "health_goals", "dietary_tags")
                if key in slots
            }

        if next_tool is None:
            return {"complete": True, "tool_calls": [], "plan_summary": "所需信息已收集"}
        return {
            "complete": False,
            "tool_calls": [
                {
                    "call_id": f"{turn_id}:{len(results) + 1}:{next_tool}",
                    "name": next_tool,
                    "arguments": arguments,
                }
            ],
            "plan_summary": f"调用 {next_tool} 获取可靠数据",
        }

    @staticmethod
    def _recommendation(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("answer_to_revise"):
            return {"response": payload["answer_to_revise"]}
        intent = payload.get("intent")
        if intent == Intent.CONVERSATION.value:
            return {"response": {"message": "不客气，有饮食或做菜的问题可以继续聊。"}}
        if intent == Intent.FOOD_QUESTION.value:
            message = str(payload.get("user_message", ""))
            discussed = payload.get("discussed_meals") or []
            sources = list((payload.get("available_sources") or {}).keys())[:12]
            if any(term in message for term in ("第二个", "第二道")):
                text = (
                    f"第二道是{discussed[1]['name']}。我可以根据刚才的配方食材解释这个选择，"
                    "不能据此判断它一定适合所有人。"
                    if len(discussed) > 1 else "你指的是哪道菜？可以告诉我菜名。"
                )
            elif any(term in message for term in ("比较", "区别")) and len(discussed) > 1:
                text = (
                    f"可以比较{discussed[0]['name']}和{discussed[1]['name']}。\n\n"
                    "先看配方使用的食材，再看整道菜的营养估算，不能直接当成人均摄入比较。"
                )
            elif "柴" in message:
                text = (
                    "鸡胸肉容易做柴，通常与加热后失水有关。\n\n"
                    "一般烹饪建议是切得厚薄均匀，避免在已经熟透后继续长时间加热；"
                    "嫩度不能代替熟度判断。这是通用原理，不是某份收录配方的原文。"
                )
            else:
                text = "你想了解哪道菜，或哪个具体的烹饪问题？"
            return {"response": {"message": text, "source_ids": sources}}
        # Fake mode renders facts locally; production generation receives no template prose.
        response = ground_recommendation_facts(
            FinalResponse(message="暂时没有足够的可靠资料回答这个问题。"),
            list(payload.get("tool_results") or []), payload.get("slots") or {},
            user_message=str(payload.get("user_message", "")),
        )
        count = (payload.get("answer_task") or {}).get("recommendation_count")
        if count and response.recommendations:
            response = ground_recommendation_facts(
                response.model_copy(update={"recommendations": response.recommendations[:count]}),
                list(payload.get("tool_results") or []), payload.get("slots") or {},
                user_message=str(payload.get("user_message", "")),
            )
        return {"response": response.model_dump(include={"status", "message", "recommendations"})}

    @staticmethod
    def _evaluation(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "score": 1.0 if payload.get("final_response") else 0.0,
            "reason": "Fake judge checks that a structured final response exists.",
        }


def _extract_slots(message: str, *, current_date: date | None = None) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    meal_types = {"早餐": "breakfast", "午餐": "lunch", "晚餐": "dinner", "夜宵": "late_snack"}
    for word, value in meal_types.items():
        if word in message:
            slots["meal_type"] = value
            break
    tastes = [word for word in ("清淡", "辣", "酸", "甜", "咸", "麻辣") if word in message]
    if tastes:
        slots["tastes"] = tastes
    goals = [word for word in ("减脂", "增肌", "控糖", "低盐", "高蛋白") if word in message]
    if goals:
        slots["health_goals"] = goals
    allergens = extract_declared_allergens(message)
    if allergens:
        slots["allergens"] = allergens
    if "素食" in message:
        slots["dietary_tags"] = ["素食"]
    budget = re.search(r"(?:预算|不超过)\s*(\d+(?:\.\d+)?)\s*元", message)
    if budget:
        slots["max_price_cny"] = float(budget.group(1))
    calories = re.search(r"(?:不超过|控制在)\s*(\d+)\s*(?:千卡|大卡)", message)
    if calories:
        slots["max_calories_kcal"] = float(calories.group(1))
    preparation = re.search(
        r"(?:不超过|控制在)\s*(\d+(?:\.\d+)?)\s*分钟"
        r"|(\d+(?:\.\d+)?)\s*分钟(?:内|以内)",
        message,
    )
    if preparation:
        slots["max_prep_minutes"] = float(preparation.group(1) or preparation.group(2))
    # This fake only handles common cancellation phrases; real extraction uses IntentSlots.
    labels = {
        "max_calories_kcal": ("热量", "卡路里"),
        "max_price_cny": ("预算", "价格"),
        "max_prep_minutes": ("准备时间", "烹饪时间", "耗时", "时间"),
        "min_protein_g": ("蛋白质",),
    }
    label = "|".join(term for terms in labels.values() for term in terms)
    target = rf"(?:{label})(?:的?(?:上限|下限|限制|要求))?"
    targets = rf"{target}(?:\s*(?:和|、|与|及)\s*{target})*"
    cleared: set[str] = set()
    for match in re.finditer(
        rf"(?:取消|去掉|撤销|不限|不限制|不用限制|不再限制)\s*(?P<before>{targets})"
        rf"|(?P<after>{targets})(?:都|也)?(?:不限|不限制|不再限制|取消|去掉)",
        message,
    ):
        if re.search(
            r"(?:不要|别|不想|不必|不用|不|如果|假如|如何|怎么|是否)(?:再)?\s*$",
            message[:match.start()],
        ):
            continue
        text = match.group("before") or match.group("after")
        cleared.update(field for field, terms in labels.items() if any(t in text for t in terms))
    if cleared:
        slots["clear_constraints"] = sorted(cleared)
    if "北京" in message:
        slots["location"] = "北京"
    start_date = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", message)
    if start_date:
        with suppress(ValueError):
            slots["start_date"] = date.fromisoformat(start_date.group(1)).isoformat()
    else:
        for term, offset in (("后天", 2), ("明天", 1), ("今天", 0)):
            if term in message:
                slots["start_date"] = (
                    (current_date or date.today()) + timedelta(days=offset)
                ).isoformat()
                break
    return slots


def _extract_food_name(message: str) -> str | None:
    match = re.search(r"(.{1,20}?)(?:有多少|多少)(?:热量|蛋白质|脂肪|碳水)", message)
    if match:
        return match.group(1).strip("，。？? ")
    match = re.search(r"(?:查询|查一下|查查)?(.{1,120}?)(?:的营养|营养信息)", message)
    if match:
        return match.group(1).strip("，。？? ")
    return None


def _profile_list(results: list[dict[str, Any]], field: str) -> list[str]:
    for result in results:
        if result.get("tool_name") != "get_user_profile" or result.get("status") != "success":
            continue
        data = result.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get(field), list):
            return [str(item) for item in data[field]]
    return []


def _feedback_excluded_ids(results: list[dict[str, Any]]) -> list[int]:
    for result in results:
        if result.get("tool_name") != "get_user_feedback" or result.get("status") != "success":
            continue
        data = result.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("excluded_meal_ids"), list):
            return sorted(
                {meal_id for meal_id in data["excluded_meal_ids"] if isinstance(meal_id, int)}
            )
    return []
