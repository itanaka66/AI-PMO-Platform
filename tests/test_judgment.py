"""PMO Core による自律的な判断のテスト / autonomous judgment tests.

確かめるのは、(1) 診断が数えて決まること、(2) 対処の選択が効用と実績で決まること、
(3) **自律度の範囲を出ない**こと — 既定は通知と読み取りの再収集だけが auto、それ以外は
提案して人の承認を待つ、(4) 歯止め — 1 日の上限、遮断器、一時停止、(5) 効いたか効かなかったかを
学習し、効かなければ次の対処へ進むこと、(6) 判断の記録が仕事として扱われないこと。

(1) diagnosis is counted; (2) selection follows utility and track record; (3) it never leaves
its autonomy envelope — by default only notifying and a read-only re-collection are `auto`;
(4) guards: daily ceiling, circuit breaker, pause; (5) it learns whether a remedy worked and
moves on if not; (6) a judgment record is never treated as work.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aipmo import cli
from aipmo.judgment import (
    DEFAULT_AUTONOMY,
    LADDERS,
    Diagnosis,
    JudgmentError,
    candidates,
    diagnose,
    judgment_id,
    load_judgment,
    rationale,
    read_control,
    remedy_score,
    success_rate,
    write_control,
)
from aipmo.pmo_core import Member, PmoCore, scope_briefing
from aipmo.task_engine import Task, TaskEngine

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def cand(**kw):
    base = {"key": None, "title": "t", "assignee": None, "due_date": None,
            "priority": None, "status": None, "blocked": False, "done": False,
            "labels": [], "tracker": "", "external_id": ""}
    return {**base, **kw}


# =============================================================================
# 設定 / config
# =============================================================================

def test_no_section_means_no_judgment_and_defaults_are_conservative():
    assert load_judgment(None) is None
    cfg = load_judgment({})
    assert cfg.autonomy == DEFAULT_AUTONOMY
    # 既定で auto なのは、人への通知と、読み取りだけの再収集だけ
    assert {r for r, level in cfg.autonomy.items() if level == "auto"} == {"notify", "recollect"}
    assert cfg.autonomy["launch"] == cfg.autonomy["retry_agent"] == cfg.autonomy["followup"] == "propose"


@pytest.mark.parametrize("raw", [
    {"autonomy": {"explode": "auto"}}, {"autonomy": {"launch": "yolo"}},
    {"launch": [{"template": "t", "addresses": ["project_risk"]}]},
    {"launch": [{"id": "a", "template": "t"}]},
    {"launch": [{"id": "a", "template": "t", "addresses": ["nonsense"]}]},
    {"launch": [{"id": "a", "template": "t", "addresses": ["project_risk"]},
                {"id": "a", "template": "u", "addresses": ["project_risk"]}]},
    {"limits": {"max_auto_per_day": "many"}},
])
def test_bad_judgment_config_is_refused(raw):
    with pytest.raises(JudgmentError):
        load_judgment(raw)


# =============================================================================
# (1) 診断 / diagnosis
# =============================================================================

def task(title="t", assignee=None, score=0, reasons=(), dispatches=(), project="P", id_=None):
    return Task(id=id_ or f"JIRA:{title}", title=title, assignee=assignee, score=score,
                reasons=list(reasons), dispatches=list(dispatches), project=project)


def run_diagnose(briefing=None, active=(), **kw):
    base = {"overloaded_members": [], "projects": [], "alerts": [], "unassignable": [],
            "collection": None}
    return diagnose({**base, **(briefing or {})}, list(active), now=NOW,
                    has_members=kw.pop("has_members", True),
                    collect_interval_minutes=kw.pop("interval", None))


def test_overload_is_diagnosed_with_its_risky_tasks():
    active = [task("a", "ann", score=80), task("b", "ann", score=20), task("c", "ann", score=70)]
    (d,) = run_diagnose({"overloaded_members": [
        {"member": "ann", "load": 3, "capacity": 1}]}, active)
    assert d.kind == "overload" and d.subject == "ann" and d.fingerprint == "overload:ann"
    assert d.severity == 40 + 10 * 2 + 5 * 2                               # 超過 2 + 高リスク 2
    assert d.data["tasks"] == ["JIRA:a", "JIRA:c"] and "ann" in d.title


def test_a_project_is_diagnosed_only_when_high_or_critical_with_alerts():
    projects = [{"project": "A", "level": "critical", "alert_count": 2, "active_count": 5},
                {"project": "B", "level": "high", "alert_count": 1, "active_count": 5},
                {"project": "C", "level": "medium", "alert_count": 4, "active_count": 5},
                {"project": "D", "level": "high", "alert_count": 0, "active_count": 5}]
    alerts = [{"project": "A", "title": "遅れ", "message": "30 日超過"}]
    found = {d.subject: d for d in run_diagnose({"projects": projects, "alerts": alerts})}
    assert set(found) == {"A", "B"} and found["A"].severity > found["B"].severity
    assert found["A"].project == "A" and "30 日超過" in found["A"].evidence[0]


def test_a_role_ai_failure_counts_only_its_latest_run_while_still_assigned_to_it():
    failed = {"agent": "dev-ai", "status": "failed", "error": "boom"}
    ok = {"agent": "dev-ai", "status": "done"}
    active = [task("a", "dev-ai", dispatches=[failed]),
              task("b", "dev-ai", dispatches=[failed, ok]),                 # 直近は成功
              task("c", "sato", dispatches=[failed]),                       # 人に戻された
              task("d", "dev-ai", dispatches=[{"agent": "dev-ai", "status": "skipped"}])]
    (d,) = run_diagnose(active=active)
    assert d.kind == "agent_failure" and d.data["tasks"] == ["JIRA:a"]
    assert d.severity == 60


@pytest.mark.parametrize("collection,interval,expected", [
    ({"at": NOW.isoformat(), "sources": [{"error": None}], "failed": 0, "error": "down"}, None, True),
    ({"at": NOW.isoformat(), "sources": [{"error": "x"}, {"error": "y"}], "failed": 0}, None, True),
    ({"at": NOW.isoformat(), "sources": [{"error": "x"}, {"error": None}], "failed": 0}, None, False),
    ({"at": NOW.isoformat(), "sources": [], "failed": 3}, None, True),
    ({"at": (NOW - timedelta(hours=5)).isoformat(), "sources": [{"error": None}],
      "failed": 0}, 30, True),                                              # 3 倍の間隔を超えて静か
    ({"at": (NOW - timedelta(minutes=40)).isoformat(), "sources": [{"error": None}],
      "failed": 0}, 30, False),
    (None, 30, False),
])
def test_collection_trouble(collection, interval, expected):
    found = run_diagnose({"collection": collection}, interval=interval)
    assert bool(found) is expected and (not found or found[0].kind == "collection_failing")


def test_capacity_shortage_and_estimate_risk_need_enough_evidence():
    few = [{"task": str(i), "title": f"t{i}"} for i in range(2)]
    many = [{"task": str(i), "title": f"t{i}"} for i in range(4)]
    assert run_diagnose({"unassignable": few}) == []
    assert run_diagnose({"unassignable": many}, has_members=False) == []
    (d,) = run_diagnose({"unassignable": many})
    assert d.kind == "capacity_shortage" and d.severity == 60
    risky = [task(f"r{i}", reasons=["見積り 5 点 × 実績ペース 2.0 日/点 → あと約 10 日"]) for i in range(3)]
    assert [x.kind for x in run_diagnose(active=risky)] == ["estimate_risk"]
    assert run_diagnose(active=risky[:2]) == []


def test_diagnosis_is_deterministic():
    briefing = {"overloaded_members": [{"member": "ann", "load": 3, "capacity": 1}],
                "unassignable": [{"task": str(i), "title": "t"} for i in range(4)]}
    a = [d.as_dict() for d in run_diagnose(briefing)]
    b = [d.as_dict() for d in run_diagnose(json.loads(json.dumps(briefing)))]
    assert a == b


# =============================================================================
# (2) 対処の選択 / choosing a remedy
# =============================================================================

def diag(kind="project_risk", subject="P"):
    return Diagnosis(kind, subject, 80, f"{kind} {subject}", ("根拠",))


def test_the_remedies_are_ordered_by_utility_and_track_record_can_reverse_them():
    d = diag()
    assert candidates(d, {"launch", "followup"}, {}, {}) == ["launch", "followup"]
    worked = {"project_risk|followup": {"ok": 20, "bad": 0},
              "project_risk|launch": {"ok": 0, "bad": 20}}
    assert candidates(d, {"launch", "followup"}, {}, worked) == ["followup", "launch"]
    assert remedy_score("project_risk", "launch", worked) < remedy_score(
        "project_risk", "launch", {})
    assert success_rate({}, "x", "y") == 0.5 and success_rate(worked, "project_risk", "followup") > 0.9


def test_what_did_not_work_or_awaits_approval_is_not_chosen_again():
    d = diag()
    episode = {"executions": [{"remedy": "launch", "state": "executed", "ineffective": True},
                              {"remedy": "followup", "state": "proposed"}]}
    assert candidates(d, {"launch", "followup"}, episode, {}) == []
    fresh = {"executions": [{"remedy": "launch", "state": "executed"}]}      # 実行済みでまだ判定前
    assert candidates(d, {"launch", "followup"}, fresh, {}) == ["launch", "followup"]


def test_unavailable_remedies_are_skipped_and_every_diagnosis_has_a_ladder():
    assert candidates(diag(), {"followup"}, {}, {}) == ["followup"]
    assert candidates(diag(), set(), {}, {}) == []
    assert set(LADDERS) >= {"overload", "project_risk", "agent_failure", "collection_failing"}


def test_the_rationale_states_the_evidence_the_record_and_the_autonomy():
    text = rationale(diag(), "launch", {"project_risk|launch": {"ok": 2, "bad": 1}}, "propose")
    assert "根拠" in text and "効いた 2 回" in text and "効かなかった 1 回" in text
    assert "提案して人の承認を待つ" in text
    assert "まだ無い" in rationale(diag(), "launch", {}, "auto") and "実行する" in rationale(
        diag(), "launch", {}, "auto")


# =============================================================================
# コアへの組み込み / wired into the PMO Core
# =============================================================================

class Spy:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, text):
        self.messages.append(text)


class Launcher:
    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple[Any, dict]] = []
        self.fail = fail

    def __call__(self, response, trigger):
        self.calls.append((response, trigger))
        if self.fail:
            raise self.fail
        return SimpleNamespace(run_id=f"run-{len(self.calls)}", results={
            "x": SimpleNamespace(output={"answer": "実行しました"})})


class CollectorSpy:
    interval_minutes = 30

    def __init__(self, reports=None):
        self.calls = 0
        self.reports = list(reports or [])

    def run_once(self):
        self.calls += 1
        if self.reports:
            report = self.reports.pop(0)
            if isinstance(report, Exception):
                raise report
            return report
        return {"at": NOW.isoformat(), "sources": [{"id": "s", "adapter": "x", "items": 1,
                                                    "new": 0, "error": None}],
                "refreshed": 1, "failed": 0, "missing": [], "completed": 0}


def setup(tmp_path, judgment=None, members=None, tasks=None, clock=None, **kw):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    te.ingest("t", "r", tasks if tasks is not None else [])
    spy = kw.pop("notify", None)
    core = PmoCore(task_engine=te, members=members or [], background=False, acting=True,
                   judgment=load_judgment(judgment if judgment is not None else {}),
                   notify=spy, **kw)
    return te, core, clock


def overloaded_tasks(n=3):
    return [cand(key=f"A-{i}", title=f"仕事{i}", assignee="ann", project="A") for i in range(n)]


def judgments(te):
    return te.judgments()


def log_kinds(tmp_path):
    path = tmp_path / "pmo-decisions.jsonl"
    return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_nothing_happens_without_the_section_and_a_display_only_core_never_acts(tmp_path):
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock())
    te.ingest("t", "r", overloaded_tasks())
    plain = PmoCore(task_engine=te, members=[Member("ann", capacity=1)], acting=True)
    assert plain.cycle()["judgment"] is None

    spy = Spy()
    shown = PmoCore(task_engine=te, members=[Member("ann", capacity=1)], notify=spy,
                    judgment=load_judgment({}), acting=False).cycle()["judgment"]
    assert shown["acting"] is False and shown["diagnoses"][0]["kind"] == "overload"
    assert spy.messages == [] and te.judgments() == []


def test_a_diagnosis_notifies_people_and_proposes_but_does_not_act_by_default(tmp_path):
    spy = Spy()
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks(),
                        notify=spy)
    summary = core.cycle()["judgment"]
    assert [d["kind"] for d in summary["diagnoses"]] == ["overload"]
    assert any("[判断]" in m and "ann" in m for m in spy.messages)
    (record,) = judgments(te)
    assert record.proposed and record.payload["state"] == "proposed"
    assert record.payload["remedy"] == "followup" and record.payload["auto"] is False
    assert "自律度 propose" in record.payload["rationale"]
    assert any("承認待ち" in m and record.id in m for m in spy.messages)
    assert [t for t in te.tasks.values() if t.origin == "followup"] == []        # まだ何も作っていない
    assert summary["pending"] == 1


def test_a_judgment_is_made_once_per_episode_and_never_spams(tmp_path):
    spy = Spy()
    te, core, clock = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks(),
                            notify=spy)
    for _ in range(4):
        core.cycle()
        clock.now += timedelta(hours=1)
    assert len(judgments(te)) == 1 and len(spy.messages) == 2     # 診断の通知 + 承認待ちの案内
    clock.now = NOW + timedelta(hours=80)                         # 再通知の間隔(72h)を過ぎて
    core.cycle()
    assert len(spy.messages) == 3 and len(judgments(te)) == 1


def test_state_survives_a_restart_without_repeating_itself(tmp_path):
    spy = Spy()
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks(),
                        notify=spy)
    core.cycle()
    sent = len(spy.messages)
    restarted = PmoCore(task_engine=te, members=[Member("ann", capacity=1)], background=False,
                        acting=True, judgment=load_judgment({}), notify=spy)
    restarted.cycle()
    assert len(spy.messages) == sent and len(judgments(te)) == 1


def test_approving_a_proposal_does_not_run_it_until_the_resident_cycle(tmp_path):
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    (record,) = judgments(te)
    approved = core.decide_proposal(record.id, approve=True)
    assert approved.payload["state"] == "approved" and not approved.done
    assert [t for t in te.tasks.values() if t.origin == "followup"] == []          # まだ実行されない
    core.cycle()                                                                   # 常駐の次の周で
    done = te.find(record.id)
    assert done.payload["state"] == "executed" and done.done and done.status == "Executed"
    (work,) = [t for t in te.tasks.values() if t.origin == "followup"]
    assert work.title.startswith("対応を決める: ann の負荷") and not work.proposed
    assert work.id in [t.id for t in te.ranked()]                                  # 人の仕事になる
    kinds = log_kinds(tmp_path)
    assert "judgment_decided" in kinds and "judgment_executed" in kinds


def test_rejecting_means_it_is_not_proposed_again_in_this_episode(tmp_path):
    te, core, clock = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    core.decide_proposal(judgments(te)[0].id, approve=False)
    for _ in range(3):
        clock.now += timedelta(days=2)
        core.cycle()
    assert len(judgments(te)) == 1 and judgments(te)[0].payload["state"] == "rejected"
    assert "judgment_rejected" in log_kinds(tmp_path)
    assert core._jstate()["stats"] == {}                               # 却下は「効かなかった」ではない


def test_a_proposal_nobody_approved_is_never_executed_however_long_it_waits(tmp_path):
    te, core, clock = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    for _ in range(10):
        clock.now += timedelta(days=2)
        core.cycle()
    (record,) = judgments(te)
    assert record.proposed and record.payload["state"] == "proposed" and not record.done
    assert [t for t in te.tasks.values() if t.origin == "followup"] == []


def test_it_waits_for_the_first_remedy_to_show_its_effect_before_trying_the_next_rung(tmp_path):
    launcher = Launcher()
    te, core, clock = setup(
        tmp_path, {**RISK, "autonomy": {"launch": "auto", "followup": "auto"},
                   "limits": {"cooldown_hours": 24, "recheck_hours": 48}},
        members=[Member("ann", capacity=9)], tasks=risky_project_tasks(), launcher=launcher)
    core.cycle()
    assert [t.payload["remedy"] for t in judgments(te)] == ["launch"]
    for _ in range(4):                                         # 24 時間以内: 様子を見る
        clock.now += timedelta(hours=5)
        core.cycle()
    assert [t.payload["remedy"] for t in judgments(te)] == ["launch"]
    clock.now += timedelta(hours=40)                           # 待ちが明け、効かなかったと判定
    core.cycle()
    assert sorted(t.payload["remedy"] for t in judgments(te)) == ["followup", "launch"]


def test_a_rejected_remedy_gives_way_to_the_next_rung_after_the_wait(tmp_path):
    launcher = Launcher()
    te, core, clock = setup(tmp_path, RISK, members=[Member("ann", capacity=9)],
                            tasks=risky_project_tasks(), launcher=launcher)
    core.cycle()
    launch = next(t for t in judgments(te) if t.payload["remedy"] == "launch")
    core.decide_proposal(launch.id, approve=False)
    clock.now += timedelta(hours=25)
    core.cycle()
    remedies = sorted(t.payload["remedy"] for t in judgments(te))
    assert remedies == ["followup", "launch"]                  # 却下した launch は出し直さず、次の段へ
    assert launcher.calls == []
    clock.now += timedelta(days=3)
    core.cycle()
    assert sorted(t.payload["remedy"] for t in judgments(te)) == ["followup", "launch"]


def test_a_judgment_record_is_never_work(tmp_path):
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1), Member("bob")],
                        tasks=overloaded_tasks())
    core.cycle()
    (record,) = judgments(te)
    core.decide_proposal(record.id, approve=True)                       # 承認済み・未実行の間も
    assert record.id not in [t.id for t in te.ranked()]
    assert all(p["task"] != record.id for p in core.briefing(te.ranked(), [], NOW)["assignment_proposals"])
    with pytest.raises(ValueError, match="判断の記録"):
        te.complete(record.id)


# ---- 自律度 / autonomy ---------------------------------------------------------------

def test_a_remedy_set_to_auto_runs_at_once_and_is_recorded_as_automatic(tmp_path):
    te, core, _ = setup(tmp_path, {"autonomy": {"followup": "auto"}},
                        members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    summary = core.cycle()["judgment"]
    (record,) = judgments(te)
    assert record.payload["auto"] is True and record.payload["state"] == "executed" and record.done
    assert summary["actions"][-1]["level"] == "auto"
    assert len(core._jstate()["auto_log"]) == 1
    assert [t.origin for t in te.tasks.values() if t.origin == "followup"] == ["followup"]


def test_off_means_the_remedy_is_never_used(tmp_path):
    te, core, _ = setup(tmp_path, {"autonomy": {"followup": "off"}},
                        members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    assert judgments(te) == []


def test_notify_off_silences_people_but_proposals_still_appear(tmp_path):
    spy = Spy()
    te, core, _ = setup(tmp_path, {"autonomy": {"notify": "off"}},
                        members=[Member("ann", capacity=1)], tasks=overloaded_tasks(), notify=spy)
    core.cycle()
    assert len(judgments(te)) == 1
    assert not any(m.startswith("[判断]\n") or "根拠" in m for m in spy.messages)


def test_the_daily_ceiling_turns_auto_into_a_proposal(tmp_path):
    tasks = (overloaded_tasks() +
             [cand(key=f"B-{i}", title=f"別{i}", assignee="bob", project="B") for i in range(3)])
    te, core, _ = setup(tmp_path, {"autonomy": {"followup": "auto"},
                                   "limits": {"max_auto_per_day": 1}},
                        members=[Member("ann", capacity=1), Member("bob", capacity=1)],
                        tasks=tasks)
    core.cycle()
    states = sorted((t.payload["state"], t.payload["auto"]) for t in judgments(te))
    assert states == [("executed", True), ("proposed", False)]


# ---- 再収集（読み取りだけ・既定で auto）/ re-collection ---------------------------------------

def failing_report():
    return {"at": NOW.isoformat(), "sources": [{"id": "s", "adapter": "x", "items": 0, "new": 0,
                                                 "error": "boom"}],
            "refreshed": 0, "failed": 0, "missing": [], "completed": 0}


def test_a_failing_collection_is_retried_automatically_because_it_only_reads(tmp_path):
    collector = CollectorSpy([failing_report()])           # 最初の収集は失敗、2 回目は成功
    te, core, _ = setup(tmp_path, members=[Member("ann")], collector=collector)
    summary = core.cycle()["judgment"]
    assert collector.calls == 2                            # 通常の収集 + 判断による再収集
    (record,) = judgments(te)
    assert record.payload["remedy"] == "recollect" and record.payload["state"] == "executed"
    assert record.payload["auto"] is True
    assert any(a["remedy"] == "recollect" and a["level"] == "auto" for a in summary["actions"])


# ---- 役割AIの再実行 / retrying a role AI ---------------------------------------------------------

def agent_member():
    return Member("dev-ai", kind="agent", template="role_developer", skills=("dev",), capacity=2)


def failed_agent_task(failures=1):
    return [cand(key="PROJ-1", title="ログイン改善", labels=["dev"], assignee="dev-ai")], failures


def inject_failures(te, failures=1):
    with te.transaction():
        te.tasks["JIRA:PROJ-1"].dispatches[:] = [
            {"id": f"f{i}", "agent": "dev-ai", "template": "role_developer",
             "at": NOW.isoformat(), "status": "failed", "error": "boom"} for i in range(failures)]


def test_a_failed_role_ai_run_is_retried_only_when_the_operator_allowed_it(tmp_path):
    tasks, _ = failed_agent_task()
    launcher = Launcher()
    te, core, _ = setup(tmp_path, members=[agent_member()], tasks=tasks, launcher=launcher)
    inject_failures(te)
    core.cycle()
    assert launcher.calls == []                                   # 既定は propose。動かさない
    (record,) = judgments(te)
    assert record.payload["remedy"] == "retry_agent" and record.proposed

    launcher2 = Launcher()
    tmp2 = tmp_path / "auto"
    tmp2.mkdir()
    te2, core2, _ = setup(tmp2, {"autonomy": {"retry_agent": "auto"}}, members=[agent_member()],
                          tasks=tasks, launcher=launcher2)
    inject_failures(te2)
    core2.cycle()
    assert len(launcher2.calls) == 1
    assert launcher2.calls[0][0].template == "role_developer"
    assert te2.find("PROJ-1").dispatches[-1]["status"] == "done"
    assert judgments(te2)[0].payload["state"] == "executed"


def test_a_task_that_failed_twice_is_not_retried_a_third_time(tmp_path):
    tasks, _ = failed_agent_task()
    launcher = Launcher()
    te, core, _ = setup(tmp_path, {"autonomy": {"retry_agent": "auto"}}, members=[agent_member()],
                        tasks=tasks, launcher=launcher)
    inject_failures(te, failures=2)
    core.cycle()
    assert launcher.calls == []
    assert all(t.payload["remedy"] != "retry_agent" for t in judgments(te))     # 人に回す(対応タスクの提案)


# ---- テンプレートの起動 / launching an allow-listed template ----------------------------------------

RISK = {"launch": [{"id": "replan", "template": "wbs_replan", "addresses": ["project_risk"],
                    "params": {"project": "{project}"}, "max_per_day": 1}]}


def risky_project_tasks():
    return [cand(key=f"R-{i}", title=f"遅れ{i}", assignee="ann", project="R",
                 due_date="2026-08-01", status="In Progress") for i in range(2)]


def test_a_template_is_launched_only_if_listed_and_authorised_and_with_the_projects_context(tmp_path):
    launcher = Launcher()
    te, core, _ = setup(tmp_path, {**RISK, "autonomy": {"launch": "auto"}},
                        members=[Member("ann", capacity=9)], tasks=risky_project_tasks(),
                        launcher=launcher)
    core.cycle()
    (response, trigger) = launcher.calls[0]
    assert response.template == "wbs_replan" and response.params == {"project": "R"}
    assert trigger["type"] == "pmo_core_judgment" and trigger["diagnosis"]["kind"] == "project_risk"
    record = next(t for t in judgments(te) if t.payload["remedy"] == "launch")
    assert record.payload["state"] == "executed" and record.payload["result"]["run_id"] == "run-1"


def test_without_authorisation_launching_is_only_proposed(tmp_path):
    launcher = Launcher()
    te, core, _ = setup(tmp_path, RISK, members=[Member("ann", capacity=9)],
                        tasks=risky_project_tasks(), launcher=launcher)
    core.cycle()
    assert launcher.calls == []
    record = next(t for t in judgments(te) if t.payload["remedy"] == "launch")
    assert record.proposed
    core.decide_proposal(record.id, approve=True)
    core.cycle()
    assert len(launcher.calls) == 1                                  # 承認されて、はじめて動く


def test_a_template_that_is_not_listed_is_never_a_candidate(tmp_path):
    launcher = Launcher()
    te, core, _ = setup(tmp_path, {"autonomy": {"launch": "auto"}},     # launch の一覧が無い
                        members=[Member("ann", capacity=9)], tasks=risky_project_tasks(),
                        launcher=launcher)
    core.cycle()
    assert launcher.calls == []
    assert all(t.payload["remedy"] != "launch" for t in judgments(te))


def test_a_listed_template_respects_its_own_daily_limit(tmp_path):
    launcher = Launcher()
    tasks = risky_project_tasks() + [cand(key=f"S-{i}", title=f"遅れ{i}", assignee="ann",
                                           project="S", due_date="2026-08-01",
                                           status="In Progress") for i in range(2)]
    te, core, _ = setup(tmp_path, {**RISK, "autonomy": {"launch": "auto"}},
                        members=[Member("ann", capacity=9)], tasks=tasks, launcher=launcher)
    core.cycle()
    assert len(launcher.calls) == 1                                   # max_per_day: 1


# =============================================================================
# (5) 効いたか効かなかったかの学習 / learning whether it worked
# =============================================================================

def resolve_overload(te):
    with te.transaction():
        for task_ in te.tasks.values():
            task_.assignee = "bob"


def test_a_remedy_followed_by_the_diagnosis_going_away_is_credited(tmp_path):
    te, core, clock = setup(tmp_path, {"autonomy": {"followup": "auto"}},
                            members=[Member("ann", capacity=1), Member("bob", capacity=9)],
                            tasks=overloaded_tasks())
    core.cycle()
    resolve_overload(te)                                           # 負荷が解消した
    clock.now += timedelta(hours=2)
    core.cycle()
    assert core._jstate()["stats"]["overload|followup"] == {"ok": 1, "bad": 0}
    assert "judgment_resolved" in log_kinds(tmp_path)


def test_a_remedy_followed_by_a_persisting_diagnosis_is_marked_ineffective_and_the_ladder_moves_on(tmp_path):
    launcher = Launcher()
    tasks = risky_project_tasks()
    te, core, clock = setup(tmp_path, {**RISK, "autonomy": {"launch": "auto", "followup": "auto"},
                                       "limits": {"cooldown_hours": 24, "recheck_hours": 24}},
                            members=[Member("ann", capacity=9)], tasks=tasks, launcher=launcher)
    core.cycle()
    assert len(launcher.calls) == 1                                  # 1 手目: launch
    clock.now += timedelta(hours=30)                                 # 診断は続いたまま
    core.cycle()
    assert core._jstate()["stats"]["project_risk|launch"] == {"ok": 0, "bad": 1}
    assert "judgment_ineffective" in log_kinds(tmp_path)
    clock.now += timedelta(hours=1)
    core.cycle()
    remedies = [t.payload["remedy"] for t in judgments(te)]
    assert remedies.count("launch") == 1 and "followup" in remedies  # 2 手目へ進んだ。launch は繰り返さない


def test_it_waits_during_the_cooldown_to_see_whether_the_remedy_worked(tmp_path):
    te, core, clock = setup(tmp_path, {"autonomy": {"followup": "auto"}},
                            members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    for _ in range(5):
        clock.now += timedelta(hours=3)
        core.cycle()
    assert len(judgments(te)) == 1


def test_a_remedy_that_worked_before_is_preferred_next_time():
    d = diag("project_risk", "X")
    stats = {"project_risk|followup": {"ok": 8, "bad": 0}, "project_risk|launch": {"ok": 0, "bad": 8}}
    assert candidates(d, {"launch", "followup"}, {}, stats)[0] == "followup"


# =============================================================================
# (4) 歯止め / guards
# =============================================================================

def test_repeated_automatic_failures_trip_the_breaker_which_demotes_auto_to_a_proposal(tmp_path):
    spy = Spy()
    launcher = Launcher(fail=RuntimeError("template exploded"))
    cfg = {**RISK, "autonomy": {"launch": "auto"}, "circuit_breaker": {"failures": 2, "hours": 24},
           "limits": {"cooldown_hours": 0, "recheck_hours": 0}}
    tasks = [cand(key=f"{p}-{i}", title=f"遅れ{p}{i}", assignee="ann", project=p,
                  due_date="2026-08-01", status="In Progress")
             for p in "PQR" for i in range(2)]
    te, core, clock = setup(tmp_path, {**cfg, "launch": [
        {"id": "replan", "template": "wbs_replan", "addresses": ["project_risk"],
         "max_per_day": 9}]}, members=[Member("ann", capacity=99)], tasks=tasks,
        launcher=launcher, notify=spy)
    summary = core.cycle()["judgment"]
    assert summary["tripped"] is True
    assert len(launcher.calls) == 2                                # 2 回失敗して止まった。3 件目は動かない
    assert any("失敗が 2 回続いた" in m for m in spy.messages)
    states = sorted(t.payload["state"] for t in judgments(te) if t.payload["remedy"] == "launch")
    assert states == ["failed", "failed", "proposed"]               # 3 件目は提案に落ちた
    assert summary["autonomy"]["launch"] == "propose" and summary["autonomy"]["notify"] == "auto"
    assert "judgment_breaker_tripped" in log_kinds(tmp_path)


def test_the_breaker_can_be_reset_by_a_human_or_by_time(tmp_path):
    spy = Spy()
    launcher = Launcher(fail=RuntimeError("boom"))
    cfg = {"launch": [{"id": "r", "template": "t", "addresses": ["project_risk"], "max_per_day": 9}],
           "autonomy": {"launch": "auto"}, "circuit_breaker": {"failures": 1, "hours": 10},
           "limits": {"cooldown_hours": 0, "recheck_hours": 0}}
    tasks = [cand(key=f"{p}-{i}", title="遅れ", assignee="ann", project=p, due_date="2026-08-01",
                  status="In Progress") for p in "PQ" for i in range(2)]
    te, core, clock = setup(tmp_path, cfg, members=[Member("ann", capacity=99)], tasks=tasks,
                            launcher=launcher, notify=spy)
    assert core.cycle()["judgment"]["tripped"] is True
    write_control(core._control_path(), reset_at=(NOW + timedelta(minutes=1)).isoformat())
    clock.now = NOW + timedelta(minutes=2)
    launcher.fail = None
    summary = core.cycle()["judgment"]
    assert summary["tripped"] is False and "judgment_breaker_reset" in log_kinds(tmp_path)

    te2, core2, clock2 = setup(tmp_path / "t2" if (tmp_path / "t2").mkdir() is None else tmp_path,
                               cfg, members=[Member("ann", capacity=99)], tasks=tasks,
                               launcher=Launcher(fail=RuntimeError("x")), notify=Spy())
    assert core2.cycle()["judgment"]["tripped"] is True
    clock2.now = NOW + timedelta(hours=11)                          # 時間がたてば自動で戻る
    assert core2.cycle()["judgment"]["tripped"] is False


def test_pausing_stops_everything_except_diagnosing(tmp_path):
    spy = Spy()
    te, core, _ = setup(tmp_path, {"autonomy": {"followup": "auto"}},
                        members=[Member("ann", capacity=1)], tasks=overloaded_tasks(), notify=spy)
    write_control(core._control_path(), paused=True)
    summary = core.cycle()["judgment"]
    assert summary["paused"] is True and summary["diagnoses"][0]["kind"] == "overload"
    assert judgments(te) == [] and spy.messages == []
    write_control(core._control_path(), paused=False)
    core.cycle()
    assert len(judgments(te)) == 1 and spy.messages


def test_pausing_also_holds_back_proposals_a_human_already_approved(tmp_path):
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    core.decide_proposal(judgments(te)[0].id, approve=True)
    write_control(core._control_path(), paused=True)
    core.cycle()
    assert te.find(judgments(te)[0].id).payload["state"] == "approved"      # まだ実行されない
    write_control(core._control_path(), paused=False)
    core.cycle()
    assert te.find(judgments(te)[0].id).payload["state"] == "executed"


def test_a_diagnosis_below_the_threshold_is_ignored(tmp_path):
    te, core, _ = setup(tmp_path, {"min_severity": 90}, members=[Member("ann", capacity=1)],
                        tasks=overloaded_tasks())
    assert core.cycle()["judgment"]["diagnoses"] == [] and judgments(te) == []


def test_a_stuck_execution_is_closed_as_failed(tmp_path):
    te, core, clock = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks(),
                            agent_timeout_minutes=30)
    core.cycle()
    record = judgments(te)[0]
    with te.transaction():
        te.tasks[record.id].proposed = False
        te.tasks[record.id].payload.update(state="executing", started_at=NOW.isoformat())
    clock.now = NOW + timedelta(minutes=45)
    core.cycle()
    stuck = te.find(record.id)
    assert stuck.payload["state"] == "failed" and "時間切れ" in stuck.payload["result"]["error"]


def test_the_confined_viewer_does_not_see_the_cores_judgments():
    briefing = {"alerts": [], "assignment_proposals": [], "unassignable": [], "projects": [],
                "overall_level": "low", "generated": {"created": [], "pending": []},
                "judgment": {"diagnoses": [{"title": "org-wide"}]}}
    assert scope_briefing(briefing, [], {"a"}, redact_org=True)["judgment"] is None
    assert scope_briefing(briefing, [], {"a"}, redact_org=False)["judgment"] is not None


# =============================================================================
# CLI
# =============================================================================

def test_cli_pause_resume_and_reset_write_the_control_file(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    control = tmp_path / "pmo-judgment-control.json"
    assert cli.main(["--config", str(config), "judgment", "pause"]) == 0
    assert read_control(control)["paused"] is True
    assert cli.main(["--config", str(config), "judgment", "resume"]) == 0
    assert read_control(control)["paused"] is False
    assert cli.main(["--config", str(config), "judgment", "reset"]) == 0
    assert "reset_at" in read_control(control)
    capsys.readouterr()


def test_cli_status_shows_diagnoses_autonomy_and_records(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  members: [{name: ann, capacity: 1}]\n"
                      "  judgment: {autonomy: {followup: auto}}\n", encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", tenant="acme")
    te.ingest("t", "r", overloaded_tasks())
    te.close()

    assert cli.main(["--config", str(config), "judgment"]) == 0
    out = capsys.readouterr().out
    assert "ann の負荷が上限を超えています" in out and "対応タスクの作成=auto" in out
    assert "表示専用" in out                                         # 実行するのは常駐だけ
    assert TaskEngine(tmp_path / "task-ledger.db", tenant="acme").judgments() == []

    plain = tmp_path / "plain.yaml"
    plain.write_text("tenant: acme\n", encoding="utf-8")
    assert cli.main(["--config", str(plain), "judgment"]) == 0
    assert "設定されていません" in capsys.readouterr().out


def test_cli_rejects_bad_judgment_config_and_unknown_templates(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("tenant: acme\npmo_core:\n  judgment: {autonomy: {launch: yolo}}\n",
                   encoding="utf-8")
    assert cli.main(["--config", str(bad), "pmo"]) == 1
    assert "pmo_core.judgment" in capsys.readouterr().err

    (tmp_path / "templates").mkdir()
    missing = tmp_path / "missing.yaml"
    missing.write_text(
        "tenant: acme\nweb: {templates_dir: templates}\npmo_core:\n  judgment:\n    launch:\n"
        "      - {id: a, template: nonexistent_template, addresses: [project_risk]}\n",
        encoding="utf-8")
    assert cli.main(["--config", str(missing), "pmo"]) == 1
    assert "nonexistent_template" in capsys.readouterr().err


def test_cli_approving_a_judgment_marks_it_and_the_resident_cycle_executes_it(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  members: [{name: ann, capacity: 1}]\n"
                      "  judgment: {}\n", encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", tenant="acme", now=Clock())
    te.ingest("t", "r", overloaded_tasks())
    resident = PmoCore(task_engine=te, members=[Member("ann", capacity=1)], background=False,
                       acting=True, judgment=load_judgment({}))
    resident.cycle()
    (record,) = te.judgments()
    assert cli.main(["--config", str(config), "generated", "approve", record.id]) == 0
    capsys.readouterr()
    assert te.find(record.id).payload["state"] == "approved"           # CLI は印を付けるだけ
    resident.cycle()
    assert te.find(record.id).payload["state"] == "executed"


# =============================================================================
# Web
# =============================================================================

def test_the_web_screen_can_approve_a_judgment_but_never_executes_it(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aipmo.adapters.base import AdapterRegistry
    from aipmo.engine.runner import Engine
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry
    from aipmo.web.server import RunStore, create_app

    te = TaskEngine(tmp_path / "task-ledger.db", tenant="acme", now=Clock())
    te.ingest("t", "r", overloaded_tasks())
    resident = PmoCore(task_engine=te, members=[Member("ann", capacity=1)], background=False,
                       acting=True, judgment=load_judgment({}))
    resident.cycle()
    (record,) = te.judgments()
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", "op-token-123",
                     viewer_token="view-token-456", lang="en", store=RunStore(), tenant="acme",
                     pmo_ledger=tmp_path / "task-ledger.db")
    client = TestClient(app)
    body = client.get("/api/pmo", headers={"x-aipmo-token": "view-token-456"}).json()
    assert record.id in [p["id"] for p in body["proposals"]]            # 承認待ちの欄に出る
    assert body["briefing"]["judgment"]["diagnoses"][0]["kind"] == "overload"
    done = client.post("/api/pmo/proposals/decide", json={"ref": record.id, "decision": "approve"},
                       headers={"x-aipmo-token": "op-token-123"})
    assert done.status_code == 200
    stored = TaskEngine(tmp_path / "task-ledger.db", tenant="acme").find(record.id)
    assert stored.payload["state"] == "approved" and not stored.done     # 画面は実行しない

    js = (Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static" / "app.js"
          ).read_text(encoding="utf-8")
    assert "judgment" in js and "innerHTML" not in js


# ---- 失敗した対処のやり直し・記録の表示 / retrying failures, listing records -------------------

def test_a_failed_remedy_is_retried_up_to_the_limit_then_the_ladder_moves_on():
    d = diag("collection_failing", "collection")
    failed = [{"remedy": "recollect", "state": "failed"}] * 2
    assert candidates(d, {"recollect", "followup"}, {"executions": failed}, {}, 3)[0] == "recollect"
    spent = {"executions": failed + [{"remedy": "recollect", "state": "failed"}]}
    assert candidates(d, {"recollect", "followup"}, spent, {}, 3) == ["followup"]
    forgiven = {"executions": [{"remedy": "recollect", "state": "failed", "forgiven": True}] * 3}
    assert candidates(d, {"recollect", "followup"}, forgiven, {}, 3)[0] == "recollect"
    assert judgment_id("a:b", "recollect", "2026-01-01T00:00:00") != judgment_id(
        "a:b", "recollect", "2026-01-01T00:00:00", 2)


def test_a_collection_that_keeps_failing_is_retried_each_cycle_then_the_breaker_trips(tmp_path):
    collector = CollectorSpy([failing_report() for _ in range(40)])
    cfg = {"limits": {"cooldown_hours": 0, "recheck_hours": 0},
           "circuit_breaker": {"failures": 3, "hours": 24}}
    te, core, clock = setup(tmp_path, cfg, members=[Member("ann")], collector=collector)
    for _ in range(4):
        core.cycle()
        clock.now = clock.now + timedelta(minutes=31)
    records = [t for t in judgments(te) if t.payload["remedy"] == "recollect"]
    assert sorted(t.payload["state"] for t in records) == ["failed", "failed", "failed"]
    assert "judgment_breaker_tripped" in log_kinds(tmp_path)

    # 人が戻して、原因が直ったら、過去の失敗は数えず再収集で解消する。
    write_control(core._control_path(), reset_at=(clock.now + timedelta(minutes=1)).isoformat())
    collector.reports.clear()
    real_run = collector.run_once

    def stamped():
        report = real_run()
        report["at"] = clock.now.isoformat()
        return report

    collector.run_once = stamped
    clock.now = clock.now + timedelta(minutes=40)
    core.cycle()
    clock.now = clock.now + timedelta(minutes=31)
    summary = core.cycle()["judgment"]
    assert summary["diagnoses"] == [] and summary["tripped"] is False
    assert "judgment_resolved" in log_kinds(tmp_path)


def test_a_display_only_core_lists_the_recent_judgments_and_pending_count(tmp_path):
    te, core, _ = setup(tmp_path, members=[Member("ann", capacity=1)], tasks=overloaded_tasks())
    core.cycle()
    shown = PmoCore(task_engine=te, members=[Member("ann", capacity=1)],
                    judgment=load_judgment({}), acting=False).cycle()["judgment"]
    assert shown["recent"] and shown["pending"] == 1
