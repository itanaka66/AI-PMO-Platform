"""役割AIと Task Engine の接続のテスト / role AIs connected to the Task Engine.

確かめるのは、(1) 役割AIが担当候補として正しく選ばれること（スキル一致のみ・人が先）、
(2) 人が確定した（または運用者が自動確定を許した）タスクだけが、役割のテンプレートに
正しい引数で任される・一度きり、(3) 結果が台帳に残り、失敗・不適合・時間切れは警告になって
人に回ること、(4) 本物の `role_developer` テンプレートまで通ること。

(1) role AIs are picked correctly (skill match only, humans first); (2) only work a
human confirmed — or the operator allowed to auto-confirm — is handed over, with the
right parameters and only once; (3) the result lands in the ledger, and a failure, a
misfit or a timeout becomes an alert for a human; (4) it works through the real
`role_developer` template.
"""
from __future__ import annotations

import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.adapters.mock import MockSlackAdapter
from aipmo.agent_roles import (
    EXCERPT_CHARS,
    ROLE_PRESETS,
    excerpt_of,
    fit,
    params_for,
    rejection_rate,
)
from aipmo.dsl import loader
from aipmo.engine.runner import Engine, PromptLibrary
from aipmo.llm.base import EchoProvider, LLMResponse
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import (
    Member,
    PmoCore,
    RuleError,
    load_members,
    member_loads,
    scope_briefing,
    suggest_assignee,
)
from aipmo.task_engine import Task, TaskEngine, extract_candidates
from aipmo.writeback import make_writer

ROOT = Path(__file__).resolve().parents[1]
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


def dev_ai(**kw) -> Member:
    base = dict(name="dev-ai", kind="agent", template="role_developer",
                skills=("dev",), capacity=2)
    return Member(**{**base, **kw})


def result(answer: str = "調べた結果です", run_id: str = "run-1"):
    return SimpleNamespace(run_id=run_id, results={
        "x": SimpleNamespace(output={"answer": answer, "iterations": 1})})


class Launcher:
    def __init__(self, answer: str = "調べた結果です", fail: Exception | None = None):
        self.calls: list[tuple[Any, dict]] = []
        self.answer, self.fail = answer, fail

    def __call__(self, response, trigger):
        self.calls.append((response, trigger))
        if self.fail:
            raise self.fail
        return result(self.answer, f"run-{len(self.calls)}")


def make(tmp_path, members, launcher=None, clock=None, tasks=None, **kw):
    clock = clock or Clock()
    te = TaskEngine(tmp_path / "task-ledger.db", now=clock)
    te.ingest("t", "r", tasks if tasks is not None else [
        cand(key="PROJ-1", title="ログイン改善", labels=["dev"], priority="High")])
    launcher = launcher if launcher is not None else Launcher()
    core = PmoCore(task_engine=te, members=members, launcher=launcher,
                   background=False, **kw)
    return te, core, launcher, clock


def confirm(core: PmoCore, ref: str = "PROJ-1") -> None:
    core.accept_assignment(ref)


def entries(te: TaskEngine, task_id: str = "JIRA:PROJ-1") -> list[dict]:
    te.sync()
    return te.tasks[task_id].dispatches


# ===== 設定 / config ============================================================

def test_role_ais_are_members_with_a_kind_and_a_template():
    (ai,) = load_members([{
        "name": "dev-ai", "kind": "agent", "template": "role_developer",
        "skills": ["Dev"], "capacity": 3, "auto_confirm": True, "prefer": True,
        "params": {"issue_key": "{external_id}"}, "trackers": ["jira"]}])
    assert ai.is_agent and ai.template == "role_developer" and ai.skills == ("dev",)
    assert ai.auto_confirm and ai.prefer and ai.params == (("issue_key", "{external_id}"),)
    assert ai.trackers == ("jira",)
    (human,) = load_members(["sato"])
    assert not human.is_agent and human.kind == "human"


@pytest.mark.parametrize("raw", [
    {"name": "x", "kind": "agent"},                       # template が無い
    {"name": "x", "kind": "robot", "template": "t"},      # 知らない種類
])
def test_bad_role_ai_config_is_refused(raw):
    with pytest.raises(RuleError):
        load_members([raw])


