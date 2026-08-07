from __future__ import annotations

from pydantic import BaseModel

from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    FeedbackHistory,
    FeedbackRecord,
    GetUserFeedbackArgs,
    GetUserProfileArgs,
    GetWeatherContextArgs,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
    UserProfile,
    WeatherContext,
)
from nutripilot.tools.local.ports import LocalDataStore, WeatherProvider
from nutripilot.tools.models import ToolAccess, ToolContext, ToolDefinition
from nutripilot.tools.registry import ToolRegistry


def build_local_tool_definitions(
    store: LocalDataStore, weather_provider: WeatherProvider
) -> list[ToolDefinition]:
    async def get_user_profile(_arguments: BaseModel, context: ToolContext) -> UserProfile:
        return await store.get_user_profile(_require_user(context))

    async def get_weather_context(arguments: BaseModel, _context: ToolContext) -> WeatherContext:
        request = GetWeatherContextArgs.model_validate(arguments)
        return await weather_provider.get_weather(request.location)

    async def get_user_feedback(
        _arguments: BaseModel, context: ToolContext
    ) -> FeedbackHistory:
        records = await store.list_feedback(_require_user(context))
        excluded_meal_ids = sorted(
            {
                record.meal_id
                for record in records
                if record.feedback_type in {"dislike", "replace"}
                or (record.feedback_type == "rating" and (record.rating or 0) <= 2)
            }
        )
        return FeedbackHistory(records=records, excluded_meal_ids=excluded_meal_ids)

    async def create_meal_plan(arguments: BaseModel, context: ToolContext) -> MealPlan:
        request = CreateMealPlanArgs.model_validate(arguments)
        return await store.create_meal_plan(
            _require_user(context), request, _require_idempotency(context)
        )

    async def record_feedback(arguments: BaseModel, context: ToolContext) -> FeedbackRecord:
        request = RecordFeedbackArgs.model_validate(arguments)
        return await store.record_feedback(
            _require_user(context), request, _require_idempotency(context)
        )

    async def update_user_profile(arguments: BaseModel, context: ToolContext) -> UserProfile:
        request = UpdateUserProfileArgs.model_validate(arguments)
        if not request.model_fields_set:
            raise ToolRuntimeError(
                "EMPTY_PROFILE_UPDATE", "At least one profile field must be provided."
            )
        return await store.update_user_profile(_require_user(context), request)

    return [
        ToolDefinition(
            name="get_user_profile",
            description="Read the verified user's preferences, dislikes, allergens and goals.",
            arguments_model=GetUserProfileArgs,
            result_model=UserProfile,
            handler=get_user_profile,
            access=ToolAccess.READ,
            required_permission="profile:read",
        ),
        ToolDefinition(
            name="get_weather_context",
            description="Get normalised weather context for meal recommendations.",
            arguments_model=GetWeatherContextArgs,
            result_model=WeatherContext,
            handler=get_weather_context,
            access=ToolAccess.READ,
            timeout_seconds=3.0,
            max_retries=1,
        ),
        ToolDefinition(
            name="get_user_feedback",
            description=(
                "Read explicit meal feedback. Disliked, replaced, or low-rated meals must be "
                "excluded from subsequent retrieval."
            ),
            arguments_model=GetUserFeedbackArgs,
            result_model=FeedbackHistory,
            handler=get_user_feedback,
            access=ToolAccess.READ,
            required_permission="profile:read",
        ),
        ToolDefinition(
            name="create_meal_plan",
            description="Create a persistent meal plan after explicit user confirmation.",
            arguments_model=CreateMealPlanArgs,
            result_model=MealPlan,
            handler=create_meal_plan,
            access=ToolAccess.WRITE,
            required_permission="meal_plan:write",
            requires_confirmation=True,
        ),
        ToolDefinition(
            name="record_feedback",
            description="Persist explicit feedback about a meal recommendation.",
            arguments_model=RecordFeedbackArgs,
            result_model=FeedbackRecord,
            handler=record_feedback,
            access=ToolAccess.WRITE,
            required_permission="feedback:write",
            requires_confirmation=False,
        ),
        ToolDefinition(
            name="update_user_profile",
            description="Update persistent preferences, dislikes, allergens or health goals.",
            arguments_model=UpdateUserProfileArgs,
            result_model=UserProfile,
            handler=update_user_profile,
            access=ToolAccess.WRITE,
            required_permission="profile:write",
            requires_confirmation=True,
        ),
    ]


def register_local_tools(
    registry: ToolRegistry,
    store: LocalDataStore,
    weather_provider: WeatherProvider,
    *,
    replace: bool = False,
) -> ToolRegistry:
    registry.register_many(build_local_tool_definitions(store, weather_provider), replace=replace)
    return registry


def _require_user(context: ToolContext) -> int:
    if context.user_id is None:
        raise ToolRuntimeError("USER_ID_REQUIRED", "A verified user is required.")
    return context.user_id


def _require_idempotency(context: ToolContext) -> str:
    if not context.idempotency_key:
        raise ToolRuntimeError("IDEMPOTENCY_KEY_REQUIRED", "An idempotency key is required.")
    return context.idempotency_key
