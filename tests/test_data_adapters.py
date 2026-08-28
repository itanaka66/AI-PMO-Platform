"""PostgreSQL / Qdrant アダプタのテスト。

主眼は「配布テンプレートが越境できないこと」の検証。
Focus: proving a distributed template cannot cross a tenant boundary.
"""
from __future__ import annotations

from typing import Any

import pytest

from aipmo.adapters.base import AdapterError, AdapterRegistry
from aipmo.adapters.postgres import PostgresAdapter
from aipmo.adapters.qdrant import QdrantAdapter
from aipmo.dsl import loader
from aipmo.engine.runner import Engine
from aipmo.llm.embeddings import HashEmbedder
from aipmo.llm.registry import LLMRegistry
from aipmo.llm.base import EchoProvider


# --- fakes ----------------------------------------------------------------

class FakeCursor:
    def __init__(self, log: list[tuple[str, list[Any]]], rows: list[tuple]) -> None:
        self._log = log
        self._rows = rows
        self.description = [("level",)]
        self.rowcount = len(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, values: list[Any] | None = None) -> None:
        self._log.append((sql, list(values or [])))

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.rows = rows or []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.log, self.rows)

    def commit(self):
        self.commits += 1


class FakeQdrantClient:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, Any]] = []
        self.searches: list[str] = []

    def upsert(self, collection_name: str, points: Any) -> None:
        self.upserts.append((collection_name, points))

    def search(self, collection_name: str, **kwargs):
        self.searches.append(collection_name)
        return []


QUERIES = {
    "overdue_tasks": "SELECT * FROM tasks WHERE tenant = :tenant AND due_date < :as_of",
    "consent_level_by_tenant": "SELECT level FROM tenant_consent WHERE tenant = :tenant",
    "insert_run": "INSERT INTO runs (id, template) VALUES (:id, :template) "
                  "ON CONFLICT (:idempotency_key) DO NOTHING",
}


# --- postgres -------------------------------------------------------------

def test_raw_sql_from_template_is_impossible():
    """テンプレートは SQL 文字列を渡せない。クエリ名しか受け付けない。"""
    connection = FakeConnection()
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a", connection=connection)

    with pytest.raises(AdapterError, match="名前付きクエリ"):
        adapter.invoke("query", {"name": "SELECT * FROM tasks; DROP TABLE tasks"})


def test_tenant_comes_from_config_not_template():
    """テンプレートが tenant を上書きしようとしても、接続設定側が勝つ。"""
    connection = FakeConnection(rows=[("B",)])
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a", connection=connection)

    adapter.invoke("query", {
        "name": "overdue_tasks",
        "params": {"tenant": "company_b", "as_of": "2026-08-27"},
    })

    sql, values = connection.log[0]
    assert "%s" in sql and ":tenant" not in sql
    assert "company_a" in values
    assert "company_b" not in values


def test_values_are_bound_never_concatenated():
    connection = FakeConnection(rows=[])
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a", connection=connection)

    adapter.invoke("query", {"name": "overdue_tasks",
                             "params": {"as_of": "2026-08-27'; DROP TABLE tasks--"}})

    sql, values = connection.log[0]
    assert "DROP TABLE" not in sql
    assert "2026-08-27'; DROP TABLE tasks--" in values


def test_write_query_rejected_by_read_action():
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a",
                              connection=FakeConnection())
    with pytest.raises(AdapterError, match="書き込みクエリ"):
        adapter.invoke("query", {"name": "insert_run"})


def test_missing_parameter_is_reported():
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a",
                              connection=FakeConnection())
    with pytest.raises(AdapterError, match="パラメータが不足"):
        adapter.invoke("query", {"name": "overdue_tasks", "params": {}})


def test_execute_commits():
    connection = FakeConnection(rows=[])
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a", connection=connection)
    adapter.invoke("execute", {"name": "insert_run",
                               "params": {"id": "r1", "template": "t"},
                               "idempotency_key": "k1"})
    assert connection.commits == 1


def test_consent_level_defaults_to_most_restrictive():
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a",
                              connection=FakeConnection(rows=[]))
    assert adapter.invoke("consent_level", {})["level"] == "A"


# --- qdrant ---------------------------------------------------------------

def build_qdrant() -> tuple[QdrantAdapter, FakeQdrantClient]:
    client = FakeQdrantClient()
    adapter = QdrantAdapter(tenant="company_a", embedder=HashEmbedder(), client=client)
    return adapter, client


def test_template_cannot_name_a_collection():
    adapter, _ = build_qdrant()
    with pytest.raises(AdapterError, match="scope"):
        adapter.invoke("search", {"text": "x", "scope": "tenant_company_b"})


def test_private_scope_resolves_to_configured_tenant():
    adapter, client = build_qdrant()
    adapter.invoke("upsert", {"documents": [{"text": "リスク事例"}]})
    assert client.upserts[0][0] == "tenant_company_a"


def test_public_write_is_refused():
    adapter, client = build_qdrant()
    with pytest.raises(AdapterError, match="人間承認"):
        adapter.invoke("upsert", {"documents": [{"text": "x"}], "scope": "public"})
    assert client.upserts == []


