from __future__ import annotations

from typing import Protocol

from nutripilot.retrieval.models import DietaryGuideline, Meal, ScoredId


class MealRepository(Protocol):
    async def list_meals(self) -> list[Meal]: ...

    async def get_meal(self, meal_id: int) -> Meal | None: ...

    async def get_meals(self, meal_ids: list[int]) -> list[Meal]: ...


class GuidelineRepository(Protocol):
    async def list_guidelines(self) -> list[DietaryGuideline]: ...


class Embedder(Protocol):
    dimension: int
    version: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    version: str

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        eligible_ids: set[int] | None = None,
    ) -> list[ScoredId]: ...


class MealReranker(Protocol):
    version: str

    async def score(self, query: str, meals: list[Meal]) -> list[float]: ...
