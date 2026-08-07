from __future__ import annotations

from typing import Protocol

from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    FeedbackRecord,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
    UserProfile,
    WeatherContext,
)


class LocalDataStore(Protocol):
    async def get_user_profile(self, user_id: int) -> UserProfile: ...

    async def update_user_profile(
        self, user_id: int, update: UpdateUserProfileArgs
    ) -> UserProfile: ...

    async def create_meal_plan(
        self, user_id: int, request: CreateMealPlanArgs, idempotency_key: str
    ) -> MealPlan: ...

    async def record_feedback(
        self, user_id: int, request: RecordFeedbackArgs, idempotency_key: str
    ) -> FeedbackRecord: ...

    async def list_feedback(self, user_id: int) -> list[FeedbackRecord]: ...


class WeatherProvider(Protocol):
    async def get_weather(self, location: str) -> WeatherContext: ...
