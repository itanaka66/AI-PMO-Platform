"""pgvector アダプタ / pgvector adapter.

既存の PostgreSQL に `CREATE EXTENSION vector` を足すだけで使えるベクトル
ストア。別のサーバーを増やしたくない・すでに Postgres を運用している、
という構成に向く。テーブルは事前に作成されている前提（Qdrant がコレクションの
事前作成を前提にしているのと同じ)。

    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE pmo_vectors (
        id         text PRIMARY KEY,
        collection text NOT NULL,
        embedding  vector(1536),   -- 埋め込みの次元に合わせる
        payload    jsonb NOT NULL
    );
    CREATE INDEX ON pmo_vectors (collection);

scope・publicability スコア・書き込み拒否といった共通の振る舞いは
[[vector_store.py]] の `VectorStoreAdapter` にある。

A vector store that needs nothing beyond `CREATE EXTENSION vector` on a
Postgres already in use — fits a shop that already runs Postgres and would
rather not stand up another server. The table is assumed to already exist,
the same assumption Qdrant makes about its collections.

Shared behaviour (scope, publicability scoring, refusing public writes) lives
in `VectorStoreAdapter` (vector_store.py).
"""
from __future__ import annotations

from typing import Any

from .base import AdapterError
from .vector_store import PRIVATE, PUBLIC, VectorStoreAdapter

__all__ = ["PgVectorAdapter", "PRIVATE", "PUBLIC"]


class PgVectorAdapter(VectorStoreAdapter):
    name = "pgvector"

    def __init__(self, dsn: str | None = None, table: str = "pmo_vectors", **config: Any) -> None:
        super().__init__(**config)
        self.dsn = dsn
        self.table = table

    # -- 接続 / connection -------------------------------------------------

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.dsn:
            raise AdapterError("pgvector: dsn が設定されていません / dsn is not configured")

        import psycopg  # 遅延 import / lazy import
        from pgvector.psycopg import register_vector

        connection = psycopg.connect(self.dsn)
        register_vector(connection)
        self._client = connection
        return self._client

    def _health_backend(self, client: Any) -> None:
        with client.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    # -- 内部 / internals ----------------------------------------------------

    @staticmethod
    def _filter_clause(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        """`payload` の JSONB 包含一致に落とす / falls back to JSONB containment."""
        if not filters:
            return "", []
        import json

        return " AND payload @> %s::jsonb", [json.dumps(filters)]

    def _search_backend(
        self, client: Any, collection: str, query_vector: list[float],
        limit: int, filters: dict[str, Any] | None, min_score: float,
    ) -> list[dict[str, Any]]:
        clause, extra = self._filter_clause(filters)
        sql = (
            f"SELECT id, payload, 1 - (embedding <=> %s) AS score "
            f"FROM {self.table} WHERE collection = %s{clause} "
            f"ORDER BY embedding <=> %s LIMIT %s"
        )
        values = [query_vector, collection, *extra, query_vector, limit]
        with client.cursor() as cur:
            cur.execute(sql, values)
            rows = cur.fetchall()

        items = [
            {"id": str(row[0]), "score": float(row[2]), "payload": dict(row[1] or {})}
            for row in rows
        ]
        if min_score:
            items = [item for item in items if item["score"] >= min_score]
        return items

    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        import json

        with client.cursor() as cur:
            for point in points:
                cur.execute(
                    f"INSERT INTO {self.table} (id, collection, embedding, payload) "
                    f"VALUES (%s, %s, %s, %s::jsonb) "
                    f"ON CONFLICT (id) DO UPDATE SET "
                    f"collection = EXCLUDED.collection, embedding = EXCLUDED.embedding, "
                    f"payload = EXCLUDED.payload",
                    [point["id"], collection, point["vector"], json.dumps(point["payload"])],
                )
        client.commit()