# ===== (1) 担当候補としての選ばれ方 / how a role AI is picked =======================

def task_with(labels, **kw) -> Task:
    return Task(id="JIRA:A-1", key="A-1", title="t", labels=list(labels), **kw)


def test_a_role_ai_is_a_candidate_only_on_a_skill_match():
    members = [Member("sato", capacity=1), dev_ai()]
    loads = {"sato": 1, "dev-ai": 0}                      # 人は満杯
    assert suggest_assignee(task_with(["dev"]), members, loads)[0].name == "dev-ai"
    assert suggest_assignee(task_with(["ops"]), members, loads) is None   # 合わなければ引き受けない
    assert suggest_assignee(task_with([]), members, loads) is None


def test_humans_come_first_unless_the_role_ai_is_preferred():
    sato = Member("sato", skills=("dev",))
    loads = {"sato": 3, "dev-ai": 0}                      # 人の負荷の方が高くても
    assert suggest_assignee(task_with(["dev"]), [sato, dev_ai()], loads)[0].name == "sato"
    chosen = suggest_assignee(task_with(["dev"]), [sato, dev_ai(prefer=True)], loads)[0]
    assert chosen.name == "dev-ai"


def test_without_role_ais_the_choice_is_exactly_what_it_was():
    members = [Member("ann", capacity=3), Member("bob", capacity=3)]
    assert suggest_assignee(task_with([]), members, {"ann": 1, "bob": 0})[0].name == "bob"


def test_among_equally_loaded_role_ais_the_one_rejected_less_often_wins():
    """差し戻し率が低い役割AIが先に選ばれる（6.15）。人には適用されない。"""
    low_rejection = dev_ai(name="careful-ai")
    high_rejection = dev_ai(name="sloppy-ai")
    loads = {"careful-ai": 0, "sloppy-ai": 0}
    tally = {"careful-ai": {"accepted": 9, "rejected": 1},
             "sloppy-ai": {"accepted": 1, "rejected": 9}}

    chosen = suggest_assignee(task_with(["dev"]), [high_rejection, low_rejection],
                              loads, tally)[0]

    assert chosen.name == "careful-ai"


def test_rejection_rate_is_only_a_tiebreak_after_load():
    """負荷が先。差し戻し率は、負荷が同じ役割AIどうしでだけ効く。"""
    busier_but_reliable = dev_ai(name="careful-ai", capacity=1)
    freer_but_rejected = dev_ai(name="sloppy-ai", capacity=1)
    loads = {"careful-ai": 1, "sloppy-ai": 0}        # careful-ai は満杯
    tally = {"careful-ai": {"accepted": 10, "rejected": 0},
             "sloppy-ai": {"accepted": 0, "rejected": 10}}

    chosen = suggest_assignee(task_with(["dev"]), [busier_but_reliable, freer_but_rejected],
                              loads, tally)[0]

    assert chosen.name == "sloppy-ai"            # 空きが無いので差し戻し率は無関係


def test_a_role_ai_with_no_review_history_ranks_as_if_perfect():
    never_reviewed = dev_ai(name="new-ai")
    some_rejections = dev_ai(name="veteran-ai")
    loads = {"new-ai": 0, "veteran-ai": 0}
    tally = {"veteran-ai": {"accepted": 5, "rejected": 1}}   # new-ai not in tally at all

    chosen = suggest_assignee(task_with(["dev"]), [some_rejections, never_reviewed],
                              loads, tally)[0]

    assert chosen.name == "new-ai"


def test_a_role_ais_load_is_what_is_running_not_what_it_finished():
    ai = dev_ai(capacity=1)
    running = Task(id="1", title="a", assignee="dev-ai",
                   dispatches=[{"agent": "dev-ai", "status": "running"}])
    done = Task(id="2", title="b", assignee="dev-ai",
                dispatches=[{"agent": "dev-ai", "status": "done"}])
    waiting = Task(id="3", title="c", assignee="dev-ai")
    assert member_loads([running, done, waiting], [ai]) == {"dev-ai": 2}


# ===== (2) 確定したものだけを、正しい引数で、一度きり / only confirmed, once ==============

