"""台帳の SQLite 化のテスト / ledger storage tests.

目的は「更新が失われないこと」。別プロセスが同時に書いても、
先に保存した更新を、あとから保存した側が消さない。
The point: no update is lost. Whoever saves last must not erase what another
process saved first.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import MAX_OUTCOMES, Task, TaskEngine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


def engine(path: Path) -> TaskEngine:
    return TaskEngine(path, now=lambda: NOW)


# ===== 更新が失われない / no lost updates =====================================

def test_a_long_lived_copy_does_not_erase_another_processs_task(tmp_path):
    scheduler, web = engine(tmp_path / "l.db"), engine(tmp_path / "l.db")
    scheduler.ingest("t", "r1", [cand(key="P-1", title="a")])
    web.sync()
    web.ingest("t", "r2", [cand(key="P-2", title="b")])       # 別プロセスが追加
    scheduler.refresh()                                       # 古い写しのまま次の周
    assert set(engine(tmp_path / "l.db").tasks) == {"JIRA:P-1", "JIRA:P-2"}
    assert set(scheduler.tasks) == {"JIRA:P-1", "JIRA:P-2"}   # こちらにも見える


def test_changes_to_different_tasks_by_two_writers_both_survive(tmp_path):
    a, b = engine(tmp_path / "l.db"), engine(tmp_path / "l.db")
    a.ingest("t", "r", [cand(key="P-1", title="x"), cand(key="P-2", title="y")])
    b.sync()
    with a.transaction():
        a.tasks["JIRA:P-1"].assignee = "ann"
    with b.transaction():
        b.tasks["JIRA:P-2"].assignee = "bob"
    fresh = engine(tmp_path / "l.db")
    assert fresh.tasks["JIRA:P-1"].assignee == "ann"
    assert fresh.tasks["JIRA:P-2"].assignee == "bob"


def test_a_confirmed_assignment_survives_the_next_scheduler_cycle(tmp_path):
    path = tmp_path / "l.db"
    scheduler = engine(path)
    scheduler.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=scheduler, members=[Member("ann")])
    core.cycle()                                         # 提案が出る
    web = PmoCore(task_engine=engine(path))
    web.accept_assignment("P-1")                         # 画面で確定
    core.cycle()                                         # 常駐側の次の周
    assert engine(path).tasks["JIRA:P-1"].assignee == "ann"


def test_an_exception_inside_a_transaction_writes_nothing_and_restores_memory(tmp_path):
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    with pytest.raises(RuntimeError):
        with te.transaction():
            te.tasks["JIRA:P-1"].assignee = "ghost"
            te.tasks["JIRA:P-9"] = Task(id="JIRA:P-9", title="new")
            raise RuntimeError("boom")
    assert te.tasks["JIRA:P-1"].assignee is None and "JIRA:P-9" not in te.tasks
    assert engine(tmp_path / "l.db").tasks["JIRA:P-1"].assignee is None


def test_nested_transactions_join_the_outer_one(tmp_path):
    te = engine(tmp_path / "l.db")
    with te.transaction():
        te.ingest("t", "r", [cand(key="P-1", title="x")])
        te.refresh()
    assert "JIRA:P-1" in engine(tmp_path / "l.db").tasks


# ===== 同時実行 / concurrency ================================================

def test_many_threads_on_separate_instances_lose_nothing(tmp_path):
    path = tmp_path / "l.db"
    engine(path)                                          # スキーマを先に作る
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            te = engine(path)
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
    assert len(engine(path).tasks) == 6 * 15


def test_real_processes_writing_at_once_lose_nothing(tmp_path):
    """本物の別プロセスで確かめる。スレッドでは再現できない競合がある。"""
    path = tmp_path / "l.db"
    engine(path)
    script = tmp_path / "writer.py"
    script.write_text(textwrap.dedent("""
        import sys
        from aipmo.task_engine import TaskEngine
        path, name = sys.argv[1], sys.argv[2]
        te = TaskEngine(path)
        base = {"key": None, "title": "t", "assignee": None, "due_date": None,
                "priority": None, "status": None, "blocked": False,
                "done": False, "labels": []}
        for i in range(20):
            te.ingest("t", f"{name}-{i}", [{**base, "key": f"{name}-{i}",
                                            "title": f"{name} {i}"}])
            te.refresh()
    """), encoding="utf-8")
    env = {"PYTHONPATH": str(ROOT), "PATH": "", "SYSTEMROOT": "C:\\Windows"}
    import os
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, str(script), str(path), f"P{n}"],
                              env=env, stderr=subprocess.PIPE) for n in range(4)]
    results = [(p.wait(timeout=120), p.stderr.read().decode()) for p in procs]
    assert [code for code, _ in results] == [0, 0, 0, 0], results
    assert len(engine(path).tasks) == 4 * 20


# ===== 移行 / migration =====================================================

def legacy_json(path: Path, n_tasks: int = 3, n_outcomes: int = 2) -> None:
    tasks = [asdict(Task(id=f"JIRA:L-{i}", key=f"L-{i}", title=f"legacy {i}",
                         first_seen=NOW.isoformat(), last_seen=NOW.isoformat()))
             for i in range(n_tasks)]
    outcomes = [{"assignee": "ann", "late_days": i, "labels": []}
                for i in range(n_outcomes)]
    path.write_text(json.dumps({"tasks": tasks, "outcomes": outcomes}), encoding="utf-8")


def test_a_legacy_json_ledger_is_imported_once_and_set_aside(tmp_path):
    legacy_json(tmp_path / "task-ledger.json")
    te = engine(tmp_path / "task-ledger.db")
    assert sorted(te.tasks) == ["JIRA:L-0", "JIRA:L-1", "JIRA:L-2"]
    assert len(te.outcomes) == 2
    assert not (tmp_path / "task-ledger.json").exists()
    assert (tmp_path / "task-ledger.json.migrated").exists()
    # 二度目の起動で重複しない / no duplicates on the next start
    again = engine(tmp_path / "task-ledger.db")
    assert len(again.tasks) == 3 and len(again.outcomes) == 2


def test_a_json_name_in_config_still_works(tmp_path):
    legacy_json(tmp_path / "task-ledger.json")
    te = engine(tmp_path / "task-ledger.json")           # 古い設定の名前
    assert te.path == tmp_path / "task-ledger.db" and len(te.tasks) == 3
    assert TaskEngine.exists(tmp_path / "task-ledger.json")
    assert not TaskEngine.exists(tmp_path / "nothing.json")


def test_processes_starting_together_import_only_once(tmp_path):
    legacy_json(tmp_path / "task-ledger.json", n_tasks=5, n_outcomes=4)
    results: list[int] = []

    def open_it() -> None:
        te = engine(tmp_path / "task-ledger.db")
        results.append(len(te.outcomes))

    threads = [threading.Thread(target=open_it) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [4] * 5
    assert len(engine(tmp_path / "task-ledger.db").tasks) == 5


def test_an_unreadable_legacy_file_does_not_stop_startup(tmp_path):
    (tmp_path / "task-ledger.json").write_text("{not json", encoding="utf-8")
    assert engine(tmp_path / "task-ledger.db").tasks == {}
    assert (tmp_path / "task-ledger.json.migrated").exists()      # 原本は残す


# ===== そのほか / other ======================================================

def test_outcomes_are_trimmed_to_the_limit(tmp_path):
    te = engine(tmp_path / "l.db")
    te.add_outcomes([{"assignee": "a", "late_days": i, "labels": []}
                     for i in range(MAX_OUTCOMES + 10)])
    kept = engine(tmp_path / "l.db").outcomes
    assert len(kept) == MAX_OUTCOMES
    assert kept[-1]["late_days"] == MAX_OUTCOMES + 9          # 新しい方を残す


def test_an_unreadable_row_is_skipped_not_fatal(tmp_path):
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    raw = sqlite3.connect(tmp_path / "l.db")
    raw.execute("INSERT INTO tasks VALUES ('JIRA:BAD', '{broken')")
    raw.execute("INSERT INTO tasks VALUES ('JIRA:ODD', '{\"nope\": 1}')")
    raw.commit()
    raw.close()
    assert list(engine(tmp_path / "l.db").tasks) == ["JIRA:P-1"]


def test_confirming_does_not_hold_the_write_lock_during_the_external_call(tmp_path):
    """Jira への書き込み中に、ほかのプロセスが台帳を書けること。"""
    te = engine(tmp_path / "l.db")
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    core.cycle()
    lock_was_free = []

    def slow_external_write(key: str, assignee: str) -> None:
        probe = sqlite3.connect(tmp_path / "l.db", timeout=0.2, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")     # ロックが握られていれば失敗する
            probe.execute("ROLLBACK")
            lock_was_free.append(True)
        except sqlite3.OperationalError:
            lock_was_free.append(False)
        finally:
            probe.close()

    core.accept_assignment("P-1", write=slow_external_write)
    assert lock_was_free == [True]


def test_confirming_refuses_if_someone_else_assigned_meanwhile(tmp_path):
    path = tmp_path / "l.db"
    te = engine(path)
    te.ingest("t", "r", [cand(key="P-1", title="x")])
    core = PmoCore(task_engine=te, members=[Member("ann")])
    core.cycle()

    def someone_else_wins(key: str, assignee: str) -> None:
        other = engine(path)
        with other.transaction():
            other.tasks["JIRA:P-1"].assignee = "bob"

    with pytest.raises(ValueError, match="bob"):
        core.accept_assignment("P-1", write=someone_else_wins)
    assert engine(path).tasks["JIRA:P-1"].assignee == "bob"
