"""タスクの生成と進捗の自動収集のテスト / task generation and automatic collection.

収集: (1) 設定した収集元を走査して取り込む、(2) 走査に現れなかった未完了タスクを読み直して
**閉じられた課題の完了を観測する**、(3) 読み取り専用 — 書き込み系は設定に書かれていても拒否、
(4) 失敗しても止まらない。生成: (5) 定期タスクは期間ごとに一度だけ、(6) 続く警告から対応タスクを
**提案**（承認待ち・却下は残る・連鎖しない）、(7) 設定しなければ何も作らない。

Collection: scan configured sources; re-read open tasks the scan missed, so a closed issue is
seen to be done; read-only (a write action is refused even if configured); a failure never stops
the rest. Generation: recurring tasks once per period; follow-up *proposals* from persistent
alerts (pending, rejections kept, no chaining); nothing is made unless configured.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from aipmo import cli
from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.adapters.azure_devops import AzureDevOpsAdapter
from aipmo.adapters.github_projects import GitHubProjectsAdapter
from aipmo.adapters.openproject import OpenProjectAdapter
from aipmo.adapters.plane import PlaneAdapter
from aipmo.collector import CollectError, Collector, load_sources
from aipmo.engine.runner import Engine
from aipmo.generation import (
    GenerationError,
    Recurring,
    followup_id,
    load_generation,
    period_key,
)
from aipmo.llm.base import EchoProvider
from aipmo.llm.registry import LLMRegistry
from aipmo.pmo_core import Member, PmoCore
from aipmo.task_engine import TaskEngine
from aipmo.writeback import make_writer

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)      # 木曜 / a Thursday


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


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.requests.append((method, url, payload))
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b'{"message":"no route"}'


def ok(payload, status: int = 200):
    return status, {}, json.dumps(payload).encode("utf-8")


def gh_issue(number, title="課題", state="open", assignee=None):
    return {"number": number, "title": title, "state": state,
            "assignee": {"login": assignee} if assignee else None,
            "labels": [], "html_url": f"https://github.com/acme/widgets/issues/{number}"}


def github(routes):
    transport = FakeTransport(routes)
    adapter = GitHubProjectsAdapter(token="t", owner="acme", repo="widgets",
                                    transport=transport, max_retries=1)
    return adapter, transport


def registry(*adapters) -> AdapterRegistry:
    reg = AdapterRegistry()
    for adapter in adapters:
        reg.register(adapter)
    return reg


def ledger(tmp_path, clock=None) -> TaskEngine:
    return TaskEngine(tmp_path / "task-ledger.db", now=clock or Clock())


GH_SOURCE = [{"id": "gh-open", "adapter": "github_projects", "params": {"query": "is:open"}}]


# ===== アダプタ: 1 件を読む / reading one issue ======================================

def test_each_tracker_can_read_one_issue_and_never_writes():
    gh, gt = github({"/issues/42": ok(gh_issue(42, "GH", "closed"))})
    assert gh.invoke("get_issue", {"issue_number": 42})["status"] == "closed"
    assert gt.requests[0][0] == "GET" and gt.requests[0][1].endswith("/repos/acme/widgets/issues/42")

    pt = FakeTransport({"/issues/i1/": ok({"id": "i1", "name": "P", "completed_at": "2026-09-01"})})
    plane = PlaneAdapter(api_key="k", workspace_slug="acme", project_id="proj-1",
                         transport=pt, max_retries=1)
    assert plane.invoke("get_issue", {"issue_id": "i1"})["completed"] is True

    ot = FakeTransport({"work_packages/7": ok({"id": 7, "subject": "OP", "lockVersion": 1,
                                               "_links": {"status": {"title": "Closed"}}})})
    op = OpenProjectAdapter(base_url="https://op.example.com", api_key="k", project_id="w",
                            transport=ot, max_retries=1)
    assert op.invoke("get_issue", {"work_package_id": 7})["status"] == "Closed"

    at = FakeTransport({"workitems/78": ok({"id": 78, "fields": {
        "System.Title": "ADO", "System.State": "Closed"}})})
    ado = AzureDevOpsAdapter(organization="acme", project="Widgets", pat="t", transport=at,
                             max_retries=1)
    assert ado.invoke("get_issue", {"work_item_id": 78})["status"] == "Closed"
    for adapter in (gh, plane, op, ado):
        assert adapter.writes("get_issue") is False
        assert all(m == "GET" for m, _, _ in (gt.requests + pt.requests + ot.requests
                                              + at.requests))


# ===== (1) 収集元の走査 / scanning sources ==============================================

def test_source_config_is_validated():
    assert load_sources(GH_SOURCE)[0].action == "search"
    for bad in ([{"adapter": "jira"}], [{"id": "x"}], [{"id": "a", "adapter": "j"},
                                                      {"id": "a", "adapter": "j"}],
                [{"id": "a", "adapter": "j", "params": "nope"}]):
        with pytest.raises(CollectError):
            load_sources(bad)
    assert load_sources(None) == []


def test_a_scan_ingests_what_the_source_returns_with_its_tracker_and_project(tmp_path):
    adapter, _ = github({"/search/issues": ok({"total_count": 2, "items": [
        gh_issue(1, "ひとつめ", assignee="ann"), gh_issue(2, "ふたつめ")]})})
    te = ledger(tmp_path)
    sources = load_sources([{**GH_SOURCE[0], "project": "widgets"}])
    report = Collector(te, registry(adapter), sources, refresh_known=False).run_once()
    assert report["sources"][0]["items"] == 2 and report["sources"][0]["new"] == 2
    assert sorted(te.tasks) == ["GH:1", "GH:2"]
    task = te.tasks["GH:1"]
    assert (task.tracker, task.external_id, task.project, task.assignee) == (
        "github_projects", "1", "widgets", "ann")


# ===== (3) 読み取り専用 / read-only ============================================================

class Writer(Adapter):
    name = "github_projects"

    def __init__(self):
        super().__init__()
        self.calls = []

    @action(writes=True)
    def search(self, query: str = "") -> dict[str, Any]:
        self.calls.append(query)
        return {"items": [{"number": 9, "title": "書き込み系の search"}]}


def test_a_write_action_is_refused_even_if_the_config_names_it(tmp_path):
    writer = Writer()
    te = ledger(tmp_path)
    report = Collector(te, registry(writer), load_sources(GH_SOURCE),
                       refresh_known=False).run_once()
    assert writer.calls == [] and te.tasks == {}
    assert "書き込み" in report["sources"][0]["error"]


def test_a_source_that_cannot_run_does_not_stop_the_others(tmp_path):
    adapter, _ = github({"/search/issues": ok({"items": [gh_issue(1)], "total_count": 1})})
    sources = load_sources([
        {"id": "nope", "adapter": "jira"},                       # アダプタ未設定
        {"id": "noaction", "adapter": "github_projects", "action": "delete_everything"},
        {"id": "broken", "adapter": "github_projects", "params": {"bogus_param": 1}},
        GH_SOURCE[0]])
    report = Collector(ledger(tmp_path), registry(adapter), sources,
                       refresh_known=False).run_once()
    errors = {s["id"]: s["error"] for s in report["sources"]}
    assert "設定されていません" in errors["nope"] and "ありません" in errors["noaction"]
    assert errors["broken"] and errors["gh-open"] is None
    assert report["sources"][3]["items"] == 1


# ===== (2) 閉じられた課題の完了を観測する / seeing a closed issue finish ======================

def seeded_open_issue(te: TaskEngine, number=42, due="2026-09-28", effort=3, started=True):
    te.ingest("template", "r0", [cand(
        key=f"GH:{number}", tracker="github_projects", external_id=str(number),
        title="閉じられる課題", assignee="ann", due_date=due, effort=effort,
        status="In Progress" if started else "To Do", project="widgets")])


def test_an_issue_that_left_the_query_is_re_read_and_seen_to_be_done(tmp_path):
    clock = Clock(NOW - timedelta(days=4))
    te = ledger(tmp_path, clock)
    seeded_open_issue(te)                                           # 4 日前に着手を観測
    clock.now = NOW
    adapter, transport = github({
        "/search/issues": ok({"items": [], "total_count": 0}),      # 閉じたので検索に出ない
        "/issues/42": ok(gh_issue(42, "閉じられる課題", "closed", "ann"))})
    report = Collector(te, registry(adapter), load_sources(GH_SOURCE)).run_once()

    assert report["refreshed"] == 1 and report["completed"] == 1
    assert te.find("GH:42").done is True
    (outcome,) = te.outcomes
    assert outcome["task"] == "GH:42" and outcome["late_days"] == 3      # 期限 9/28 → 10/1
    assert outcome["effort"] == 3 and outcome["duration_days"] == 4      # 学習の材料になる
    assert any(url.endswith("/issues/42") for _, url, _ in transport.requests)


def test_what_the_scan_returned_is_not_read_again(tmp_path):
    te = ledger(tmp_path)
    seeded_open_issue(te)
    adapter, transport = github({"/search/issues": ok({"items": [gh_issue(42)], "total_count": 1}),
                                 "/issues/42": ok(gh_issue(42, state="closed"))})
    report = Collector(te, registry(adapter), load_sources(GH_SOURCE)).run_once()
    assert report["refreshed"] == 0 and not te.find("GH:42").done
    assert not any(url.endswith("/issues/42") for _, url, _ in transport.requests)


def test_only_tracker_tasks_are_re_read_and_the_oldest_first_within_the_cap(tmp_path):
    clock = Clock(NOW - timedelta(days=10))
    te = ledger(tmp_path, clock)
    for number in (1, 2, 3):                                   # 1 が最も古く、3 が新しい
        seeded_open_issue(te, number)
        clock.now += timedelta(days=1)
    te.create_task("PMO:rec:x:1", "台帳だけ", origin="recurring", proposed=False)
    te.ingest("wbs", "r", [cand(key="WBS:1.1", tracker="wbs_file", external_id="1.1",
                                title="WBS の作業")])
    adapter, transport = github({
        "/search/issues": ok({"items": [], "total_count": 0}),
        "/issues/1": ok(gh_issue(1)), "/issues/2": ok(gh_issue(2)), "/issues/3": ok(gh_issue(3))})
    Collector(te, registry(adapter), load_sources(GH_SOURCE), max_refresh=2).run_once()
    read = [url.rsplit("/", 1)[-1] for m, url, _ in transport.requests if "/issues/" in url
            and "search" not in url]
    assert read == ["1", "2"]                                  # 古い順に 2 件。台帳だけ・WBS は対象外


def test_refresh_can_be_switched_off(tmp_path):
    te = ledger(tmp_path)
    seeded_open_issue(te)
    adapter, transport = github({"/search/issues": ok({"items": [], "total_count": 0})})
    report = Collector(te, registry(adapter), load_sources(GH_SOURCE),
                       refresh_known=False).run_once()
    assert report["refreshed"] == 0 and len(transport.requests) == 1


def test_jira_tasks_are_re_read_in_batches_and_a_vanished_issue_is_reported(tmp_path):
    class Jira(Adapter):
        name = "jira"

        def __init__(self):
            super().__init__()
            self.queries: list[str] = []

        @action()
        def search(self, jql: str, limit: int = 50) -> dict[str, Any]:
            self.queries.append(jql)
            if jql.startswith("key in"):                        # 再読み込み。A-2 は消えた
                return {"items": [{"key": "A-1", "summary": "終わった", "status": "Done"}]}
            return {"items": []}

    jira = Jira()
    te = ledger(tmp_path, Clock(NOW - timedelta(days=2)))
    te.ingest("t", "r", [cand(key="A-1", title="終わった", tracker="jira", external_id="A-1",
                              status="In Progress", effort=2, due_date="2026-09-30"),
                         cand(key="A-2", title="消えた", tracker="jira", external_id="A-2")])
    te.clock = None
    te.now = Clock(NOW)
    report = Collector(te, registry(jira), load_sources([
        {"id": "j", "adapter": "jira", "params": {"jql": "project = A AND statusCategory != Done"}}]
    )).run_once()
    assert any(q.startswith("key in (") and "A-1" in q and "A-2" in q for q in jira.queries)
    assert te.find("A-1").done is True and report["completed"] == 1
    assert report["missing"] == ["JIRA:A-2"] and te.find("A-2").done is False


# ===== (4) 失敗しても止まらない / failures do not stop it ========================================

def test_a_task_that_cannot_be_re_read_is_counted_and_left_as_it_was(tmp_path):
    te = ledger(tmp_path)
    seeded_open_issue(te, 1)
    seeded_open_issue(te, 2)
    adapter, _ = github({"/search/issues": ok({"items": [], "total_count": 0}),
                         "/issues/1": (500, {}, b'{"message":"boom"}'),
                         "/issues/2": ok(gh_issue(2, state="closed"))})
    report = Collector(te, registry(adapter), load_sources(GH_SOURCE)).run_once()
    assert report["failed"] == 1 and report["refreshed"] == 1
    assert te.find("GH:1").done is False and te.find("GH:2").done is True
    assert any("GH:1" in e for e in report["errors"])


# ===== PMO Core に載せる / wired into the cycle ======================================================

class SpyCollector:
    interval_minutes = 30

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def run_once(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("tracker down")
        return {"at": NOW.isoformat(), "sources": [{"id": "s", "adapter": "x", "items": 3,
                                                    "new": 1, "error": None}],
                "refreshed": 2, "failed": 0, "missing": [], "completed": 1}


def test_the_cycle_collects_only_when_the_interval_has_passed_and_remembers_across_restarts(tmp_path):
    clock = Clock()
    te = ledger(tmp_path, clock)
    spy = SpyCollector()
    core = PmoCore(task_engine=te, collector=spy)
    first = core.cycle()
    core.cycle()
    assert spy.calls == 1 and first["collection"]["refreshed"] == 2
    clock.now = NOW + timedelta(minutes=10)
    PmoCore(task_engine=te, collector=spy).cycle()                 # 再起動しても間隔を覚えている
    assert spy.calls == 1
    clock.now = NOW + timedelta(minutes=31)
    core.cycle()
    assert spy.calls == 2
    assert "collected" in (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")


def test_a_failing_collection_is_reported_and_never_breaks_the_cycle(tmp_path):
    te = ledger(tmp_path)
    briefing = PmoCore(task_engine=te, collector=SpyCollector(fail=True)).cycle()
    assert "tracker down" in briefing["collection"]["error"]
    assert briefing["overall_level"] == "low"


def test_a_display_only_core_shows_the_last_collection_without_going_out(tmp_path):
    te = ledger(tmp_path)
    PmoCore(task_engine=te, collector=SpyCollector()).cycle()
    shown = PmoCore(task_engine=te).cycle()                         # collector=None の表示専用
    assert shown["collection"]["refreshed"] == 2


def test_collection_makes_the_learning_see_a_task_no_template_lists_any_more(tmp_path):
    """収集 → 完了の観測 → 実績 → 学習、までつながる。"""
    clock = Clock(NOW - timedelta(days=60))
    te = ledger(tmp_path, clock)
    adapter, _ = github({"/search/issues": ok({"items": [], "total_count": 0})} | {
        f"/issues/{n}": ok(gh_issue(n, state="closed")) for n in range(1, 9)})
    for n in range(1, 9):                                           # 8 件が 4 日で終わる、を観測
        te.ingest("t", f"s{n}", [cand(key=f"GH:{n}", tracker="github_projects",
                                      external_id=str(n), title=f"t{n}", status="In Progress",
                                      effort=2, assignee="ann")])
        clock.now += timedelta(days=4)
        Collector(te, registry(adapter), load_sources(GH_SOURCE)).run_once()
    core = PmoCore(task_engine=te, members=[Member("ann")])
    briefing = core.cycle()
    assert len(te.outcomes) == 8
    assert briefing["learning"]["pace"]["team"] == 2.0


# ===== 生成の設定 / generation config =============================================================

def test_generation_config_is_validated_and_defaults_to_nothing():
    cfg = load_generation(None)
    assert cfg.recurring == [] and cfg.followups == []
    assert [f.rule for f in load_generation({"followups": True}).followups] == [
        "overdue_severe", "blocked_long", "agent_attention"]
    for bad in ({"recurring": [{"title": "x"}]}, {"recurring": [{"id": "a"}]},
                {"recurring": [{"id": "a", "title": "t", "every": "hourly"}]},
                {"recurring": [{"id": "a", "title": "t", "weekday": "FUNDAY"}]},
                {"recurring": [{"id": "a", "title": "t", "every": "month", "day": 31}]},
                {"recurring": [{"id": "a", "title": "t", "timezone": "Mars/Base"}]},
                {"recurring": [{"id": "a", "title": "t"}, {"id": "a", "title": "u"}]},
                {"followups": [{"after_days": 1}]}, {"followups": "yes"}):
        with pytest.raises(GenerationError):
            load_generation(bad)


def test_period_keys_follow_the_weekday_day_and_timezone():
    weekly = Recurring("w", "t", "week", weekday=0)                       # 月曜以降
    assert period_key(weekly, datetime(2026, 10, 4, 12, tzinfo=timezone.utc)) == "2026-W40"   # 日
    assert period_key(Recurring("w", "t", "week", weekday=4),
                      datetime(2026, 10, 1, tzinfo=timezone.utc)) is None                     # 木 < 金
    assert period_key(Recurring("w", "t", "week", weekday=3),
                      datetime(2026, 10, 1, tzinfo=timezone.utc)) == "2026-W40"
    assert period_key(Recurring("d", "t", "day"), NOW) == "2026-10-01"
    assert period_key(Recurring("m", "t", "month", day=15), NOW) is None
    assert period_key(Recurring("m", "t", "month", day=1), NOW) == "2026-10"
    late_sunday = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc)
    assert period_key(Recurring("w", "t", "week", weekday=0, timezone="Asia/Tokyo"),
                      late_sunday) == "2026-W41"                      # 東京ではもう月曜


# ===== (5) 定期タスク / recurring tasks ================================================================

REC = {"recurring": [{"id": "review", "title": "週次レビュー", "every": "week",
                      "weekday": "MON", "priority": "Medium", "assignee": "sato",
                      "labels": ["pmo"], "project": "aipmo", "due_in_days": 3}]}


def test_a_recurring_task_is_made_once_per_period_without_approval(tmp_path):
    clock = Clock()
    te = ledger(tmp_path, clock)
    core = PmoCore(task_engine=te, generation=load_generation(REC))
    core.cycle()
    core.cycle()
    (task,) = [t for t in te.tasks.values()]
    assert task.id == "PMO:rec:review:2026-W40" and task.origin == "recurring"
    assert task.proposed is False and task.done is False
    assert (task.project, task.assignee, task.priority, task.labels) == (
        "aipmo", "sato", "Medium", ["pmo"])
    assert task.due_date == "2026-10-04"
    assert [t.id for t in te.ranked()] == [task.id]                  # すぐ仕事として順位に入る
    clock.now = NOW + timedelta(days=7)
    core.cycle()
    assert sorted(te.tasks) == ["PMO:rec:review:2026-W40", "PMO:rec:review:2026-W41"]
    assert "task_generated" in (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")


def test_an_open_ledger_only_task_is_never_aged_out_but_a_finished_one_is(tmp_path):
    clock = Clock()
    te = ledger(tmp_path, clock)
    PmoCore(task_engine=te, generation=load_generation(REC)).cycle()
    clock.now = NOW + timedelta(days=90)
    te.refresh()
    assert "PMO:rec:review:2026-W40" in te.tasks                   # 開いている → 残る
    te.complete("PMO:rec:review:2026-W40")
    clock.now = NOW + timedelta(days=200)
    te.refresh()
    assert te.tasks == {}                                           # 終わって久しい → 消える


def test_nothing_is_generated_unless_configured(tmp_path):
    te = ledger(tmp_path)
    core = PmoCore(task_engine=te)
    for _ in range(3):
        core.cycle()
    assert te.tasks == {}


# ===== (6) 対応タスクの提案 / follow-up proposals ====================================================

def overdue_setup(tmp_path, notify=None, followups=True):
    clock = Clock()
    te = ledger(tmp_path, clock)
    te.ingest("t", "r", [cand(key="P-1", title="遅れている課題", assignee="ann",
                              due_date="2026-09-01", project="P", status="In Progress")])
    core = PmoCore(task_engine=te, members=[Member("ann")], notify=notify,
                   generation=load_generation({"followups": followups}))
    return te, core, clock


def test_a_persistent_serious_alert_raises_a_proposal_that_is_not_work_yet(tmp_path):
    sent = []
    te, core, clock = overdue_setup(tmp_path, notify=sent.append)
    first = core.cycle()
    assert first["generated"]["created"] == [] and te.proposals() == []        # 出たばかりでは提案しない
    clock.now = NOW + timedelta(days=1, minutes=1)
    briefing = core.cycle()
    (proposal,) = te.proposals()
    assert proposal.proposed and proposal.origin == "followup"
    assert proposal.title.startswith("対応を決める: 遅れている課題")
    assert proposal.generated_from.startswith("alert:overdue_severe|JIRA:P-1")
    assert proposal.project == "P" and proposal.priority == "High"
    assert briefing["generated"]["created"] == [proposal.id]
    assert proposal.id not in [t.id for t in te.ranked()]                       # 順位に入らない
    assert briefing["assignment_proposals"] == []                               # 担当提案にも入らない
    assert any("[提案]" in s and "承認待ち" in s for s in sent)
    core.cycle()
    assert len(te.proposals()) == 1                                             # 何周しても 1 件


def test_a_rejection_is_kept_and_the_same_episode_is_not_proposed_again(tmp_path):
    te, core, clock = overdue_setup(tmp_path)
    core.cycle()
    clock.now = NOW + timedelta(days=2)
    core.cycle()
    (proposal,) = te.proposals()
    rejected = core.decide_proposal(proposal.id, approve=False)
    assert rejected.status == "Rejected" and rejected.done and not rejected.proposed
    core.cycle()
    core.cycle()
    assert te.proposals() == [] and len([t for t in te.tasks.values() if t.origin]) == 1
    assert te.outcomes == []                                                    # 却下は実績にしない
    assert "proposal_rejected" in (tmp_path / "pmo-decisions.jsonl").read_text(encoding="utf-8")


def test_approving_makes_it_work_that_is_ranked_assigned_and_can_be_closed(tmp_path):
    te, core, clock = overdue_setup(tmp_path)
    core.cycle()
    clock.now = NOW + timedelta(days=2)
    core.cycle()
    (proposal,) = te.proposals()
    approved = core.decide_proposal(proposal.id, approve=True)
    assert approved.proposed is False and approved.status == "To Do"
    briefing = core.cycle()
    assert proposal.id in [t.id for t in te.ranked()]
    assert proposal.id in [p["task"] for p in briefing["assignment_proposals"]]  # 担当も提案される
    core.accept_assignment(proposal.id, write=make_writer(AdapterRegistry(), [Member("ann")]))
    assert te.find(proposal.id).assignee == "ann"                                # トラッカー無し → 台帳のみ
    core.complete_task(proposal.id)
    assert te.find(proposal.id).done and len(te.outcomes) == 1                   # 完了は実績になる


def test_a_new_episode_after_the_alert_cleared_is_proposed_again(tmp_path):
    te, core, clock = overdue_setup(tmp_path)
    core.cycle()
    clock.now = NOW + timedelta(days=2)
    core.cycle()
    first = te.proposals()[0].id
    core.decide_proposal(first, approve=False)
    with te.transaction():                                  # 期限を延ばして警告を解消
        te.tasks["JIRA:P-1"].due_date = "2099-01-01"
    clock.now = NOW + timedelta(days=3)
    core.cycle()
    with te.transaction():                                  # また遅れた(別の回)
        te.tasks["JIRA:P-1"].due_date = "2026-09-01"
    clock.now = NOW + timedelta(days=4)
    core.cycle()
    clock.now = NOW + timedelta(days=6)
    core.cycle()
    ids = {t.id for t in te.tasks.values() if t.origin}
    assert len(ids) == 2 and first in ids


def test_a_generated_task_never_spawns_proposals_from_its_own_alerts(tmp_path):
    te, core, clock = overdue_setup(tmp_path)
    core.cycle()
    clock.now = NOW + timedelta(days=2)
    core.cycle()
    core.decide_proposal(te.proposals()[0].id, approve=True)
    clock.now = NOW + timedelta(days=60)                    # 提案タスク自身も期限超過で警告に
    briefing = core.cycle()
    assert any(a["task"].startswith("PMO:fu:") for a in briefing["alerts"])
    clock.now = NOW + timedelta(days=63)                    # その警告が続いても(続いた日数は足りる)
    core.cycle()
    core.cycle()
    assert [t.origin for t in te.tasks.values() if t.origin] == ["followup"]   # 連鎖しない


def test_followups_off_means_none(tmp_path):
    te, core, clock = overdue_setup(tmp_path, followups=False)
    core.cycle()
    clock.now = NOW + timedelta(days=5)
    core.cycle()
    assert [t for t in te.tasks.values() if t.origin] == []


def test_proposal_ids_name_the_episode():
    assert followup_id("overdue_severe", "JIRA:P-1", "2026-10-02T09:30:00+00:00") == \
        "PMO:fu:overdue_severe:JIRA:P-1:2026-10-02T0930"


def test_decisions_and_closing_are_checked(tmp_path):
    te, core, _ = overdue_setup(tmp_path)
    core.cycle()
    with pytest.raises(ValueError, match="承認待ち"):
        core.decide_proposal("P-1", approve=True)               # 提案ではない
    with pytest.raises(KeyError):
        core.decide_proposal("NOPE", approve=True)
    with pytest.raises(ValueError, match="課題管理ツール"):
        core.complete_task("P-1")                               # トラッカーのタスクはそちらで閉じる
    te.create_task("PMO:fu:x:y:z", "提案", origin="followup", proposed=True)
    with pytest.raises(ValueError, match="先に承認"):
        core.complete_task("PMO:fu:x:y:z")
    done = te.create_task("PMO:rec:a:1", "定期", origin="recurring", proposed=False)
    core.complete_task(done.id)
    with pytest.raises(ValueError, match="すでに完了"):
        core.complete_task(done.id)
    assert te.create_task("PMO:rec:a:1", "again", origin="recurring", proposed=False) is None


def test_scoping_hides_other_projects_proposals():
    from aipmo.pmo_core import scope_briefing
    briefing = {"alerts": [], "assignment_proposals": [], "unassignable": [], "projects": [],
                "overall_level": "low", "generated": {"created": ["x"], "pending": [
                    {"id": "1", "project": "A"}, {"id": "2", "project": "B"}]},
                "collection": {"at": "x"}}
    scoped = scope_briefing(briefing, [], {"a"}, redact_org=True)
    assert [p["id"] for p in scoped["generated"]["pending"]] == ["1"]
    assert scoped["collection"] is None


# ===== CLI ===========================================================================================

def with_engine(monkeypatch, *adapters):
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    monkeypatch.setattr(cli, "build_engine", lambda *a, **k: Engine(registry(*adapters), llms))


def test_cli_collect_reads_the_trackers_and_reports(tmp_path, monkeypatch, capsys):
    adapter, _ = github({"/search/issues": ok({"items": [gh_issue(7, "新規")], "total_count": 1}),
                         "/issues/42": ok(gh_issue(42, "閉じた", "closed", "ann"))})
    with_engine(monkeypatch, adapter)
    config = tmp_path / "config.yaml"
    config.write_text(
        "tenant: acme\npmo_core:\n  collect:\n    sources:\n"
        "      - {id: gh, adapter: github_projects, params: {query: 'is:open'}}\n",
        encoding="utf-8")
    seed = TaskEngine(tmp_path / "task-ledger.db", now=Clock(NOW - timedelta(days=3)),
                      tenant="acme")
    seeded_open_issue(seed)
    seed.close()

    assert cli.main(["--config", str(config), "collect"]) == 0
    out = capsys.readouterr().out
    assert "gh" in out and "新たに完了と分かった 1 件" in out
    stored = TaskEngine(tmp_path / "task-ledger.db", tenant="acme")
    assert stored.find("GH:42").done and stored.find("GH:7") is not None


def test_cli_collect_needs_a_configuration_and_reports_failures(tmp_path, monkeypatch, capsys):
    with_engine(monkeypatch)
    plain = tmp_path / "plain.yaml"
    plain.write_text("tenant: acme\n", encoding="utf-8")
    assert cli.main(["--config", str(plain), "collect"]) == 1
    assert "pmo_core.collect" in capsys.readouterr().err

    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\npmo_core:\n  collect:\n    sources:\n"
                      "      - {id: gh, adapter: github_projects}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "collect"]) == 1                  # アダプタが無い
    assert "設定されていません" in capsys.readouterr().out


def test_cli_rejects_bad_collect_and_generate_config_before_touching_anything(
        tmp_path, capsys):
    for body, needle in (
            ("pmo_core:\n  collect:\n    sources: [{adapter: x}]\n", "pmo_core.collect"),
            ("pmo_core:\n  generate:\n    recurring: [{id: a, title: t, every: hourly}]\n",
             "pmo_core.generate")):
        config = tmp_path / "bad.yaml"
        config.write_text("tenant: acme\n" + body, encoding="utf-8")
        assert cli.main(["--config", str(config), "pmo"]) == 1
        assert needle in capsys.readouterr().err


def test_cli_generated_lists_approves_rejects_and_closes(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("tenant: acme\n", encoding="utf-8")
    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock(), tenant="acme")
    te.create_task("PMO:fu:a:1", "提案その1", origin="followup", proposed=True,
                   priority="High", due_date="2026-10-04")
    te.create_task("PMO:fu:a:2", "提案その2", origin="followup", proposed=True)
    te.create_task("PMO:rec:r:1", "週次レビュー", origin="recurring", proposed=False)
    te.close()

    assert cli.main(["--config", str(config), "generated"]) == 0
    out = capsys.readouterr().out
    assert "提案その1" in out and "提案その2" in out and "週次レビュー" in out

    assert cli.main(["--config", str(config), "generated", "approve", "PMO:fu:a:1"]) == 0
    assert cli.main(["--config", str(config), "generated", "reject", "PMO:fu:a:2"]) == 0
    assert cli.main(["--config", str(config), "generated", "done", "PMO:rec:r:1"]) == 0
    capsys.readouterr()
    stored = TaskEngine(tmp_path / "task-ledger.db", tenant="acme")
    assert stored.find("PMO:fu:a:1").proposed is False and not stored.find("PMO:fu:a:1").done
    assert stored.find("PMO:fu:a:2").status == "Rejected"
    assert stored.find("PMO:rec:r:1").done

    assert cli.main(["--config", str(config), "generated", "approve", "PMO:fu:a:1"]) == 1
    assert "承認待ち" in capsys.readouterr().err
    assert cli.main(["--config", str(config), "generated", "done", "NOPE"]) == 1


# ===== Web ===========================================================================================

def web_client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aipmo.web.server import RunStore, create_app

    te = TaskEngine(tmp_path / "task-ledger.db", now=Clock(), tenant="acme")
    te.create_task("PMO:fu:a:1", "Alpha の提案", origin="followup", proposed=True,
                   project="ALPHA", priority="High")
    te.create_task("PMO:fu:b:1", "Beta の提案", origin="followup", proposed=True, project="BETA")
    te.create_task("PMO:rec:r:1", "定期", origin="recurring", proposed=False, project="ALPHA")
    te.close()
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    (tmp_path / "t").mkdir()
    app = create_app(Engine(AdapterRegistry(), llms), tmp_path / "t", "op-token-123",
                     viewer_token="view-token-456", lang="en", store=RunStore(), tenant="acme",
                     pmo_ledger=tmp_path / "task-ledger.db", viewer_projects=["ALPHA"])
    return TestClient(app)


OP = {"x-aipmo-token": "op-token-123"}
VIEW = {"x-aipmo-token": "view-token-456"}


def test_web_lists_proposals_and_marks_ledger_only_tasks(tmp_path):
    client = web_client(tmp_path)
    everything = client.get("/api/pmo", headers=OP).json()
    assert {p["id"] for p in everything["proposals"]} == {"PMO:fu:a:1", "PMO:fu:b:1"}
    assert [t["origin"] for t in everything["tasks"]] == ["recurring"]       # 提案は仕事の一覧に入らない
    confined = client.get("/api/pmo", headers=VIEW).json()
    assert [p["id"] for p in confined["proposals"]] == ["PMO:fu:a:1"]         # 見てよいプロジェクトだけ


def test_web_decides_proposals_for_the_operator_only(tmp_path):
    client = web_client(tmp_path)
    body = {"ref": "PMO:fu:a:1", "decision": "approve"}
    assert client.post("/api/pmo/proposals/decide", json=body, headers=VIEW).status_code == 403
    assert client.post("/api/pmo/proposals/decide", json=body).status_code == 401
    assert client.post("/api/pmo/proposals/decide", json=body, headers=OP).status_code == 200
    assert client.post("/api/pmo/proposals/decide", json=body, headers=OP).status_code == 409
    assert client.post("/api/pmo/proposals/decide", json={"ref": "NOPE", "decision": "reject"},
                       headers=OP).status_code == 404
    for bad in ({}, {"ref": "x"}, {"ref": "x", "decision": "maybe"}):
        assert client.post("/api/pmo/proposals/decide", json=bad, headers=OP).status_code == 422
    after = client.get("/api/pmo", headers=OP).json()
    assert {t["id"] for t in after["tasks"]} == {"PMO:rec:r:1", "PMO:fu:a:1"}
    assert [p["id"] for p in after["proposals"]] == ["PMO:fu:b:1"]


def test_the_screen_offers_approve_and_reject_without_parsing_html():
    js = (Path(__file__).resolve().parents[1] / "aipmo" / "web" / "static"
          / "app.js").read_text(encoding="utf-8")
    assert "generatedRow" in js and "/api/pmo/proposals/decide" in js
    assert "web_pmo_generated" in js and "innerHTML" not in js