def test_a_proposal_is_not_run_until_a_human_confirms_it(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()])
    briefing = core.cycle()
    assert [p["assignee"] for p in briefing["assignment_proposals"]] == ["dev-ai"]
    assert launcher.calls == [] and briefing["agent_dispatch"] == []     # 提案だけ

    confirm(core)                                                       # 人が確定
    briefing = core.cycle()
    assert len(launcher.calls) == 1
    assert briefing["agent_dispatch"] == [
        {"task": "JIRA:PROJ-1", "agent": "dev-ai", "status": "started",
         "dispatch": entries(te)[0]["id"]}]


def test_the_template_gets_the_tasks_fields_as_parameters(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()])
    core.cycle()
    confirm(core)
    core.cycle()
    response, trigger = launcher.calls[0]
    assert response.template == "role_developer"
    assert response.params == {"issue_key": "PROJ-1", "jira_project": "PROJ"}
    assert trigger["type"] == "pmo_core_agent" and trigger["agent"] == "dev-ai"
    assert trigger["task"]["id"] == "JIRA:PROJ-1" and trigger["task"]["title"] == "ログイン改善"


def test_one_hand_over_per_task_and_the_result_is_kept(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()], launcher=Launcher("方針はこうです"))
    core.cycle()
    confirm(core)
    core.cycle()
    core.cycle()
    core.cycle()
    assert len(launcher.calls) == 1                                     # 何周しても一度きり
    (entry,) = entries(te)
    assert entry["status"] == "done" and entry["run_id"] == "run-1"
    assert entry["excerpt"] == "方針はこうです" and entry["finished_at"]
    assert entry["template"] == "role_developer" and entry["agent"] == "dev-ai"


def test_auto_confirm_skips_the_human_only_where_the_operator_allowed_it(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai(auto_confirm=True)])
    core.cycle()
    assert te.find("PROJ-1").assignee == "dev-ai" and te.find("PROJ-1").suggested_assignee is None
    assert len(launcher.calls) == 1
    log = (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")
    assert "assignment_auto_confirmed" in log and "agent_dispatched" in log


def test_a_task_assigned_to_a_human_is_never_handed_to_a_role_ai(tmp_path):
    te, core, launcher, _ = make(tmp_path, [Member("sato", skills=("dev",)), dev_ai()],
                                 tasks=[cand(key="PROJ-1", title="x", labels=["dev"],
                                             assignee="sato")])
    core.cycle()
    assert launcher.calls == []


def test_a_role_ai_never_becomes_the_trackers_assignee(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()])
    core.cycle()

    class Jira(Adapter):
        name = "jira"
        calls: list[Any] = []

        @action(writes=True)
        def update_issue(self, issue_key: str, assignee: str | None = None) -> dict[str, Any]:
            Jira.calls.append(assignee)
            return {}

    adapters = AdapterRegistry()
    adapters.register(Jira())
    core.accept_assignment("PROJ-1", write=make_writer(adapters, [dev_ai()]))
    assert Jira.calls == [] and te.find("PROJ-1").assignee == "dev-ai"
    assert core.last_writeback == {"tracker": None, "skipped": "agent"}


# ===== (3) 向かない・失敗・時間切れ / misfit, failure, timeout ========================

def test_a_task_that_does_not_fit_the_role_is_not_run_and_becomes_an_alert(tmp_path):
    github_task = extract_candidates(
        {"items": [{"number": 7, "title": "GH の課題", "labels": ["dev"]}]}, "github_projects")
    te, core, launcher, _ = make(tmp_path, [dev_ai()], tasks=github_task)
    core.cycle()
    confirm(core, "GH:7")
    briefing = core.cycle()
    assert launcher.calls == []
    (entry,) = entries(te, "GH:7")
    assert entry["status"] == "skipped" and "jira" in entry["error"]
    assert [a["rule"] for a in briefing["alerts"]] == ["agent_attention"]
    assert "人が引き取って" in briefing["alerts"][0]["message"]


def test_a_task_missing_a_required_field_is_skipped_with_the_reason(tmp_path):
    te, core, launcher, _ = make(
        tmp_path, [dev_ai(trackers=())], tasks=[cand(title="キーの無いタスク", labels=["dev"])])
    core.cycle()
    (task,) = te.ranked()
    confirm(core, task.id)
    core.cycle()
    assert launcher.calls == []
    (entry,) = entries(te, task.id)
    assert entry["status"] == "skipped"
    assert "宛先不明" in entry["error"] or "必要な" in entry["error"]


