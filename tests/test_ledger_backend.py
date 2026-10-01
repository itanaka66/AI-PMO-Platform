"""台帳の保存先の切り替え（設定・CLI・Web）のテスト / backend switching tests.

`task_engine.backend: postgres` で、CLI も Web も同じ PostgreSQL の台帳を使う
こと、設定の誤りは起動時にはっきり分かること、SQLite からの移行が安全なこと。
PostgreSQL が要るものは `AIPMO_TEST_PG_DSN` があるときだけ動く。

With `task_engine.backend: postgres`, the CLI and the web share one PostgreSQL
ledger; misconfiguration is clear at startup; moving from SQLite is safe.
Tests needing PostgreSQL run only when `AIPMO_TEST_PG_DSN` is set.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.ledger_store import PostgresStore
from aipmo.task_engine import TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


@pytest.fixture
def tenant():
    name = f"t{uuid.uuid4().hex[:12]}"
    yield name
    if PG_DSN:
        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta"):
                conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (name,))


def write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(body, encoding="utf-8")
    return config


def pg_config(tmp_path: Path, tenant: str, extra: str = "") -> Path:
    return write_config(tmp_path, f"tenant: {tenant}\n"
                                  f"task_engine:\n  backend: postgres\n  dsn: \"{PG_DSN}\"\n{extra}")


# ===== 設定の誤り / misconfiguration =========================================

def test_the_default_backend_is_sqlite():
    assert cli.ledger_store_factory({}) is None
    assert cli.ledger_store_factory({"task_engine": {"backend": "sqlite"}}) is None
    assert cli.ledger_store_factory({"task_engine": {}}) is None


@pytest.mark.parametrize("body,needle", [
    ("task_engine:\n  backend: mysql\n", "mysql"),
    ("task_engine:\n  backend: postgres\n  dsn: postgresql://x/y\n", "tenant"),
    ("tenant: acme\ntask_engine:\n  backend: postgres\n", "DSN"),
])
def test_bad_backend_config_is_a_clear_error_before_anything_is_touched(
        tmp_path, capsys, body, needle):
    config = write_config(tmp_path, body)
    for command in (["tasks"], ["pmo"], ["ledger", "info"]):
        assert cli.main(["--config", str(config), *command]) == 1
        assert needle in capsys.readouterr().err
    assert not list(tmp_path.glob("*.db"))             # SQLite の台帳も作らない


def test_an_unreachable_postgres_is_a_clear_error(tmp_path, capsys):
    config = write_config(
        tmp_path, "tenant: acme\ntask_engine:\n  backend: postgres\n"
                  "  dsn: \"postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2\"\n")
    assert cli.main(["--config", str(config), "tasks"]) == 1
    assert "connect" in capsys.readouterr().err


@needs_pg
def test_the_dsn_falls_back_to_the_postgres_adapter_setting(tmp_path, tenant, capsys):
    config = write_config(
        tmp_path, f"tenant: {tenant}\nadapters:\n  mode: mock\n  postgres:\n"
                  f"    dsn: \"{PG_DSN}\"\ntask_engine:\n  backend: postgres\n")
    assert cli.main(["--config", str(config), "ledger", "info"]) == 0
    assert "postgres" in capsys.readouterr().out


# ===== CLI ====================================================================

@needs_pg
def test_the_cli_reads_and_writes_the_postgres_ledger(tmp_path, tenant, capsys):
    config = pg_config(tmp_path, tenant, "pmo_core:\n  members: [ann]\n")
    writer = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant=tenant,
                        store=PostgresStore(PG_DSN or "", tenant))
    writer.ingest("t", "r", [cand(key="A-1", title="postgres で見える", due_date="2026-09-01")])

    assert cli.main(["--config", str(config), "tasks", "--why"]) == 0
    assert "postgres で見える" in capsys.readouterr().out
    assert cli.main(["--config", str(config), "assign", "A-1", "--apply"]) == 0
    capsys.readouterr()
    assert writer.find("A-1").assignee == "ann"          # 別の接続から見える
    assert not list(tmp_path.glob("*.db"))               # SQLite のファイルは作られない


@needs_pg
def test_ledger_info_describes_the_store(tmp_path, tenant, capsys):
    config = pg_config(tmp_path, tenant)
    TaskEngine(tmp_path / "x.db", now=lambda: NOW, tenant=tenant,
               store=PostgresStore(PG_DSN or "", tenant)).ingest(
        "t", "r", [cand(key="A-1", title="a"), cand(key="B-1", title="b", done=True)])
    assert cli.main(["--config", str(config), "ledger", "info"]) == 0
    out = capsys.readouterr().out
    assert "postgres" in out and tenant in out and "2" in out and "A, B" not in out


def test_ledger_info_on_sqlite(tmp_path, capsys):
    config = write_config(tmp_path, "tenant: acme\n")
    TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant="acme").ingest(
        "t", "r", [cand(key="A-1", title="a")])
    assert cli.main(["--config", str(config), "ledger", "info"]) == 0
    out = capsys.readouterr().out
    assert "sqlite" in out and "acme" in out and "A" in out


# ===== 移行 / migration ========================================================

@needs_pg
def test_migrate_copies_a_sqlite_ledger_to_postgres_and_leaves_the_source(
        tmp_path, tenant, capsys):
    source = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant=tenant)
    source.ingest("t", "r", [cand(key="A-1", title="one", assignee="ann"),
                             cand(key="A-2", title="two")])
    source.add_outcomes([{"assignee": "ann", "late_days": 1, "labels": []}])
    source.close()

    config = pg_config(tmp_path, tenant)
    assert cli.main(["--config", str(config), "ledger", "migrate"]) == 0
    assert "2 tasks" in capsys.readouterr().out

    moved = TaskEngine(tmp_path / "other.db", now=lambda: NOW, tenant=tenant,
                       store=PostgresStore(PG_DSN or "", tenant))
    assert set(moved.tasks) == {"JIRA:A-1", "JIRA:A-2"}
    assert moved.tasks["JIRA:A-1"].assignee == "ann" and len(moved.outcomes) == 1
    assert (tmp_path / "task-ledger.db").exists()          # 移行元は消さない


@needs_pg
def test_migrate_refuses_a_non_empty_target_unless_forced(tmp_path, tenant, capsys):
    source = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant=tenant)
    source.ingest("t", "r", [cand(key="A-1", title="from sqlite")])
    source.close()
    target = TaskEngine(tmp_path / "other.db", now=lambda: NOW, tenant=tenant,
                        store=PostgresStore(PG_DSN or "", tenant))
    target.ingest("t", "r", [cand(key="A-1", title="already there"),
                             cand(key="Z-9", title="keep me")])
    config = pg_config(tmp_path, tenant)

    assert cli.main(["--config", str(config), "ledger", "migrate"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.find("A-1").title == "already there"       # 何も変わっていない

    assert cli.main(["--config", str(config), "ledger", "migrate", "--force"]) == 0
    capsys.readouterr()
    target.sync()
    assert target.tasks["JIRA:A-1"].title == "from sqlite"   # 同じ id は上書き
    assert "JIRA:Z-9" in target.tasks                        # ほかの行は消さない


def test_migrate_needs_a_postgres_target_and_an_existing_source(tmp_path, capsys):
    config = write_config(tmp_path, "tenant: acme\n")
    assert cli.main(["--config", str(config), "ledger", "migrate"]) == 1
    assert "postgres" in capsys.readouterr().err


@needs_pg
def test_migrate_refuses_another_tenants_sqlite_file(tmp_path, tenant, capsys):
    TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant="someone-else").close()
    config = pg_config(tmp_path, tenant)
    assert cli.main(["--config", str(config), "ledger", "migrate"]) == 1
    assert "someone-else" in capsys.readouterr().err
    assert TaskEngine(tmp_path / "o.db", now=lambda: NOW, tenant=tenant,
                      store=PostgresStore(PG_DSN or "", tenant)).tasks == {}


@needs_pg
def test_migrate_reports_a_missing_source(tmp_path, tenant, capsys):
    config = pg_config(tmp_path, tenant)
    assert cli.main(["--config", str(config), "ledger", "migrate",
                     "--from-sqlite", str(tmp_path / "nope.db")]) == 1
    assert "nope.db" in capsys.readouterr().err


# ===== Web ======================================================================

@needs_pg
def test_the_web_screen_reads_the_postgres_ledger(tmp_path, tenant):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aipmo.adapters.base import AdapterRegistry
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry
    from aipmo.web.server import RunStore, create_app

    writer = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant=tenant,
                        store=PostgresStore(PG_DSN or "", tenant))
    writer.ingest("t", "r", [cand(key="A-1", title="PG の課題", due_date="2026-09-01"),
                             cand(key="A-2", title="open")])
    config = {"tenant": tenant, "task_engine": {"backend": "postgres", "dsn": PG_DSN}}
    factory = cli.ledger_store_factory(config)

    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", "op-token-123",
                     viewer_token="view-token-456", lang="en", store=RunStore(),
                     tenant=tenant, pmo_ledger=tmp_path / "task-ledger.db",
                     ledger_store_factory=factory)
    client = TestClient(app)
    operator, viewer = {"x-aipmo-token": "op-token-123"}, {"x-aipmo-token": "view-token-456"}

    body = client.get("/api/pmo", headers=viewer).json()
    assert {t["key"] for t in body["tasks"]} == {"A-1", "A-2"}
    assert not list(tmp_path.glob("*.db"))               # SQLite のファイルは作られない

    # 別の接続（別ホストのつもり）から確定した担当が、画面の応答に出る
    done = client.post("/api/pmo/assignments/accept", json={"ref": "A-2"}, headers=operator)
    assert done.status_code == 409                       # 提案がまだ無い（周が回っていない）
    with writer.transaction():
        writer.tasks["JIRA:A-2"].suggested_assignee = "ann"
    assert client.post("/api/pmo/assignments/accept", json={"ref": "A-2"},
                       headers=operator).status_code == 200
    writer.sync()
    assert writer.tasks["JIRA:A-2"].assignee == "ann"


def test_the_web_reports_an_unreachable_ledger_as_503(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aipmo.adapters.base import AdapterRegistry
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry
    from aipmo.web.server import RunStore, create_app

    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(
        Engine(AdapterRegistry(), llms), tmp_path / "t", "op-token-123", lang="en",
        store=RunStore(), tenant="acme", pmo_ledger=tmp_path / "task-ledger.db",
        ledger_store_factory=lambda: PostgresStore(
            "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2", "acme"))
    response = TestClient(app).get("/api/pmo", headers={"x-aipmo-token": "op-token-123"})
    assert response.status_code == 503 and "connect" in response.json()["detail"]
