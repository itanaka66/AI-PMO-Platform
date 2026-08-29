"""Qdrant アダプタ / Qdrant adapter.

コレクション構成 / Collection layout:

    tenant_<company>        企業ごとの非公開ナレッジ / per-tenant private knowledge
    public_pmo_knowledge    一般化済みの公開ナレッジ / generalized public knowledge

scope・publicability スコア・書き込み拒否といった共通の振る舞いは
[[vector_store.py]] の `VectorStoreAdapter` にある。ここに残るのは
Qdrant クライアントへの接続と、実際の search / upsert 呼び出しだけ。

Scope handling, publicability scoring, and the public-write refusal all live
in `VectorStoreAdapter` (vector_store.py). What remains here is only the
Qdrant client connection and the actual search/upsert calls.
"""
from __future__ import annotations

from typing import Any

from .base import AdapterError
from .vector_store import PRIVATE, PUBLIC, VectorStoreAdapter

__all__ = ["QdrantAdapter", "PRIVATE", "PUBLIC"]


class QdrantAdapter(VectorStoreAdapter):
    name = "qdrant"

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.url:
            raise AdapterError("qdrant: url が設定されていません / url is not configured")
        from qdrant_client import QdrantClient  # 遅延 import / lazy import

        self._client = QdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    def _health_backend(self, client: Any) -> None:
        client.get_collections()

    def _search_backend(
        self, client: Any, collection: str, query_vector: list[float],
        limit: int, filters: dict[str, Any] | None, min_score: float,
    ) -> list[dict[str, Any]]:
        query_filter = None
        if filters:
            from qdrant_client import models

            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    for k, v in filters.items()
                ]
            )

        hits = client.search(
            collection_name=collection,
            query_vector=query_vector,
            limit=limit,
            query_filter=query_filter,
            score_threshold=min_score or None,
        )
        return [
            {"id": str(h.id), "score": float(h.score), "payload": dict(h.payload or {})}
            for h in hits
        ]

    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        from qdrant_client import models

        client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
                for p in points
            ],
        )