def test_a_failure_is_recorded_not_retried_and_raised_to_a_human(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()],
                                 launcher=Launcher(fail=RuntimeError("template exploded")))
    core.cycle()
    confirm(core)
    briefing = core.cycle()
    core.cycle()
    assert len(launcher.calls) == 1                                     # 自動では再試行しない
    (entry,) = entries(te)
    assert entry["status"] == "failed" and "template exploded" in entry["error"]
    assert "agent_attention" in [a["rule"] for a in core.cycle()["alerts"]]
    log = (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")
    assert "agent_failed" in log and briefing is not None


def test_a_human_can_retry_with_dispatch_now(tmp_path):
    launcher = Launcher(fail=RuntimeError("first time bad"))
    te, core, _, _ = make(tmp_path, [dev_ai()], launcher=launcher)
    core.cycle()
    confirm(core)
    core.cycle()
    launcher.fail = None
    outcome = core.dispatch_now("PROJ-1")
    assert outcome["status"] == "started" and outcome["latest"]["status"] == "done"
    assert [e["status"] for e in entries(te)] == ["failed", "done"]     # 履歴が残る
    assert core.cycle()["alerts"] == []                                 # 直近は成功 → 警告は消える


def test_a_rejected_result_s_note_is_passed_to_the_retry(tmp_path):
    """差し戻しの理由が、再試行の引数（review_feedback）に乗る（6.15）。"""
    te, core, launcher, _ = make(tmp_path, [dev_ai()])
    core.cycle()
    confirm(core)
    core.cycle()
    core.review_dispatch("PROJ-1", "rejected", "sato", "テストが無い")

    core.dispatch_now("PROJ-1")

    assert launcher.calls[0][0].params.get("review_feedback") is None
    assert launcher.calls[1][0].params["review_feedback"] == "テストが無い"


def test_an_accepted_result_leaves_no_review_feedback_on_the_next_run(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()])
    core.cycle()
    confirm(core)
    core.cycle()
    core.review_dispatch("PROJ-1", "accepted", "sato")

    core.dispatch_now("PROJ-1")

    assert "review_feedback" not in launcher.calls[1][0].params


def test_dispatch_now_refuses_what_is_not_assigned_to_a_role_ai(tmp_path):
    te, core, _, _ = make(
        tmp_path, [Member("sato", skills=("dev",)), dev_ai()],
        tasks=[cand(key="PROJ-1", title="x", labels=["dev"], assignee="sato"),
               cand(key="PROJ-2", title="y", labels=["dev"], assignee="dev-ai")])
    with pytest.raises(ValueError, match="役割AI"):
        core.dispatch_now("PROJ-1")                       # 人のタスク
    with pytest.raises(KeyError):
        core.dispatch_now("NOPE-9")
    core.launcher = None
    with pytest.raises(RuntimeError, match="起動"):
        core.dispatch_now("PROJ-2")                       # 役割AIのタスクでも、起動手段が無い


def test_a_run_that_never_comes_back_is_timed_out(tmp_path):
    clock = Clock()
    te, core, launcher, _ = make(tmp_path, [dev_ai()], clock=clock, agent_timeout_minutes=30)
    core.cycle()
    confirm(core)
    with te.transaction():                       # 実行中のまま戻らなかった記録
        te.tasks["JIRA:PROJ-1"].dispatches.append(
            {"id": "stuck", "agent": "dev-ai", "template": "role_developer",
             "at": clock.now.isoformat(), "status": "running"})
    clock.now = NOW + timedelta(minutes=45)
    briefing = core.cycle()
    (entry,) = entries(te)
    assert entry["status"] == "abandoned" and "30 分" in entry["error"]
    assert "agent_attention" in [a["rule"] for a in briefing["alerts"]]
    assert launcher.calls == []                                         # 任せ直しもしない


