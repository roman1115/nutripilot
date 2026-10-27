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
from nutripilot.agent.planning import PlanBuilder, PlanBuildError, candidate_meals_from_results
from nutripilot.domain.enums import Intent
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
            "evaluation": self._evaluation,
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
    def _intent(payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message", "")).strip()
        slots = dict(payload.get("slots") or {})
        current_date = (
            date.fromisoformat(str(payload["current_date"]))
            if payload.get("current_date")
            else date.today()
        )
        slots.update(_extract_slots(message, current_date=current_date))

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
        elif any(word in message for word in ("换一个", "换一批", "不想吃这个", "再来")):
            intent = Intent.MEAL_ADJUSTMENT
        elif any(word in message for word in ("热量", "蛋白质", "营养", "脂肪", "碳水")):
            intent = Intent.NUTRITION_QUERY
        elif any(word in message for word in ("记住", "更新偏好", "以后不要", "我喜欢")):
            intent = Intent.PROFILE_UPDATE
        elif any(word in message for word in ("吃什么", "推荐", "早餐", "午餐", "晚餐", "夜宵")):
            intent = Intent.MEAL_RECOMMENDATION
        else:
            intent = Intent.OTHER

        missing: list[str] = []
        if intent is Intent.MEAL_RECOMMENDATION and "meal_type" not in slots:
            missing.append("meal_type")
        if (
            intent is Intent.OTHER
            and payload.get("clarification_intent") == Intent.NUTRITION_QUERY.value
        ):
            intent = Intent.NUTRITION_QUERY
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
        if intent is Intent.PROFILE_UPDATE and not any(
            key in slots for key in ("allergens", "preferences", "excluded_ingredients")
        ):
            missing.append("preference")

        return {
            "intent": intent.value,
            "confidence": 0.96 if intent is not Intent.OTHER else 0.65,
            "extracted_slots": slots,
            "missing_slots": missing,
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
                    if intent == Intent.MEAL_ADJUSTMENT.value
                    else _feedback_excluded_ids(results),
                    "max_calories_kcal": slots.get("max_calories_kcal"),
                    "max_price_cny": slots.get("max_price_cny"),
                    "limit": 20 if intent == Intent.MEAL_PLAN.value else 3,
                }
            elif intent == Intent.MEAL_PLAN.value and "create_meal_plan" not in called:
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
        tool_results = list(payload.get("tool_results") or [])
        meals = _meals_from_results(tool_results)
        recommendations: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        for meal in meals[:3]:
            meal_id = int(meal.get("id") or meal.get("meal_id") or 0)
            if meal_id <= 0:
                continue
            nutrition = meal.get("nutrition") or {}
            recommendations.append(
                {
                    "meal_id": meal_id,
                    "name": str(meal.get("name", f"餐食 {meal_id}")),
                    "reason": "符合当前餐次、偏好和安全约束",
                    "calories_kcal": nutrition.get("calories_kcal", meal.get("calories_kcal")),
                    "protein_g": nutrition.get("protein_g", meal.get("protein_g")),
                    "tags": list(meal.get("tags") or meal.get("health_goals") or []),
                }
            )
            citations.append(
                {
                    "source_id": f"meal:{meal_id}",
                    "title": str(meal.get("name", f"餐食 {meal_id}")),
                    "source_type": "meal",
                    "uri": meal.get("source_uri"),
                }
            )

        intent = str(payload.get("intent") or "")
        if intent == Intent.NUTRITION_QUERY.value:
            nutrition_data: Any = next(
                (
                    result.get("data")
                    for result in tool_results
                    if result.get("tool_name") == "get_nutrition"
                    and result.get("status") == "success"
                ),
                {},
            )
            message = _nutrition_message(nutrition_data)
        elif recommendations:
            names = "、".join(item["name"] for item in recommendations)
            message = f"根据你的条件，优先推荐：{names}。"
        else:
            message = "暂时没有找到同时满足全部条件的餐食，请尝试放宽非安全条件。"

        return {
            "response": {
                "status": "completed",
                "message": message,
                "recommendations": recommendations,
                "citations": citations,
                "warnings": [],
                "suggested_actions": ["换一批", "查看营养信息"] if recommendations else [],
            }
        }

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


def _meals_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for result in results:
        if result.get("tool_name") != "search_meals" or result.get("status") != "success":
            continue
        data = result.get("data") or {}
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            if isinstance(data.get("meals"), list):
                return [item for item in data["meals"] if isinstance(item, dict)]
            if isinstance(data.get("hits"), list):
                return [
                    item.get("meal", item)
                    for item in data["hits"]
                    if isinstance(item, dict) and isinstance(item.get("meal", item), dict)
                ]
    return []


def _nutrition_message(data: Any) -> str:
    if not isinstance(data, dict) or not data:
        return "暂时没有查到该食物的可靠营养数据。"
    name = data.get("name") or data.get("food_name") or "该食物"
    raw_nutrition = data.get("nutrition")
    nutrition: dict[str, Any] = raw_nutrition if isinstance(raw_nutrition, dict) else data
    calories = nutrition.get("calories_kcal")
    protein = nutrition.get("protein_g")
    details: list[str] = []
    if calories is not None:
        details.append(f"约 {calories} 千卡")
    if protein is not None:
        details.append(f"蛋白质约 {protein} 克")
    return f"{name}{'，'.join(details)}。" if details else f"已查询到{name}的营养信息。"
