"""台帳の保存先（SQLite / PostgreSQL）のテスト / ledger storage tests.

目的は「更新が失われないこと」。別プロセスが同時に書いても、先に保存した
更新を、あとから保存した側が消さない。同じ内容を **SQLite と PostgreSQL の
両方**に対して実行する（`ledger` フィクスチャ）。PostgreSQL は環境変数
`AIPMO_TEST_PG_DSN` があるときだけ動き、無ければ飛ばす。

The point: no update is lost. Whoever saves last must not erase what another
process saved first. The same tests run against **both SQLite and
PostgreSQL** (the `ledger` fixture); PostgreSQL ones run only when
`AIPMO_TEST_PG_DSN` is set.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipmo.ledger_store import LedgerConfigError, PostgresStore
from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import MAX_OUTCOMES, Task, TaskEngine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


class Ledger:
    """あるバックエンドの「同じ台帳」を、何度でも開き直せるようにする。"""

    def __init__(self, kind: str, tmp_path: Path) -> None:
        self.kind = kind
        self.tmp = tmp_path
        self.path = tmp_path / "task-ledger.db"
        self.tenant = f"t{uuid.uuid4().hex[:12]}"

    def open(self, **kw) -> TaskEngine:
        kw.setdefault("now", lambda: NOW)
        if self.kind == "postgres":
            return TaskEngine(self.path, tenant=self.tenant,
                              store=PostgresStore(PG_DSN or "", self.tenant), **kw)
        return TaskEngine(self.path, **kw)

    # 子プロセスに渡す、台帳の指定 / how a child process is told which ledger
    def args(self) -> list[str]:
        return [self.kind, str(self.path), PG_DSN or "", self.tenant]

    def insert_odd_row(self) -> None:
        """形の合わない行を直接入れる（読み飛ばされること）。"""
        if self.kind == "postgres":
            import psycopg
            with psycopg.connect(PG_DSN or "", autocommit=True) as conn:
                conn.execute("INSERT INTO ledger_tasks VALUES (%s, 'JIRA:ODD', '{\"nope\": 1}')",
                             (self.tenant,))
        else:
            raw = sqlite3.connect(self.path)
            raw.execute("INSERT INTO tasks VALUES ('JIRA:ODD', '{\"nope\": 1}')")
            raw.execute("INSERT INTO tasks VALUES ('JIRA:BAD', '{broken')")
            raw.commit()
            raw.close()

    def write_lock_is_free(self) -> bool:
        """別の接続から、書き込み取引に入れる状態か。"""
        if self.kind == "postgres":
            import psycopg
            with psycopg.connect(PG_DSN or "", autocommit=True) as conn:
                got = conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (f"aipmo-ledger:{self.tenant}",)).fetchone()[0]
                if got:
                    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                                 (f"aipmo-ledger:{self.tenant}",))
                return bool(got)
        probe = sqlite3.connect(self.path, timeout=0.2, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            probe.close()

    def cleanup(self) -> None:
        if self.kind == "postgres":
            import psycopg
            with psycopg.connect(PG_DSN or "", autocommit=True) as conn:
                for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta"):
                    conn.execute(f"DELETE FROM {table} WHERE tenant = %s", (self.tenant,))


@pytest.fixture(params=["sqlite", "postgres"])
def ledger(request, tmp_path):
    if request.param == "postgres" and not PG_DSN:
        pytest.skip("AIPMO_TEST_PG_DSN が未設定 / not set")
    handle = Ledger(request.param, tmp_path)
    yield handle
    handle.cleanup()


# ===== 更新が失われない / no lost updates =====================================

def test_a_long_lived_copy_does_not_erase_another_processs_task(ledger):
    scheduler, web = ledger.open(), ledger.open()
    scheduler.ingest("t", "r1", [cand(key="P-1", title="a")])
    web.sync()
    web.ingest("t", "r2", [cand(key="P-2", title="b")])       # 別プロセスが追加
    scheduler.refresh()                                       # 古い写しのまま次の周
    assert set(ledger.open().tasks) == {"JIRA:P-1", "JIRA:P-2"}
    assert set(scheduler.tasks) == {"JIRA:P-1", "JIRA:P-2"}   # こちらにも見える


def test_changes_to_different_tasks_by_two_writers_both_survive(ledger):
    a, b = ledger.open(), ledger.open()
    a.ingest("t", "r", [cand(key="P-1", title="x"), cand(key="P-2", title="y")])
    b.sync()
    with a.transaction():
        a.tasks["JIRA:P-1"].assignee = "ann"
    with b.transaction():
        b.tasks["JIRA:P-2"].assignee = "bob"
    fresh = ledger.open()
    assert fresh.tasks["JIRA:P-1"].assignee == "ann"
    assert fresh.tasks["JIRA:P-2"].assignee == "bob"


def test_a_confirmed_assignment_survives_the_next_scheduler_cycle(ledger):
    scheduler = ledger.open()
    scheduler.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=scheduler, members=[Member("ann")])
    core.cycle()                                         # 提案が出る
    web = PmoCore(task_engine=ledger.open())
    web.accept_assignment("P-1")                         # 画面で確定
    core.cycle()                                         # 常駐側の次の周
    assert ledger.open().tasks["JIRA:P-1"].assignee == "ann"


def test_an_exception_inside_a_transaction_writes_nothing_and_restores_memory(ledger):
    te = ledger.open()
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    with pytest.raises(RuntimeError):
        with te.transaction():
            te.tasks["JIRA:P-1"].assignee = "ghost"
            te.tasks["JIRA:P-9"] = Task(id="JIRA:P-9", title="new")
            raise RuntimeError("boom")
    assert te.tasks["JIRA:P-1"].assignee is None and "JIRA:P-9" not in te.tasks
    assert ledger.open().tasks["JIRA:P-1"].assignee is None
    assert "JIRA:P-9" not in ledger.open().tasks


def test_nested_transactions_join_the_outer_one(ledger):
    te = ledger.open()
    with te.transaction():
        te.ingest("t", "r", [cand(key="P-1", title="x")])
        te.refresh()
    assert "JIRA:P-1" in ledger.open().tasks


# ===== 同時実行 / concurrency ================================================

def test_many_threads_on_separate_instances_lose_nothing(ledger):
    ledger.open()                                         # 表を先に作る
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            te = ledger.open()
            for i in range(15):
                te.ingest("t", f"r{n}-{i}", [cand(key=f"T{n}-{i}", title=f"t{n}-{i}")])
        except BaseException as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(ledger.open().tasks) == 6 * 15


def test_read_modify_write_on_the_same_row_loses_nothing(ledger):
    """同じ行を、読んで・変えて・書く取引が競合しても、全部の変更が残る。

    行ごとの差分書き込みだけでは守れない — 2人が同じ行を読み、それぞれの
    変更を書けば、片方が消える。取引の排他がそれを防ぐ。
    Per-row diffs alone do not protect this: two writers who read the same row
    and each write their change would erase one. Transaction exclusion does.
    """
    ledger.open().ingest("t", "r", [cand(key="P-1", title="shared")])
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            te = ledger.open()
            for i in range(10):
                with te.transaction():
                    task = te.tasks["JIRA:P-1"]
                    task.labels = sorted({*task.labels, f"w{n}-{i}"})
        except BaseException as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(ledger.open().tasks["JIRA:P-1"].labels) == 4 * 10


def test_real_processes_writing_at_once_lose_nothing(ledger):
    """本物の別プロセスで確かめる。スレッドでは再現できない競合がある。"""
    ledger.open()
    script = ledger.tmp / "writer.py"
    script.write_text(textwrap.dedent("""
        import sys
        from pathlib import Path
        from aipmo.ledger_store import PostgresStore
        from aipmo.task_engine import TaskEngine
        kind, path, dsn, tenant, name = sys.argv[1:6]
        store = PostgresStore(dsn, tenant) if kind == "postgres" else None
        te = TaskEngine(Path(path), tenant=tenant if store else None, store=store)
        base = {"key": None, "title": "t", "assignee": None, "due_date": None,
                "priority": None, "status": None, "blocked": False,
                "done": False, "labels": []}
        for i in range(20):
            te.ingest("t", f"{name}-{i}", [{**base, "key": f"{name}-{i}",
                                            "title": f"{name} {i}"}])
            te.refresh()
    """), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, str(script), *ledger.args(), f"P{n}"],
                              env=env, stderr=subprocess.PIPE) for n in range(4)]
    results = [(p.wait(timeout=180), p.stderr.read().decode()) for p in procs]
    assert [code for code, _ in results] == [0, 0, 0, 0], results
    assert len(ledger.open().tasks) == 4 * 20


# ===== そのほか / other ======================================================

def test_outcomes_are_trimmed_to_the_limit(ledger):
    te = ledger.open()
    te.add_outcomes([{"assignee": "a", "late_days": i, "labels": []}
                     for i in range(MAX_OUTCOMES + 10)])
    kept = ledger.open().outcomes
    assert len(kept) == MAX_OUTCOMES
    assert kept[-1]["late_days"] == MAX_OUTCOMES + 9          # 新しい方を残す


def test_an_unreadable_row_is_skipped_not_fatal(ledger):
    te = ledger.open()
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    ledger.insert_odd_row()
    assert list(ledger.open().tasks) == ["JIRA:P-1"]


def test_confirming_does_not_hold_the_write_lock_during_the_external_call(ledger):
    """Jira への書き込み中に、ほかのプロセスが台帳を書けること。"""
    te = ledger.open()
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    core.cycle()
    lock_was_free = []

    def slow_external_write(key: str, assignee: str) -> None:
        lock_was_free.append(ledger.write_lock_is_free())

    core.accept_assignment("P-1", write=slow_external_write)
    assert lock_was_free == [True]


def test_confirming_refuses_if_someone_else_assigned_meanwhile(ledger):
    te = ledger.open()
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    core.cycle()

    def someone_else_wins(key: str, assignee: str) -> None:
        other = ledger.open()
        with other.transaction():
            other.tasks["JIRA:P-1"].assignee = "bob"

    with pytest.raises(ValueError, match="bob"):
        core.accept_assignment("P-1", write=someone_else_wins)
    assert ledger.open().tasks["JIRA:P-1"].assignee == "bob"


def test_the_backend_is_reported(ledger):
    te = ledger.open()
    assert te.backend == ledger.kind and ledger.kind in te.describe()


# ===== SQLite 固有 / SQLite only =============================================

NOW_ISO = NOW.isoformat()


def legacy_json(path: Path, n_tasks: int = 3, n_outcomes: int = 2) -> None:
    tasks = [asdict(Task(id=f"JIRA:L-{i}", key=f"L-{i}", title=f"legacy {i}",
                         first_seen=NOW_ISO, last_seen=NOW_ISO))
             for i in range(n_tasks)]
    outcomes = [{"assignee": "ann", "late_days": i, "labels": []}
                for i in range(n_outcomes)]
    path.write_text(json.dumps({"tasks": tasks, "outcomes": outcomes}), encoding="utf-8")


def engine(path: Path) -> TaskEngine:
    return TaskEngine(path, now=lambda: NOW)


def test_a_legacy_json_ledger_is_imported_once_and_set_aside(tmp_path):
    legacy_json(tmp_path / "task-ledger.json")
    te = engine(tmp_path / "task-ledger.db")
    assert sorted(te.tasks) == ["JIRA:L-0", "JIRA:L-1", "JIRA:L-2"]
    assert len(te.outcomes) == 2
    assert not (tmp_path / "task-ledger.json").exists()
    assert (tmp_path / "task-ledger.json.migrated").exists()
    again = engine(tmp_path / "task-ledger.db")                # 二度目で重複しない
    assert len(again.tasks) == 3 and len(again.outcomes) == 2


def test_a_json_name_in_config_still_works(tmp_path):
    legacy_json(tmp_path / "task-ledger.json")
    te = engine(tmp_path / "task-ledger.json")                 # 古い設定の名前
    assert te.path == tmp_path / "task-ledger.db" and len(te.tasks) == 3
    assert TaskEngine.exists(tmp_path / "task-ledger.json")
    assert not TaskEngine.exists(tmp_path / "nothing.json")


def test_processes_starting_together_import_only_once(tmp_path):
    legacy_json(tmp_path / "task-ledger.json", n_tasks=5, n_outcomes=4)
    results: list[int] = []
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            results.append(len(engine(tmp_path / "task-ledger.db").outcomes))
        except BaseException as exc:                           # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == [4] * 5
    assert len(engine(tmp_path / "task-ledger.db").tasks) == 5


def test_starting_many_processes_on_a_new_ledger_never_fails_with_locked(tmp_path):
    """WAL への切り替えは busy タイムアウトが効かない。起動の競合で落ちないこと。"""
    errors: list[BaseException] = []

    def open_it() -> None:
        try:
            engine(tmp_path / "fresh.db")
        except BaseException as exc:                           # noqa: BLE001
            errors.append(exc)

    for round_ in range(8):
        path = tmp_path / f"fresh{round_}.db"
        threads = [threading.Thread(target=lambda p=path: engine(p)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors


def test_an_unreadable_legacy_file_does_not_stop_startup(tmp_path):
    (tmp_path / "task-ledger.json").write_text("{not json", encoding="utf-8")
    assert engine(tmp_path / "task-ledger.db").tasks == {}
    assert (tmp_path / "task-ledger.json.migrated").exists()      # 原本は残す


# ===== PostgreSQL 固有 / PostgreSQL only =====================================

pg_only = pytest.mark.skipif(not PG_DSN, reason="AIPMO_TEST_PG_DSN が未設定")


def pg_engine(tenant: str, tmp_path: Path) -> TaskEngine:
    return TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant=tenant,
                      store=PostgresStore(PG_DSN or "", tenant))


@pytest.fixture
def pg_tenants(tmp_path):
    names = [f"t{uuid.uuid4().hex[:12]}" for _ in range(2)]
    yield names
    import psycopg
    with psycopg.connect(PG_DSN or "", autocommit=True) as conn:
        for table in ("ledger_tasks", "ledger_outcomes", "ledger_meta"):
            conn.execute(f"DELETE FROM {table} WHERE tenant = ANY(%s)", (names,))


@pg_only
def test_tenants_share_a_database_but_never_each_others_rows(pg_tenants, tmp_path):
    acme, globex = (pg_engine(t, tmp_path) for t in pg_tenants)
    acme.ingest("t", "r", [cand(key="A-1", title="acme secret")])
    globex.ingest("t", "r", [cand(key="G-1", title="globex")])
    assert list(pg_engine(pg_tenants[0], tmp_path).tasks) == ["JIRA:A-1"]
    assert list(pg_engine(pg_tenants[1], tmp_path).tasks) == ["JIRA:G-1"]
    acme.refresh()
    assert list(pg_engine(pg_tenants[1], tmp_path).tasks) == ["JIRA:G-1"]


@pg_only
def test_trimming_outcomes_never_evicts_another_tenants(pg_tenants, tmp_path):
    acme, globex = (pg_engine(t, tmp_path) for t in pg_tenants)
    globex.add_outcomes([{"assignee": "g", "late_days": i, "labels": []} for i in range(5)])
    acme.add_outcomes([{"assignee": "a", "late_days": i, "labels": []}
                       for i in range(MAX_OUTCOMES + 20)])
    assert len(pg_engine(pg_tenants[0], tmp_path).outcomes) == MAX_OUTCOMES
    assert len(pg_engine(pg_tenants[1], tmp_path).outcomes) == 5


@pg_only
def test_one_tenants_open_transaction_does_not_block_another(pg_tenants, tmp_path):
    acme, globex = (pg_engine(t, tmp_path) for t in pg_tenants)
    inside, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold() -> None:
        with acme.transaction():
            inside.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert inside.wait(10)
    worker = threading.Thread(target=lambda: (
        globex.ingest("t", "r", [cand(key="G-1", title="g")]), done.set()))
    worker.start()
    assert done.wait(5), "別テナントの書き込みが待たされた / another tenant was blocked"
    release.set()
    holder.join()
    worker.join()


@pg_only
def test_a_writer_waits_for_the_same_tenants_open_transaction(pg_tenants, tmp_path):
    first, second = pg_engine(pg_tenants[0], tmp_path), pg_engine(pg_tenants[0], tmp_path)
    inside, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold() -> None:
        with first.transaction():
            inside.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert inside.wait(10)
    worker = threading.Thread(target=lambda: (
        second.ingest("t", "r", [cand(key="A-1", title="a")]), done.set()))
    worker.start()
    assert not done.wait(1), "同じテナントの書き込みが排他されていない / not serialised"
    release.set()
    holder.join()
    worker.join()
    assert done.is_set()


@pg_only
def test_it_reconnects_after_the_server_drops_the_connection(pg_tenants, tmp_path):
    import psycopg
    te = pg_engine(pg_tenants[0], tmp_path)
    te.ingest("t", "r", [cand(key="A-1", title="a")])
    with psycopg.connect(PG_DSN or "", autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() AND datname = current_database()")
    te.ingest("t", "r2", [cand(key="A-2", title="b")])            # 切れていても続けられる
    assert set(pg_engine(pg_tenants[0], tmp_path).tasks) == {"JIRA:A-1", "JIRA:A-2"}


def test_postgres_needs_a_tenant_and_a_dsn():
    with pytest.raises(LedgerConfigError, match="tenant"):
        PostgresStore("postgresql://x", None)
    with pytest.raises(LedgerConfigError, match="DSN"):
        PostgresStore("", "acme")


def test_an_unreachable_server_is_a_clear_error(tmp_path):
    pytest.importorskip("psycopg")
    with pytest.raises(LedgerConfigError, match="connect"):
        TaskEngine(tmp_path / "task-ledger.db", tenant="acme", store=PostgresStore(
            "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=2", "acme"))