def test_a_run_that_is_still_alive_is_not_timed_out(tmp_path):
    clock = Clock()
    te, core, _, _ = make(tmp_path, [dev_ai()], clock=clock, agent_timeout_minutes=30)
    core.cycle()
    confirm(core)
    with te.transaction():
        te.tasks["JIRA:PROJ-1"].dispatches.append(
            {"id": "alive", "agent": "dev-ai", "template": "role_developer",
             "at": clock.now.isoformat(), "status": "running"})
    core._dispatching.add("JIRA:PROJ-1")
    clock.now = NOW + timedelta(minutes=45)
    core.cycle()
    assert entries(te)[0]["status"] == "running"


# ===== 上限 / limits ===============================================================

def test_the_role_ais_parallel_slots_are_honoured_and_work_waits_its_turn(tmp_path):
    release = threading.Event()
    started: list[str] = []

    def slow(response, trigger):
        started.append(trigger["task"]["id"])
        release.wait(5)
        return result("done")

    te, core, _, _ = make(
        tmp_path, [dev_ai(capacity=1)], launcher=slow,
        tasks=[cand(key="PROJ-1", title="一つ目", labels=["dev"], priority="High"),
               cand(key="PROJ-2", title="二つ目", labels=["dev"])])
    core.background = True
    core.cycle()
    for ref in ("PROJ-1", "PROJ-2"):
        te.sync()
        te.find(ref)
    # 提案は枠の分（1 件）だけ。1 件目を確定して走らせる
    first = next(t.id for t in te.ranked() if t.suggested_assignee)
    confirm(core, first)
    briefing = core.cycle()
    assert [d["status"] for d in briefing["agent_dispatch"]] == ["started"]
    assert len(started) == 1 and core.cycle()["agents"][0]["running"] == 1
    release.set()
    core.wait(5)


def test_the_daily_ceiling_per_role_ai(tmp_path):
    te, core, launcher, _ = make(
        tmp_path, [dev_ai(capacity=5)], agent_max_per_day=1,
        tasks=[cand(key="PROJ-1", title="a", labels=["dev"], assignee="dev-ai"),
               cand(key="PROJ-2", title="b", labels=["dev"], assignee="dev-ai")])
    briefing = core.cycle()
    assert sorted(d["status"] for d in briefing["agent_dispatch"]) == ["daily_limit", "started"]
    assert len(launcher.calls) == 1
    assert core.cycle()["agent_dispatch"][0]["status"] == "daily_limit"


def test_a_display_only_core_reports_but_never_launches(tmp_path):
    te, core, launcher, _ = make(tmp_path, [dev_ai()],
                                 tasks=[cand(key="PROJ-1", title="x", labels=["dev"],
                                             assignee="dev-ai")])
    core.launcher = None
    briefing = core.cycle()
    assert briefing["agent_dispatch"][0]["status"] == "would_dispatch"
    assert entries(te) == [] and launcher.calls == []


def test_a_background_run_does_not_block_the_cycle(tmp_path):
    release, started = threading.Event(), threading.Event()

    def slow(response, trigger):
        started.set()
        release.wait(5)
        return result("後で終わる")

    te, core, _, _ = make(tmp_path, [dev_ai()], launcher=slow,
                          tasks=[cand(key="PROJ-1", title="x", labels=["dev"],
                                      assignee="dev-ai")])
    core.background = True
    core.cycle()                                              # 戻ってくる（待たない）
    assert started.wait(5)
    assert entries(te)[0]["status"] == "running"
    release.set()
    core.wait(5)
    (entry,) = entries(te)
    assert entry["status"] == "done" and entry["excerpt"] == "後で終わる"


# ===== 結果の要約 / the excerpt =======================================================

def test_the_excerpt_is_the_agent_answer_and_is_bounded():
    assert excerpt_of(result("  要約  ")) == "要約"
    long = excerpt_of(result("あ" * (EXCERPT_CHARS + 50)))
    assert len(long) == EXCERPT_CHARS + 1 and long.endswith("…")
    assert excerpt_of(SimpleNamespace(results={"s": SimpleNamespace(output="plain text")})) == ""
    assert excerpt_of(None) == ""


def test_braces_in_a_title_cannot_break_the_parameters():
    ai = dev_ai(template="role_researcher", params=(("question", "調べて: {title}"),))
    task = Task(id="T:x", title="{ブレース} と {key} を含む", key="")
    assert params_for(ai, task)["question"] == "調べて: {ブレース} と {key} を含む"
    assert fit(ai, task)[0] is True


