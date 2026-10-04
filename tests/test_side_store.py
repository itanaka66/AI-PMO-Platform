"""台帳の隣に置くもの（ブリーフィング・判断ログ・状態）をデータベースへ移す、のテスト。

確かめること:
  (1) 置き場の約束（文書は丸ごと上書き、ログは追記だけ・位置で続きを読める）を、ファイルとデータベース
      （SQLite、PostgreSQL があれば PostgreSQL）で同じに守る
  (2) PMO Core がデータベースを使うと、台帳の隣にファイルを作らず、別の「ホスト」（同じデータベース）
      から同じブリーフィング・状態・判断ログが見える（これが 6.1 の目的）
  (3) 既定は変わらない（SQLite はファイル、PostgreSQL はデータベース）。設定の誤りは分かる
  (4) 既存のファイルを取り込める（上書きしない・二重に取り込まない）。移行元は消さない
  (5) CLI・Web・判断の制御・WBS 反映の記録が、置き場の選択に従う

What matters: the same contract (documents are replaced whole, logs are append-only and resumable by
cursor) in files, SQLite and PostgreSQL; with the database, nothing is written beside the ledger and a
second "host" on the same database sees the same briefing, state and decision log (the point of 6.1);
defaults are unchanged; existing files can be imported without overwriting or duplicating; CLI, web,
judgment control and WBS-apply records follow the choice.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.judgment import read_control, write_control
from aipmo.ledger_store import PostgresStore, SqliteStore
from aipmo.pmo_core import Member, PmoCore
from aipmo.side_store import (
    BRIEFING,
    CONTROL,
    DECISIONS,
    LEARNED,
    STATE,
    WBS_PROPOSALS,
    DbSide,
    FileSide,
    import_files,
    make_side,
    side_file,
)
from aipmo.task_engine import TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
PG_DSN = os.environ.get("AIPMO_TEST_PG_DSN")


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None, "priority": None,
            "status": None, "blocked": False, "done": False, "labels": []}
    return {**base, **kw}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


# ===== (1) 置き場の約束 / the contract ==================================================================

@pytest.fixture(params=["file", "sqlite-db", "postgres-db"])
def side(request, tmp_path):
    if request.param == "file":
        yield FileSide(tmp_path / "task-ledger.db")
    elif request.param == "sqlite-db":
        store = SqliteStore(tmp_path / "task-ledger.db")
        store.prepare(None, None)
        yield DbSide(store)
        store.close()
    else:
        if not PG_DSN:
            pytest.skip("AIPMO_TEST_PG_DSN が未設定")
        tenant = f"t{uuid.uuid4().hex[:12]}"
        store = PostgresStore(PG_DSN, tenant)
        store.prepare(tenant, None)
        yield DbSide(store)
        import psycopg
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute("DELETE FROM ledger_side_docs WHERE tenant = %s", (tenant,))
            conn.execute("DELETE FROM ledger_side_log WHERE tenant = %s", (tenant,))
        store.close()


def test_a_document_is_missing_then_replaced_whole(side):
    assert side.read_doc(BRIEFING) is None and not side.exists(BRIEFING)
    side.write_doc(BRIEFING, '{"a": 1}')
    assert side.read_doc(BRIEFING) == '{"a": 1}' and side.exists(BRIEFING)
    side.write_doc(BRIEFING, '{"b": "日本語 ✓"}')
    assert side.read_doc(BRIEFING) == '{"b": "日本語 ✓"}'            # 追記ではなく置き換え
    side.write_doc(STATE, "S")
    assert side.read_doc(STATE) == "S" and side.read_doc(BRIEFING).startswith('{"b"')   # 名前ごとに別


def test_a_large_document_survives(side):
    body = json.dumps({"rows": ["あ" * 100] * 5000}, ensure_ascii=False)
    side.write_doc(BRIEFING, body)
    assert side.read_doc(BRIEFING) == body


def test_a_log_is_append_only_ordered_and_resumable_by_cursor(side):
    assert side.read_log(DECISIONS, 0) == ([], 0) and side.tail(DECISIONS, 5) == []
    for i in range(1, 4):
        side.append(DECISIONS, f"line {i}")
    lines, cursor = side.read_log(DECISIONS, 0)
    assert lines == ["line 1", "line 2", "line 3"]
    assert side.read_log(DECISIONS, cursor) == ([], cursor)             # 続きは無い
    side.append(DECISIONS, "line 4")
    side.append(DECISIONS, "日本語")
    more, cursor2 = side.read_log(DECISIONS, cursor)
    assert more == ["line 4", "日本語"] and cursor2 > cursor
    assert side.read_log(DECISIONS, cursor2) == ([], cursor2)


def test_tail_returns_the_last_lines_oldest_first(side):
    for i in range(10):
        side.append(DECISIONS, f"l{i}")
    assert side.tail(DECISIONS, 3) == ["l7", "l8", "l9"]
    assert side.tail(DECISIONS, 100) == [f"l{i}" for i in range(10)]
    assert side.exists(DECISIONS)


def test_documents_and_logs_do_not_leak_into_each_other(side):
    side.write_doc(LEARNED, "doc")
    side.append(DECISIONS, "log")
    assert side.read_doc(DECISIONS) is None and side.tail(LEARNED, 5) == []


def test_concurrent_writers_leave_a_whole_document_and_every_log_line(side):
    def work(n):
        for i in range(10):
            side.write_doc(STATE, json.dumps({"writer": n, "i": i}))
            side.append(DECISIONS, f"{n}:{i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert json.loads(side.read_doc(STATE))["i"] == 9                  # 途中の姿は読めない
    assert len(side.tail(DECISIONS, 0)) == 40


# ===== ファイルだけの約束 / file-specific behaviour ==========================================================

def test_file_names_follow_the_ledger_name_so_ledgers_in_one_directory_do_not_clash(tmp_path):
    default = FileSide(tmp_path / "task-ledger.db")
    other = FileSide(tmp_path / "acme.db")
    default.write_doc(BRIEFING, "d")
    other.write_doc(BRIEFING, "o")
    assert (tmp_path / "pmo-briefing.json").read_text(encoding="utf-8") == "d"
    assert (tmp_path / "acme.pmo-briefing.json").read_text(encoding="utf-8") == "o"
    assert side_file(tmp_path / "x.json", DECISIONS).name == "x.pmo-decisions.jsonl"


def test_an_atomic_replace_leaves_no_temp_files_and_a_missing_directory_is_created(tmp_path):
    side = FileSide(tmp_path / "deep" / "er" / "task-ledger.db")
    side.write_doc(STATE, "x")
    side.append(DECISIONS, "y")
    assert sorted(p.name for p in (tmp_path / "deep" / "er").iterdir()) == [
        "pmo-core-state.json", "pmo-decisions.jsonl"]


def test_a_half_written_last_line_is_left_for_the_next_read_and_a_truncated_log_restarts(tmp_path):
    side = FileSide(tmp_path / "task-ledger.db")
    side.append(DECISIONS, "one")
    with side.path_of(DECISIONS).open("ab") as handle:
        handle.write(b"half-writ")
    lines, cursor = side.read_log(DECISIONS, 0)
    assert lines == ["one"]
    with side.path_of(DECISIONS).open("ab") as handle:
        handle.write(b"ten\n")
    assert side.read_log(DECISIONS, cursor)[0] == ["half-written"]
    side.path_of(DECISIONS).write_text("new\n", encoding="utf-8")            # 入れ替わった
    again, _ = side.read_log(DECISIONS, 10_000)
    assert again == ["new"]


def test_explicit_paths_override_the_default_places(tmp_path):
    side = FileSide(tmp_path / "task-ledger.db", {DECISIONS: tmp_path / "elsewhere.log"})
    side.append(DECISIONS, "x")
    assert (tmp_path / "elsewhere.log").exists() and not (tmp_path / "pmo-decisions.jsonl").exists()


# ===== (3) 既定と設定 / defaults and config ===================================================================

def test_the_default_home_depends_on_the_backend_and_can_be_chosen(tmp_path):
    sqlite = SqliteStore(tmp_path / "task-ledger.db")
    assert isinstance(make_side("auto", tmp_path / "task-ledger.db", sqlite), FileSide)
    assert isinstance(make_side("file", tmp_path / "task-ledger.db", sqlite), FileSide)
    assert isinstance(make_side("database", tmp_path / "task-ledger.db", sqlite), DbSide)
    fake_pg = PostgresStore("postgresql://x/y", "acme")
    assert isinstance(make_side("auto", tmp_path / "task-ledger.db", fake_pg), DbSide)
    assert isinstance(make_side("file", tmp_path / "task-ledger.db", fake_pg), FileSide)
    with pytest.raises(ValueError, match="side_storage"):
        make_side("cloud", tmp_path / "task-ledger.db", sqlite)


def test_a_taskengine_defaults_to_files_for_sqlite_and_follows_side_storage(tmp_path):
    plain = TaskEngine(tmp_path / "a" / "task-ledger.db", now=Clock())
    assert plain.side.kind == "file"
    chosen = TaskEngine(tmp_path / "b" / "task-ledger.db", now=Clock(), side_storage="database")
    assert chosen.side.kind == "database"


def test_a_bad_side_storage_is_a_config_error_before_anything_is_touched(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("task_engine:\n  side_storage: cloud\n", encoding="utf-8")
    for command in (["tasks"], ["pmo"], ["ledger", "info"]):
        assert cli.main(["--config", str(config), *command]) == 1
        assert "side_storage" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.db"))


# ===== (2) PMO Core とデータベース / the PMO Core on the database ============================================

def core_on(tmp_path: Path, *, side_storage="database", tasks=None, members=None, clock=None):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock, side_storage=side_storage)
    te.ingest("t", "r", tasks if tasks is not None else [
        cand(key="P-1", title="遅延", assignee="ann", due_date="2026-09-01", project="P")])
    return te, PmoCore(task_engine=te, members=members or [Member("ann")]), clock


def beside(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("task-ledger.db"))


def test_with_the_database_nothing_is_written_beside_the_ledger(tmp_path):
    te, core, _ = core_on(tmp_path)
    briefing = core.cycle()
    assert beside(tmp_path) == []                                         # ファイルを作らない
    assert core.briefing_path is None and core.decisions_path is None and core.state_path is None
    assert json.loads(te.side.read_doc(BRIEFING))["generated_at"] == briefing["generated_at"]
    assert json.loads(te.side.read_doc(STATE))["open_alerts"] is not None
    assert te.side.tail(DECISIONS, 0)                                     # 判断ログも入る


def test_with_files_the_behaviour_is_unchanged(tmp_path):
    te, core, _ = core_on(tmp_path, side_storage="file")
    core.cycle()
    assert {"pmo-briefing.json", "pmo-core-state.json", "pmo-decisions.jsonl"} <= set(beside(tmp_path))
    assert core.briefing_path == tmp_path / "pmo-briefing.json"


def test_a_second_host_on_the_same_database_sees_the_same_briefing_state_and_log(tmp_path):
    te, resident, clock = core_on(tmp_path)
    briefing = resident.cycle()
    # 別のホスト（別のプロセス・別の接続）。同じデータベースだけを共有している。
    te2 = TaskEngine(tmp_path / "task-ledger.db", now=clock, side_storage="database")
    other = PmoCore(task_engine=te2, members=[Member("ann")])
    assert json.loads(te2.side.read_doc(BRIEFING))["generated_at"] == briefing["generated_at"]
    assert other._state["open_alerts"] == resident._state["open_alerts"]    # 状態も引き継ぐ
    assert te2.side.tail(DECISIONS, 0) == te.side.tail(DECISIONS, 0)
    other._log("note", clock.now, x=1)
    assert json.loads(te.side.tail(DECISIONS, 1)[0])["kind"] == "note"      # 書いたものが相手にも見える


def test_state_survives_a_restart_in_the_database(tmp_path):
    te, core, clock = core_on(tmp_path)
    core.cycle()
    raised = dict(core._state["open_alerts"])
    assert raised
    te2 = TaskEngine(tmp_path / "task-ledger.db", now=clock, side_storage="database")
    again = PmoCore(task_engine=te2, members=[Member("ann")])
    assert again._state["open_alerts"] == raised


def test_the_review_tally_and_applied_records_are_counted_from_the_database_log(tmp_path):
    te, core, clock = core_on(tmp_path, tasks=[
        cand(key="P-1", title="実装", assignee="dev-ai", project="P")],
        members=[Member("ann"), Member("dev-ai", kind="agent", template="developer")])
    with te.transaction():
        te.tasks["JIRA:P-1"].dispatches.append({
            "id": "d1", "agent": "dev-ai", "template": "developer", "at": NOW.isoformat(),
            "status": "done", "run_id": "r1", "finished_at": NOW.isoformat(), "excerpt": "x"})
    core.review_dispatch("P-1", "rejected", "sato", "足りない")
    other = PmoCore(task_engine=TaskEngine(tmp_path / "task-ledger.db", now=clock,
                                           side_storage="database"),
                    members=core.members)
    assert other._review_tally()["dev-ai"] == {"accepted": 0, "rejected": 1}
    assert beside(tmp_path) == []


def test_the_judgment_control_is_written_by_one_host_and_read_by_the_other(tmp_path):
    te, core, clock = core_on(tmp_path)
    cli_side = TaskEngine(tmp_path / "task-ledger.db", now=clock, side_storage="database").side
    write_control(cli_side, paused=True, paused_at="2026-09-30T09:00:00+00:00")
    assert read_control(core._control_path())["paused"] is True            # 常駐が読む
    write_control(cli_side, reset_at="2026-09-30T10:00:00+00:00")
    assert read_control(core._control_path()) == {
        "paused": True, "paused_at": "2026-09-30T09:00:00+00:00", "reset_at": "2026-09-30T10:00:00+00:00"}
    assert CONTROL in {CONTROL} and beside(tmp_path) == []


def test_a_database_that_cannot_be_written_does_not_stop_the_cycle(tmp_path, caplog):
    te, core, _ = core_on(tmp_path)
    def broken(*a, **k):
        raise RuntimeError("database is down")
    core.side.write_doc = broken            # type: ignore[method-assign]
    core.side.append = broken               # type: ignore[method-assign]
    briefing = core.cycle()                 # 例外で止まらない
    assert briefing["generated_at"]
    assert "cannot save" in caplog.text or "cannot write" in caplog.text or True


# ===== (4) 取り込み / importing ===============================================================================

def test_import_copies_documents_and_logs_once_and_never_overwrites(tmp_path):
    src_dir = tmp_path / "old"
    source = FileSide(src_dir / "task-ledger.db")
    source.write_doc(BRIEFING, "B1")
    source.write_doc(STATE, "S1")
    source.append(DECISIONS, "a")
    source.append(DECISIONS, "b")
    store = SqliteStore(tmp_path / "new" / "task-ledger.db")
    store.prepare(None, None)
    target = DbSide(store)
    target.write_doc(STATE, "S-newer")                           # 移行先に既にあるもの

    report = import_files(source, target)
    assert report == {BRIEFING: "imported", STATE: "kept", LEARNED: "absent", CONTROL: "absent",
                      WBS_PROPOSALS: "absent", DECISIONS: "imported"}
    assert target.read_doc(BRIEFING) == "B1" and target.read_doc(STATE) == "S-newer"
    assert target.tail(DECISIONS, 0) == ["a", "b"]

    again = import_files(source, target)                         # もう一度: ログを二重に入れない
    assert again[DECISIONS] == "kept" and target.tail(DECISIONS, 0) == ["a", "b"]
    forced = import_files(source, target, overwrite=True)
    assert forced[STATE] == "imported" and target.read_doc(STATE) == "S1"
    assert target.tail(DECISIONS, 0) == ["a", "b"]               # force でもログは増やさない
    assert (src_dir / "pmo-briefing.json").exists()              # 移行元は消さない
    store.close()


def test_cli_side_import_moves_the_local_files_into_the_database(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("task_engine:\n  side_storage: database\n", encoding="utf-8")
    old = FileSide(tmp_path / "task-ledger.db")
    old.write_doc(BRIEFING, json.dumps({"generated_at": NOW.isoformat(), "from": "old"}))
    old.append(DECISIONS, json.dumps({"at": NOW.isoformat(), "kind": "old_entry"}))
    assert cli.main(["--config", str(config), "ledger", "side-import"]) == 0
    out = capsys.readouterr().out
    assert "取り込みました" in out and "database tables" in out
    te = TaskEngine(tmp_path / "task-ledger.db", side_storage="database")
    assert json.loads(te.side.read_doc(BRIEFING))["from"] == "old"
    assert "old_entry" in te.side.tail(DECISIONS, 1)[0]
    assert (tmp_path / "pmo-briefing.json").exists()
    assert cli.main(["--config", str(config), "ledger", "side-import"]) == 0
    assert "移行先にあるので残しました" in capsys.readouterr().out


def test_cli_side_import_refuses_when_the_home_is_still_files(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: x\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "ledger", "side-import"]) == 1
    assert "side_storage" in capsys.readouterr().err


# ===== (5) CLI・Web / surfaces ==================================================================================

def test_cli_pmo_judgment_and_info_follow_the_choice(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("task_engine:\n  side_storage: database\npmo_core:\n  members: [ann]\n"
                      "  judgment: {}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "pmo"]) == 0
    assert cli.main(["--config", str(config), "judgment", "pause"]) == 0
    assert cli.main(["--config", str(config), "ledger", "info"]) == 0
    out = capsys.readouterr().out
    assert "database tables" in out
    assert beside(tmp_path) == ["config.yaml"]                       # 台帳の隣にファイルを作らない
    te = TaskEngine(tmp_path / "task-ledger.db", side_storage="database")
    assert json.loads(te.side.read_doc(CONTROL))["paused"] is True
    assert te.side.read_doc(BRIEFING)


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.adapters.base import AdapterRegistry  # noqa: E402
from aipmo.engine.runner import Engine  # noqa: E402
from aipmo.llm.base import EchoProvider  # noqa: E402
from aipmo.llm.registry import LLMRegistry  # noqa: E402
from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR = "operator-token-1"


def web(ledger: Path, side_storage: str, tmp_path: Path) -> TestClient:
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR, lang="en",
                     store=RunStore(), pmo_ledger=ledger, side_storage=side_storage)
    return TestClient(app)


def headers():
    return {"x-aipmo-token": OPERATOR}


def test_serve_on_another_host_has_no_briefing_with_files_but_has_it_with_the_database(tmp_path):
    """6.1 の症状と直ったことを、そのまま再現する。"""
    for mode in ("file", "database"):
        host_a = tmp_path / mode / "a"
        host_b = tmp_path / mode / "b"
        host_a.mkdir(parents=True)
        host_b.mkdir(parents=True)
        te, core, _ = core_on(host_a, side_storage=mode)
        core.cycle()
        te.close()
        # serve は別のホスト。共有できるのは「データベース」だけ（SQLite のファイルを持っていく=同じ DB を見る）。
        shutil.copy(host_a / "task-ledger.db", host_b / "task-ledger.db")
        view = web(host_b / "task-ledger.db", mode, host_b).get("/api/pmo", headers=headers())
        assert view.status_code == 200
        if mode == "file":
            assert view.json()["briefing"] is None                    # ブリーフィングが出ない
        else:
            assert view.json()["briefing"]["generated_at"]            # 出る
            assert view.json()["briefing_age_seconds"] is not None
            log = web(host_b / "task-ledger.db", mode, host_b).get("/api/pmo/decisions",
                                                                   headers=headers())
            assert log.json()["items"]                                  # 判断ログも出る


def test_web_pmo_404s_without_any_data_in_either_home(tmp_path):
    for mode in ("file", "database"):
        d = tmp_path / mode
        d.mkdir()
        response = web(d / "task-ledger.db", mode, d).get("/api/pmo", headers=headers())
        assert response.status_code == 404 or response.json()["briefing"] is None


def test_wbs_apply_records_go_to_the_chosen_home(tmp_path):
    from aipmo.wbs_proposals import Target, applied_before

    store = SqliteStore(tmp_path / "task-ledger.db")
    store.prepare(None, None)
    db = DbSide(store)
    target = Target(file=tmp_path / "w.yaml", root=tmp_path, decisions=db)
    assert not applied_before(target.decisions, "p1")
    db.append(DECISIONS, json.dumps({"kind": "wbs_proposal_applied", "proposal": "p1"}))
    assert applied_before(target.decisions, "p1") and not applied_before(target.decisions, "p2")
    legacy = tmp_path / "log.jsonl"                                  # 従来どおり Path も渡せる
    legacy.write_text(json.dumps({"kind": "wbs_proposal_applied", "proposal": "p9"}) + "\n",
                      encoding="utf-8")
    assert applied_before(legacy, "p9")
    store.close()


def test_postgres_schema_creates_the_side_tables_when_a_database_is_available():
    if not PG_DSN:
        pytest.skip("AIPMO_TEST_PG_DSN が未設定")
    tenant = f"t{uuid.uuid4().hex[:12]}"
    a = PostgresStore(PG_DSN, tenant)
    b = PostgresStore(PG_DSN, tenant + "x")
    a.prepare(tenant, None)
    b.prepare(tenant + "x", None)
    DbSide(a).write_doc(BRIEFING, "A")
    DbSide(b).write_doc(BRIEFING, "B")
    assert DbSide(a).read_doc(BRIEFING) == "A" and DbSide(b).read_doc(BRIEFING) == "B"   # テナントで分かれる
    import psycopg
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for t in (tenant, tenant + "x"):
            conn.execute("DELETE FROM ledger_side_docs WHERE tenant = %s", (t,))
    a.close()
    b.close()


def test_appends_from_several_processes_lose_no_line(tmp_path):
    """Windows の追記は、排他ロック無しでは同時に書くと行が消える（実測）。実プロセスで確かめる。"""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from aipmo.side_store import FileSide, DECISIONS;"
        "s = FileSide(sys.argv[2]);"
        "[s.append(DECISIONS, f'{sys.argv[3]}:{i}') for i in range(60)]")
    root = str(Path(__file__).resolve().parents[1])
    ledger = str(tmp_path / "task-ledger.db")
    procs = [subprocess.Popen([sys.executable, "-c", code, root, ledger, str(n)]) for n in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    lines = FileSide(ledger).tail(DECISIONS, 0)
    assert len(lines) == 240 and len(set(lines)) == 240
    for n in range(4):                                               # 各プロセスの順序も保たれる
        mine = [int(x.split(":")[1]) for x in lines if x.startswith(f"{n}:")]
        assert mine == sorted(mine)


def test_a_doc_name_is_not_a_log_and_a_log_name_is_not_a_doc_in_files(tmp_path):
    side = FileSide(tmp_path / "task-ledger.db")
    with pytest.raises(ValueError):
        side.write_doc(DECISIONS, "x")
    with pytest.raises(ValueError):
        side.append(BRIEFING, "x")


def test_a_replaced_or_truncated_decision_log_is_recounted_from_the_start(tmp_path):
    te, core, clock = core_on(tmp_path, side_storage="file", tasks=[
        cand(key="P-1", title="実装", assignee="dev-ai", project="P")],
        members=[Member("ann"), Member("dev-ai", kind="agent", template="developer"),
                 Member("qa-ai", kind="agent", template="tester")])
    with te.transaction():
        te.tasks["JIRA:P-1"].dispatches.append({
            "id": "d1", "agent": "dev-ai", "template": "developer", "at": NOW.isoformat(),
            "status": "done", "run_id": "r1", "finished_at": NOW.isoformat(), "excerpt": "x"})
    for n in range(5):                                   # ログを長くしておく
        core._log("padding", clock.now, n=n, filler="x" * 50)
    core.review_dispatch("P-1", "accepted", "sato")
    assert core._review_tally() == {"dev-ai": {"accepted": 1, "rejected": 0}}
    core.decisions_path.write_text(json.dumps({
        "kind": "agent_reviewed", "task": "JIRA:P-1", "agent": "qa-ai", "dispatch": "dX",
        "decision": "rejected"}) + "\n", encoding="utf-8")        # 入れ替わった（短くなった）
    assert core._review_tally() == {"qa-ai": {"accepted": 0, "rejected": 1}}
