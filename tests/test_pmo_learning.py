"""学習と高リスク時の自動起動のテスト / learning and auto-launch tests."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from aipmo import cli
from aipmo.pmo_core import Member, PmoCore, RuleError, load_responses
from aipmo.pmo_learning import learn
from aipmo.task_engine import TaskEngine, score_task

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


def outcome(who="ann", late=0, labels=()):
    return {"assignee": who, "late_days": late, "labels": list(labels)}


def decisions(tmp_path):
    lines = (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def kinds(tmp_path):
    return [d["kind"] for d in decisions(tmp_path)]


# ===== 学習 / learning ======================================================

def test_no_adjustment_below_minimum_samples():
    model = learn([outcome(late=5)] * 4)
    assert model.member_factor == {} and model.label_bonus == {}


def test_reliable_member_gains_and_slow_member_loses_capacity():
    outcomes = ([outcome("ann", 0)] * 10 + [outcome("bob", 3)] * 10
                + [outcome("cat", 0)] * 3)          # cat は標本不足
    model = learn(outcomes)
    assert model.baseline_late_rate == pytest.approx(10 / 23, abs=1e-3)
    assert model.member_factor["ann"] > 1 > model.member_factor["bob"]
    assert "cat" not in model.member_factor


def test_factors_are_clamped_and_shrunk():
    huge = learn([outcome("ann", 0)] * 200 + [outcome("bob", 9)] * 200)
    assert huge.member_factor["ann"] <= 1.5 and huge.member_factor["bob"] >= 0.5
    # チーム基準が同じ（遅延率 0.5）で、標本数だけが違う
    small = learn([outcome("ann", 0)] * 5 + [outcome("bob", 9)] * 5)
    big = learn([outcome("ann", 0)] * 50 + [outcome("bob", 9)] * 50)
    assert small.member_factor["ann"] < big.member_factor["ann"]   # 平均側へ縮小


def test_only_slower_than_average_labels_get_a_bonus_and_it_is_capped():
    outcomes = ([outcome("a", 5, ["db"])] * 30 + [outcome("b", 0, ["ui"])] * 30
                + [outcome("c", 0)] * 30)
    model = learn(outcomes)
    assert 0 < model.label_bonus["db"] <= 15
    assert "ui" not in model.label_bonus


def test_undated_outcomes_are_ignored_and_result_is_deterministic():
    outcomes = [outcome("ann", None)] * 20 + [outcome("ann", 0)] * 3
    assert learn(outcomes).samples == 3
    data = [outcome("ann", i % 3) for i in range(30)]
    assert learn(data).as_dict() == learn(list(data)).as_dict()


def test_ledger_records_outcome_when_completion_is_observed(tmp_path):
    clock = Clock()
    te = TaskEngine(tmp_path / "task-ledger.json", now=clock)
    te.ingest("t", "r1", [cand(key="P-1", title="a", assignee="ann",
                               due_date="2026-09-28", labels=["DB"])])
    clock.now = NOW + timedelta(days=1)
    te.ingest("t", "r2", [cand(key="P-1", title="a", done=True)])
    (o,) = te.outcomes
    assert o["assignee"] == "ann" and o["late_days"] == 3 and o["labels"] == ["DB"]
    # 最初から完了で現れたものは、観測していないので残さない
    te.ingest("t", "r3", [cand(key="P-2", title="b", done=True)])
    assert len(te.outcomes) == 1
    assert TaskEngine(tmp_path / "task-ledger.json").outcomes == te.outcomes


def test_learned_label_bonus_raises_rank_and_explains_itself(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.json", now=Clock())
    te.ingest("t", "r", [cand(key="P-1", title="a", labels=["x"]),
                         cand(key="P-2", title="b", labels=["db"])])
    assert te.ranked()[0].key == "P-1"        # 同点は id 順
    te.label_bonus = {"db": 10}
    te.refresh()
    top = te.ranked()[0]
    assert top.key == "P-2" and any("遅れやすい" in r for r in top.reasons)
    other = te.tasks["JIRA:P-1"]
    assert score_task(other, NOW.date(), {"db": 10})[0] == score_task(other, NOW.date())[0]


def test_core_applies_learned_capacity_and_logs_model_change(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.json", now=Clock())
    te.add_outcomes([outcome("ann", 0)] * 10 + [outcome("bob", 4)] * 10)
    core = PmoCore(task_engine=te, members=[Member("ann", capacity=4),
                                            Member("bob", capacity=4)])
    briefing = core.cycle()
    caps = {m["member"]: m["capacity"] for m in briefing["member_loads"]}
    assert caps["ann"] > 4 > caps["bob"]
    assert kinds(tmp_path).count("model_updated") == 1
    core.cycle()
    assert kinds(tmp_path).count("model_updated") == 1
    assert (tmp_path / "pmo-learned.json").exists()


def test_learning_can_be_switched_off(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.json", now=Clock())
    te.add_outcomes([outcome("ann", 0)] * 10 + [outcome("bob", 4)] * 10)
    core = PmoCore(task_engine=te, members=[Member("ann", capacity=4)], learning=False)
    assert core.cycle()["member_loads"][0]["capacity"] == 4


# ===== 自動起動 / auto-launch =================================================

def risky(tmp_path, clock=None, **kw):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.json", now=clock)
    te.ingest("t", "r", [cand(key="P-1", title="a", assignee="x", due_date="2026-09-01")])
    launched = []
    responses = load_responses([{"id": "replan", "template": "wbs_replan",
                                 "params": {"k": 1}, **kw}])
    core = PmoCore(task_engine=te, responses=responses, background=False,
                   launcher=lambda r, trig: launched.append((r.template, r.params, trig)))
    return te, core, clock, launched


def test_high_risk_launches_the_named_template_with_context(tmp_path):
    _, core, _, launched = risky(tmp_path)
    briefing = core.cycle()
    assert briefing["responses"] == [
        {"id": "replan", "template": "wbs_replan", "status": "launched"}]
    template, params, trigger = launched[0]
    assert template == "wbs_replan" and params == {"k": 1}
    assert trigger["type"] == "pmo_core" and trigger["overall_level"] == "critical"
    assert trigger["alerts"] and trigger["reasons"]
    assert "response_launched" in kinds(tmp_path)


def test_cooldown_daily_limit_and_condition(tmp_path):
    _, core, clock, launched = risky(tmp_path, cooldown_hours=1, max_per_day=2)
    core.cycle()
    assert core.cycle()["responses"][0]["status"] == "cooldown"
    clock.now = NOW + timedelta(hours=2)
    core.cycle()
    clock.now = NOW + timedelta(hours=4)
    assert core.cycle()["responses"][0]["status"] == "daily_limit"
    assert len(launched) == 2

    (tmp_path / "q").mkdir()
    _, quiet, _, none = risky(tmp_path / "q", min_level="critical", rules=["stalled"])
    assert quiet.cycle()["responses"][0]["status"] == "not_triggered" and none == []


def test_without_a_launcher_it_only_reports_what_would_launch(tmp_path):
    _, core, _, launched = risky(tmp_path)
    core.launcher = None
    assert core.cycle()["responses"][0]["status"] == "would_launch"
    assert launched == []


def test_failure_is_logged_and_does_not_break_the_cycle_or_retry_at_once(tmp_path):
    _, core, _, _ = risky(tmp_path)
    calls = []

    def boom(response, trigger):
        calls.append(1)
        raise RuntimeError("template exploded")

    core.launcher = boom
    core.cycle()
    assert core.cycle()["responses"][0]["status"] == "cooldown"
    assert len(calls) == 1
    assert "response_failed" in kinds(tmp_path)


def test_background_launch_does_not_overlap_itself(tmp_path):
    _, core, _, _ = risky(tmp_path, cooldown_hours=0, max_per_day=5)
    core.background = True
    gate, started = threading.Event(), threading.Event()

    def slow(response, trigger):
        started.set()
        gate.wait(5)

    core.launcher = slow
    core.cycle()
    assert started.wait(5)
    assert core.cycle()["responses"][0]["status"] == "running"
    gate.set()
    core.wait(5)


def test_bad_response_config_is_refused():
    for bad in ([{"id": "x"}], [{"template": "t"}],
                [{"id": "x", "template": "t", "min_level": "meh"}],
                [{"id": "x", "template": "t"}, {"id": "x", "template": "t"}],
                [{"id": "x", "template": "t", "max_per_day": 0}]):
        with pytest.raises(RuleError):
            load_responses(bad)


def test_cli_refuses_unknown_template_at_startup(tmp_path, capsys):
    (tmp_path / "templates").mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("pmo_core:\n  responses:\n    - {id: r, template: nope}\n",
                      encoding="utf-8")
    assert cli.main(["--config", str(config), "pmo"]) == 1
    assert "nope" in capsys.readouterr().err


def test_cli_wiring_really_runs_the_configured_template(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "replan.yaml").write_text(
        "name: replan\nsteps:\n  - id: mk\n    expression: count\n"
        "    inputs: {items: [1, 2]}\n", encoding="utf-8")
    config = {"adapters": {"mode": "mock"}, "web": {"templates_dir": "templates"},
              "pmo_core": {"responses": [{"id": "r", "template": "replan"}]}}
    engine = cli.build_engine(config, tmp_path)
    te = TaskEngine(tmp_path / "task-ledger.json", now=Clock())
    te.attach(engine)
    te.ingest("t", "r0", [cand(key="P-1", title="a", assignee="x", due_date="2026-09-01")])

    core = cli.build_pmo_core(config, te, engine, tmp_path)
    assert core.launcher is not None
    assert core.cycle()["responses"][0]["status"] == "launched"
    core.wait(10)
    assert "response_finished" in kinds(tmp_path)

    # 表示専用（engine を渡さない）なら起動しない
    passive = cli.build_pmo_core(config, te, None, tmp_path)
    assert passive.launcher is None
