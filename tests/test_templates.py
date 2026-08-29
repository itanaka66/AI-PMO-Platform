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
INDUSTRIES = ROOT / "templates" / "industries"
ALL_TEMPLATES = sorted((ROOT / "templates").rglob("*.yaml"))


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

@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.stem)
def test_every_shipped_template_loads(path):
    loader.load_file(path)


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.stem)
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


# --- 業界別 / industry templates --------------------------------------------

@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.stem)
def test_every_template_declares_its_industry(path):
    """業界が分からないと、どれを使えばよいか選べない。"""
    assert loader.load_file(path).industry


# --- 建設 / construction ----------------------------------------------------

CONSTRUCTION_REPLY = json.dumps({
    "title": "C工区 工程会議",
    "summary": "躯体工事の進捗を確認",
    "progress": ["3階躯体 出来高 60%"],
    "decisions": ["4階の型枠に着手"],
    "safety_lines": ["[即時] 4階north — 足場の手すり欠落（鈴木）",
                     "[予定] 1階 — 資材置場の通路が狭い（田中）"],
    "safety_items": [
        {"description": "足場の手すり欠落", "location": "4階north",
         "raised_by": "鈴木", "urgency": "immediate"},
        {"description": "通路が狭い", "location": "1階",
         "raised_by": "田中", "urgency": "scheduled"},
    ],
    "corrections": [{"assignee": "鈴木", "task": "足場手すりの是正",
                     "due_hint": "本日中"}],
    "weather_impact": "金曜は降雨予報",
    "open_questions": [],
}, ensure_ascii=False)

SAFE_REPLY = json.dumps({
    "title": "C工区 工程会議", "summary": "確認", "progress": [],
    "decisions": [], "safety_lines": [], "safety_items": [],
    "corrections": [], "weather_impact": None, "open_questions": [],
}, ensure_ascii=False)


def run_site_meeting(reply=CONSTRUCTION_REPLY):
    adapters = AdapterRegistry()
    adapters.register(FakeTeams())
    jira, slack = MockJiraAdapter(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "construction" / "site_meeting.yaml"),
        params={"organiser": "t@example.com"},
        trigger={"join_url": "https://teams.microsoft.com/l/x"})
    return ctx, jira, slack


def test_urgent_safety_goes_to_the_safety_channel_on_its_own():
    """工程の遅れは明日取り戻せるが、けがは取り戻せない。

    ほかの報告に混ぜると流し読みで飛ばされるので、単独で送る。
    A schedule slip can be made up tomorrow; an injury cannot. Mixed in with
    other items it gets skimmed past, so it is sent alone.
    """
    _, _, slack = run_site_meeting()
    urgent = [m for m in slack.posted if "【安全】" in m["text"]]

    assert len(urgent) == 1
    assert urgent[0]["channel"] == "#safety"
    assert "足場の手すり欠落" in urgent[0]["text"]


def test_only_immediate_items_raise_an_alert():
    """予定して直すものまで即時通知にすると、即時の重みが失われる。"""
    _, _, slack = run_site_meeting()
    urgent = [m for m in slack.posted if "【安全】" in m["text"]]

    assert "通路が狭い" not in urgent[0]["text"]


def test_safety_never_goes_to_the_progress_channel():
    _, _, slack = run_site_meeting()
    progress = [m for m in slack.posted if m["channel"] == "#site-updates"]

    assert progress
    assert all("足場" not in m["text"] for m in progress)


def test_the_safety_digest_is_readable_not_json():
    """現場で読まれるもの。生の JSON を貼らない。"""
    _, _, slack = run_site_meeting()
    digest = [m for m in slack.posted if "指摘一覧" in m["text"]][0]

    assert "[即時] 4階north" in digest["text"]
    assert '{"' not in digest["text"]


def test_corrections_become_issues():
    _, jira, _ = run_site_meeting()
    assert jira.created[0]["assignee"] == "鈴木"


