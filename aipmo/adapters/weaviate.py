"""Weaviate アダプタ / Weaviate adapter.

v4 クライアントを使う。コレクション（Weaviate 用語の "collection"）は
事前に作成されている前提（Qdrant がコレクションの事前作成を前提にしている
のと同じ)。REST と gRPC の両方のポートが要る。

scope・publicability スコア・書き込み拒否といった共通の振る舞いは
[[vector_store.py]] の `VectorStoreAdapter` にある。

Uses the v4 client. The collection is assumed to already exist, the same
assumption Qdrant makes. Needs both the REST and the gRPC port.

Shared behaviour (scope, publicability scoring, refusing public writes) lives
in `VectorStoreAdapter` (vector_store.py).
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import AdapterError
from .vector_store import PRIVATE, PUBLIC, VectorStoreAdapter

__all__ = ["WeaviateAdapter", "PRIVATE", "PUBLIC"]


class WeaviateAdapter(VectorStoreAdapter):
    name = "weaviate"

    def __init__(self, grpc_port: int = 50051, **config: Any) -> None:
        super().__init__(**config)
        self.grpc_port = grpc_port

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.url:
            raise AdapterError("weaviate: url が設定されていません / url is not configured")

        import weaviate  # 遅延 import / lazy import
        from weaviate.auth import AuthApiKey

        parts = urlsplit(self.url)
        secure = parts.scheme == "https"
        self._client = weaviate.connect_to_custom(
            http_host=parts.hostname or "localhost",
            http_port=parts.port or (443 if secure else 80),
            http_secure=secure,
            grpc_host=parts.hostname or "localhost",
            grpc_port=self.grpc_port,
            grpc_secure=secure,
            auth_credentials=AuthApiKey(self.api_key) if self.api_key else None,
        )
        return self._client

    def _health_backend(self, client: Any) -> None:
        if not client.is_ready():
            raise AdapterError("weaviate: is_ready() が false を返しました / not ready")

    def _search_backend(
        self, client: Any, collection: str, query_vector: list[float],
        limit: int, filters: dict[str, Any] | None, min_score: float,
    ) -> list[dict[str, Any]]:
        where = None
        if filters:
            from weaviate.classes.query import Filter  # 遅延 import / lazy import

            conditions = [Filter.by_property(k).equal(v) for k, v in filters.items()]
            where = conditions[0]
            for extra in conditions[1:]:
                where = where & extra

        result = client.collections.get(collection).query.near_vector(
            near_vector=query_vector,
            limit=limit,
            filters=where,
            # MetadataQuery(distance=True) と等価な省略形。ここを型付き
            # ヘルパーにすると weaviate 未導入でも import が必要になり、
            # フィルタなしの経路までテスト用の擬似クライアントで検証できなくなる。
            # Shorthand equivalent to MetadataQuery(distance=True). Using the
            # typed helper here would force importing weaviate even on the
            # unfiltered path, which would keep it from being exercised
            # against a fake client without weaviate-client installed.
            return_metadata=["distance"],
        )
        items = []
        for obj in result.objects:
            distance = obj.metadata.distance if obj.metadata else None
            score = 1.0 - float(distance) if distance is not None else 0.0
            if score < min_score:
                continue
            items.append({"id": str(obj.uuid), "score": score, "payload": dict(obj.properties or {})})
        return items

    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        coll = client.collections.get(collection)
        for point in points:
            try:
                coll.data.replace(uuid=point["id"], properties=point["payload"], vector=point["vector"])
            except Exception:
                coll.data.insert(uuid=point["id"], properties=point["payload"], vector=point["vector"])
