"""出荷するテンプレートのテスト / tests for the shipped templates.

例として配るものが壊れているのは、実装が壊れているより質が悪い。
最初に触るのがこれなので、動かなければそこで終わる。

A broken example is worse than a broken implementation: it is the first thing
anyone touches, and if it does not work they stop there.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aipmo.adapters.base import AdapterRegistry, action
from aipmo.adapters.mock import MockJiraAdapter, MockSlackAdapter, MockTeamsAdapter
from aipmo.dsl import loader
from aipmo.engine.runner import Engine, PromptLibrary
from aipmo.llm.base import EchoProvider, LLMResponse
from aipmo.llm.registry import LLMRegistry

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates" / "examples"


class Scripted(EchoProvider):
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def complete(self, request):
        return LLMResponse(text=self.replies.pop(0), model="scripted")


class FakeTeams(MockTeamsAdapter):
    def find_meeting(self, join_url, organiser_id=None):
        return {"meeting_id": "M1", "subject": "設計レビュー"}

    def get_transcript(self, meeting_id, organiser_id=None, wait_seconds=0,
                       with_timestamps=False):
        return {"text": "田中: 認証基盤を移行する。", "participants": ["田中 太郎"],
                "utterance_count": 1}


FakeTeams.find_meeting = action()(FakeTeams.find_meeting)
FakeTeams.get_transcript = action()(FakeTeams.get_transcript)


def build(replies=()):
    adapters = AdapterRegistry()
    adapters.register(FakeTeams())
    jira, slack = MockJiraAdapter(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted(replies))

    return Engine(adapters, llms, PromptLibrary(ROOT / "prompts")), jira, slack


# --- 全テンプレート共通 / every shipped template ---------------------------

@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.yaml")),
                         ids=lambda p: p.stem)
def test_every_shipped_template_loads(path):
    loader.load_file(path)


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.yaml")),
                         ids=lambda p: p.stem)
def test_referenced_prompts_exist(path):
    """プロンプト名の打ち間違いは、実行するまで気づけない。"""
    library = PromptLibrary(ROOT / "prompts")
    for step in loader.load_file(path).steps:
        if step.prompt:
            library.get(step.prompt)


# --- 個別催促 / chasing -----------------------------------------------------

CHASE_REPLY = json.dumps({
    "messages": [
        {"assignee": "佐藤 花子", "channel": "U02", "text": "PROJ-7 の状況はいかがでしょうか",
         "task_count": 1, "worst_days_overdue": 7},
    ],
    "unreachable": ["鈴木 一郎"],
}, ensure_ascii=False)


def overdue_jira(jira):
    jira.created = [
        {"key": "PROJ-7", "summary": "API互換対応", "assignee": "佐藤 花子",
         "due_date": "2026-08-20", "status": "In Progress"},
    ]


def test_chase_sends_one_message_per_person():
    engine, jira, slack = build([CHASE_REPLY])
    overdue_jira(jira)
    engine.run(loader.load_file(TEMPLATES / "overdue_chase.yaml"))

    personal = [m for m in slack.posted if m["channel"] == "U02"]
    assert len(personal) == 1
    assert "PROJ-7" in personal[0]["text"]


def test_chase_reports_people_it_could_not_reach():
    """黙って落とすと、その人だけ永久に催促が届かない。

    Dropped silently, those people would simply never be chased.
    """
    engine, jira, slack = build([CHASE_REPLY])
    overdue_jira(jira)
    engine.run(loader.load_file(TEMPLATES / "overdue_chase.yaml"))

    reported = [m for m in slack.posted if m["channel"] == "#pmo"]
    assert reported and "鈴木 一郎" in reported[0]["text"]


def test_chase_is_silent_when_nothing_is_overdue():
    """0件で「0件でした」と送らない。毎朝それが届けば無視されるようになる。

    An empty run stays silent: a daily "nothing to report" trains people to
    ignore the channel.
    """
    engine, jira, slack = build([])
    jira.created = []
    ctx = engine.run(loader.load_file(TEMPLATES / "overdue_chase.yaml"))

    assert slack.posted == []
    assert ctx.results["compose"].status == "skipped"


def test_chase_continues_when_one_person_fails():
    class Picky(MockSlackAdapter):
        def post_message(self, channel, text, thread_ts=None):
            if channel == "U_BAD":
                raise RuntimeError("no such user")
            return super().post_message(channel, text, thread_ts)

    reply = json.dumps({"messages": [
        {"assignee": "a", "channel": "U_BAD", "text": "x"},
        {"assignee": "b", "channel": "U_OK", "text": "y"},
    ], "unreachable": []}, ensure_ascii=False)

    adapters = AdapterRegistry()
    jira = MockJiraAdapter()
    overdue_jira(jira)
    slack = Picky()
    adapters.register(jira)
    adapters.register(slack)
    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    engine = Engine(adapters, llms, PromptLibrary(ROOT / "prompts"))
    ctx = engine.run(loader.load_file(TEMPLATES / "overdue_chase.yaml"))

    assert ctx.results["chase"].output["count"] == 1
    assert ctx.results["chase"].output["failed"] == 1
    assert [m["channel"] for m in slack.posted] == ["U_OK"]


# --- WBS --------------------------------------------------------------------

WBS_REPLY = json.dumps({
    "title": "認証基盤の移行",
    "assumptions": ["対象は社内向けのみと解釈した"],
    "phases": [
        {"name": "調査", "lines": ["利用箇所を洗い出す — 一覧 / 3日 / 佐藤"], "tasks": []},
        {"name": "設計", "lines": ["移行方式を決める — 資料 / 5日 / 未定"], "tasks": []},
    ],
    "milestones": [{"name": "設計レビュー完了", "after_phase": "設計"}],
    "unknowns": ["移行先の製品"],
}, ensure_ascii=False)


def run_wbs():
    engine, _, slack = build([WBS_REPLY])
    ctx = engine.run(loader.load_file(TEMPLATES / "wbs_from_meeting.yaml"),
                     params={"organiser": "t@example.com"},
                     trigger={"join_url": "https://teams.microsoft.com/l/x"})
    return ctx, slack


def test_wbs_posts_one_message_per_phase():
    """WBS 全体を1通に収めると読めなくなる。"""
    _, slack = run_wbs()
    phase_posts = [m for m in slack.posted if "—" in m["text"]
                   and "確認が必要" not in m["text"]]
    assert len(phase_posts) == 2


def test_phase_numbering_starts_at_one():
    """「1 / 2」が2番目を指す表示は、読む側が必ず取り違える。"""
    _, slack = run_wbs()
    assert "(1 / 2)" in slack.posted[0]["text"]
    assert "(2 / 2)" in slack.posted[1]["text"]


def test_wbs_tasks_are_readable_not_json():
    """生の JSON を貼られても人は読まない。"""
    _, slack = run_wbs()
    body = slack.posted[0]["text"]
    assert "利用箇所を洗い出す" in body
    assert '{"' not in body and "deliverable" not in body


def test_assumptions_and_unknowns_are_always_posted():
    """隠すと、補った推測が決まったことのように見える。

    Hidden, the model's guesses would read as decisions.
    """
    _, slack = run_wbs()
    caveats = slack.posted[-1]["text"]
    assert "対象は社内向けのみと解釈した" in caveats
    assert "移行先の製品" in caveats
    assert "草案" in caveats


def test_wbs_is_not_produced_without_a_transcript():
    """記録が無いのに WBS を作ると、全部が推測になる。"""
    class Silent(FakeTeams):
        def get_transcript(self, meeting_id, organiser_id=None, wait_seconds=0,
                           with_timestamps=False):
            return {"text": "", "participants": [], "utterance_count": 0}

    Silent.get_transcript = action()(Silent.get_transcript)

    adapters = AdapterRegistry()
    adapters.register(Silent())
    slack = MockSlackAdapter()
    adapters.register(slack)
    llms = LLMRegistry()
    llms.register("default", Scripted([]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(TEMPLATES / "wbs_from_meeting.yaml"),
        params={"organiser": "t@example.com"},
        trigger={"join_url": "https://x"})

    assert ctx.results["wbs"].status == "skipped"
    assert slack.posted == []


# --- 課題の更新 / updating existing work ------------------------------------

UPDATE_REPLY = json.dumps({
    "updates": [
        {"issue_key": "PROJ-7", "reason": "水曜まで延ばしたいと発言",
         "confidence": 0.95, "status": None, "due_date": "2026-09-02",
         "assignee": None, "note": None},
        {"issue_key": "PROJ-9", "reason": "それらしき発言があった",
         "confidence": 0.55, "status": None, "due_date": "2026-09-10",
         "assignee": None, "note": None},
    ],
    "unmatched": ["ドキュメント整備の話が出たが該当課題なし"],
}, ensure_ascii=False)


class RecordingJira(MockJiraAdapter):
    def __init__(self):
        super().__init__()
        self.updates = []

    def search(self, jql, fields=None, limit=50):
        return {"items": [{"key": "PROJ-7", "summary": "API互換対応"},
                          {"key": "PROJ-9", "summary": "負荷試験"}], "count": 2}

    def update_issue(self, issue_key, due_date=None, assignee=None,
                     comment=None, **kwargs):
        self.updates.append({"key": issue_key, "due_date": due_date,
                             "comment": comment})
        return {"issue_key": issue_key, "changed": ["duedate"]}


RecordingJira.search = action()(RecordingJira.search)
RecordingJira.update_issue = action(writes=True)(RecordingJira.update_issue)


def run_update():
    adapters = AdapterRegistry()
    adapters.register(FakeTeams())
    jira, slack = RecordingJira(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([UPDATE_REPLY]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(TEMPLATES / "meeting_task_update.yaml"),
        params={"organiser": "t@example.com"},
        trigger={"join_url": "https://teams.microsoft.com/l/x"})
    return ctx, jira, slack


def test_only_confident_changes_are_written():
    """誤った更新は、すでに正しかった期限を消す。迷ったら書き換えない。

    A mistaken update destroys a due date that was right, so uncertainty
    means not writing.
    """
    _, jira, _ = run_update()
    assert [u["key"] for u in jira.updates] == ["PROJ-7"]


def test_withheld_changes_are_reported():
    """判断を保留したことが誰にも伝わらないのが一番困る。"""
    _, _, slack = run_update()
    report = slack.posted[0]["text"]
    assert "保留: 1 件" in report
    assert "反映: 1 件" in report


def test_the_reason_is_recorded_on_the_issue():
    """後から辿れない変更は、誰にも信用されない。"""
    _, jira, _ = run_update()
    assert "水曜まで延ばしたい" in jira.updates[0]["comment"]


def test_unmatched_remarks_reach_a_person():
    _, _, slack = run_update()
    assert "ドキュメント整備" in slack.posted[0]["text"]


def test_no_updates_are_attempted_without_a_transcript():
    class Silent(FakeTeams):
        def get_transcript(self, meeting_id, organiser_id=None, wait_seconds=0,
                           with_timestamps=False):
            return {"text": "", "participants": [], "utterance_count": 0}

    Silent.get_transcript = action()(Silent.get_transcript)

    adapters = AdapterRegistry()
    adapters.register(Silent())
    jira = RecordingJira()
    adapters.register(jira)
    adapters.register(MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", Scripted([]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(TEMPLATES / "meeting_task_update.yaml"),
        params={"organiser": "t@example.com"},
        trigger={"join_url": "https://x"})

    assert jira.updates == []
    assert ctx.results["propose"].status == "skipped"


# --- スプリントの状況 / sprint health --------------------------------------

SPRINT_REPLY = json.dumps({
    "assessment": "注意",
    "headline": "残り2日で完了率50%。全部は入りきらない見込みです。",
    "concerns": ["P-4 が見積もりも担当も未定のまま残っています"],
}, ensure_ascii=False)

HEALTHY_REPLY = json.dumps({
    "assessment": "順調",
    "headline": "予定どおり進んでいます。",
    "concerns": [],
}, ensure_ascii=False)


class FakeAgile(MockSlackAdapter):
    """集計まで済ませたアダプタの形 / stands in for the aggregated result."""

    name = "agile"

    def __init__(self, active=True, count=4, points_total=16.0):
        super().__init__()
        self.active = active
        self.count = count
        self.points_total = points_total

    def active_sprint(self, board_id=None):
        if not self.active:
            return {"active": False, "reason": "進行中のスプリントがありません"}
        return {"active": True, "id": 8, "name": "Sprint 12",
                "goal": "移行を完了する", "days_remaining": 2}

    def sprint_issues(self, sprint_id, board_id=None):
        return {"items": [{"key": "P-1"}], "count": self.count, "done_count": 2,
                "points_total": self.points_total, "points_done": 8.0,
                "percent_done": 50, "percent_basis": "points",
                "estimation_field": "customfield_10016",
                "unestimated": ["P-4"], "unassigned": ["P-4"]}


FakeAgile.active_sprint = action()(FakeAgile.active_sprint)
FakeAgile.sprint_issues = action()(FakeAgile.sprint_issues)


def run_sprint(reply=SPRINT_REPLY, **kwargs):
    adapters = AdapterRegistry()
    agile = FakeAgile(**kwargs)
    slack = MockSlackAdapter()
    adapters.register(agile)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(TEMPLATES / "sprint_health.yaml"))
    return ctx, slack


def test_the_posted_figures_come_from_the_aggregation():
    """数字を AI に書き直させない。言い換えで変わっても読む側は気づけない。

    Figures are not rephrased by the model: if one changed in the process, a
    reader would have no way to tell.
    """
    _, slack = run_sprint()
    body = slack.posted[0]["text"]
    assert "完了 50%" in body
    assert "残り 2 日" in body


def test_a_healthy_sprint_says_nothing():
    """毎朝「順調です」が届くチャンネルは、危ないときにも読まれなくなる。

    A channel that receives "all fine" every morning is not read when it
    matters either.
    """
    ctx, slack = run_sprint(reply=HEALTHY_REPLY)
    assert slack.posted == []
    assert ctx.results["warn"].status == "skipped"


def test_nothing_is_said_between_sprints():
    ctx, slack = run_sprint(active=False)
    assert slack.posted == []
    assert ctx.results["issues"].status == "skipped"


def test_an_empty_sprint_produces_no_assessment():
    ctx, _ = run_sprint(count=0)
    assert ctx.results["assess"].status == "skipped"


def test_no_estimates_at_all_prompts_a_configuration_check():
    """全件見積もり無しは、項目 ID を引けていない可能性が高い。

    Nothing estimated usually means the field id is wrong, not that nobody
    estimated.
    """
    _, slack = run_sprint(reply=HEALTHY_REPLY, points_total=None)
    assert any("項目" in m["text"] for m in slack.posted)
