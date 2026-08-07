from __future__ import annotations

from typing import Any

from nutripilot.retrieval.models import ScoredId


class QdrantMealIndex:
    """Optional async Qdrant index. Authoritative meal data remains in PostgreSQL."""

    version = "qdrant-meal-search-v1"

    def __init__(self, client: Any, *, collection_name: str = "meal_search") -> None:
        self.client = client
        self.collection_name = collection_name

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        api_key: str | None = None,
        collection_name: str = "meal_search",
    ) -> QdrantMealIndex:
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as exc:
            raise RuntimeError("qdrant-client is required to use QdrantMealIndex") from exc
        return cls(
            AsyncQdrantClient(url=url, api_key=api_key),
            collection_name=collection_name,
        )

    async def ensure_collection(self, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        exists = await self.client.collection_exists(self.collection_name)
        if not exists:
            await self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )

    async def upsert(self, records: list[tuple[int, list[float], dict[str, Any]]]) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(id=meal_id, vector=vector, payload={"meal_id": meal_id, **payload})
            for meal_id, vector, payload in records
        ]
        if points:
            await self.client.upsert(collection_name=self.collection_name, points=points, wait=True)

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        eligible_ids: set[int] | None = None,
    ) -> list[ScoredId]:
        query_filter = None
        if eligible_ids is not None:
            from qdrant_client.models import FieldCondition, Filter, MatchAny

            query_filter = Filter(
                must=[FieldCondition(key="meal_id", match=MatchAny(any=sorted(eligible_ids)))]
            )
        response = await self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
        return [
            ScoredId(
                id=int((point.payload or {}).get("meal_id", point.id)),
                score=float(point.score),
                source="semantic",
            )
            for point in points
        ]

    async def close(self) -> None:
        await self.client.close()
