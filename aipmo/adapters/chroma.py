"""Chroma アダプタ / Chroma adapter.

自前サーバー（`chromadb.HttpClient`）を前提にする。コレクションは
事前に作成されている前提（Qdrant がコレクションの事前作成を前提にして
いるのと同じ)。

scope・publicability スコア・書き込み拒否といった共通の振る舞いは
[[vector_store.py]] の `VectorStoreAdapter` にある。

Assumes a self-hosted server (`chromadb.HttpClient`). The collection is
assumed to already exist, the same assumption Qdrant makes.

Shared behaviour (scope, publicability scoring, refusing public writes) lives
in `VectorStoreAdapter` (vector_store.py).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import AdapterError
from .vector_store import PRIVATE, PUBLIC, VectorStoreAdapter

__all__ = ["ChromaAdapter", "PRIVATE", "PUBLIC"]


class ChromaAdapter(VectorStoreAdapter):
    name = "chroma"

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.url:
            raise AdapterError("chroma: url が設定されていません / url is not configured")

        import chromadb  # 遅延 import / lazy import

        parts = urlsplit(self.url)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        self._client = chromadb.HttpClient(
            host=parts.hostname or "localhost",
            port=parts.port or (443 if parts.scheme == "https" else 8000),
            ssl=parts.scheme == "https",
            headers=headers,
        )
        return self._client

    def _health_backend(self, client: Any) -> None:
        client.heartbeat()

    def _search_backend(
        self, client: Any, collection: str, query_vector: list[float],
        limit: int, filters: dict[str, Any] | None, min_score: float,
    ) -> list[dict[str, Any]]:
        coll = client.get_collection(name=collection)
        result = coll.query(
            query_embeddings=[query_vector],
            n_results=limit,
            where=filters or None,
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

        items = []
        for point_id, distance, metadata in zip(ids, distances, metadatas):
            # コサイン距離での近似 / approximate for cosine space.
            score = 1.0 - float(distance)
            if score < min_score:
                continue
            items.append({"id": str(point_id), "score": score, "payload": dict(metadata or {})})
        return items

    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        coll = client.get_collection(name=collection)
        coll.upsert(
            ids=[p["id"] for p in points],
            embeddings=[p["vector"] for p in points],
            metadatas=[p["payload"] for p in points],
        )
