"""PostgreSQL の台帳を SQLite へ戻す（`aipmo ledger migrate-to-sqlite`）のテスト。

確かめること:
  (1) タスク・完了実績・隣に置くもの（ブリーフィング・判断ログ・状態）が、そのまま写る。写したあと
      読み直して一致を確かめる。移行元は変わらない
  (2) 持ち主（tenant）の刻印を守る。別テナントの SQLite へは書かない
  (3) 既存の行を壊さない：行があれば止まり、--force でも実績を二重にしない。隣のものも上書きしない
  (4) 移行元が PostgreSQL でないとき、一致しないときは、はっきり止まる
  (5) 往復（SQLite → PostgreSQL → SQLite）で元に戻る（PostgreSQL があるとき）

PostgreSQL が無い環境でも、PostgreSQL のふりをした SQLite（kind=postgres）で同じ道筋を通す。
実 PostgreSQL が要るものは `AIPMO_TEST_PG_DSN` があるときだけ動く。

What matters: tasks, outcomes and the files beside the ledger copy over as they are and the copy is
re-read and compared; the source is untouched; the tenant stamp holds; existing rows are never
silently damaged (stop unless --force, and outcomes never double); a non-PostgreSQL source or a
mismatch stops loudly; a round trip returns the original. A SQLite pretending to be PostgreSQL covers
the same path without one; real-PostgreSQL tests run when AIPMO_TEST_PG_DSN is set.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.ledger_store import LedgerTenantError, SqliteStore
from aipmo.side_store import BRIEFING, CONTROL, DECISIONS, LEARNED, STATE, FileSide
from aipmo.task_engine import MAX_OUTCOMES, TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None, "priority": None,
            "status": None, "blocked": False, "done": False, "labels": []}
    return {**base, **kw}


class PretendPostgres(SqliteStore):
    """PostgreSQL のふりをする SQLite。台帳の保存先の種類（kind）だけが postgres。"""
    kind = "postgres"

    def describe(self) -> str:
        return f"postgres-like:{self.path}"


def seed(engine: TaskEngine) -> None:
    engine.ingest("t", "r", [
        cand(key="P-1", title="ログイン画面が遅い", assignee="ann", due_date="2026-09-01",
             priority="High", project="P", labels=["bug"]),
        cand(key="P-2", title="日本語のタスク ✓", project="P")])
    with engine.transaction():
        engine.tasks["JIRA:P-1"].dispatches.append({"id": "d1", "agent": "dev-ai", "status": "done"})
        engine.tasks["JIRA:P-1"].payload = {"filing": {"state": "filed", "key": "GH:1"}}
    engine.add_outcomes([{"task": "JIRA:X-1", "assignee": "ann", "labels": [], "priority": "High",
                          "due_date": None, "first_seen": NOW.isoformat(), "done_at": NOW.isoformat(),
                          "late_days": 2, "effort": 3, "duration_days": 4},
                         {"task": "JIRA:X-2", "assignee": "bob", "labels": ["bug"], "priority": None,
                          "due_date": None, "first_seen": NOW.isoformat(), "done_at": NOW.isoformat(),
                          "late_days": None, "effort": None, "duration_days": None}])


def seed_side(side) -> None:
    side.write_doc(BRIEFING, json.dumps({"generated_at": NOW.isoformat(), "msg": "日本語"}))
    side.write_doc(STATE, json.dumps({"open_alerts": {"k": {"raised_at": NOW.isoformat()}}}))
    side.write_doc(LEARNED, json.dumps({"samples": 7}))
    side.write_doc(CONTROL, json.dumps({"paused": True}))
    for i in range(3):
        side.append(DECISIONS, json.dumps({"kind": "n", "i": i}))


def canonical_tasks(engine: TaskEngine) -> dict[str, dict]:
    engine.sync()
    return {k: json.loads(json.dumps(v.__dict__, default=str, sort_keys=True))
            for k, v in engine.tasks.items()}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """移行元（PostgreSQL のふり）と、設定。"""
    source_path = tmp_path / "src" / "task-ledger.db"
    source = TaskEngine(source_path, now=lambda: NOW, tenant="acme",
                        store=PretendPostgres(source_path), side_storage="database")
    seed(source)
    seed_side(source.side)
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\ntask_engine:\n  backend: postgres\n  dsn: x\n", encoding="utf-8")

    real_open = cli.open_ledger

    def fake_open(cfg, base, **kw):
        # 設定の台帳（PostgreSQL のふり）を開く。
        return TaskEngine(source_path, now=lambda: NOW, tenant="acme",
                          store=PretendPostgres(source_path), side_storage="database", **kw)

    monkeypatch.setattr(cli, "open_ledger", fake_open)
    monkeypatch.setattr(cli, "ledger_path", lambda cfg, base: tmp_path / "dest" / "task-ledger.db")
    return source, tmp_path, config, real_open


def run(config: Path, *args: str) -> int:
    return cli.main(["--config", str(config), "ledger", "migrate-to-sqlite", *args])


# ===== (1) そのまま写る / copied as they are ==============================================================

def test_tasks_outcomes_and_the_files_beside_the_ledger_copy_over(world, capsys):
    source, tmp_path, config, _ = world
    before_tasks = canonical_tasks(source)
    before_outcomes = list(source.outcomes)
    assert run(config) == 0
    out = capsys.readouterr().out
    assert "migrated: 2 tasks, 2 outcomes" in out and "読み直して一致を確認" in out

    dest_path = tmp_path / "dest" / "task-ledger.db"
    dest = TaskEngine(dest_path, now=lambda: NOW, tenant="acme")
    assert canonical_tasks(dest) == before_tasks                  # 中身まで同じ
    assert dest.outcomes == before_outcomes
    assert dest.tasks["JIRA:P-1"].payload["filing"]["key"] == "GH:1"
    assert dest.tasks["JIRA:P-2"].title == "日本語のタスク ✓"

    # 隣に置くもの: 既定は移行先の隣のファイル
    beside = FileSide(dest_path)
    assert json.loads(beside.read_doc(BRIEFING))["msg"] == "日本語"
    assert json.loads(beside.read_doc(CONTROL))["paused"] is True
    assert [json.loads(x)["i"] for x in beside.tail(DECISIONS, 0)] == [0, 1, 2]    # 順序も同じ
    assert (tmp_path / "dest" / "pmo-briefing.json").exists()

    # 移行元は変わらない
    assert canonical_tasks(source) == before_tasks and source.outcomes == before_outcomes
    assert source.side.tail(DECISIONS, 0) and source.side.read_doc(BRIEFING)


def test_side_database_puts_the_beside_data_into_sqlite_tables_without_files(world, capsys):
    source, tmp_path, config, _ = world
    assert run(config, "--side", "database") == 0
    dest_path = tmp_path / "dest" / "task-ledger.db"
    dest = TaskEngine(dest_path, tenant="acme", side_storage="database")
    assert json.loads(dest.side.read_doc(BRIEFING))["msg"] == "日本語"
    assert len(dest.side.tail(DECISIONS, 0)) == 3
    assert sorted(p.name for p in dest_path.parent.iterdir() if not p.name.startswith("task-ledger.db")) == []


def test_the_copy_works_as_a_sqlite_ledger_afterwards(world, tmp_path, monkeypatch, capsys):
    source, tmp_path, config, real_open = world
    assert run(config) == 0
    capsys.readouterr()
    sqlite_config = tmp_path / "dest" / "config.yaml"
    sqlite_config.write_text("tenant: acme\ntask_engine: {}\npmo_core:\n  members: [ann]\n",
                             encoding="utf-8")
    monkeypatch.setattr(cli, "open_ledger", real_open)
    monkeypatch.undo()                                          # 実物に戻す
    assert cli.main(["--config", str(sqlite_config), "tasks"]) == 0
    assert "ログイン画面が遅い" in capsys.readouterr().out
    assert cli.main(["--config", str(sqlite_config), "ledger", "info"]) == 0
    info = capsys.readouterr().out
    assert "sqlite" in info and "2" in info


def test_an_empty_source_copies_nothing_and_succeeds(tmp_path, monkeypatch, capsys):
    path = tmp_path / "src" / "task-ledger.db"
    monkeypatch.setattr(cli, "open_ledger", lambda cfg, base, **kw: TaskEngine(
        path, tenant="acme", store=PretendPostgres(path), side_storage="database", **kw))
    monkeypatch.setattr(cli, "ledger_path", lambda cfg, base: tmp_path / "dest" / "task-ledger.db")
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    assert run(config) == 0
    assert "0 tasks, 0 outcomes" in capsys.readouterr().out


# ===== (2) 持ち主の刻印 / the tenant stamp ==================================================================

def test_the_destination_is_stamped_with_the_tenant_and_another_tenants_file_is_refused(world, capsys):
    source, tmp_path, config, _ = world
    dest_path = tmp_path / "dest" / "task-ledger.db"
    assert run(config) == 0
    with pytest.raises(LedgerTenantError):
        TaskEngine(dest_path, tenant="other")                  # 刻印で守られている
    capsys.readouterr()

    foreign = tmp_path / "foreign.db"
    TaskEngine(foreign, tenant="someone-else").close()
    assert run(config, "--to", str(foreign)) == 1
    assert "someone-else" in capsys.readouterr().err                 # 持ち主が違うと理由を言う
    assert TaskEngine(foreign, tenant="someone-else").tasks == {}   # 何も書いていない


# ===== (3) 既存の行を壊さない / existing rows ==========================================================

def test_a_non_empty_destination_stops_unless_forced_and_outcomes_never_double(world, capsys):
    source, tmp_path, config, _ = world
    dest_path = tmp_path / "dest" / "task-ledger.db"
    assert run(config) == 0
    capsys.readouterr()

    dest = TaskEngine(dest_path, tenant="acme")
    with dest.transaction():
        dest.tasks["JIRA:P-1"].title = "移行先で手を入れた"
    dest.close()

    assert run(config) == 1                                      # 行があるので止まる
    assert "--force" in capsys.readouterr().err
    assert TaskEngine(dest_path, tenant="acme").tasks["JIRA:P-1"].title == "移行先で手を入れた"

    assert run(config, "--force") == 0
    again = TaskEngine(dest_path, tenant="acme")
    assert again.tasks["JIRA:P-1"].title == "ログイン画面が遅い"       # --force は同じ id を上書き
    assert len(again.outcomes) == 2                              # 実績は二重にならない
    assert "0 outcomes" in capsys.readouterr().out


def test_force_keeps_rows_that_exist_only_in_the_destination_and_never_overwrites_side_data_silently(
        world, capsys):
    source, tmp_path, config, _ = world
    dest_path = tmp_path / "dest" / "task-ledger.db"
    assert run(config) == 0
    dest = TaskEngine(dest_path, tenant="acme")
    dest.ingest("t", "r", [cand(key="Z-9", title="移行先にだけある")])
    FileSide(dest_path).write_doc(STATE, '{"newer": true}')
    capsys.readouterr()
    assert run(config, "--force") == 0
    out = capsys.readouterr().out
    assert "JIRA:Z-9" in TaskEngine(dest_path, tenant="acme").tasks         # 消さない
    assert json.loads(FileSide(dest_path).read_doc(STATE)) == {"open_alerts": {
        "k": {"raised_at": NOW.isoformat()}}}                                # force は文書を上書きする
    assert len(FileSide(dest_path).tail(DECISIONS, 0)) == 3                  # ログは足さない
    assert "pmo-core-state.json" in out and "写しました" in out


def test_without_force_existing_side_files_are_left_alone(world, capsys):
    source, tmp_path, config, _ = world
    dest_path = tmp_path / "dest" / "task-ledger.db"
    FileSide(dest_path).write_doc(STATE, '{"mine": 1}')
    assert run(config) == 0
    assert FileSide(dest_path).read_doc(STATE) == '{"mine": 1}'
    assert "移行先にあるので残しました" in capsys.readouterr().out


# ===== (4) 止まるとき / stopping loudly ==========================================================================

def test_a_source_that_is_not_postgres_is_refused(tmp_path, monkeypatch, capsys):
    path = tmp_path / "task-ledger.db"
    monkeypatch.setattr(cli, "open_ledger", lambda cfg, base, **kw: TaskEngine(path, **kw))
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    assert run(config) == 1
    assert "PostgreSQL ではありません" in capsys.readouterr().err
    assert not (tmp_path / "dest").exists()


def test_a_copy_that_does_not_match_the_source_is_reported_not_called_a_success(world, monkeypatch, capsys):
    source, tmp_path, config, _ = world
    real = cli._copy_ledger_rows

    def lossy(snapshot, destination):
        snapshot.tasks.pop(next(iter(snapshot.tasks)))               # 1 件落とす
        return real(snapshot, destination)

    # 落とした分は、読み直しの比較で「移行先に無い」として見つかる（比較は元の写しで行うため）
    original = cli._verify_copy
    full = {}

    def remember(snapshot, destination):
        return original(full["snapshot"], destination)

    def lossy2(snapshot, destination):
        full["snapshot"] = type(snapshot)(tasks=dict(snapshot.tasks), outcomes=list(snapshot.outcomes))
        return lossy(snapshot, destination)

    monkeypatch.setattr(cli, "_copy_ledger_rows", lossy2)
    monkeypatch.setattr(cli, "_verify_copy", remember)
    assert run(config) == 1
    err = capsys.readouterr().err
    assert "一致しません" in err and "移行先にありません" in err


def test_force_rerun_does_not_duplicate_outcomes_even_when_the_formatting_differs(tmp_path):
    """PostgreSQL の jsonb は整形し直すので、文字列の比較では二重になる。正規化して比べる。"""
    from aipmo.ledger_store import Snapshot

    dest = SqliteStore(tmp_path / "d.db")
    dest.prepare(None, None)
    compact = '{"task":"X-1","done_at":"2026-09-30"}'
    spaced = '{"task": "X-1", "done_at": "2026-09-30"}'
    assert cli._copy_ledger_rows(Snapshot(tasks={}, outcomes=[compact]), dest) == 1
    assert cli._copy_ledger_rows(Snapshot(tasks={}, outcomes=[spaced]), dest) == 0    # 同じ実績
    assert len(dest.read().outcomes) == 1
    dest.close()


# ===== (5) 往復 / a round trip with a real PostgreSQL ====================================================

needs_pg = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")


@needs_pg
def test_sqlite_to_postgres_and_back_returns_the_original(tmp_path, capsys):
    tenant = f"t{uuid.uuid4().hex[:12]}"
    original_path = tmp_path / "orig" / "task-ledger.db"
    original = TaskEngine(original_path, now=lambda: NOW, tenant=tenant)
    seed(original)
    FileSide(original_path)  # 元のファイルの隣は使わない
    before = canonical_tasks(original)
    outcomes = list(original.outcomes)
    original.close()

    pg_config = tmp_path / "pg" / "config.yaml"
    pg_config.parent.mkdir()
    pg_config.write_text(f"tenant: {tenant}\ntask_engine:\n  backend: postgres\n  dsn: \"{PG_DSN}\"\n",
                         encoding="utf-8")
    assert cli.main(["--config", str(pg_config), "ledger", "migrate", "--from-sqlite",
                     str(original_path)]) == 0
    seed_engine = cli.open_ledger(cli.load_config(pg_config), pg_config.parent)
    seed_side(seed_engine.side)                                   # PostgreSQL 側の隣に置くもの
    seed_engine.close()
    capsys.readouterr()

    back = tmp_path / "back" / "task-ledger.db"
    assert cli.main(["--config", str(pg_config), "ledger", "migrate-to-sqlite", "--to", str(back)]) == 0
    out = capsys.readouterr().out
    assert "verified" in out
    returned = TaskEngine(back, now=lambda: NOW, tenant=tenant)
    assert canonical_tasks(returned) == before
    assert [json.loads(o) if isinstance(o, str) else o for o in returned.outcomes] == [
        json.loads(o) if isinstance(o, str) else o for o in outcomes]
    assert json.loads(FileSide(back).read_doc(BRIEFING))["msg"] == "日本語"
    assert len(FileSide(back).tail(DECISIONS, 0)) == 3

    # 後始末
    import psycopg
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta", "ledger_side_docs",
                      "ledger_side_log"):
            conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (tenant,))


@needs_pg
def test_forward_migrate_run_twice_with_force_does_not_double_the_outcomes(tmp_path, capsys):
    tenant = f"t{uuid.uuid4().hex[:12]}"
    source = TaskEngine(tmp_path / "s" / "task-ledger.db", now=lambda: NOW, tenant=tenant)
    seed(source)
    source.close()
    config = tmp_path / "config.yaml"
    config.write_text(f"tenant: {tenant}\ntask_engine:\n  backend: postgres\n  dsn: \"{PG_DSN}\"\n",
                      encoding="utf-8")
    args = ["--config", str(config), "ledger", "migrate", "--from-sqlite",
            str(tmp_path / "s" / "task-ledger.db")]
    assert cli.main(args) == 0
    assert cli.main([*args, "--force"]) == 0
    engine = cli.open_ledger(cli.load_config(config), tmp_path)
    assert len(engine.outcomes) == 2 and len(engine.tasks) == 2
    engine.close()
    import psycopg
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta", "ledger_side_docs",
                      "ledger_side_log"):
            conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (tenant,))
    assert MAX_OUTCOMES >= 2


def test_the_verification_notices_a_changed_task_and_a_missing_outcome(tmp_path):
    from aipmo.ledger_store import Snapshot

    dest = SqliteStore(tmp_path / "d.db")
    dest.prepare(None, None)
    snapshot = Snapshot(tasks={"A": '{"id": "A", "title": "元"}', "B": '{"id": "B"}'},
                        outcomes=['{"task": "X-1"}', '{"task": "X-2"}'])
    with dest.write() as tx:
        tx.apply({"A": '{"id": "A", "title": "違う"}', "B": '{ "id":"B" }'}, [],
                 ['{"task": "X-1"}'], 500)
    problems = cli._verify_copy(snapshot, dest)
    assert any("A" in p and "中身が違います" in p for p in problems)           # 中身の違い
    assert not any("タスク B" in p for p in problems)                          # 書式の違いだけなら一致
    assert any("完了実績が 1 件" in p for p in problems)                        # 実績の欠け
    dest.close()
