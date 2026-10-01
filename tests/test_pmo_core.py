"""PMO Core（担当割当・進捗ルール・統括）のテスト / PMO Core tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aipmo import cli
from aipmo.pmo_core import (Member, PmoCore, RuleError, evaluate_rules, load_rules,
                            suggest_assignee)
from aipmo.task_engine import TaskEngine

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": []}
    return {**base, **kw}


def make(tmp_path, members=None, clock=None, notify=None, **kw):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.json", now=clock)
    core = PmoCore(task_engine=te, members=members or [], notify=notify, **kw)
    return te, core, clock


def kinds(tmp_path):
    lines = (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["kind"] for line in lines]


# ===== 担当割当 / assignment ================================================

def test_proposal_goes_to_least_loaded_and_respects_capacity(tmp_path):
    members = [Member("ann", capacity=2), Member("bob", capacity=1)]
    te, core, _ = make(tmp_path, members)
    te.ingest("t", "r", [cand(key="P-1", title="a", priority="High"),
                         cand(key="P-2", title="b", priority="Medium"),
                         cand(key="P-3", title="c", priority="Low"),
                         cand(key="P-4", title="d", priority="Low")])
    briefing = core.cycle()
    got = {p["task"]: p["assignee"] for p in briefing["assignment_proposals"]}
    # 空きは合計3。最下位のタスクには割り当て先が無い。
    assert len(got) == 3 and set(got.values()) == {"ann", "bob"}
    assert got["JIRA:P-1"] == "ann"           # 比率が同じなら名前順
    assert [u["task"] for u in briefing["unassignable"]] == ["JIRA:P-4"]


def test_skill_match_wins_over_free_capacity(tmp_path):
    members = [Member("ann", skills=("db",)), Member("bob")]
    te, core, _ = make(tmp_path, members)
    te.ingest("t", "r", [cand(key="P-1", title="a", labels=["DB"])])
    core.cycle()
    task = te.tasks["JIRA:P-1"]
    assert task.suggested_assignee == "ann" and "スキル" in task.suggestion_reason


def test_nobody_free_means_no_proposal():
    task = type("T", (), {"labels": []})()
    assert suggest_assignee(task, [Member("ann", capacity=1)], {"ann": 1}) is None


def test_proposals_are_stable_and_never_touch_assigned_tasks(tmp_path):
    members = [Member("ann"), Member("bob")]
    te, core, _ = make(tmp_path, members)
    te.ingest("t", "r", [cand(key="P-1", title="a"),
                         cand(key="P-2", title="b", assignee="ann")])
    core.cycle()
    first = te.tasks["JIRA:P-1"].suggested_assignee
    core.cycle()
    assert te.tasks["JIRA:P-1"].suggested_assignee == first
    assert te.tasks["JIRA:P-2"].suggested_assignee is None
    assert kinds(tmp_path).count("assignment_proposed") == 1


def test_accept_writes_first_and_leaves_ledger_alone_on_failure(tmp_path):
    te, core, _ = make(tmp_path, [Member("ann")])
    te.ingest("t", "r", [cand(key="P-1", title="a")])
    core.cycle()

    def boom(key, who):
        raise RuntimeError("jira down")

    with pytest.raises(RuntimeError):
        core.accept_assignment("p-1", write=boom)
    assert te.tasks["JIRA:P-1"].assignee is None

    calls = []
    core.accept_assignment("P-1", write=lambda k, w: calls.append((k, w)))
    assert calls == [("P-1", "ann")] and te.tasks["JIRA:P-1"].assignee == "ann"
    with pytest.raises(ValueError):
        core.accept_assignment("P-1")


def test_overloaded_member_is_reported(tmp_path):
    te, core, _ = make(tmp_path, [Member("ann", capacity=1)])
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="ann"),
                         cand(key="P-2", title="b", assignee="ANN")])
    assert core.cycle()["overloaded_members"] == [
        {"member": "ann", "load": 2, "capacity": 1}]


# ===== 進捗ルール / progress rules ==========================================

def test_overdue_tiers_keep_only_the_heaviest(tmp_path):
    te, _, _ = make(tmp_path)
    te.ingest("t", "r", [cand(key="P-1", title="a", due_date="2026-09-20")])
    violations = evaluate_rules(load_rules(None), te.ranked(), NOW)
    assert [(v.rule, v.severity) for v in violations if v.rule.startswith("overdue")] \
        == [("overdue_severe", "critical")]


def test_stalled_and_blocked_long_use_how_long_it_has_lasted(tmp_path):
    clock = Clock()
    te, core, _ = make(tmp_path, clock=clock)
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="x",
                              status="In Progress", blocked=True)])
    assert core.cycle()["alerts"] == []
    clock.now = NOW + timedelta(days=6)
    te.ingest("t", "r2", [cand(key="P-1", title="a", assignee="x",
                               status="In Progress", blocked=True)])
    assert {a["rule"] for a in core.cycle()["alerts"]} == {"stalled", "blocked_long"}
    # 状態が変われば時計はやり直し
    te.ingest("t", "r3", [cand(key="P-1", title="a", assignee="x", status="In Review")])
    assert core.cycle()["alerts"] == []


def test_not_started_near_due_and_unassigned(tmp_path):
    clock = Clock()
    te, core, _ = make(tmp_path, clock=clock)
    te.ingest("t", "r", [cand(key="P-1", title="a", status="To Do",
                              due_date="2026-10-02")])
    assert {a["rule"] for a in core.cycle()["alerts"]} == {"not_started_near_due"}
    clock.now = NOW + timedelta(days=2)
    te.ingest("t", "r2", [cand(key="P-1", title="a", status="To Do",
                               due_date="2026-10-02")])
    assert "unassigned" in {a["rule"] for a in core.cycle()["alerts"]}


def test_rules_can_be_overridden_and_bad_config_is_refused():
    rules = {r.id: r for r in load_rules([{"id": "stalled", "days": 1, "severity": "high"},
                                          {"id": "unassigned", "enabled": False}])}
    assert rules["stalled"].days == 1 and rules["stalled"].severity == "high"
    assert not rules["unassigned"].enabled
    for bad in ([{"kind": "overdue"}], [{"id": "x", "kind": "nope"}],
                [{"id": "stalled", "severity": "meh"}]):
        with pytest.raises(RuleError):
            load_rules(bad)


# ===== 通知と判断ログ / notification and decision log ============================

def test_alert_is_notified_once_then_again_after_cooldown_and_resolves(tmp_path):
    sent = []
    clock = Clock()
    te, core, _ = make(tmp_path, clock=clock, notify=sent.append)
    item = cand(key="P-1", title="a", assignee="x", due_date="2026-09-25")
    te.ingest("t", "r", [item])
    core.cycle()
    core.cycle()
    assert len(sent) == 1 and "期限を 5 日超過" in sent[0]

    clock.now = NOW + timedelta(hours=25)
    te.ingest("t", "r2", [item])
    core.cycle()
    assert len(sent) == 2

    te.ingest("t", "r3", [cand(key="P-1", title="a", done=True)])
    core.cycle()
    log = kinds(tmp_path)
    assert log.count("alert_raised") == 1 and log[-1] == "alert_resolved"


def test_failed_notification_is_retried_next_cycle(tmp_path):
    attempts = []

    def flaky(text):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("slack down")

    te, core, _ = make(tmp_path, notify=flaky)
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="x", due_date="2026-09-25")])
    core.cycle()
    core.cycle()
    assert len(attempts) == 2


def test_alert_state_survives_restart_without_renotifying(tmp_path):
    sent = []
    te, core, clock = make(tmp_path, notify=sent.append)
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="x", due_date="2026-09-25")])
    core.cycle()
    te2 = TaskEngine(tmp_path / "task-ledger.json", now=clock)
    PmoCore(task_engine=te2, notify=sent.append).cycle()
    assert len(sent) == 1


def test_briefing_level_and_priorities(tmp_path):
    te, core, _ = make(tmp_path)
    assert core.cycle()["overall_level"] == "low"
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="x", due_date="2026-09-01")])
    briefing = core.cycle()
    assert briefing["overall_level"] == "critical"
    assert briefing["top_priorities"][0]["id"] == "JIRA:P-1"
    assert json.loads((tmp_path / "pmo-briefing.json").read_text(encoding="utf-8"))


# ===== CLI ==================================================================

def test_cli_assign_requires_apply_and_pmo_prints(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  members: [ann]\n", encoding="utf-8")
    ledger = tmp_path / "task-ledger.json"
    TaskEngine(ledger).ingest("t", "r", [cand(key="P-1", title="Do it")])

    assert cli.main(["--config", str(config), "assign"]) == 0
    assert "P-1" in capsys.readouterr().out

    assert cli.main(["--config", str(config), "assign", "P-1"]) == 1
    assert "--apply" in capsys.readouterr().err
    assert TaskEngine(ledger).tasks["JIRA:P-1"].assignee is None

    assert cli.main(["--config", str(config), "assign", "P-1", "--apply"]) == 0
    assert TaskEngine(ledger).tasks["JIRA:P-1"].assignee == "ann"

    assert cli.main(["--config", str(config), "pmo"]) == 0
    assert "top priorities" in capsys.readouterr().out
