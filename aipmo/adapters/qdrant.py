"""Qdrant アダプタ / Qdrant adapter.

コレクション構成 / Collection layout:

    tenant_<company>        企業ごとの非公開ナレッジ / per-tenant private knowledge
    public_pmo_knowledge    一般化済みの公開ナレッジ / generalized public knowledge

設計判断 1 — コレクション名をテンプレートに書かせない:
  テンプレートが指定できるのは論理スコープ "private" / "public" のみ。
  実コレクション名は接続設定で解決する。
  配布テンプレートに tenant_company_b と書かれても、他社データには届かない。

  Design decision 1 — templates never name a collection.
  A template may only select the logical scope "private" or "public"; the
  concrete collection is resolved from connection config. A distributed
  template that hardcodes `tenant_company_b` cannot reach another tenant.

設計判断 2 — 公開コレクションへの書き込みをアダプタが拒否する:
  ナレッジの公開は人間承認を経た昇格フローだけが行える。
  自動公開の経路を、そもそもテンプレートから作れないようにする。
  テンプレートができるのは「昇格候補として提出する」ところまで。

  Design decision 2 — the adapter refuses writes to the public collection.
  Publication happens only through the reviewed promotion workflow. A
  template can submit a candidate; it cannot publish. The automatic-
  publication path does not exist at the adapter level, so no template —
  including one written by a third party — can create it.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..knowledge import score_publicability
from ..llm.embeddings import Embedder
from .base import Adapter, AdapterError, action

PRIVATE = "private"
PUBLIC = "public"


class QdrantAdapter(Adapter):
    name = "qdrant"

    def __init__(
        self,
        url: str | None = None,
        tenant: str | None = None,
        public_collection: str = "public_pmo_knowledge",
        embedder: Embedder | None = None,
        client: Any = None,
        api_key: str | None = None,
        **config: Any,
    ) -> None:
        super().__init__(**config)
        self.url = url
        self.tenant = tenant
        self.public_collection = public_collection
        self.embedder = embedder
        self.api_key = api_key
        self._client = client

    # -- 接続 / connection -------------------------------------------------

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.url:
            raise AdapterError("qdrant: url が設定されていません / url is not configured")
        from qdrant_client import QdrantClient  # 遅延 import / lazy import

        self._client = QdrantClient(url=self.url, api_key=self.api_key)
        return self._client

    def health_check(self) -> bool:
        try:
            self._connect().get_collections()
            return True
        except Exception:
            return False

    # -- 内部 / internals --------------------------------------------------

    def _collection(self, scope: str) -> str:
        if scope == PUBLIC:
            return self.public_collection
        if scope == PRIVATE:
            if not self.tenant:
                raise AdapterError(
                    "qdrant: tenant 未設定のため private スコープを使えません "
                    "/ private scope requires a configured tenant"
                )
            return f"tenant_{self.tenant}"
        raise AdapterError(
            f"qdrant: scope は '{PRIVATE}' か '{PUBLIC}' のみです "
            f"/ scope must be '{PRIVATE}' or '{PUBLIC}' (受領 / got: {scope!r})"
        )

    def _vector(self, text: str | None, vector: list[float] | None) -> list[float]:
        if vector is not None:
            return vector
        if text is None:
            raise AdapterError("qdrant: text か vector のいずれかが必要です / text or vector required")
        if self.embedder is None:
            raise AdapterError(
                "qdrant: embedder が未設定のため text 検索できません "
                "/ text search requires a configured embedder"
            )
        return self.embedder.embed_one(text)

    # -- アクション / actions ----------------------------------------------

    @action()
    def search(
        self,
        text: str | None = None,
        vector: list[float] | None = None,
        scope: str = PRIVATE,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
        min_score: float = 0.0,
    ) -> dict[str, Any]:
        collection = self._collection(scope)
        query_vector = self._vector(text, vector)

        query_filter = None
        if filters:
            from qdrant_client import models

            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key=k, match=models.MatchValue(value=v))
                    for k, v in filters.items()
                ]
            )

        hits = self._connect().search(
            collection_name=collection,
            query_vector=query_vector,
            limit=limit,
            query_filter=query_filter,
            score_threshold=min_score or None,
        )

        items = [
            {"id": str(h.id), "score": float(h.score), "payload": dict(h.payload or {})}
            for h in hits
        ]
        return {"items": items, "count": len(items), "collection": collection}

    @action(writes=True)
    def upsert(
        self,
        documents: list[dict[str, Any]],
        scope: str = PRIVATE,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """非公開スコープにのみ書き込める / writes are permitted to private scope only."""
        if scope == PUBLIC:
            raise AdapterError(
                "qdrant: 公開コレクションへの直接書き込みは禁止です。"
                "submit_candidate による人間承認フローを使ってください "
                "/ direct writes to the public collection are not permitted; "
                "use submit_candidate and the human review workflow"
            )
        collection = self._collection(scope)

        from qdrant_client import models

        points = []
        for index, document in enumerate(documents):
            text = document.get("text")
            vector = document.get("vector")
            payload = {k: v for k, v in document.items() if k not in ("vector",)}
            payload.setdefault("tenant", self.tenant)
            if idempotency_key:
                payload.setdefault("source_key", f"{idempotency_key}:{index}")

            point_id = document.get("id") or str(
                uuid.uuid5(uuid.NAMESPACE_URL, payload.get("source_key") or f"{collection}:{text}")
            )
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=self._vector(text, vector),
                    payload=payload,
                )
            )

        self._connect().upsert(collection_name=collection, points=points)
        return {"upserted": len(points), "collection": collection}

    @action(writes=True)
    def submit_candidate(
        self,
        knowledge: dict[str, Any],
        knowledge_level: int = 3,
        consent_level: str | None = None,
        publicability_score: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """公開候補として提出する。これ自体は公開しない。

        公開可能性スコアは自動で算出する。テンプレート側が数値を用意する
        必要はない — レビューの並び順を決める下書きの値であって、
        承認・却下そのものはここでは決まらない。明示的に渡せばそちらが
        優先される。consent_level は `postgres.consent_level` の結果
        （A/B/C）をそのまま渡すことを想定している。

        Submit a candidate for publication. This does not publish anything.
        The record lands in the private collection tagged for review; a human
        approves it in the promotion workflow, which runs outside any template.

        The publicability score is computed automatically — a template need
        not supply one. It is a draft value used only to order the review
        queue; nothing here approves or rejects anything. An explicit value,
        if given, takes priority. `consent_level` is meant to be passed
        straight from the result of `postgres.consent_level` (A/B/C).
        """
        scored = score_publicability(
            knowledge, knowledge_level=knowledge_level,
            consent_level=consent_level, tenant=self.tenant,
        )
        score = scored.value if publicability_score is None else publicability_score

        payload = {
            **knowledge,
            "review_status": "pending",
            "knowledge_level": knowledge_level,
            "publicability_score": score,
            "publicability_reasons": scored.reasons,
        }
        result = self.upsert([payload], scope=PRIVATE, idempotency_key=idempotency_key)
        return {
            **result,
            "review_status": "pending",
            "publicability_score": score,
            "publicability_reasons": scored.reasons,
        }
