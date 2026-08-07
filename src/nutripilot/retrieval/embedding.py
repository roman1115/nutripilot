from __future__ import annotations

import hashlib
import importlib
import math
import re
from itertools import pairwise
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


def lexical_tokens(text: str) -> list[str]:
    base = _WORD_RE.findall(text.casefold())
    chinese = [token for token in base if "\u4e00" <= token <= "\u9fff"]
    bigrams = [f"{left}{right}" for left, right in pairwise(chinese)]
    return base + bigrams


class HashingEmbedder:
    """Deterministic offline fallback; BGE can replace it behind the same port."""

    version = "hashing-embedding-v1"

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 32:
            raise ValueError("dimension must be at least 32")
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in lexical_tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "big")
            index = raw % self.dimension
            sign = 1.0 if raw & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            return [value / norm for value in vector]
        return vector


class BGEEmbedder:
    """Lazy optional BGE-M3 adapter; no model download occurs on import."""

    version = "BAAI/bge-m3"

    def __init__(self, model_name: str = "BAAI/bge-m3", device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device
        self._model: Any | None = None
        self.dimension = 1024

    def _load(self) -> Any:
        if self._model is None:
            try:
                module = importlib.import_module("sentence_transformers")
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is required to use BGEEmbedder") from exc
            self._model = module.SentenceTransformer(self.model_name, device=self.device)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        model = self._load()
        vectors = await asyncio.to_thread(
            model.encode,
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        if len(vectors):
            self.dimension = len(vectors[0])
        return [vector.tolist() for vector in vectors]
