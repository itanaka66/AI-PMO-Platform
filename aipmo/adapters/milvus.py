"""Milvus アダプタ / Milvus adapter.

`pymilvus.MilvusClient`（高レベル API）を使う。コレクションは動的フィールド
有効で事前に作成されている前提（Qdrant がコレクションの事前作成を
前提にしているのと同じ)。

scope・publicability スコア・書き込み拒否といった共通の振る舞いは
[[vector_store.py]] の `VectorStoreAdapter` にある。

Uses `pymilvus.MilvusClient`, the high-level API. The collection is assumed
to already exist with dynamic fields enabled, the same assumption Qdrant
makes about its collections.

Shared behaviour (scope, publicability scoring, refusing public writes) lives
in `VectorStoreAdapter` (vector_store.py).
"""
from __future__ import annotations

from typing import Any

from .base import AdapterError
from .vector_store import PRIVATE, PUBLIC, VectorStoreAdapter

__all__ = ["MilvusAdapter", "PRIVATE", "PUBLIC"]


class MilvusAdapter(VectorStoreAdapter):
    name = "milvus"

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.url:
            raise AdapterError("milvus: url が設定されていません / url is not configured")

        from pymilvus import MilvusClient  # 遅延 import / lazy import

        self._client = MilvusClient(uri=self.url, token=self.api_key)
        return self._client

    def _health_backend(self, client: Any) -> None:
        client.list_collections()

    @staticmethod
    def _filter_expr(filters: dict[str, Any] | None) -> str:
        """dict の完全一致だけを Milvus のフィルタ式に変換する。

        Converts only exact-match dict filters into a Milvus filter expression.
        """
        if not filters:
            return ""
        return " and ".join(f"{key} == {value!r}" for key, value in filters.items())

    def _search_backend(
        self, client: Any, collection: str, query_vector: list[float],
        limit: int, filters: dict[str, Any] | None, min_score: float,
    ) -> list[dict[str, Any]]:
        hits = client.search(
            collection_name=collection,
            data=[query_vector],
            limit=limit,
            filter=self._filter_expr(filters),
            output_fields=["*"],
        )
        items = []
        for hit in hits[0] if hits else []:
            score = float(hit.get("distance", 0.0))
            if score < min_score:
                continue
            entity = dict(hit.get("entity") or {})
            entity.pop("vector", None)
            items.append({"id": str(hit.get("id")), "score": score, "payload": entity})
        return items

    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        client.upsert(
            collection_name=collection,
            data=[{"id": p["id"], "vector": p["vector"], **p["payload"]} for p in points],
        )