def test_params_for_adds_review_feedback_only_when_given():
    ai = dev_ai(template="role_researcher", params=(("question", "{title}"),))
    task = Task(id="T:x", title="t", key="")
    assert "review_feedback" not in params_for(ai, task)
    assert "review_feedback" not in params_for(ai, task, review_note="")
    assert params_for(ai, task, review_note="テストが無い")["review_feedback"] == "テストが無い"


def test_rejection_rate_is_zero_with_no_history_and_otherwise_a_ratio():
    assert rejection_rate("dev-ai", {}) == 0.0
    assert rejection_rate("dev-ai", {"dev-ai": {"accepted": 3, "rejected": 1}}) == 0.25
    assert rejection_rate("dev-ai", {"dev-ai": {"accepted": 0, "rejected": 0}}) == 0.0
    assert rejection_rate("other-ai", {"dev-ai": {"accepted": 0, "rejected": 5}}) == 0.0


# ===== 見え方 / visibility ===============================================================

def test_the_briefing_summarises_role_ais_and_their_runs(tmp_path):
    te, core, _, _ = make(tmp_path, [dev_ai()],
                          tasks=[cand(key="PROJ-1", title="a", labels=["dev"], assignee="dev-ai"),
                                 cand(key="PROJ-2", title="b", labels=["dev"], assignee="dev-ai")])
    briefing = core.cycle()
    (summary,) = briefing["agents"]
    assert summary["member"] == "dev-ai" and summary["template"] == "role_developer"
    assert summary["dispatched_today"] == 2 and summary["capacity"] == 2
    assert {r["task"] for r in briefing["agent_runs"]} == {"JIRA:PROJ-1", "JIRA:PROJ-2"}
    assert all(r["status"] == "done" for r in briefing["agent_runs"])


def test_a_confined_viewer_does_not_see_role_ais_or_other_projects_runs(tmp_path):
    te, core, _, _ = make(tmp_path, [dev_ai()],
                          tasks=[cand(key="AAA-1", title="a", labels=["dev"], assignee="dev-ai"),
                                 cand(key="BBB-1", title="b", labels=["dev"], assignee="dev-ai")])
    briefing = core.cycle()
    scoped = scope_briefing(briefing, te.ranked(projects=["aaa"]), {"aaa"}, redact_org=True)
    assert scoped["agents"] == [] and scoped["agent_dispatch"] == []
    assert [r["project"] for r in scoped["agent_runs"]] == ["AAA"]


def test_every_built_in_roles_parameters_exist_in_its_template():
    """役割ごとの既定の対応が、テンプレートの引数名と食い違っていないこと。"""
    for name, preset in ROLE_PRESETS.items():
        template = loader.load_file(ROOT / "templates" / "roles" / f"{name}.yaml")
        missing = set(preset.params) - set(template.params)
        assert not missing, f"{name}: テンプレートに無い引数 {missing}"


# ===== (4) 本物の役割テンプレートまで / through the real role template ===================

class FakeJira(Adapter):
    name = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.searched: list[str] = []

    @action()
    def search(self, jql: str, limit: int = 50) -> dict[str, Any]:
        self.searched.append(jql)
        return {"items": [{"key": "PROJ-1", "summary": "ログイン改善"}], "count": 1}

    @action(writes=True)
    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        return {"ok": True}