def test_submit_candidate_stays_private_and_pending():
    adapter, client = build_qdrant()
    result = adapter.invoke("submit_candidate", {
        "knowledge": {"text": "主要担当者への依存はスケジュールリスクになる",
                      "pattern": "key_person_dependency"},
        "publicability_score": 98,
    })

    collection, points = client.upserts[0]
    assert collection == "tenant_company_a"
    assert result["review_status"] == "pending"
    assert points[0].payload["review_status"] == "pending"
    assert points[0].payload["tenant"] == "company_a"


def test_private_scope_requires_tenant():
    adapter = QdrantAdapter(embedder=HashEmbedder(), client=FakeQdrantClient())
    with pytest.raises(AdapterError, match="tenant"):
        adapter.invoke("search", {"text": "x", "scope": "private"})


def test_upsert_id_is_stable_across_runs():
    """同じ冪等キーなら同じ point ID になる = 再実行で重複しない。"""
    ids = []
    for _ in range(2):
        adapter, client = build_qdrant()
        adapter.invoke("upsert", {"documents": [{"text": "同じ内容"}],
                                  "idempotency_key": "meeting:MTG-001"})
        ids.append(client.upserts[0][1][0].id)
    assert ids[0] == ids[1]


# --- engine integration ---------------------------------------------------

def test_template_using_both_adapters_runs():
    connection = FakeConnection(rows=[("B",)])
    adapters = AdapterRegistry()
    adapters.register(PostgresAdapter(queries=QUERIES, tenant="company_a",
                                      connection=connection))
    qdrant, client = build_qdrant()
    adapters.register(qdrant)

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    raw = {
        "name": "knowledge_capture",
        "steps": [
            {"id": "consent", "adapter": "postgres", "action": "consent_level"},
            {"id": "recall", "adapter": "qdrant", "action": "search",
             "inputs": {"text": "スケジュール遅延", "scope": "private", "limit": 3}},
            {"id": "store", "adapter": "qdrant", "action": "submit_candidate",
             "when": "{{ steps.consent.output.level }} != 'A'",
             "inputs": {"knowledge": {"text": "一般化されたパターン"},
                        "publicability_score": 90}},
        ],
    }
    ctx = Engine(adapters, llms).run(loader.load_dict(raw))

    assert ctx.results["consent"].output["level"] == "B"
    assert ctx.results["store"].status == "success"
    assert client.upserts[0][0] == "tenant_company_a"


def test_consent_level_a_blocks_knowledge_capture():
    """許諾レベル A（二次利用不可）なら候補提出そのものが走らない。"""
    connection = FakeConnection(rows=[])  # 該当なし → A
    adapters = AdapterRegistry()
    adapters.register(PostgresAdapter(queries=QUERIES, tenant="company_a",
                                      connection=connection))
    qdrant, client = build_qdrant()
    adapters.register(qdrant)

    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    raw = {
        "name": "knowledge_capture",
        "steps": [
            {"id": "consent", "adapter": "postgres", "action": "consent_level"},
            {"id": "store", "adapter": "qdrant", "action": "submit_candidate",
             "when": "{{ steps.consent.output.level }} != 'A'",
             "inputs": {"knowledge": {"text": "x"}}},
        ],
    }
    ctx = Engine(adapters, llms).run(loader.load_dict(raw))

    assert ctx.results["store"].status == "skipped"
    assert client.upserts == []


# --- 無料枠のマネージド DB 特有の挙動 / managed free-tier behaviour ---------

class ClosableConnection(FakeConnection):
    """一定回数で切断するダミー。アイドル停止からの復帰を再現する。

    Drops itself after a set number of uses, reproducing a service that was
    powered off while idle.
    """

    def __init__(self, rows=None, die_after: int = 0):
        super().__init__(rows)
        self.closed = 0
        self._die_after = die_after
        self.uses = 0

    def cursor(self):
        self.uses += 1
        if self._die_after and self.uses > self._die_after:
            self.closed = 1
            raise RuntimeError("server closed the connection unexpectedly")
        return FakeCursor(self.log, self.rows)


def test_reconnects_when_the_service_woke_up_cold(monkeypatch):
    """アイドル停止した DB に対し、一度切れても張り直して成功すること。"""
    dead = ClosableConnection(rows=[("B",)], die_after=0)
    fresh = ClosableConnection(rows=[("B",)])
    dead.closed = 1

    handed_out = [fresh]
    adapter = PostgresAdapter(dsn="postgresql://x", queries=QUERIES, tenant="company_a")
    adapter._connection = dead
    monkeypatch.setattr(adapter, "_connect", lambda: handed_out[0])

    result = adapter.invoke("query", {"name": "overdue_tasks",
                                      "params": {"as_of": "2026-08-27"}})
    assert result["rows"][0]["level"] == "B"


def test_connect_failure_reports_attempt_count(monkeypatch):
    """起床を待てずに諦めた場合、回数がわかるメッセージにする。"""
    import psycopg

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    adapter = PostgresAdapter(dsn="postgresql://x", queries=QUERIES,
                              connect_attempts=2, connect_backoff=0)

    with pytest.raises(AdapterError, match="2 attempts"):
        adapter.health_check() or adapter._connect()


def test_injected_connection_is_never_replaced():
    """テストで注入した接続を、勝手に張り替えないこと。"""
    connection = ClosableConnection(rows=[])
    connection.closed = 1
    adapter = PostgresAdapter(queries=QUERIES, tenant="company_a",
                              connection=connection)
    assert adapter._connect() is connection
