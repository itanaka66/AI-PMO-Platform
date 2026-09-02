"""pgvector / Chroma / Milvus / Weaviate アダプタのテスト。

Qdrant と共有している scope・publicability・公開拒否の振る舞いは
tests/test_data_adapters.py 側で検証済み。ここでの主眼は、各バックエンド
固有の接続・検索・書き込み呼び出しが正しい形に変換されていること。

Shared behaviour with Qdrant (scope, publicability, refusing public writes)
is already covered in tests/test_data_adapters.py. The focus here is that
each backend-specific connect/search/upsert call is translated correctly.
"""
from __future__ import annotations

from typing import Any

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.chroma import ChromaAdapter
from aipmo.adapters.milvus import MilvusAdapter
from aipmo.adapters.pgvector import PgVectorAdapter
from aipmo.adapters.weaviate import WeaviateAdapter
from aipmo.llm.embeddings import HashEmbedder

# --- pgvector ---------------------------------------------------------------


class FakePgCursor:
    def __init__(self, log: list[tuple[str, list[Any]]], rows: list[tuple]) -> None:
        self._log = log
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, values: list[Any] | None = None) -> None:
        self._log.append((sql, list(values or [])))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakePgVectorConnection:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.rows = rows or []
        self.commits = 0

    def cursor(self):
        return FakePgCursor(self.log, self.rows)

    def commit(self) -> None:
        self.commits += 1


def build_pgvector(rows=None) -> tuple[PgVectorAdapter, FakePgVectorConnection]:
    connection = FakePgVectorConnection(rows=rows)
    adapter = PgVectorAdapter(tenant="company_a", embedder=HashEmbedder(), client=connection)
    return adapter, connection


def test_pgvector_upsert_inserts_a_row_and_commits():
    adapter, connection = build_pgvector()
    adapter.invoke("upsert", {"documents": [{"text": "リスク事例"}]})

    sql, values = connection.log[0]
    assert "pmo_vectors" in sql
    assert "tenant_company_a" in values
    assert connection.commits == 1


def test_pgvector_search_filters_by_min_score():
    rows = [("id1", {"text": "a"}, 0.9), ("id2", {"text": "b"}, 0.1)]
    adapter, _ = build_pgvector(rows=rows)
    result = adapter.invoke("search", {"text": "x", "min_score": 0.5})

    assert [item["id"] for item in result["items"]] == ["id1"]


def test_pgvector_public_write_is_refused():
    adapter, connection = build_pgvector()
    with pytest.raises(AdapterError, match="人間承認"):
        adapter.invoke("upsert", {"documents": [{"text": "x"}], "scope": "public"})
    assert connection.log == []


def test_pgvector_health_check_runs_select_1():
    adapter, connection = build_pgvector(rows=[(1,)])
    assert adapter.health_check() is True
    assert connection.log[0][0] == "SELECT 1"


# --- Chroma -------------------------------------------------------------


class FakeChromaCollection:
    def __init__(self) -> None:
        self.upserted: tuple[Any, Any, Any] | None = None

    def query(self, query_embeddings, n_results, where=None):
        return {
            "ids": [["id1", "id2"]],
            "distances": [[0.1, 0.9]],
            "metadatas": [[{"text": "a"}, {"text": "b"}]],
        }

    def upsert(self, ids, embeddings, metadatas):
        self.upserted = (ids, embeddings, metadatas)


class FakeChromaClient:
    def __init__(self) -> None:
        self.collection = FakeChromaCollection()
        self.heartbeats = 0

    def get_collection(self, name):
        return self.collection

    def heartbeat(self):
        self.heartbeats += 1
        return 1


def build_chroma() -> tuple[ChromaAdapter, FakeChromaClient]:
    client = FakeChromaClient()
    adapter = ChromaAdapter(tenant="company_a", embedder=HashEmbedder(), client=client)
    return adapter, client


def test_chroma_search_converts_distance_to_score_and_applies_min_score():
    adapter, _ = build_chroma()
    result = adapter.invoke("search", {"text": "x", "min_score": 0.5})

    assert result["items"] == [{"id": "id1", "score": 0.9, "payload": {"text": "a"}}]


def test_chroma_upsert_passes_ids_embeddings_and_metadata():
    adapter, client = build_chroma()
    adapter.invoke("upsert", {"documents": [{"text": "リスク事例"}]})

    ids, embeddings, metadatas = client.collection.upserted
    assert len(ids) == 1 and len(embeddings) == 1
    assert metadatas[0]["tenant"] == "company_a"


def test_chroma_health_check_calls_heartbeat():
    adapter, client = build_chroma()
    assert adapter.health_check() is True
    assert client.heartbeats == 1


# --- Milvus ---------------------------------------------------------------