def test_a_meeting_without_safety_items_raises_no_alert():
    """指摘が無いのに安全チャンネルへ送らない。"""
    _, _, slack = run_site_meeting(reply=SAFE_REPLY)
    assert [m for m in slack.posted if m["channel"] == "#safety"] == []


# --- マーケティング / marketing ---------------------------------------------

CAMPAIGN_REPLY = json.dumps({
    "assessment": "注意",
    "headline": "承認待ちが3件たまっています。",
    "blocked_by_approval": [
        {"item": "MKT-12", "waiting_days": 6, "who_to_ask": "法務"},
    ],
    "at_risk": ["MKT-12 のバナー差し替え"],
    "concerns": [],
}, ensure_ascii=False)

HEALTHY_CAMPAIGN = json.dumps({
    "assessment": "順調", "headline": "予定どおりです。",
    "blocked_by_approval": [], "at_risk": [], "concerns": [],
}, ensure_ascii=False)


class SearchingJira(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [{"key": "MKT-12", "summary": "バナー差し替え"}],
                "count": 1}


SearchingJira.search = action()(SearchingJira.search)


def run_campaign(reply=CAMPAIGN_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJira(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "marketing" / "campaign_check.yaml"))
    return ctx, slack


def test_days_to_launch_are_counted_not_left_to_the_model():
    """日付だけ渡すと、モデルが自分で数えることになる。"""
    ctx, _ = run_campaign()
    assert isinstance(ctx.results["days_left"].output, int)


def test_approval_waits_are_reported_separately_from_late_work():
    """遅れている作業は担当者に聞けば動くが、承認待ちは動かない。
    同じ文面で送ると、動かせない人を責めることになる。

    Chasing the assignee cannot move an approval, and the wording used for late
    work would be blaming someone with no means to act.
    """
    _, slack = run_campaign()
    approval = [m for m in slack.posted if "承認待ち" in m["text"]]

    assert approval
    assert "作業側は手を離れています" in approval[0]["text"]


def test_a_healthy_campaign_says_nothing():
    _, slack = run_campaign(reply=HEALTHY_CAMPAIGN)
    assert slack.posted == []


# --- 製造 / manufacturing ----------------------------------------------------

DOWNTIME_REPLY = json.dumps({
    "headline": "安全案件が1件、資材待ちが1件あります",
    "safety_items": [
        {"issue_key": "MFG-1", "line": "Aライン", "description": "投入部のロックアウト未解除",
         "downtime_hours": 2},
    ],
    "supply_blocked": [
        {"issue_key": "MFG-2", "line": "Bライン", "waiting_on": "モーター部品",
         "downtime_hours": 6},
    ],
    "internal": [],
}, ensure_ascii=False)

NO_STOPPAGES_REPLY = json.dumps({
    "headline": "対応が必要な停止はありません",
    "safety_items": [], "supply_blocked": [], "internal": [],
}, ensure_ascii=False)


class SearchingJiraStoppages(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "MFG-1", "summary": "投入部が停止"},
            {"key": "MFG-2", "summary": "モーター交換待ち"},
        ], "count": 2}


SearchingJiraStoppages.search = action()(SearchingJiraStoppages.search)


def run_downtime(reply=DOWNTIME_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraStoppages(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "manufacturing" / "line_downtime_triage.yaml"))
    return ctx, slack


def test_safety_stoppages_go_to_the_safety_channel_alone():
    """安全に関わる停止は、進捗の報告に混ぜない。"""
    _, slack = run_downtime()
    safety = [m for m in slack.posted if m["channel"] == "#safety"]

    assert len(safety) == 1
    assert "ロックアウト" in safety[0]["text"]


def test_supply_blocked_stoppages_go_to_procurement_not_the_floor():
    """資材待ちは現場ではなく調達へ。現場に催促しても資材は届かない。"""
    _, slack = run_downtime()
    supply = [m for m in slack.posted if m["channel"] == "#procurement"]
    floor = [m for m in slack.posted if m["channel"] == "#line-a-floor"]

    assert supply and "モーター部品" in supply[0]["text"]
    assert all("モーター部品" not in m["text"] for m in floor)


