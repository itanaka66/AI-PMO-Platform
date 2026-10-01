"""役割AIの成果を人が確認した記録（レビュー）のテスト / human review of role-AI results.

確かめること:
  (1) 完了した実行だけがレビュー待ちになり、レビューは台帳の実行記録に残る
  (2) 役割AIはレビューできない。差し戻しには理由が要る。失敗した実行はレビューできない
  (3) 差し戻した成果は警告になる（人が引き取る／もう一度任せる）。認めても警告にならない
  (4) 確かめ直すと上書きされ、前の判断が残る。集計もずれない
  (5) 判断ログに全件残る（実行記録は新しい 5 件しか持たないため）
  (6) 再起動しても集計が残る。新しい実行は新しくレビュー待ちになる
  (7) CLI と Web（operator のみ）から記録できる

What matters: only a finished run awaits review; the review is stored on the ledger's dispatch
record; a role AI can never review; a rejection needs a reason and raises an alert; a second
review overwrites but remembers; every review is in the decision log; counters survive a restart;
CLI and web (operator only) can record.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipmo import cli
from aipmo.adapters.base import AdapterRegistry
from aipmo.agent_roles import review_of
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore, scope_briefing
from aipmo.task_engine import TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None, "priority": None,
            "status": None, "blocked": False, "done": False, "labels": []}
    return {**base, **kw}


MEMBERS = [Member("ann"), Member("dev-ai", kind="agent", template="developer")]


def build(tmp_path: Path, tasks=None):
    clock = Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    te.ingest("t", "r", tasks if tasks is not None else [
        cand(key="P-1", title="API を実装", assignee="dev-ai", project="P")])
    core = PmoCore(task_engine=te, members=MEMBERS)
    return te, core, clock


def run(te: TaskEngine, task_id="JIRA:P-1", status="done", n=1, agent="dev-ai", **extra):
    entry = {"id": f"d{n}", "agent": agent, "template": "developer", "at": NOW.isoformat(),
             "status": status, "run_id": f"r{n}", "finished_at": NOW.isoformat(),
             "error": None, "excerpt": f"実装案 {n}", **extra}
    with te.transaction():
        te.tasks[task_id].dispatches.append(entry)
    return entry


def log(core):
    return [json.loads(x) for x in core.decisions_path.read_text(encoding="utf-8").splitlines()]


# ===== (1) 待ちと記録 / pending and the record ================================================

def test_only_a_finished_unreviewed_run_awaits_review_and_the_review_is_stored_on_it(tmp_path):
    te, core, _ = build(tmp_path)
    assert core.reviews_pending() == []                       # まだ何も任せていない
    run(te, status="running")
    assert core.reviews_pending() == []                       # 実行中は対象外
    with te.transaction():
        te.tasks["JIRA:P-1"].dispatches[-1].update(status="done")
    (pending,) = core.reviews_pending()
    assert pending["task"] == "JIRA:P-1" and pending["agent"] == "dev-ai"
    assert pending["excerpt"] == "実装案 1" and pending["dispatch"] == "d1"
    assert core.cycle()["agent_review"]["pending"][0]["task"] == "JIRA:P-1"

    result = core.review_dispatch("P-1", "accepted", "sato", "問題なし")
    assert result["decision"] == "accepted" and result["by"] == "sato"
    entry = te.tasks["JIRA:P-1"].dispatches[-1]
    assert review_of(entry)["note"] == "問題なし" and review_of(entry)["at"]
    assert core.reviews_pending() == []                       # 待ちから消える


def test_a_failed_run_cannot_be_reviewed(tmp_path):
    te, core, _ = build(tmp_path)
    run(te, status="failed", error="boom")
    for status in ("skipped", "abandoned"):
        run(te, status=status, n=2)
    with pytest.raises(ValueError, match="完了した実行だけ"):
        core.review_dispatch("P-1", "accepted", "sato")
    with pytest.raises(ValueError, match="完了した実行だけ"):
        core.review_dispatch("P-1", "accepted", "sato", dispatch_id="d1")
    assert core.reviews_pending() == []


def test_a_task_with_no_run_or_no_such_task_is_refused(tmp_path):
    te, core, _ = build(tmp_path)
    with pytest.raises(ValueError, match="実行記録がありません"):
        core.review_dispatch("P-1", "accepted", "sato")
    with pytest.raises(KeyError):
        core.review_dispatch("NOPE-9", "accepted", "sato")
    run(te)
    with pytest.raises(ValueError, match="実行記録がありません"):
        core.review_dispatch("P-1", "accepted", "sato", dispatch_id="zzz")


# ===== (2) だれが・どう / who and how =============================================================

def test_a_role_ai_can_never_review_even_itself(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    for who in ("dev-ai", "DEV-AI"):
        with pytest.raises(ValueError, match="役割AI"):
            core.review_dispatch("P-1", "accepted", who)
    assert not review_of(te.tasks["JIRA:P-1"].dispatches[-1])


def test_a_reviewer_is_required_and_a_rejection_needs_a_reason(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    with pytest.raises(ValueError, match="reviewer"):
        core.review_dispatch("P-1", "accepted", "  ")
    with pytest.raises(ValueError, match="理由"):
        core.review_dispatch("P-1", "rejected", "sato", "")
    with pytest.raises(ValueError, match="decision"):
        core.review_dispatch("P-1", "maybe", "sato")
    assert not review_of(te.tasks["JIRA:P-1"].dispatches[-1])
    assert core.review_dispatch("P-1", "rejected", "sato", "テストが無い")["decision"] == "rejected"


# ===== (3) 警告 / alerts =========================================================================

def alerts(core):
    return [a for a in core.cycle()["alerts"] if a["rule"] == "agent_attention"]


def test_a_rejected_result_raises_an_alert_and_an_accepted_one_does_not(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    assert alerts(core) == []                                   # 待っているだけでは警告にしない
    core.review_dispatch("P-1", "accepted", "sato")
    assert alerts(core) == []

    run(te, n=2)
    core.review_dispatch("P-1", "rejected", "sato", "例外処理が足りない")
    (alert,) = alerts(core)
    assert "差し戻" in alert["message"] and "例外処理が足りない" in alert["message"]
    assert "dev-ai" in alert["message"]


def test_the_alert_clears_when_a_new_run_replaces_it_or_the_task_is_reassigned(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    core.review_dispatch("P-1", "rejected", "sato", "やり直し")
    assert alerts(core)
    run(te, n=2)                                                # もう一度任せた
    assert alerts(core) == []
    core.review_dispatch("P-1", "rejected", "sato", "まだ駄目")
    assert alerts(core)
    with te.transaction():
        te.tasks["JIRA:P-1"].assignee = "ann"                   # 人が引き取った
    assert alerts(core) == []


# ===== (4) 確かめ直し / re-reviewing =================================================================

def test_reviewing_again_overwrites_keeps_the_earlier_decision_and_keeps_counters_right(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    core.review_dispatch("P-1", "accepted", "sato", "ok")
    core.review_dispatch("P-1", "rejected", "tanaka", "やっぱり駄目")
    review = review_of(te.tasks["JIRA:P-1"].dispatches[-1])
    assert review["decision"] == "rejected" and review["by"] == "tanaka"
    assert review["previous"]["decision"] == "accepted" and review["previous"]["by"] == "sato"
    assert core._review_tally()["dev-ai"] == {"accepted": 0, "rejected": 1}
    briefing = core.cycle()
    summary = next(a for a in briefing["agents"] if a["member"] == "dev-ai")
    assert (summary["accepted"], summary["rejected"], summary["awaiting_review"]) == (0, 1, 0)


# ===== (5) 判断ログ / the decision log ==================================================================

def test_every_review_is_in_the_decision_log_even_after_the_ledger_forgets_the_run(tmp_path):
    te, core, _ = build(tmp_path)
    for n in range(1, 9):                                       # 実行記録は新しい 5 件だけ
        run(te, n=n)
        core.review_dispatch("P-1", "accepted" if n % 2 else "rejected", "sato", f"n{n}")
    with te.transaction():
        te.tasks["JIRA:P-1"].dispatches[:] = te.tasks["JIRA:P-1"].dispatches[-5:]
    reviews = [e for e in log(core) if e["kind"] == "agent_reviewed"]
    assert len(reviews) == 8 and reviews[0]["note"] == "n1" and reviews[0]["by"] == "sato"
    assert reviews[0]["dispatch"] == "d1" and reviews[0]["agent"] == "dev-ai"
    assert core._review_tally()["dev-ai"] == {"accepted": 4, "rejected": 4}


# ===== (6) 再起動・新しい実行 / restart and new runs ===============================================

def test_the_tally_survives_a_restart_and_a_new_run_awaits_review_again(tmp_path):
    te, core, clock = build(tmp_path)
    run(te)
    core.review_dispatch("P-1", "accepted", "sato")
    core.cycle()
    te2 = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    core2 = PmoCore(task_engine=te2, members=MEMBERS)
    assert core2._review_tally()["dev-ai"]["accepted"] == 1
    run(te2, n=2)
    assert [p["dispatch"] for p in core2.reviews_pending()] == ["d2"]
    assert review_of(te2.tasks["JIRA:P-1"].dispatches[0])["decision"] == "accepted"   # 前のは残る


def test_a_review_is_scoped_by_project_and_hidden_from_confined_viewers(tmp_path):
    te, core, _ = build(tmp_path, [
        cand(key="A-1", title="a", assignee="dev-ai", project="alpha"),
        cand(key="B-1", title="b", assignee="dev-ai", project="beta")])
    run(te, "JIRA:A-1")
    run(te, "JIRA:B-1", n=2)
    briefing = core.cycle()
    assert len(briefing["agent_review"]["pending"]) == 2
    only = scope_briefing(briefing, [], {"alpha"}, redact_org=False)
    assert [p["project"] for p in only["agent_review"]["pending"]] == ["alpha"]
    assert scope_briefing(briefing, [], {"alpha"}, redact_org=True)["agent_review"] is None


# ===== (7) CLI と Web ======================================================================================

def cli_config(tmp_path: Path) -> Path:
    (tmp_path / "templates").mkdir(exist_ok=True)
    (tmp_path / "templates" / "developer.yaml").write_text(
        "name: developer\nsteps:\n  - id: s\n    expression: noop\n    inputs: {ok: true}\n",
        encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f"web: {{templates_dir: '{(tmp_path / 'templates').as_posix()}'}}\npmo_core:\n  members:\n    - ann\n"
        "    - {name: dev-ai, kind: agent, template: developer}\n", encoding="utf-8")
    return config


def test_cli_lists_waiting_results_and_records_a_review(tmp_path, capsys):
    te, _, _ = build(tmp_path)
    run(te)
    te.close()
    config = cli_config(tmp_path)
    assert cli.main(["--config", str(config), "agents", "review"]) == 0
    out = capsys.readouterr().out
    assert "JIRA:P-1" in out and "実装案 1" in out

    assert cli.main(["--config", str(config), "agents", "review", "P-1"]) == 1          # 判断が無い
    assert cli.main(["--config", str(config), "agents", "review", "P-1", "--accept", "--reject"]) == 1
    assert cli.main(["--config", str(config), "agents", "review", "P-1", "--reject",
                     "--by", "sato"]) == 1                                              # 理由が無い
    assert cli.main(["--config", str(config), "agents", "review", "P-1", "--accept",
                     "--by", "dev-ai"]) == 1                                            # AI は不可
    capsys.readouterr()
    assert cli.main(["--config", str(config), "agents", "review", "P-1", "--accept",
                     "--by", "sato", "--note", "ok"]) == 0
    assert "認めました" in capsys.readouterr().out
    reopened = TaskEngine(tmp_path / "task-ledger.db")
    assert review_of(reopened.tasks["JIRA:P-1"].dispatches[-1])["by"] == "sato"

    assert cli.main(["--config", str(config), "agents"]) == 0
    shown = capsys.readouterr().out
    assert "認めた 1" in shown and "認めた: sato" in shown


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aipmo.web.server import RunStore, create_app  # noqa: E402

OPERATOR, VIEWER = "operator-token-1", "viewer-token-2"


def web(tmp_path: Path, **kw):
    te, _, _ = build(tmp_path, [
        cand(key="A-1", title="a", assignee="dev-ai", project="alpha"),
        cand(key="B-1", title="b", assignee="dev-ai", project="beta")])
    run(te, "JIRA:A-1")
    run(te, "JIRA:B-1", n=2)
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir(exist_ok=True)
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", OPERATOR, viewer_token=VIEWER,
                     lang="en", store=RunStore(), pmo_ledger=tmp_path / "task-ledger.db",
                     members=MEMBERS, **kw)
    return TestClient(app)


def headers(token):
    return {"x-aipmo-token": token}


def test_web_lists_and_records_reviews_for_the_operator_only(tmp_path):
    client = web(tmp_path)
    view = client.get("/api/pmo", headers=headers(OPERATOR)).json()
    assert {r["task"] for r in view["agent_review"]} == {"JIRA:A-1", "JIRA:B-1"}

    denied = client.post("/api/pmo/agents/review", headers=headers(VIEWER),
                         json={"ref": "A-1", "decision": "accept"})
    assert denied.status_code in (401, 403)
    assert client.post("/api/pmo/agents/review", headers=headers(OPERATOR),
                       json={"ref": "A-1", "decision": "reject"}).status_code == 409   # 理由が無い
    assert client.post("/api/pmo/agents/review", headers=headers(OPERATOR),
                       json={"ref": "A-1", "decision": "x"}).status_code == 422
    assert client.post("/api/pmo/agents/review", headers=headers(OPERATOR),
                       json={"ref": "NOPE-1", "decision": "accept"}).status_code == 404
    assert client.post("/api/pmo/agents/review", headers=headers(OPERATOR),
                       json={"ref": "A-1", "decision": "accept", "by": "dev-ai"}).status_code == 409

    ok = client.post("/api/pmo/agents/review", headers=headers(OPERATOR),
                     json={"ref": "A-1", "decision": "reject", "note": "足りない", "by": "sato"})
    assert ok.status_code == 200 and ok.json()["decision"] == "rejected"
    after = client.get("/api/pmo", headers=headers(OPERATOR)).json()
    assert [r["task"] for r in after["agent_review"]] == ["JIRA:B-1"]            # 押した直後に消える
    shown = next(t for t in after["tasks"] if t["id"] == "JIRA:A-1")
    assert shown["dispatches"][-1]["review"]["decision"] == "rejected"
    assert shown["dispatches"][-1]["review"]["by"] == "sato"


def test_web_hides_the_review_list_from_confined_viewers_and_scopes_the_others(tmp_path):
    client = web(tmp_path, viewer_projects=["alpha"])
    assert client.get("/api/pmo", headers=headers(VIEWER)).json()["agent_review"] is None
    both = client.get("/api/pmo", headers=headers(OPERATOR)).json()["agent_review"]
    assert len(both) == 2
    scoped = client.get("/api/pmo?project=alpha", headers=headers(OPERATOR)).json()["agent_review"]
    assert [r["task"] for r in scoped] == ["JIRA:A-1"]


def test_a_review_recorded_by_another_process_is_not_erased_by_the_residents_state_write(tmp_path):
    te, resident, clock = build(tmp_path)
    run(te)
    resident.cycle()                                            # 常駐が自分の写しで状態を持つ
    cli_core = PmoCore(task_engine=TaskEngine(tmp_path / "task-ledger.db", now=clock),
                       members=MEMBERS)                         # 別プロセスの CLI
    cli_core.review_dispatch("P-1", "rejected", "sato", "足りない")
    for _ in range(2):
        clock.now += timedelta(minutes=1)
        briefing = resident.cycle()                             # 常駐が状態を書き戻す
    summary = next(a for a in briefing["agents"] if a["member"] == "dev-ai")
    assert (summary["accepted"], summary["rejected"]) == (0, 1)
    assert briefing["agent_review"]["tally"]["dev-ai"] == {"accepted": 0, "rejected": 1}
    assert PmoCore(task_engine=te, members=MEMBERS)._review_tally()["dev-ai"]["rejected"] == 1


def test_a_half_written_last_log_line_is_not_counted_and_is_read_once_complete(tmp_path):
    te, core, _ = build(tmp_path)
    run(te)
    core.review_dispatch("P-1", "accepted", "sato")
    assert core._review_tally()["dev-ai"]["accepted"] == 1
    with core.decisions_path.open("ab") as handle:               # 書きかけ
        handle.write(b'{"kind": "agent_reviewed", "task": "JIRA:P-1", "agent": "dev-ai", "dispatch": "d9", "decision": "rej')
    assert core._review_tally()["dev-ai"] == {"accepted": 1, "rejected": 0}
    with core.decisions_path.open("ab") as handle:
        handle.write(b'ected"}\n')
    assert core._review_tally()["dev-ai"] == {"accepted": 1, "rejected": 1}
