"""Legacy views and preparation against the authoritative diet services."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from nutripilot.application.diet_management import DietManagement
from nutripilot.domain.diet import DietError, FieldEvidence, SubmissionAnchor
from nutripilot.domain.diet_records import DietChangePreview, target_on
from nutripilot.tools.local.diet_models import CreateMealPlan, SetTarget
from nutripilot.tools.local.models import CreateMealPlanArgs, UpdateUserProfileArgs, UserProfile

GOAL_FIELDS = frozenset({"daily_calorie_target", "daily_protein_target_g"})


class DietCompatibility:
    def __init__(self, diet: DietManagement) -> None:
        self.diet = diet

    async def profile(self, user_id: int, profile: UserProfile) -> UserProfile:
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            today = SubmissionAnchor.capture(self.diet.clock(), tx.state.timezone).anchor_local_date
            current = target_on(await tx.list_targets(), today)
            values: dict[str, float | None] = {}
            for old, new, maximum in (
                ("daily_calorie_target", "calories_kcal", Decimal("10000")),
                ("daily_protein_target_g", "protein_g", Decimal("1000")),
            ):
                goal = getattr(current.goals, new) if current else None
                # The legacy scalar cannot faithfully represent a range or a larger target.
                values[old] = (
                    float(goal.lower)
                    if goal and goal.lower == goal.upper and goal.upper <= maximum
                    else None
                )
            return UserProfile.model_validate({**profile.model_dump(), **values})

    async def prepare_profile_target(
        self, user_id: int, request: UpdateUserProfileArgs, *, key: str
    ) -> DietChangePreview:
        goals = GOAL_FIELDS & request.model_fields_set
        if not goals:
            raise DietError("NO_CHANGE", "No nutrition target was supplied.")
        if request.model_fields_set - GOAL_FIELDS:
            raise DietError(
                "MIXED_PROFILE_UPDATE", "偏好与营养目标请分别提交，本次未保存任何修改。"
            )
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            anchor = SubmissionAnchor.capture(self.diet.clock(), tx.state.timezone)
        values = {}
        for old, new in (
            ("daily_calorie_target", "calories_kcal"),
            ("daily_protein_target_g", "protein_g"),
        ):
            if old in goals:
                value = getattr(request, old)
                values[new] = (
                    None
                    if value is None
                    else {"lower": str(Decimal(str(value))), "upper": str(Decimal(str(value)))}
                )
        return await self.diet.records.prepare_target(
            user_id,
            SetTarget.model_validate(
                {
                    "action": "set",
                    "effective_date": anchor.anchor_local_date,
                    "goals": values,
                    "evidence": [self.evidence(key)],
                }
            ),
            idempotency_key=key,
            source="user_form",
            source_ref=f"legacy-form:{key}",
        )

    @staticmethod
    def evidence(key: str) -> FieldEvidence:
        return FieldEvidence(
            field="operation",
            source_kind="server_default",
            source_ref=f"legacy:{key}",
            original_text="legacy compatibility preparation",
            operation="set",
            status="derived",
        )

    async def prepare_plan(
        self,
        user_id: int,
        request: CreateMealPlanArgs,
        *,
        key: str,
        names: dict[int, str],
        thread_id: str,
    ) -> DietChangePreview:
        grouped: dict[tuple[date, str], list[dict[str, Any]]] = defaultdict(list)
        matched: dict[int, dict[str, Any]] = {}
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            for meal_id, name in names.items():
                hits = await self.diet.foods.search(
                    name,
                    food_kind="recipe",
                    food_state=None,
                    limit=20,
                    now=self.diet.clock(),
                    tx=tx,
                )
                exact = [hit for hit in hits if hit.name == name]
                if len(exact) == 1:
                    matched[meal_id] = exact[0].food_ref.model_dump(mode="json")
        for index, item in enumerate(request.items):
            item_name = names.get(item.meal_id)
            if not item_name:
                raise DietError("SOURCE_CHANGED", "餐单来源已不可用，请重新预览。")
            grouped[item.meal_date, item.meal_type].append(
                {
                    "client_item_key": f"legacy_{index}",
                    "food": matched.get(item.meal_id, {"kind": "unknown", "name": item_name}),
                    "quantity": {"kind": "unknown", "description": "未提供个人份量"},
                    **({"note": item.note} if item.note is not None else {}),
                }
            )
        proposed = CreateMealPlan.model_validate(
            {
                "action": "create",
                "parent": {"kind": "new", "title": request.title, "start_date": request.start_date},
                "entries": [
                    {
                        "client_entry_key": f"meal_{index}",
                        "entry": {
                            "actuality": "planned",
                            "subject": "self",
                            "temporal": {"local_date": day, "meal_type": meal_type},
                            "items": items,
                        },
                    }
                    for index, ((day, meal_type), items) in enumerate(grouped.items())
                ],
                "evidence": [self.evidence(key)],
            }
        )
        return await self.diet.plans.prepare(
            user_id,
            proposed,
            idempotency_key=key,
            thread_id=thread_id,
        )

    async def plans(self, user_id: int) -> list[dict[str, Any]]:
        async with self.diet.store.transaction(
            user_id, default_timezone=self.diet.default_timezone
        ) as tx:
            plans = await tx.list_plans(date.min, date.max)
            result = []
            for plan in plans:
                meals = await tx.list_planned_meals(plan.id)
                items = []
                for meal in meals:
                    for item in meal.items:
                        legacy_ref = next(
                            (
                                ref
                                for ref, item_id in meal.legacy_item_refs.items()
                                if item_id == item.id
                            ),
                            str(item.id),
                        )
                        items.append(
                            {
                                "id": legacy_ref,
                                "stable_id": str(item.id),
                                "planned_meal_id": str(meal.id),
                                "version": meal.version,
                                "date": meal.local_date.isoformat(),
                                "meal_type": meal.meal_type,
                                "meal_id": (
                                    item.food_ref.id
                                    if item.food_ref.kind == "recipe"
                                    else item.legacy_food_id
                                ),
                                "meal_name": item.name_snapshot,
                                "note": item.note,
                                "quantity_status": item.quantity.kind,
                                "quantity": item.quantity.model_dump(mode="json"),
                                "nutrition": None,
                                "calories": None,
                                "protein_g": None,
                                "reference_recipe_nutrition": None,
                                "recipe_id": None,
                                "meal_role": None,
                                "is_active": True,
                            }
                        )
                result.append(
                    {
                        **plan.model_dump(mode="json"),
                        "status": "active",
                        "items": items,
                        "quality": None,
                        "end_date": max(
                            (meal.local_date for meal in meals), default=plan.start_date
                        ).isoformat(),
                    }
                )
            return result