def test_internal_fixes_are_batched_not_sent_individually():
    """件数分バラバラに送ると、対応が必要なものが埋もれる。"""
    reply = json.dumps({
        "headline": "内製で直せる停止が2件",
        "safety_items": [], "supply_blocked": [],
        "internal": [
            {"issue_key": "MFG-3", "line": "Cライン", "description": "センサー再調整",
             "downtime_hours": 1},
            {"issue_key": "MFG-4", "line": "Dライン", "description": "ベルト張り直し",
             "downtime_hours": 1},
        ],
    }, ensure_ascii=False)
    _, slack = run_downtime(reply=reply)
    floor = [m for m in slack.posted if m["channel"] == "#line-a-floor"]

    assert len(floor) == 1
    assert "MFG-3" in floor[0]["text"] and "MFG-4" in floor[0]["text"]


def test_nothing_is_said_when_no_stoppage_needs_attention():
    _, slack = run_downtime(reply=NO_STOPPAGES_REPLY)
    assert slack.posted == []


# --- 法務 / legal and compliance ---------------------------------------------

DEADLINE_REPLY = json.dumps({
    "urgent": [
        {"issue_key": "LEGAL-1", "matter_name": "契約更新期限", "deadline": "2026-09-01",
         "days_until_deadline": 2},
    ],
    "blocked": [
        {"issue_key": "LEGAL-2", "matter_name": "係争案件A", "waiting_on": "相手方の回答待ち"},
    ],
    "internal": [],
    "privileged": [
        {"issue_key": "LEGAL-3", "days_until_deadline": 5},
    ],
}, ensure_ascii=False)

NO_MATTERS_REPLY = json.dumps({
    "urgent": [], "blocked": [], "internal": [], "privileged": [],
}, ensure_ascii=False)


class SearchingJiraMatters(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "LEGAL-1", "summary": "契約更新"},
            {"key": "LEGAL-2", "summary": "係争案件A"},
            {"key": "LEGAL-3", "summary": "秘匿特権対象案件"},
        ], "count": 3}


SearchingJiraMatters.search = action()(SearchingJiraMatters.search)


def run_legal(reply=DEADLINE_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraMatters(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "legal" / "matter_deadline_triage.yaml"))
    return ctx, slack


def test_urgent_deadlines_are_sent_alone_to_the_urgent_channel():
    """期日を過ぎると法的な効果が生じうる。まとめて翌朝に送らない。"""
    _, slack = run_legal()
    urgent = [m for m in slack.posted if m["channel"] == "#legal-urgent"]

    assert len(urgent) == 1
    assert "契約更新期限" in urgent[0]["text"]
    assert "残り 2 日" in urgent[0]["text"]


def test_matters_blocked_on_the_other_side_are_not_a_chase():
    """担当者に確認しても、相手方や裁判所は動かせない。"""
    _, slack = run_legal()
    blocked = [m for m in slack.posted if "相手方の回答待ち" in m["text"]]

    assert blocked
    assert blocked[0]["channel"] == "#legal-ops"
    assert "担当者の手を離れています" in blocked[0]["text"]


def test_privileged_matters_go_only_to_the_restricted_channel_without_detail():
    """秘匿特権の対象は、緊急度に関わらず限定チャンネルへ、詳細を伏せて。"""
    _, slack = run_legal()
    privileged = [m for m in slack.posted if m["channel"] == "#legal-privileged"]
    other_channels_text = "".join(
        m["text"] for m in slack.posted if m["channel"] != "#legal-privileged")

    assert privileged and "LEGAL-3" in privileged[0]["text"]
    # 案件名や内容は他のどの通知にも出てこないこと。
    assert "LEGAL-3" not in other_channels_text


def test_nothing_is_said_when_no_matter_needs_attention():
    _, slack = run_legal(reply=NO_MATTERS_REPLY)
    assert slack.posted == []


