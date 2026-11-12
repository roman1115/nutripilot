from __future__ import annotations

from collections import defaultdict

from nutripilot.retrieval.embedding import HashingEmbedder, lexical_tokens
from nutripilot.retrieval.models import (
    GuidelineSearchHit,
    Meal,
    MealSearchFilters,
    MealSearchHit,
    MealSearchRequest,
    MealSearchResponse,
    ScoredId,
)
from nutripilot.retrieval.ports import (
    Embedder,
    GuidelineRepository,
    MealRepository,
    MealReranker,
    VectorIndex,
)
from nutripilot.retrieval.rerank import LexicalReranker
from nutripilot.safety import has_dietary_conflict
from nutripilot.safety.dietary import match_ingredient, meets_ingredient_requirements


class InMemoryVectorIndex:
    version = "memory-vector-index-v1"

    def __init__(self, repository: MealRepository, embedder: Embedder | None = None) -> None:
        self.repository = repository
        self.embedder = embedder or HashingEmbedder()

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        eligible_ids: set[int] | None = None,
    ) -> list[ScoredId]:
        meals = await self.repository.list_meals()
        if eligible_ids is not None:
            meals = [meal for meal in meals if meal.id in eligible_ids]
        vectors = await self.embedder.embed([meal.searchable_text() for meal in meals])
        scored = [
            ScoredId(id=meal.id, score=max(0.0, _dot(vector, meal_vector)), source="semantic")
            for meal, meal_vector in zip(meals, vectors, strict=True)
        ]
        return sorted(scored, key=lambda item: (-item.score, item.id))[:limit]


