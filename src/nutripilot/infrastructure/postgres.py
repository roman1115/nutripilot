from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from nutripilot.retrieval.models import DietaryGuideline, Meal
from nutripilot.tools.errors import ToolRuntimeError
from nutripilot.tools.local.models import (
    CreateMealPlanArgs,
    FeedbackRecord,
    MealPlan,
    RecordFeedbackArgs,
    UpdateUserProfileArgs,
    UserProfile,
)


class PostgresDataStore:
    """Optional PostgreSQL authority adapter using compact JSONB records.

    SQLAlchemy is imported lazily so the default in-memory demo has no database
    requirement. Alembic may own the same DDL in deployed environments.
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False) -> PostgresDataStore:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
        except ImportError as exc:
            raise RuntimeError("sqlalchemy is required to use PostgresDataStore") from exc
        return cls(create_async_engine(url, echo=echo, pool_pre_ping=True))

    async def initialize(self) -> None:
        from sqlalchemy import text

        statements = [
            """
            CREATE TABLE IF NOT EXISTS meals (
                id BIGINT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dietary_guidelines (
                id TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id BIGINT PRIMARY KEY,
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS meal_plans (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_feedback (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, idempotency_key)
            )
            """,
        ]
        async with self.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))

    async def seed(self, meals: list[Meal], guidelines: list[DietaryGuideline]) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as connection:
            for meal in meals:
                await connection.execute(
                    text(
                        """
                        INSERT INTO meals (id, payload) VALUES (:id, CAST(:payload AS JSONB))
                        ON CONFLICT (id) DO UPDATE
                        SET payload = EXCLUDED.payload, updated_at = NOW()
                        """
                    ),
                    {"id": meal.id, "payload": _json(meal)},
                )
            for guideline in guidelines:
                await connection.execute(
                    text(
                        """
                        INSERT INTO dietary_guidelines (id, payload)
                        VALUES (:id, CAST(:payload AS JSONB))
                        ON CONFLICT (id) DO UPDATE
                        SET payload = EXCLUDED.payload, updated_at = NOW()
                        """
                    ),
                    {"id": guideline.id, "payload": _json(guideline)},
                )

    async def list_meals(self) -> list[Meal]:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(text("SELECT payload FROM meals ORDER BY id"))
            return [Meal.model_validate(_payload(row[0])) for row in result]

    async def get_meal(self, meal_id: int) -> Meal | None:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT payload FROM meals WHERE id = :id"), {"id": meal_id}
            )
            row = result.first()
            return Meal.model_validate(_payload(row[0])) if row else None

    async def get_meals(self, meal_ids: list[int]) -> list[Meal]:
        if not meal_ids:
            return []
        meals = await self.list_meals()
        by_id = {meal.id: meal for meal in meals}
        return [by_id[meal_id] for meal_id in meal_ids if meal_id in by_id]

    async def list_guidelines(self) -> list[DietaryGuideline]:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT payload FROM dietary_guidelines ORDER BY id")
            )
            return [DietaryGuideline.model_validate(_payload(row[0])) for row in result]

    async def get_user_profile(self, user_id: int) -> UserProfile:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT payload FROM user_profiles WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
            row = result.first()
        return UserProfile.model_validate(_payload(row[0])) if row else UserProfile(user_id=user_id)

    async def update_user_profile(self, user_id: int, update: UpdateUserProfileArgs) -> UserProfile:
        from sqlalchemy import text

        current = await self.get_user_profile(user_id)
        patch = update.to_patch()
        patch["updated_at"] = datetime.now(UTC)
        profile = UserProfile.model_validate(current.model_copy(update=patch))
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_profiles (user_id, payload)
                    VALUES (:user_id, CAST(:payload AS JSONB))
                    ON CONFLICT (user_id) DO UPDATE
                    SET payload = EXCLUDED.payload, updated_at = NOW()
                    """
                ),
                {"user_id": user_id, "payload": _json(profile)},
            )
        return profile

    async def seed_user_profile(self, profile: UserProfile) -> None:
        """Insert demo seed data once without overwriting persisted user changes."""

        from sqlalchemy import text

        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_profiles (user_id, payload)
                    VALUES (:user_id, CAST(:payload AS JSONB))
                    ON CONFLICT (user_id) DO NOTHING
                    """
                ),
                {"user_id": profile.user_id, "payload": _json(profile)},
            )

    async def create_meal_plan(
        self, user_id: int, request: CreateMealPlanArgs, idempotency_key: str
    ) -> MealPlan:
        from sqlalchemy import text

        unknown_ids = [
            item.meal_id for item in request.items if await self.get_meal(item.meal_id) is None
        ]
        if unknown_ids:
            raise ToolRuntimeError(
                "MEAL_NOT_FOUND",
                "One or more selected meals no longer exist.",
                details={"meal_ids": sorted(set(unknown_ids))},
            )
        plan = MealPlan(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=request.title,
            start_date=request.start_date,
            items=request.items,
            created_at=datetime.now(UTC),
            quality=request.quality,
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO meal_plans (id, user_id, idempotency_key, payload)
                    VALUES (:id, :user_id, :key, CAST(:payload AS JSONB))
                    ON CONFLICT (user_id, idempotency_key) DO NOTHING
                    """
                ),
                {
                    "id": plan.id,
                    "user_id": user_id,
                    "key": idempotency_key,
                    "payload": _json(plan),
                },
            )
            result = await connection.execute(
                text(
                    "SELECT payload FROM meal_plans "
                    "WHERE user_id = :user_id AND idempotency_key = :key"
                ),
                {"user_id": user_id, "key": idempotency_key},
            )
            row = result.first()
        return MealPlan.model_validate(_payload(row[0]))

    async def record_feedback(
        self, user_id: int, request: RecordFeedbackArgs, idempotency_key: str
    ) -> FeedbackRecord:
        from sqlalchemy import text

        if await self.get_meal(request.meal_id) is None:
            raise ToolRuntimeError("MEAL_NOT_FOUND", "The selected meal no longer exists.")
        feedback = FeedbackRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            meal_id=request.meal_id,
            feedback_type=request.feedback_type,
            rating=request.rating,
            reason=request.reason,
            created_at=datetime.now(UTC),
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO user_feedback (id, user_id, idempotency_key, payload)
                    VALUES (:id, :user_id, :key, CAST(:payload AS JSONB))
                    ON CONFLICT (user_id, idempotency_key) DO NOTHING
                    """
                ),
                {
                    "id": feedback.id,
                    "user_id": user_id,
                    "key": idempotency_key,
                    "payload": _json(feedback),
                },
            )
            result = await connection.execute(
                text(
                    "SELECT payload FROM user_feedback "
                    "WHERE user_id = :user_id AND idempotency_key = :key"
                ),
                {"user_id": user_id, "key": idempotency_key},
            )
            row = result.first()
        return FeedbackRecord.model_validate(_payload(row[0]))

    async def list_plans(self, user_id: int) -> list[MealPlan]:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT payload FROM meal_plans WHERE user_id = :user_id ORDER BY created_at"),
                {"user_id": user_id},
            )
            return [MealPlan.model_validate(_payload(row[0])) for row in result]

    async def list_feedback(self, user_id: int) -> list[FeedbackRecord]:
        from sqlalchemy import text

        async with self.engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT payload FROM user_feedback WHERE user_id = :user_id ORDER BY created_at"
                ),
                {"user_id": user_id},
            )
            return [FeedbackRecord.model_validate(_payload(row[0])) for row in result]

    async def close(self) -> None:
        await self.engine.dispose()


def _json(model: Any) -> str:
    value = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _payload(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value