# --- カスタマーサクセス / customer success -----------------------------------

ACCOUNT_HEALTH_REPLY = json.dumps({
    "at_risk": [
        {"issue_key": "CS-1", "account_name": "株式会社アクメ",
         "reason": "ヘルススコア低下、更新間近", "days_until_renewal": 14},
    ],
    "waiting_on_customer": [
        {"issue_key": "CS-2", "account_name": "株式会社ベータ",
         "waiting_on": "導入データの提供待ち"},
    ],
    "internal_delivery": [],
}, ensure_ascii=False)

NO_ACCOUNTS_REPLY = json.dumps({
    "at_risk": [], "waiting_on_customer": [], "internal_delivery": [],
}, ensure_ascii=False)


class SearchingJiraAccounts(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "CS-1", "summary": "株式会社アクメ 更新前レビュー"},
            {"key": "CS-2", "summary": "株式会社ベータ 導入対応"},
        ], "count": 2}


SearchingJiraAccounts.search = action()(SearchingJiraAccounts.search)


def run_account_health(reply=ACCOUNT_HEALTH_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraAccounts(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "customer_success" / "account_health_triage.yaml"))
    return ctx, slack


def test_at_risk_accounts_are_escalated_alone_to_leadership():
    """解約は後戻りしにくい。翌朝まとめて送っても対処の時間が削られるだけ。"""
    _, slack = run_account_health()
    escalated = [m for m in slack.posted if m["channel"] == "#cs-leadership"]

    assert len(escalated) == 1
    assert "株式会社アクメ" in escalated[0]["text"]
    assert "残り 14 日" in escalated[0]["text"]


def test_customer_waits_are_not_framed_as_blaming_the_owner():
    """社内の遅れと違い、担当者の落ち度ではない。"""
    _, slack = run_account_health()
    waiting = [m for m in slack.posted if m["channel"] == "#csm-followups"]

    assert waiting and "導入データの提供待ち" in waiting[0]["text"]
    assert "急かす連絡にはしない" in waiting[0]["text"]


def test_internal_delays_go_to_the_team_not_the_csm_channel():
    """自社側の遅れは、緊急度を上げて配送チームへ。顧客の遅れとは別扱い。"""
    reply = json.dumps({
        "at_risk": [], "waiting_on_customer": [],
        "internal_delivery": [
            {"issue_key": "CS-3", "account_name": "株式会社ガンマ",
             "next_step": "導入手順書の送付"},
        ],
    }, ensure_ascii=False)
    _, slack = run_account_health(reply=reply)
    team = [m for m in slack.posted if m["channel"] == "#customer-success"]
    csm = [m for m in slack.posted if m["channel"] == "#csm-followups"]

    assert team and "株式会社ガンマ" in team[0]["text"]
    assert csm == []


def test_nothing_is_said_when_every_account_is_healthy():
    _, slack = run_account_health(reply=NO_ACCOUNTS_REPLY)
    assert slack.posted == []


# --- 財務監査 / financial services and audit ---------------------------------

FINDING_REPLY = json.dumps({
    "material_weakness": [
        {"issue_key": "AUDIT-1", "finding": "決算処理の承認統制が機能していない",
         "days_until_deadline": 14},
    ],
    "significant_deficiency": [
        {"issue_key": "AUDIT-2", "finding": "アクセス権限の定期棚卸が未実施",
         "owner": "情報システム部長", "days_until_deadline": 30},
    ],
    "control_deficiency": [],
}, ensure_ascii=False)

NO_FINDINGS_REPLY = json.dumps({
    "material_weakness": [], "significant_deficiency": [], "control_deficiency": [],
}, ensure_ascii=False)


class SearchingJiraFindings(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "AUDIT-1", "summary": "決算処理の承認統制"},
            {"key": "AUDIT-2", "summary": "アクセス権限の棚卸"},
        ], "count": 2}


SearchingJiraFindings.search = action()(SearchingJiraFindings.search)


