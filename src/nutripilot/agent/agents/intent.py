"""Intent and slot extraction Worker Agent."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from nutripilot.agent.agents.base import fallback_update, record_worker_event
from nutripilot.agent.contracts import IntentOutput
from nutripilot.agent.slots import (
    explicit_food_request,
    extract_calorie_limit,
    extract_plan_scope,
    is_recipe_request,
    resolve_ingredient_slots,
    resolve_recipe_name,
)
from nutripilot.agent.state import AgentState
from nutripilot.domain.enums import Intent, Phase
from nutripilot.domain.execution import ExecutionOwnershipLost
from nutripilot.memory.context import merge_slots
from nutripilot.model_gateway import ModelGateway, ModelUsage
from nutripilot.model_gateway.budget import ExecutionLimitExceeded
from nutripilot.tracing import TraceRecorder


class IntentAgent:
    name = "IntentAgent"
    prompt_version = "intent-v12"
    system_prompt = (
        "你是饮食意图识别 Worker。仅依据用户消息和已有状态识别意图、抽取槽位；"
        "不得调用工具或提供医疗结论，必须返回符合给定 Schema 的结构化结果。"
        "extracted_slots 仅允许 meal_type、plan_range、start_date（YYYY-MM-DD）、food_name、"
        "location、tastes、preferences、cuisines、health_goals、dietary_tags、allergens、"
        "dislikes、excluded_ingredients、required_ingredients、max_calories_kcal、"
        "ingredient_any_groups、ingredient_action、"
        "min_protein_g、max_price_cny、"
        "max_prep_minutes、clear_constraints、daily_calorie_target、daily_protein_target_g。"
        "每日/全天热量和蛋白质目标必须放入 daily_* 字段，不得误用为单餐检索上下限。"
        "千卡、大卡和 kcal 是相同单位，例如不超过1千卡应提取数值1，不能换算成1000。"
        "依据 current_date 解析相对日期。"
        "计划默认一周三餐，不要把可选的饮食限制作为必填项。"
        "用户明确要求一道菜、几道菜或菜谱时，不必追问餐次，也不要自行补出餐次。"
        "询问菜的做法、怎么做、烹饪步骤时使用 RECIPE_QUERY，提取 food_name，"
        "不得改成推荐或营养查询。discussed_meals 是上一轮展示顺序；"
        "第二道指第二项，只有唯一一道菜时才可直接解析“这个”。有歧义则缺少 food_name。"
        "不能把上一轮 food_name 自动当成本轮明确询问的菜名。"
        "用户想吃某种食材时提取 required_ingredients，例如想吃鸡胸肉是[鸡胸肉]，"
        "不能用鸡翅代替。只提取本轮明确想吃的食材，不把菜名拆成必含食材，"
        "不把不想吃、过敏、忌口提为必含项；不要复制上一轮的必含食材。"
        "明确想吃某种食材时可以直接推荐菜，不必追问餐次。"
        "required_ingredients 表示全部必含；ingredient_any_groups 是组内任选、组间全部满足，"
        "如鸡胸肉或豆腐都可以是[[鸡胸肉,豆腐]]，不能把两项放到required_ingredients。"
        "保留用户食材原词，不改写同义词；排除食材放excluded_ingredients。"
        "ingredient_action：改成/换成=replace，再加/还要=append，"
        "换一批/条件不变=keep，不限食材/取消食材限制=clear。"
        "clear只取消临时食材条件，用户过敏事实始终保留。"
        "延续推荐条件的改成、再加、不限食材、不要某食材应识别为MEAL_ADJUSTMENT。"
        "没有历史推荐时，首次同时表达不吃某食材、想吃另一食材仍是MEAL_RECOMMENDATION。"
        "取消热量、预算、准备时间或蛋白质下限等临时数值限制也使用MEAL_ADJUSTMENT，"
        "把要取消的字段放入clear_constraints，只允许max_calories_kcal、max_price_cny、"
        "max_prep_minutes、min_protein_g；例如不限热量了是[max_calories_kcal]。"
        "未提及的条件保持不变，null或遗漏不表示取消。不得在clear_constraints中取消过敏或忌口。"
        "不要取消、保留限制、询问如何取消不属于取消指令。"
        "重新指定数值时提取新值，不复制旧值；同一字段以本轮最后明确要求为准，"
        "最后为新数值则不放入clear_constraints，最后为取消则只输出取消、不再输出旧数值。"
        "FOOD_QUESTION 用于饮食范围内的解释、比较、食材知识、烹饪原理、失败补救和技巧问答，"
        "例如为什么推荐第二个、前两道有什么区别、鸡胸肉为什么容易做柴、怎么做才不柴；"
        "用户只问原理或技巧时，不得改成推荐、完整菜谱或具体菜品营养查询。"
        "查具体菜谱的完整做法仍用RECIPE_QUERY，查询具体营养数据仍用NUTRITION_QUERY。"
        "CONVERSATION用于感谢、寒暄、简短确认等非任务对话，不代表批准任何写操作。"
        "与饮食烹饪无关的请求仍是OTHER，医疗请求仍是HEALTH_RISK。"
        "FOOD_QUESTION和CONVERSATION不提取或改变筛选条件，不要求餐次或菜名；"
        "对象不明可交给回答阶段自然追问。"
    )
    reads = frozenset({"user_message", "slots", "conversation_summary"})
    writes = frozenset({"intent", "intent_confidence", "slots", "missing_slots"})

    def __init__(self, gateway: ModelGateway, recorder: TraceRecorder) -> None:
        self.gateway = gateway
        self.recorder = recorder

    async def run(self, state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        current_date = date.fromisoformat(state["current_date"])
        explicit, invalid = extract_plan_scope(state["user_message"], current_date=current_date)
        if invalid and any(term in state["user_message"] for term in ("计划", "餐单")):
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="intent.scope_validated",
                payload={"invalid_fields": invalid, "deterministic": True},
            )
            return {
                "phase": Phase.INTENT.value,
                "intent": Intent.MEAL_PLAN.value,
                "intent_confidence": 1.0,
                "slots": merge_slots(state.get("slots", {}), explicit),
                "missing_slots": invalid,
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        payload = {
            "message": state["user_message"],
            "slots": state.get("slots", {}),
            "conversation_summary": state.get("conversation_summary"),
            "current_date": state["current_date"],
            "clarification_intent": state.get("clarification_intent"),
            "discussed_meals": state.get("discussed_meals", []),
        }
        try:
            result = await self.gateway.generate_structured(
                worker="intent",
                system_prompt=self.system_prompt,
                payload=payload,
                response_model=IntentOutput,
                prompt_version=self.prompt_version,
            )
            output = result.output
            if _fallback_intent(state["user_message"], {}).intent is Intent.CONVERSATION:
                output = output.model_copy(update={"intent": Intent.CONVERSATION})
            if (
                output.intent is Intent.MEAL_ADJUSTMENT
                and not state.get("slots")
                and not state.get("discussed_meals")
                and not state.get("conversation_summary")
                and explicit_food_request(state["user_message"])
            ):
                output = output.model_copy(update={"intent": Intent.MEAL_RECOMMENDATION})
            if (
                is_recipe_request(state["user_message"])
                and output.intent not in {Intent.HEALTH_RISK, Intent.FOOD_QUESTION}
            ):
                output = output.model_copy(update={"intent": Intent.RECIPE_QUERY})
            if state.get("replacing_plan") and output.intent in {
                Intent.MEAL_ADJUSTMENT,
                Intent.MEAL_PLAN,
            }:
                output = output.model_copy(update={"intent": Intent.MEAL_PLAN})
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.completed",
                payload={
                    "intent": output.intent.value,
                    "missing_slots": output.missing_slots,
                    "model": result.model,
                    "attempts": result.attempts,
                    "used_fallback_model": result.used_fallback,
                    "prompt_version": self.prompt_version,
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            slots, missing = _resolved_slots(state, output)
            return {
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": slots,
                "missing_slots": missing,
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }
        except (ExecutionLimitExceeded, ExecutionOwnershipLost):
            raise
        except Exception as exc:
            output = _fallback_intent(state["user_message"], state.get("slots", {}))
            if (
                output.intent is Intent.OTHER
                and state.get("clarification_intent") == Intent.RECIPE_QUERY.value
            ):
                output = output.model_copy(
                    update={
                        "intent": Intent.RECIPE_QUERY,
                        "extracted_slots": {"food_name": state["user_message"].strip("，。？? ")},
                    }
                )
            if state.get("replacing_plan"):
                output = output.model_copy(update={"intent": Intent.MEAL_PLAN})
            usage = getattr(exc, "usage", ModelUsage())
            record_worker_event(
                self.recorder,
                state,
                worker=self.name,
                event_type="model.fallback",
                payload={
                    "error_type": type(exc).__name__,
                    "intent": output.intent.value,
                    "error_code": getattr(exc, "code", "MODEL_OR_SCHEMA_ERROR"),
                },
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            slots, missing = _resolved_slots(state, output)
            return {
                "phase": Phase.INTENT.value,
                "intent": output.intent.value,
                "intent_confidence": output.confidence,
                "slots": slots,
                "missing_slots": missing,
                "fallback_events": fallback_update(state, self.name, "MODEL_OR_SCHEMA_ERROR"),
                "prompt_versions": {
                    **state.get("prompt_versions", {}),
                    self.name: self.prompt_version,
                },
            }


def _required_missing(output: IntentOutput, slots: dict[str, Any]) -> list[str]:
    if output.intent is Intent.MEAL_RECOMMENDATION:
        return [] if slots.get("meal_type") else ["meal_type"]
    if output.intent in {Intent.NUTRITION_QUERY, Intent.RECIPE_QUERY}:
        return [] if slots.get("food_name") else ["food_name"]
    if output.intent is Intent.PROFILE_UPDATE:
        fields = {"preferences", "allergens", "dislikes", "health_goals", "dietary_tags"}
        return [] if any(slots.get(field) for field in fields) else ["preference"]
    return []


def _resolved_slots(state: AgentState, output: IntentOutput) -> tuple[dict[str, Any], list[str]]:
    if output.intent in {Intent.FOOD_QUESTION, Intent.CONVERSATION}:
        return dict(state.get("slots", {})), []
    slots = merge_slots(
        state.get("slots", {}),
        {key: value for key, value in output.extracted_slots.items()
         if key not in {"ingredient_any_groups", "ingredient_action", "clear_constraints"}},
    )
    slots.pop("ingredient_action", None)
    if output.intent is Intent.RECIPE_QUERY:
        name = resolve_recipe_name(
            state["user_message"],
            output.extracted_slots.get("food_name"),
            state.get("discussed_meals", []),
        )
        slots.pop("food_name", None)
        if name:
            slots["food_name"] = name
        return slots, [] if name else ["food_name"]
    if output.intent in {
        Intent.MEAL_RECOMMENDATION, Intent.MEAL_ADJUSTMENT, Intent.MEAL_PLAN
    }:
        slots.update(resolve_ingredient_slots(
            state["user_message"], state.get("slots", {}), output.extracted_slots,
            adjustment=output.intent is Intent.MEAL_ADJUSTMENT or bool(state.get("replacing_plan")),
        ))
        food_name = output.extracted_slots.get("food_name")
        if food_name and food_name in state["user_message"]:
            slots["food_name"] = food_name
        elif (
            output.intent is Intent.MEAL_RECOMMENDATION
            or output.extracted_slots.get("ingredient_action") in {"replace", "clear"}
            or any(term in state["user_message"] for term in (
                "不限食材", "食材不限", "取消食材限制", "不限制食材", "改成", "换成",
            ))
        ):
            slots.pop("food_name", None)
        calorie_limit = extract_calorie_limit(state["user_message"])
        if calorie_limit is not None:
            slots["max_calories_kcal"] = calorie_limit
        # Keep an explicit tombstone so later planner calls cannot restore a cancelled limit.
        for field in output.extracted_slots.get("clear_constraints", []):
            slots[field] = None
    if output.intent is Intent.MEAL_PLAN:
        explicit, missing = extract_plan_scope(
            state["user_message"],
            current_date=date.fromisoformat(state["current_date"]),
        )
        slots.update(explicit)
        if state.get("replacing_plan"):
            for field in ("plan_range", "meal_type", "start_date"):
                if field not in explicit and field in state.get("slots", {}):
                    slots[field] = state["slots"][field]
        if (
            explicit.get("daily_calorie_target") is not None
            and slots.get("max_calories_kcal") == explicit["daily_calorie_target"]
        ):
            slots.pop("max_calories_kcal", None)
        if (
            explicit.get("daily_protein_target_g") is not None
            and slots.get("min_protein_g") == explicit["daily_protein_target_g"]
        ):
            slots.pop("min_protein_g", None)
        return slots, missing
    if output.intent is Intent.MEAL_RECOMMENDATION and any(
        term in state["user_message"] for term in ("一道", "几道", "道菜", "菜谱")
    ):
        return slots, []
    if output.intent is Intent.MEAL_RECOMMENDATION and (
        slots.get("required_ingredients") or slots.get("ingredient_any_groups")
    ):
        return slots, []
    return slots, _required_missing(output, slots)


def _fallback_intent(message: str, slots: dict[str, Any]) -> IntentOutput:
    extracted = dict(slots)
    extracted.pop("required_ingredients", None)
    for keyword, value in {
        "早餐": "breakfast",
        "午餐": "lunch",
        "晚餐": "dinner",
        "夜宵": "late_snack",
    }.items():
        if keyword in message:
            extracted["meal_type"] = value
            break
    if any(word in message for word in ("诊断", "药物", "绝食", "催吐")):
        intent = Intent.HEALTH_RISK
    elif any(word in message for word in ("一周", "计划", "餐单")):
        intent = Intent.MEAL_PLAN
    elif message.strip("，,。！!？? ") in {"好的", "好的，谢谢", "谢谢", "你好", "明白了"}:
        intent = Intent.CONVERSATION
    elif any(term in message for term in ("鸡胸肉为什么", "为什么推荐", "有什么区别", "怎么做才")):
        intent = Intent.FOOD_QUESTION
    elif is_recipe_request(message):
        intent = Intent.RECIPE_QUERY
        extracted.pop("food_name", None)
    elif any(word in message for word in ("营养", "热量", "蛋白质")):
        intent = Intent.NUTRITION_QUERY
    elif any(word in message for word in ("吃", "推荐", "早餐", "午餐", "晚餐")):
        intent = Intent.MEAL_RECOMMENDATION
    else:
        intent = Intent.OTHER
    missing = (
        ["meal_type"]
        if intent is Intent.MEAL_RECOMMENDATION and not extracted.get("meal_type")
        else []
    )
    return IntentOutput(
        intent=intent,
        confidence=0.55,
        extracted_slots=extracted,
        missing_slots=missing,
        notes="rule fallback",
    )
