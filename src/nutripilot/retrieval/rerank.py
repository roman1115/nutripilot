from __future__ import annotations

import importlib
from typing import Any

from nutripilot.retrieval.embedding import lexical_tokens
from nutripilot.retrieval.models import Meal


class LexicalReranker:
    version = "lexical-reranker-v1"

    async def score(self, query: str, meals: list[Meal]) -> list[float]:
        query_tokens = set(lexical_tokens(query))
        scores: list[float] = []
        for meal in meals:
            meal_tokens = set(lexical_tokens(meal.searchable_text()))
            overlap = len(query_tokens & meal_tokens)
            denominator = max(1, len(query_tokens))
            scores.append(overlap / denominator)
        return scores


class BGEReranker:
    version = "BAAI/bge-reranker-v2-m3"

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                module = importlib.import_module("sentence_transformers")
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is required to use BGEReranker") from exc
            self._model = module.CrossEncoder(self.model_name, device=self.device)
        return self._model

    async def score(self, query: str, meals: list[Meal]) -> list[float]:
        import asyncio

        model = self._load()
        pairs = [(query, meal.searchable_text()) for meal in meals]
        raw_scores = await asyncio.to_thread(model.predict, pairs, show_progress_bar=False)
        return [float(score) for score in raw_scores]
