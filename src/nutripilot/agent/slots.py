"""Deterministic interpretation of explicit plan scope and calendar dates."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def is_negative_ingredient(message: str, term: str) -> bool:
    mentions = []
    for clause in re.split(r"[，,。；;！？!?]|但是|不过|但", message):
        for occurrence in re.finditer(re.escape(term), clause):
            prefix, suffix = clause[:occurrence.start()], clause[occurrence.end():]
            markers = list(re.finditer(
                r"不想吃|不要|不吃|不喜欢|忌口|过敏|想吃|要吃|改成|换成|再加", prefix
            ))
            mentions.append(bool(
                re.match(r"(?:有点)?过敏", suffix) or (
                    markers and markers[-1].group() in {
                        "不想吃", "不要", "不吃", "不喜欢", "忌口", "过敏"
                    }
                )
            ))
    return bool(mentions) and all(mentions)


def resolve_ingredient_slots(
    message: str, previous: dict[str, Any], extracted: dict[str, Any], *, adjustment: bool
) -> dict[str, Any]:
    """Apply this turn's ingredient operation without changing persisted allergy facts."""
    required = [
        term for term in extracted.get("required_ingredients", [])
        if term in message and not is_negative_ingredient(message, term)
    ]
    groups = [
        group for group in extracted.get("ingredient_any_groups", [])
        if all(term in message and not is_negative_ingredient(message, term) for term in group)
    ]
    action = extracted.get("ingredient_action")
    if any(term in message for term in ("不限食材", "食材不限", "取消食材限制", "不限制食材")):
        action = "clear"
    elif any(term in message for term in ("再加", "加上", "还要", "同时要", "另外加")):
        action = "append"
    elif any(term in message for term in ("改成", "换成")):
        action = "replace"
    elif action not in {"append", "replace", "keep", "clear"}:
        action = "replace" if required or groups or not adjustment else "keep"
    if action == "clear":
        return {
            "required_ingredients": [], "ingredient_any_groups": [], "excluded_ingredients": []
        }
    if action in {"append", "keep"}:
        carried = [
            term for term in previous.get("required_ingredients", [])
            if not is_negative_ingredient(message, term)
        ]
        required = list(dict.fromkeys([*carried, *required]))
        groups = [*previous.get("ingredient_any_groups", []), *groups]
    return {"required_ingredients": required, "ingredient_any_groups": groups}


def explicit_food_request(message: str) -> str | None:
    match = re.search(
        r"(?<!不)(?<!别)(?:想吃点|想吃|想来点|来点|推荐(?:一道|几道)?|用)"
        r"\s*([^，,。；;！？!?\s]{1,40})",
        message,
    )
    return match.group(1) if match else None


def is_recipe_request(message: str) -> bool:
    return any(
        term in message
        for term in ("做法", "怎么做", "怎么炒", "如何做", "如何制作", "制作步骤", "烹饪步骤")
    )


def resolve_recipe_name(
    message: str, extracted: str | None, discussed_meals: list[dict[str, Any]]
) -> str | None:
    text = message.strip("，。？?！! ")
    ordinal = re.search(r"第([一二三四五六七八九十]|[1-9]\d*)[道个份]", text)
    if ordinal:
        value = ordinal.group(1)
        index = int(value) - 1 if value.isdigit() else "一二三四五六七八九十".index(value)
        return str(discussed_meals[index]["name"]) if index < len(discussed_meals) else None
    references = {"", "这个", "这道", "这道菜", "它", "那个", "那道菜", "这", "菜"}
    if (
        extracted
        and extracted not in references
        and extracted in text
        and not is_recipe_request(extracted)
    ):
        return extracted
    match = re.search(
        r"^(?:(?:请|麻烦)?(?:给我|告诉我|教我|查一下|查询|我想知道|我想学|想学|请问))*"
        r"(.+?)(?:的?做法|怎么做|怎么炒|如何做|如何制作|的制作步骤|的烹饪步骤)",
        text,
    )
    explicit = match.group(1).strip(" ，,：:的") if match else ""
    if (match and explicit in references) or text in {
        "怎么做", "如何做", "做法", "给我做法", "告诉我做法", "制作步骤", "烹饪步骤"
    }:
        return str(discussed_meals[0]["name"]) if len(discussed_meals) == 1 else None
    if not explicit:
        suffix = re.search(r"^(?:怎么做|如何做|如何制作)(.+)$", text)
        if suffix:
            explicit = suffix.group(1).strip(" ，,：:的")
    return explicit or extracted


def extract_calorie_limit(message: str) -> float | None:
    match = re.search(
        r"(?:不超过|最多|至多|控制在)\s*(\d+(?:\.\d+)?)\s*(?:千卡|大卡|kcal)",
        message,
        re.IGNORECASE,
    )
    value = float(match.group(1)) if match else None
    return value if value is not None and value > 0 else None


def today_in_timezone(timezone_name: str, *, now: datetime | None = None) -> date:
    instant = now or datetime.now(UTC)
    local = instant.astimezone(ZoneInfo(timezone_name))
    return local.date()


def extract_plan_scope(
    message: str, *, current_date: date | None = None
) -> tuple[dict[str, Any], list[str]]:
    slots: dict[str, Any] = {}
    missing: list[str] = []
    if "三餐" in message or all(term in message for term in ("早餐", "午餐", "晚餐")):
        slots["meal_type"] = None
    calorie = re.search(
        r"(?:每天|每日|全天|日)(?:总)?(?:热量|能量)?(?:目标|控制在|约|为|达到)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:千卡|大卡|kcal)",
        message,
        re.IGNORECASE,
    )
    protein = re.search(
        r"(?:每天|每日|全天)(?:至少|不低于|目标)?(?:摄入)?蛋白质"
        r"(?:至少|不低于|目标|达到|为)?\s*(\d+(?:\.\d+)?)\s*(?:克|g)",
        message,
        re.IGNORECASE,
    )
    if calorie:
        value = float(calorie.group(1))
        if 0 < value <= 10_000:
            slots["daily_calorie_target"] = value
        else:
            missing.append("daily_calorie_target")
    if protein:
        value = float(protein.group(1))
        if 0 <= value <= 1_000:
            slots["daily_protein_target_g"] = value
        else:
            missing.append("daily_protein_target_g")
    if any(term in message for term in ("一天", "一日", "1天")):
        slots["plan_range"] = "one_day"
    elif any(term in message for term in ("一周", "一星期", "七天", "7天")):
        slots["plan_range"] = "one_week"
    explicit = re.search(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?!\d)", message)
    if explicit:
        try:
            slots["start_date"] = date(*(int(value) for value in explicit.groups())).isoformat()
        except ValueError:
            missing.append("start_date")
    else:
        today = current_date or date.today()
        for term, offset in (("后天", 2), ("明天", 1), ("今天", 0)):
            if term in message:
                slots["start_date"] = (today + timedelta(days=offset)).isoformat()
                break
    return slots, missing