def run_audit(reply=FINDING_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraFindings(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "financial_audit" / "finding_remediation_triage.yaml"))
    return ctx, slack


def test_material_weakness_is_sent_alone_to_the_audit_committee():
    """財務諸表の信頼性に関わるため、翌週の定例報告まで待たない。"""
    _, slack = run_audit()
    board = [m for m in slack.posted if m["channel"] == "#audit-committee"]

    assert len(board) == 1
    assert "承認統制が機能していない" in board[0]["text"]
    assert "残り 14 日" in board[0]["text"]


def test_significant_deficiency_goes_to_management_not_the_audit_team():
    """監査チームの是正担当者だけでは、統制の所有者を動かせないことが多い。"""
    _, slack = run_audit()
    management = [m for m in slack.posted if m["channel"] == "#audit-management"]
    team = [m for m in slack.posted if m["channel"] == "#audit-remediation"]

    assert management and "アクセス権限" in management[0]["text"]
    assert team == []


def test_control_deficiencies_are_batched_for_the_audit_team():
    reply = json.dumps({
        "material_weakness": [], "significant_deficiency": [],
        "control_deficiency": [
            {"issue_key": "AUDIT-3", "finding": "証憑の保管ルール未整備",
             "next_step": "保管ルールの策定"},
            {"issue_key": "AUDIT-4", "finding": "承認履歴の記録漏れ",
             "next_step": "記録様式の見直し"},
        ],
    }, ensure_ascii=False)
    _, slack = run_audit(reply=reply)
    team = [m for m in slack.posted if m["channel"] == "#audit-remediation"]

    assert len(team) == 1
    assert "AUDIT-3" in team[0]["text"] and "AUDIT-4" in team[0]["text"]


def test_nothing_is_said_when_no_finding_needs_attention():
    _, slack = run_audit(reply=NO_FINDINGS_REPLY)
    assert slack.posted == []


# --- 保険 / insurance claims --------------------------------------------------

CLAIM_REPLY = json.dumps({
    "deadline_at_risk": [
        {"issue_key": "CLAIM-1", "claim_number": "CA-2026-0042",
         "jurisdiction": "California", "days_until_deadline": 1},
    ],
    "fraud_referral": [
        {"issue_key": "CLAIM-2", "claim_number": "TX-2026-0099", "days_until_deadline": 5},
    ],
    "policyholder_blocked": [
        {"issue_key": "CLAIM-3", "claim_number": "NY-2026-0110",
         "waiting_on": "被害箇所の写真提出"},
    ],
    "internal": [],
}, ensure_ascii=False)

NO_CLAIMS_REPLY = json.dumps({
    "deadline_at_risk": [], "fraud_referral": [], "policyholder_blocked": [], "internal": [],
}, ensure_ascii=False)


class SearchingJiraClaims(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "CLAIM-1", "summary": "追突事故"},
            {"key": "CLAIM-2", "summary": "火災損害"},
            {"key": "CLAIM-3", "summary": "水漏れ損害"},
        ], "count": 3}


SearchingJiraClaims.search = action()(SearchingJiraClaims.search)


def run_claims(reply=CLAIM_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraClaims(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "insurance" / "claim_sla_triage.yaml"))
    return ctx, slack


def test_deadline_at_risk_claims_are_sent_alone_immediately():
    """規制期限は州ごとに異なり、守れないと行政処分につながりうる。"""
    _, slack = run_claims()
    urgent = [m for m in slack.posted if m["channel"] == "#claims-sla-alerts"]

    assert len(urgent) == 1
    assert "CA-2026-0042" in urgent[0]["text"]
    assert "残り 1 日" in urgent[0]["text"]


def test_fraud_referrals_go_only_to_siu_without_detail():
    """不正の疑いは、期限や対応状況に関わらず調査部門だけに、詳細を伏せて。"""
    _, slack = run_claims()
    siu = [m for m in slack.posted if m["channel"] == "#siu-referrals"]
    other_channels_text = "".join(
        m["text"] for m in slack.posted if m["channel"] != "#siu-referrals")

    assert siu and "TX-2026-0099" in siu[0]["text"]
    assert "TX-2026-0099" not in other_channels_text


