"""Deterministic interpretation of explicit plan scope and calendar dates."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any


def extract_plan_scope(message: str) -> tuple[dict[str, Any], list[str]]:
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
        for term, offset in (("后天", 2), ("明天", 1), ("今天", 0)):
            if term in message:
                slots["start_date"] = (date.today() + timedelta(days=offset)).isoformat()
                break
    return slots, missing