class FakeMilvusClient:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, Any]] = []
        self.searches: list[tuple[str, Any, int, str]] = []

    def search(self, collection_name, data, limit, filter, output_fields):
        self.searches.append((collection_name, data, limit, filter))
        return [[
            {"id": "id1", "distance": 0.9, "entity": {"text": "a"}},
            {"id": "id2", "distance": 0.1, "entity": {"text": "b"}},
        ]]

    def upsert(self, collection_name, data):
        self.upserts.append((collection_name, data))

    def list_collections(self):
        return ["tenant_company_a"]


def build_milvus() -> tuple[MilvusAdapter, FakeMilvusClient]:
    client = FakeMilvusClient()
    adapter = MilvusAdapter(tenant="company_a", embedder=HashEmbedder(), client=client)
    return adapter, client


def test_milvus_search_filters_by_min_score_and_drops_the_vector_field():
    adapter, _ = build_milvus()
    result = adapter.invoke("search", {"text": "x", "min_score": 0.5})

    assert result["items"] == [{"id": "id1", "score": 0.9, "payload": {"text": "a"}}]


def test_milvus_search_builds_an_equality_filter_expression():
    adapter, client = build_milvus()
    adapter.invoke("search", {"text": "x", "filters": {"pattern": "key_person"}})

    _, _, _, expr = client.searches[0]
    assert expr == "pattern == 'key_person'"


def test_milvus_upsert_flattens_payload_alongside_id_and_vector():
    adapter, client = build_milvus()
    adapter.invoke("upsert", {"documents": [{"text": "リスク事例"}]})

    collection, data = client.upserts[0]
    assert collection == "tenant_company_a"
    assert data[0]["text"] == "リスク事例"
    assert data[0]["tenant"] == "company_a"
    assert "id" in data[0] and "vector" in data[0]


def test_milvus_health_check_lists_collections():
    adapter, _ = build_milvus()
    assert adapter.health_check() is True


# --- Weaviate ---------------------------------------------------------------


class FakeWeaviateMetadata:
    def __init__(self, distance: float) -> None:
        self.distance = distance


class FakeWeaviateObject:
    def __init__(self, uuid: str, properties: dict, distance: float) -> None:
        self.uuid = uuid
        self.properties = properties
        self.metadata = FakeWeaviateMetadata(distance)


class FakeWeaviateResult:
    def __init__(self, objects: list[FakeWeaviateObject]) -> None:
        self.objects = objects


class FakeWeaviateQuery:
    def __init__(self, objects: list[FakeWeaviateObject]) -> None:
        self._objects = objects

    def near_vector(self, near_vector, limit, filters, return_metadata):
        return FakeWeaviateResult(self._objects)


class FakeWeaviateData:
    def __init__(self) -> None:
        self.replaced: list[tuple[str, dict, list]] = []

    def replace(self, uuid, properties, vector):
        self.replaced.append((uuid, properties, vector))


class FakeWeaviateCollection:
    def __init__(self, objects: list[FakeWeaviateObject]) -> None:
        self.query = FakeWeaviateQuery(objects)
        self.data = FakeWeaviateData()


class FakeWeaviateCollections:
    def __init__(self, collection: FakeWeaviateCollection) -> None:
        self._collection = collection

    def get(self, name):
        return self._collection


class FakeWeaviateClient:
    def __init__(self, objects: list[FakeWeaviateObject], ready: bool = True) -> None:
        self.collections = FakeWeaviateCollections(FakeWeaviateCollection(objects))
        self._ready = ready

    def is_ready(self):
        return self._ready


def build_weaviate(objects=None, ready: bool = True) -> tuple[WeaviateAdapter, FakeWeaviateClient]:
    client = FakeWeaviateClient(objects or [], ready=ready)
    adapter = WeaviateAdapter(tenant="company_a", embedder=HashEmbedder(), client=client)
    return adapter, client


def test_weaviate_search_converts_distance_to_score_and_applies_min_score():
    objects = [
        FakeWeaviateObject("id1", {"text": "a"}, distance=0.1),
        FakeWeaviateObject("id2", {"text": "b"}, distance=0.9),
    ]
    adapter, _ = build_weaviate(objects)
    result = adapter.invoke("search", {"text": "x", "min_score": 0.5})

    assert result["items"] == [{"id": "id1", "score": 0.9, "payload": {"text": "a"}}]


def test_weaviate_upsert_replaces_each_point():
    adapter, client = build_weaviate()
    adapter.invoke("upsert", {"documents": [{"text": "リスク事例"}]})

    uuid, properties, vector = client.collections._collection.data.replaced[0]
    assert properties["tenant"] == "company_a"
    assert isinstance(vector, list)


def test_weaviate_health_check_reflects_is_ready():
    adapter, _ = build_weaviate(ready=False)
    assert adapter.health_check() is False


def test_weaviate_submit_candidate_stays_pending():
    adapter, client = build_weaviate()
    result = adapter.invoke("submit_candidate", {
        "knowledge": {"text": "主要担当者への依存はスケジュールリスクになる"},
        "publicability_score": 90,
    })

    assert result["review_status"] == "pending"
    _, properties, _ = client.collections._collection.data.replaced[0]
    assert properties["review_status"] == "pending"