def test_policyholder_waits_are_not_a_chase_aimed_at_the_adjuster():
    """担当者に確認しても、契約者の対応は早まらない。"""
    _, slack = run_claims()
    blocked = [m for m in slack.posted if "被害箇所の写真提出" in m["text"]]

    assert blocked
    assert blocked[0]["channel"] == "#claims-processing"
    assert "急かす連絡にはしないこと" in blocked[0]["text"]


def test_nothing_is_said_when_no_claim_needs_attention():
    _, slack = run_claims(reply=NO_CLAIMS_REPLY)
    assert slack.posted == []


# --- 政府調達 / government contracting ---------------------------------------

CLEARANCE_REPLY = json.dumps({
    "clearance_blocked": [
        {"issue_key": "GOVCON-1", "task_name": "暗号モジュール統合", "assignee": "山田太郎",
         "clearance_status": "expiring_soon", "days_until_clearance_expiry": 20},
    ],
    "deliverable_at_risk": [
        {"issue_key": "GOVCON-2", "deliverable_name": "CDRL A003 月次進捗報告",
         "days_until_deadline": 3},
    ],
    "internal": [],
}, ensure_ascii=False)

NO_TASKS_REPLY = json.dumps({
    "clearance_blocked": [], "deliverable_at_risk": [], "internal": [],
}, ensure_ascii=False)


class SearchingJiraGovTasks(MockJiraAdapter):
    def search(self, jql, fields=None, limit=50):
        return {"items": [
            {"key": "GOVCON-1", "summary": "暗号モジュール統合"},
            {"key": "GOVCON-2", "summary": "月次進捗報告の提出"},
        ], "count": 2}


SearchingJiraGovTasks.search = action()(SearchingJiraGovTasks.search)


def run_govcon(reply=CLEARANCE_REPLY):
    adapters = AdapterRegistry()
    jira, slack = SearchingJiraGovTasks(), MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", Scripted([reply]))

    ctx = Engine(adapters, llms, PromptLibrary(ROOT / "prompts")).run(
        loader.load_file(INDUSTRIES / "government_contracting"
                         / "clearance_deliverable_triage.yaml"))
    return ctx, slack


def test_clearance_blocked_tasks_go_to_the_fso_not_the_program_team():
    """現場に確認しても、クリアランスの発給・更新は進められない。"""
    _, slack = run_govcon()
    fso = [m for m in slack.posted if m["channel"] == "#fso-clearance-alerts"]
    team = [m for m in slack.posted if m["channel"] == "#program-delivery"]

    assert fso and "山田太郎" in fso[0]["text"]
    assert team == []


def test_deliverable_deadlines_are_sent_alone_immediately():
    """契約上の納品期限を過ぎると、契約履行評価に影響しうる。"""
    _, slack = run_govcon()
    deliverables = [m for m in slack.posted if m["channel"] == "#cdrl-deadlines"]

    assert len(deliverables) == 1
    assert "CDRL A003" in deliverables[0]["text"]
    assert "残り 3 日" in deliverables[0]["text"]


def test_internal_tasks_are_batched():
    reply = json.dumps({
        "clearance_blocked": [], "deliverable_at_risk": [],
        "internal": [
            {"issue_key": "GOVCON-3", "next_step": "設計レビュー資料の更新"},
            {"issue_key": "GOVCON-4", "next_step": "テスト計画書の作成"},
        ],
    }, ensure_ascii=False)
    _, slack = run_govcon(reply=reply)
    team = [m for m in slack.posted if m["channel"] == "#program-delivery"]

    assert len(team) == 1
    assert "GOVCON-3" in team[0]["text"] and "GOVCON-4" in team[0]["text"]


def test_nothing_is_said_when_no_task_needs_attention():
    _, slack = run_govcon(reply=NO_TASKS_REPLY)
    assert slack.posted == []