def test_it_works_through_the_real_developer_template_end_to_end(tmp_path):
    adapters = AdapterRegistry()
    jira, slack = FakeJira(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)
    llms = LLMRegistry()
    llm = EchoProvider(script=[LLMResponse(text="## 要約\nログイン改善の方針です", model="s")])
    llms.register("default", llm)
    engine = Engine(adapters, llms, PromptLibrary(ROOT / "prompts"))
    template = loader.load_file(ROOT / "templates" / "roles" / "role_developer.yaml")

    def launcher(response, trigger):
        assert response.template == template.name
        return engine.run(template, params=response.params, trigger=trigger)

    te, core, _, _ = make(tmp_path, [dev_ai()], launcher=launcher)
    te.attach(engine)
    core.cycle()
    core.accept_assignment("PROJ-1")
    core.cycle()

    prompt = llm.conversations[0][-1]["content"]
    assert "PROJ-1" in prompt and "PROJ" in prompt                       # 台帳の値が引数に入った
    assert slack.posted and "開発AI: PROJ-1" in slack.posted[0]["text"]
    (entry,) = entries(te)
    assert entry["status"] == "done" and "ログイン改善の方針" in entry["excerpt"]
    assert entry["params"] == {"issue_key": "PROJ-1", "jira_project": "PROJ"}
    # 人が確認するまで、タスクは未完了のまま（役割AIは完了にしない）
    assert te.find("PROJ-1").done is False and te.find("PROJ-1").assignee == "dev-ai"


# ===== CLI =========================================================================

def agent_config(tmp_path: Path, member_extra: str = "template: role_researcher") -> Path:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "role_researcher.yaml").write_text(textwrap.dedent("""\
        name: role_researcher
        params: {question: ""}
        steps:
          - id: research
            expression: findings
            inputs: {answer: "調査結果: {{ params.question }}"}
        """), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(textwrap.dedent(f"""\
        tenant: acme
        adapters: {{mode: mock}}
        web: {{templates_dir: templates}}
        task_engine: {{}}
        pmo_core:
          members:
            - name: research-ai
              kind: agent
              {member_extra}
              skills: [research]
        """), encoding="utf-8")
    return config


def test_cli_runs_an_assigned_task_now_and_lists_the_result(tmp_path, capsys):
    config = agent_config(tmp_path)
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant="acme")
    te.ingest("t", "r", [cand(key="PROJ-3", title="競合の動向", labels=["research"],
                              assignee="research-ai")])
    te.close()

    assert cli.main(["--config", str(config), "agents", "run", "PROJ-3"]) == 0
    out = capsys.readouterr().out
    assert "started" in out and "調査結果: 競合の動向" in out

    stored = TaskEngine(tmp_path / "task-ledger.db", tenant="acme").find("PROJ-3")
    assert stored.dispatches[-1]["status"] == "done"

    assert cli.main(["--config", str(config), "agents"]) == 0
    listing = capsys.readouterr().out
    assert "research-ai" in listing and "role_researcher" in listing and "done" in listing


def test_cli_refuses_a_task_that_is_not_for_a_role_ai_and_a_bad_template(tmp_path, capsys):
    config = agent_config(tmp_path)
    te = TaskEngine(tmp_path / "task-ledger.db", now=lambda: NOW, tenant="acme")
    te.ingest("t", "r", [cand(key="PROJ-4", title="人のタスク", assignee="sato")])
    te.close()
    assert cli.main(["--config", str(config), "agents", "run", "PROJ-4"]) == 1
    assert "役割AI" in capsys.readouterr().err

    broken = agent_config(tmp_path / "b", "template: role_nonexistent")
    assert cli.main(["--config", str(broken), "agents"]) == 1
    assert "role_nonexistent" in capsys.readouterr().err

    nothing = tmp_path / "plain.yaml"
    nothing.write_text("tenant: acme\n", encoding="utf-8")
    assert cli.main(["--config", str(nothing), "agents"]) == 0
    assert "kind: agent" in capsys.readouterr().out


# ===== Web ==========================================================================

def test_the_web_task_list_carries_the_runs(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aipmo.web.server import RunStore, create_app

    te, core, _, _ = make(tmp_path, [dev_ai()],
                          tasks=[cand(key="PROJ-1", title="x", labels=["dev"],
                                      assignee="dev-ai")])
    core.cycle()
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", "op-token-123",
                     viewer_token="view-token-456", lang="en", store=RunStore(),
                     pmo_ledger=tmp_path / "task-ledger.db")
    body = TestClient(app).get("/api/pmo", headers={"x-aipmo-token": "view-token-456"}).json()
    (task,) = body["tasks"]
    assert task["dispatches"][-1]["status"] == "done"
    assert task["dispatches"][-1]["agent"] == "dev-ai"
    assert body["briefing"]["agent_runs"][0]["status"] == "done"
    js = (ROOT / "aipmo" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "dispatches" in js and "innerHTML" not in js