class RetrievalService:
    version = "hybrid-rrf-v3"

    def __init__(
        self,
        meal_repository: MealRepository,
        *,
        vector_index: VectorIndex | None = None,
        embedder: Embedder | None = None,
        reranker: MealReranker | None = None,
        rrf_k: int = 60,
    ) -> None:
        self.meal_repository = meal_repository
        self.embedder = embedder or HashingEmbedder()
        self.vector_index = vector_index or InMemoryVectorIndex(meal_repository, self.embedder)
        self.reranker = reranker or LexicalReranker()
        self.rrf_k = rrf_k
        self.version = f"hybrid-rrf-v3|embed={self.embedder.version}|rerank={self.reranker.version}"

    async def search_meals(self, request: MealSearchRequest) -> MealSearchResponse:
        all_meals = await self.meal_repository.list_meals()
        diagnostics: dict[str, int] = defaultdict(int)
        eligible = []
        for meal in all_meals:
            if not meal.is_active:
                continue
            diagnostics["active_meals"] += 1
            failures = self._filter_failures(meal, request.filters)
            for failure in failures:
                diagnostics[failure] += 1
            if not failures:
                eligible.append(meal)
        diagnostics["eligible_meals"] = len(eligible)
        eligible_by_id = {meal.id: meal for meal in eligible}
        if not eligible:
            return MealSearchResponse(
                query=request.query,
                hits=[],
                total_candidates=0,
                retrieval_version=self.version,
                filter_diagnostics=dict(diagnostics),
            )

        exact_ids = {
            meal.id
            for meal in eligible
            if request.query.strip().casefold()
            in {name.casefold() for name in (meal.name, *meal.aliases)}
        }
        structured = sorted(
            self._structured_rank(request.query, request.filters, eligible),
            key=lambda item: item.id not in exact_ids,
        )[: request.candidate_limit]
        query_vector = (await self.embedder.embed([request.query]))[0]
        semantic = await self.vector_index.search(
            query_vector,
            limit=request.candidate_limit,
            eligible_ids=set(eligible_by_id),
        )
        semantic = [item for item in semantic if item.id in eligible_by_id]
        fused, sources = self._rrf_fuse(structured, semantic)
        candidate_ids = [
            meal_id
            for meal_id, _ in sorted(fused, key=lambda item: item[0] not in exact_ids)[
                : request.candidate_limit
            ]
        ]
        candidates = [eligible_by_id[meal_id] for meal_id in candidate_ids]
        rerank_scores = await self.reranker.score(request.query, candidates)

        combined: list[tuple[Meal, float, list[str]]] = []
        fused_scores = dict(fused)
        for meal, rerank_score in zip(candidates, rerank_scores, strict=True):
            # Normalise the final score for stable UI display; ranking is what matters.
            score = max(0.0, fused_scores[meal.id] * 100 + rerank_score)
            combined.append((meal, score, sources[meal.id]))
        combined.sort(
            key=lambda item: (item[0].id not in exact_ids, -item[1], item[0].id)
        )

        selected = self._apply_diversity(combined, request.limit)
        hits = [
            MealSearchHit(
                meal=meal,
                score=round(score, 6),
                rank=rank,
                reasons=self._reasons(meal, request.filters),
                retrieval_sources=retrieval_sources,
                citation={
                    "source_id": f"meal:{meal.id}",
                    "title": meal.name,
                    "uri": meal.source_uri,
                    "source": meal.source,
                    "data_version": meal.data_version,
                },
            )
            for rank, (meal, score, retrieval_sources) in enumerate(selected, start=1)
        ]
        return MealSearchResponse(
            query=request.query,
            hits=hits,
            total_candidates=len(eligible),
            retrieval_version=self.version,
            filter_diagnostics=dict(diagnostics),
            match_evidence={
                hit.meal.id: [
                    match
                    for term in [
                        *request.filters.required_ingredients,
                        *(
                            term for group in request.filters.ingredient_any_groups
                            for term in group
                        ),
                    ]
                    for match in match_ingredient(
                        term, hit.meal.ingredients, hit.meal.ingredient_records
                    )
                ]
                for hit in hits
            },
        )

    @staticmethod
    def _passes_hard_filters(meal: Meal, filters: MealSearchFilters) -> bool:
        return not RetrievalService._filter_failures(meal, filters)

    @staticmethod
    def _filter_failures(meal: Meal, filters: MealSearchFilters) -> list[str]:
        failures = []
        if not meal.is_active:
            failures.append("inactive")
        if filters.food_name and filters.food_name.strip().casefold() not in {
            name.strip().casefold() for name in (meal.name, *meal.aliases)
        }:
            failures.append("food_name")
        if meal.id in set(filters.excluded_meal_ids):
            failures.append("excluded_meal")
        if filters.meal_role is not None and meal.meal_role != filters.meal_role:
            failures.append("meal_role")
        if filters.meal_type and filters.meal_type.casefold() not in meal.meal_types:
            failures.append("meal_type")
        if has_dietary_conflict(
            meal_allergens=meal.allergens,
            meal_ingredients=meal.ingredients,
            declared_allergens=filters.excluded_allergens,
            excluded_ingredients=filters.excluded_ingredients,
            ingredient_records=meal.ingredient_records,
        ):
            failures.append("dietary_conflict")
        if filters.dietary_tags and not set(filters.dietary_tags).issubset(meal.dietary_tags):
            failures.append("dietary_tags")
        if not meets_ingredient_requirements(
            meal.model_dump(), filters.required_ingredients, filters.ingredient_any_groups
        ):
            failures.append("ingredient_requirements")
        if (
            filters.max_calories_kcal is not None
            and meal.nutrition.bounds("calories_kcal").maximum > filters.max_calories_kcal
        ):
            failures.append("calories")
        if (
            filters.min_protein_g is not None
            and meal.nutrition.bounds("protein_g").minimum < filters.min_protein_g
        ):
            failures.append("protein")
        if filters.max_prep_minutes is not None and (
            meal.prep_minutes is None or meal.prep_minutes > filters.max_prep_minutes
        ):
            failures.append("unknown_prep" if meal.prep_minutes is None else "prep_minutes")
        if (
            filters.max_price_cny is not None
            and (meal.price_cny is None or meal.price_cny > filters.max_price_cny)
        ):
            failures.append("unknown_price" if meal.price_cny is None else "price")
        return failures

    @staticmethod
    def _structured_rank(
        query: str, filters: MealSearchFilters, meals: list[Meal]
    ) -> list[ScoredId]:
        query_tokens = set(lexical_tokens(query))
        ranked: list[ScoredId] = []
        for meal in meals:
            searchable_tokens = set(lexical_tokens(meal.searchable_text()))
            score = len(query_tokens & searchable_tokens) / max(1, len(query_tokens))
            score += 0.3 * len(set(filters.cuisines) & set(meal.cuisines))
            score += 0.25 * len(set(filters.tastes) & set(meal.tastes))
            score += 0.25 * len(set(filters.health_goals) & set(meal.health_goals))
            ranked.append(ScoredId(id=meal.id, score=score, source="structured"))
        return sorted(ranked, key=lambda item: (-item.score, item.id))

    def _rrf_fuse(
        self, structured: list[ScoredId], semantic: list[ScoredId]
    ) -> tuple[list[tuple[int, float]], dict[int, list[str]]]:
        scores: dict[int, float] = defaultdict(float)
        sources: dict[int, list[str]] = defaultdict(list)
        for weight, ranking in ((0.45, structured), (0.55, semantic)):
            for rank, item in enumerate(ranking, start=1):
                scores[item.id] += weight / (self.rrf_k + rank)
                if item.source not in sources[item.id]:
                    sources[item.id].append(item.source)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0])), sources

    @staticmethod
    def _apply_diversity(
        ranked: list[tuple[Meal, float, list[str]]], limit: int
    ) -> list[tuple[Meal, float, list[str]]]:
        selected: list[tuple[Meal, float, list[str]]] = []
        name_keys: set[str] = set()
        for item in ranked:
            meal = item[0]
            key = meal.name.casefold().replace(" ", "")
            if key in name_keys:
                continue
            selected.append(item)
            name_keys.add(key)
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _reasons(meal: Meal, filters: MealSearchFilters) -> list[str]:
        reasons: list[str] = []
        for term in [
            *filters.required_ingredients,
            *(term for group in filters.ingredient_any_groups for term in group),
        ]:
            if match_ingredient(term, meal.ingredients, meal.ingredient_records):
                reasons.append(f"收录配方含有{term}")
        if filters.health_goals:
            matched = sorted(set(filters.health_goals) & set(meal.health_goals))
            if matched:
                reasons.append(f"匹配健康目标：{'、'.join(matched)}")
        if filters.tastes:
            matched = sorted(set(filters.tastes) & set(meal.tastes))
            if matched:
                reasons.append(f"匹配口味：{'、'.join(matched)}")
        if filters.max_calories_kcal is not None:
            bounds = meal.nutrition.bounds("calories_kcal")
            reasons.append(f"估算 {bounds.minimum:g}～{bounds.maximum:g} 千卡")
        if filters.max_prep_minutes is not None:
            reasons.append(f"准备时间约 {meal.prep_minutes} 分钟")
        return reasons or ["与当前描述和筛选条件相关"]


class GuidelineSearchService:
    def __init__(self, repository: GuidelineRepository) -> None:
        self.repository = repository

    async def search(self, query: str, limit: int = 5) -> list[GuidelineSearchHit]:
        query_tokens = set(lexical_tokens(query))
        guidelines = await self.repository.list_guidelines()
        hits: list[GuidelineSearchHit] = []
        for guideline in guidelines:
            text = " ".join([guideline.title, guideline.content, *guideline.topics])
            tokens = set(lexical_tokens(text))
            score = len(query_tokens & tokens) / max(1, len(query_tokens))
            if score > 0:
                hits.append(GuidelineSearchHit(guideline=guideline, score=score))
        return sorted(hits, key=lambda hit: (-hit.score, hit.guideline.id))[:limit]


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))
