from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nutripilot.retrieval.catalog import FoodCatalog, load_food_catalog
from nutripilot.retrieval.models import DietaryGuideline, Meal
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    FeedbackRecord,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
    UserProfile,
    WeatherContext,
)


class InMemoryDataStore:
    """Authoritative offline fallback used by tests and credential-free demos."""

    def __init__(
        self,
        *,
        meals: list[Meal] | None = None,
        guidelines: list[DietaryGuideline] | None = None,
        profiles: list[UserProfile] | None = None,
        catalog: FoodCatalog | None = None,
    ) -> None:
        self.catalog = catalog or FoodCatalog(sources=[], ingredients=[], recipes=[])
        self._meals = {meal.id: meal for meal in meals or []}
        self._guidelines = {item.id: item for item in guidelines or []}
        self._profiles = {profile.user_id: profile for profile in profiles or []}
        self._plans: dict[str, MealPlan] = {}
        self._feedback: dict[str, FeedbackRecord] = {}
        self._write_keys: dict[tuple[str, int, str], str] = {}
        self._lock = asyncio.Lock()

    async def list_meals(self) -> list[Meal]:
        return [self._meals[key] for key in sorted(self._meals)]

    async def get_meal(self, meal_id: int) -> Meal | None:
        return self._meals.get(meal_id)

    async def get_meals(self, meal_ids: list[int]) -> list[Meal]:
        return [self._meals[meal_id] for meal_id in meal_ids if meal_id in self._meals]

    async def list_guidelines(self) -> list[DietaryGuideline]:
        return [self._guidelines[key] for key in sorted(self._guidelines)]

    async def get_user_profile(self, user_id: int) -> UserProfile:
        async with self._lock:
            profile = self._profiles.get(user_id)
            if profile is None:
                profile = UserProfile(user_id=user_id)
                self._profiles[user_id] = profile
            return profile.model_copy(deep=True)

    async def update_user_profile(self, user_id: int, update: UpdateUserProfileArgs) -> UserProfile:
        async with self._lock:
            current = self._profiles.get(user_id, UserProfile(user_id=user_id))
            patch = update.to_patch()
            patch["updated_at"] = datetime.now(UTC)
            updated = current.model_copy(update=patch)
            self._profiles[user_id] = UserProfile.model_validate(updated)
            return self._profiles[user_id].model_copy(deep=True)

    async def create_meal_plan(
        self, user_id: int, request: CreateMealPlanArgs, idempotency_key: str
    ) -> MealPlan:
        async with self._lock:
            storage_key = ("meal_plan", user_id, idempotency_key)
            existing_id = self._write_keys.get(storage_key)
            if existing_id is not None:
                return self._plans[existing_id].model_copy(deep=True)
            unknown_ids = sorted(
                {item.meal_id for item in request.items if item.meal_id not in self._meals}
            )
            if unknown_ids:
                raise ToolRuntimeError(
                    "MEAL_NOT_FOUND",
                    "One or more selected meals no longer exist.",
                    details={"meal_ids": unknown_ids},
                )
            plan_id = str(uuid.uuid4())
            plan = MealPlan(
                id=plan_id,
                user_id=user_id,
                title=request.title,
                start_date=request.start_date,
                items=request.items,
                created_at=datetime.now(UTC),
                quality=request.quality,
            )
            self._plans[plan_id] = plan
            self._write_keys[storage_key] = plan_id
            return plan.model_copy(deep=True)

    async def record_feedback(
        self, user_id: int, request: RecordFeedbackArgs, idempotency_key: str
    ) -> FeedbackRecord:
        async with self._lock:
            if request.meal_id not in self._meals:
                raise ToolRuntimeError("MEAL_NOT_FOUND", "The selected meal no longer exists.")
            storage_key = ("feedback", user_id, idempotency_key)
            existing_id = self._write_keys.get(storage_key)
            if existing_id is not None:
                return self._feedback[existing_id].model_copy(deep=True)
            feedback_id = str(uuid.uuid4())
            feedback = FeedbackRecord(
                id=feedback_id,
                user_id=user_id,
                meal_id=request.meal_id,
                feedback_type=request.feedback_type,
                rating=request.rating,
                reason=request.reason,
                created_at=datetime.now(UTC),
            )
            self._feedback[feedback_id] = feedback
            self._write_keys[storage_key] = feedback_id
            return feedback.model_copy(deep=True)

    async def list_plans(self, user_id: int) -> list[MealPlan]:
        return [
            plan.model_copy(deep=True) for plan in self._plans.values() if plan.user_id == user_id
        ]

    async def list_feedback(self, user_id: int) -> list[FeedbackRecord]:
        return [
            feedback.model_copy(deep=True)
            for feedback in self._feedback.values()
            if feedback.user_id == user_id
        ]


class InMemoryTTLStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                self._items.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        async with self._lock:
            self._items[key] = (time.monotonic() + ttl_seconds, value)

    async def add_if_absent(self, key: str, value: Any, ttl_seconds: int) -> bool:
        async with self._lock:
            now = time.monotonic()
            item = self._items.get(key)
            if item is not None and item[0] > now:
                return False
            self._items[key] = (now + ttl_seconds, value)
            return True

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._items.pop(key, None)


class StaticWeatherProvider:
    """Stable fallback that keeps the recommendation flow usable offline."""

    def __init__(self, default_temperature_c: float = 22.0) -> None:
        self.default_temperature_c = default_temperature_c

    async def get_weather(self, location: str) -> WeatherContext:
        normalised = location.strip() or "上海"
        temperature = self.default_temperature_c
        condition = "舒适"
        contexts = ["适合均衡、清淡的日常餐食"]
        if temperature >= 30:
            condition = "炎热"
            contexts = ["优先清爽餐食", "注意补充水分"]
        elif temperature <= 8:
            condition = "寒冷"
            contexts = ["可考虑温热餐食", "兼顾蔬菜和蛋白质"]
        return WeatherContext(
            location=normalised,
            condition=condition,
            temperature_c=temperature,
            humidity_percent=55,
            recommendation_context=contexts,
            observed_at=datetime.now(UTC),
            provider="offline-static",
        )


def load_seed_store(data_directory: str | Path) -> InMemoryDataStore:
    data_path = Path(data_directory)
    raw_meals = _load_json_array(data_path / "meals.json")
    for item in raw_meals:
        if "allergens" not in item or "ingredients" not in item:
            raise ValueError("Seed meals must explicitly declare ingredients and allergens")
        if not isinstance(item.get("nutrition"), dict) or not item["nutrition"].get("serving_size"):
            raise ValueError("Seed nutrition must include a serving size")
    meals = [Meal.model_validate(item) for item in raw_meals]
    catalog_paths = [data_path / f"{name}.json" for name in ("sources", "ingredients", "recipes")]
    catalog = load_food_catalog(data_path) if any(path.exists() for path in catalog_paths) else None
    if catalog is not None:
        meals.extend(catalog.calculated_meals())
    if len({meal.id for meal in meals}) != len(meals):
        raise ValueError("Duplicate meal IDs in seed data")
    names: dict[str, int] = {}
    for meal in meals:
        if not meal.ingredients:
            raise ValueError(f"Meal {meal.id} has no ingredient metadata")
        for name in (meal.name, *meal.aliases):
            key = name.strip().casefold()
            if not key or (key in names and names[key] != meal.id):
                raise ValueError(f"Empty or ambiguous meal name/alias in seed data: {name}")
            names[key] = meal.id
    guidelines = [
        DietaryGuideline.model_validate(item)
        for item in _load_json_array(data_path / "dietary_guidelines.json")
    ]
    profile_file = data_path / "profiles.json"
    profiles = (
        [UserProfile.model_validate(item) for item in _load_json_array(profile_file)]
        if profile_file.exists()
        else []
    )
    return InMemoryDataStore(
        meals=meals, guidelines=guidelines, profiles=profiles, catalog=catalog
    )


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return payload
