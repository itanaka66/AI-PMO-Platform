"""ベクトルストア・アダプタの共通基盤 / shared base for vector-store adapters.

Qdrant / pgvector / Chroma / Milvus / Weaviate は、テンプレートから見ると
同じ形（private/public スコープ・text か vector・payload）で使える。差が
あるのは接続方法と実際の検索・書き込み呼び出しだけなので、そこだけを
サブクラスに残し、それ以外はここに一度だけ書く。

From a template's perspective, Qdrant, pgvector, Chroma, Milvus, and Weaviate
all look the same (private/public scope, text or vector, a payload). What
differs is only how to connect and how to issue the actual search/upsert
call; everything else lives here once instead of five times.

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

設計判断 3 — どのベクトルストアを選んでも同じ道具として渡せる:
  接続設定で選んだバックエンドは、常にそのバックエンド名でも登録される
  （例: `adapters.pgvector` を設定すれば `pgvector.search` が使える）が、
  ちょうど1種類だけ設定されているときは、加えて論理名 `vector_store` でも
  登録される。新しく書くテンプレートは `vector_store.search` /
  `vector_store.submit_candidate` を使えば、あとでバックエンドを
  乗り換えてもテンプレート側の変更が要らない。LLM の `profile` と同じ考え方。

  Design decision 3 — any chosen backend can be handed to a template as the
  same tool. Whichever backend is configured always registers under its own
  name (e.g. configuring `adapters.pgvector` makes `pgvector.search`
  available); when exactly one backend is configured, it is additionally
  registered under the logical name `vector_store`. A newly written template
  that uses `vector_store.search` / `vector_store.submit_candidate` survives
  a later backend switch untouched — the same idea as an LLM `profile`.
"""
from __future__ import annotations

import threading
import uuid
from abc import abstractmethod
from typing import Any

from ..knowledge import score_publicability
from ..llm.embeddings import Embedder
from .base import Adapter, AdapterError, action

PRIVATE = "private"
PUBLIC = "public"


class VectorStoreAdapter(Adapter):
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
        # 並列ステップ・スケジューラの複数ジョブが同じアダプタ・インスタンスを
        # 同時に使うことがある。バックエンドのクライアントは1つの接続や
        # セッションを使い回すことが多いので、それに触れる区間を直列化する。
        # A parallel step group or several scheduler jobs can share one
        # adapter instance at once. The backend client typically reuses a
        # single connection or session, so access to it is serialized here.
        self._lock = threading.Lock()

    # -- サブクラスが実装する部分 / what a subclass provides ---------------

    @abstractmethod
    def _connect(self) -> Any:
        """バックエンドのクライアントを返す（遅延接続）。

        Returns the backend client, connecting lazily on first use.
        """

    @abstractmethod
    def _health_backend(self, client: Any) -> None:
        """軽い呼び出しを1回行う。例外が出れば health_check が False を返す。

        Issue one cheap call; an exception makes health_check report False.
        """

    @abstractmethod
    def _search_backend(
        self,
        client: Any,
        collection: str,
        query_vector: list[float],
        limit: int,
        filters: dict[str, Any] | None,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """[{"id": str, "score": float, "payload": dict}] の形で返す。"""

    @abstractmethod
    def _upsert_backend(
        self, client: Any, collection: str, points: list[dict[str, Any]]
    ) -> None:
        """points の各要素は {"id": str, "vector": list[float], "payload": dict}。"""

    # -- 共通の内部処理 / shared internals -----------------------------------

    def _collection(self, scope: str) -> str:
        if scope == PUBLIC:
            return self.public_collection
        if scope == PRIVATE:
            if not self.tenant:
                raise AdapterError(
                    f"{self.name}: tenant 未設定のため private スコープを使えません "
                    "/ private scope requires a configured tenant"
                )
            return f"tenant_{self.tenant}"
        raise AdapterError(
            f"{self.name}: scope は '{PRIVATE}' か '{PUBLIC}' のみです "
            f"/ scope must be '{PRIVATE}' or '{PUBLIC}' (受領 / got: {scope!r})"
        )

    def _vector(self, text: str | None, vector: list[float] | None) -> list[float]:
        if vector is not None:
            return vector
        if text is None:
            raise AdapterError(f"{self.name}: text か vector のいずれかが必要です / text or vector required")
        if self.embedder is None:
            raise AdapterError(
                f"{self.name}: embedder が未設定のため text 検索できません "
                "/ text search requires a configured embedder"
            )
        return self.embedder.embed_one(text)

    def health_check(self) -> bool:
        try:
            with self._lock:
                self._health_backend(self._connect())
            return True
        except Exception:
            return False

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
        with self._lock:
            items = self._search_backend(
                self._connect(), collection, query_vector, limit, filters, min_score
            )
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
                f"{self.name}: 公開コレクションへの直接書き込みは禁止です。"
                "submit_candidate による人間承認フローを使ってください "
                "/ direct writes to the public collection are not permitted; "
                "use submit_candidate and the human review workflow"
            )
        collection = self._collection(scope)

        points: list[dict[str, Any]] = []
        for index, document in enumerate(documents):
            text = document.get("text")
            vector = document.get("vector")
            payload = {k: v for k, v in document.items() if k != "vector"}
            payload.setdefault("tenant", self.tenant)
            if idempotency_key:
                payload.setdefault("source_key", f"{idempotency_key}:{index}")

            point_id = document.get("id") or str(
                uuid.uuid5(uuid.NAMESPACE_URL, payload.get("source_key") or f"{collection}:{text}")
            )
            points.append({
                "id": point_id,
                "vector": self._vector(text, vector),
                "payload": payload,
            })

        with self._lock:
            self._upsert_backend(self._connect(), collection, points)
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
