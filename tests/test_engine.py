import json
from pathlib import Path

import pytest

from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.mock import MockJiraAdapter, MockSlackAdapter, MockTeamsAdapter
from aipmo.dsl import loader
from aipmo.dsl.expr import evaluate_condition, lookup, render
from aipmo.engine.runner import Engine, PromptLibrary, StepFailure
from aipmo.llm.base import EchoProvider, LLMResponse
from aipmo.llm.registry import LLMRegistry

ROOT = Path(__file__).resolve().parents[1]


# --- expr -----------------------------------------------------------------

def test_lookup_nested_and_index():
    scope = {"steps": {"a": {"output": {"items": [{"title": "X"}]}}}}
    assert lookup("steps.a.output.items[0].title", scope) == "X"


def test_render_preserves_type_for_whole_placeholder():
    scope = {"steps": {"a": {"output": [1, 2, 3]}}}
    assert render("{{ steps.a.output }}", scope) == [1, 2, 3]


def test_render_interpolates_within_string():
    scope = {"run": {"date": "2026-08-27"}}
    assert render("本日は {{ run.date }} です", scope) == "本日は 2026-08-27 です"


def test_condition_comparison():
    scope = {"steps": {"a": {"output": {"count": 5}}}}
    assert evaluate_condition("{{ steps.a.output.count }} > 3", scope) is True
    assert evaluate_condition("{{ steps.a.output.count }} > 10", scope) is False


# --- loader ---------------------------------------------------------------

def test_forward_reference_is_rejected():
    raw = {
        "name": "bad",
        "steps": [
            {"id": "first", "adapter": "slack", "action": "post_message",
             "inputs": {"text": "{{ steps.later.output }}"}},
            {"id": "later", "adapter": "slack", "action": "post_message"},
        ],
    }
    with pytest.raises(loader.TemplateError, match="後方のステップ"):
        loader.load_dict(raw)


def test_duplicate_step_id_is_rejected():
    raw = {
        "name": "dup",
        "steps": [
            {"id": "a", "adapter": "slack", "action": "post_message"},
            {"id": "a", "adapter": "slack", "action": "post_message"},
        ],
    }
    with pytest.raises(loader.TemplateError, match="重複"):
        loader.load_dict(raw)


def test_ambiguous_step_kind_is_rejected():
    raw = {"name": "x", "steps": [{"id": "a", "adapter": "slack", "llm": "default"}]}
    with pytest.raises(loader.TemplateError, match="いずれか 1 つ"):
        loader.load_dict(raw)


def test_example_template_loads():
    template = loader.load_file(ROOT / "templates/examples/meeting_minutes.yaml")
    assert template.industry == "software"
    assert template.trigger.type == "event"
    assert template.step_ids() == [
        "fetch_transcript", "minutes", "todos", "register_jira", "notify",
    ]


# --- engine ---------------------------------------------------------------

class ScriptedProvider(EchoProvider):
    """呼ばれた順にあらかじめ用意した応答を返す。"""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)

    def complete(self, request):
        self.calls.append(request)
        return LLMResponse(text=self._responses.pop(0), model="scripted")


def build_engine(llm_responses):
    adapters = AdapterRegistry()
    adapters.register(MockTeamsAdapter())
    jira = MockJiraAdapter()
    slack = MockSlackAdapter()
    adapters.register(jira)
    adapters.register(slack)

    llms = LLMRegistry()
    llms.register("default", ScriptedProvider(llm_responses))

    engine = Engine(adapters, llms, PromptLibrary(ROOT / "prompts"))
    return engine, jira, slack


MINUTES = json.dumps({
    "title": "認証基盤移行の設計レビュー",
    "summary": "移行方針を確認した。",
    "decisions": ["来週金曜までに設計レビューを完了する"],
    "action_items": [
        {"assignee": "佐藤", "task": "API 互換対応の一覧作成", "due_hint": "水曜まで"},
    ],
    "open_questions": [],
}, ensure_ascii=False)

TODOS = json.dumps({
    "items": [
        {"summary": "API 互換対応の一覧を作成する",
         "description": "認証基盤移行にあたり影響範囲を洗い出す",
         "assignee": "佐藤", "due_date": "2026-09-02",
         "priority": "High", "confidence": 0.9},
    ]
}, ensure_ascii=False)


def test_full_run_creates_issue_and_notifies():
    engine, jira, slack = build_engine([MINUTES, TODOS])
    template = loader.load_file(ROOT / "templates/examples/meeting_minutes.yaml")

    ctx = engine.run(template, trigger={"meeting_id": "MTG-001"})

    assert all(r.status == "success" for r in ctx.results.values())
    assert len(jira.created) == 1
    assert jira.created[0]["assignee"] == "佐藤"
    assert jira.created[0]["project"] == "PROJ"
    assert "認証基盤移行の設計レビュー" in slack.posted[0]["text"]


def test_idempotency_key_is_derived_from_meeting_not_run():
    engine, jira, _ = build_engine([MINUTES, TODOS])
    template = loader.load_file(ROOT / "templates/examples/meeting_minutes.yaml")
    engine.run(template, trigger={"meeting_id": "MTG-001"})

    engine2, jira2, _ = build_engine([MINUTES, TODOS])
    engine2.run(template, trigger={"meeting_id": "MTG-001"})

    # 別プロセスで再実行してもキーが一致する = 実アダプタ側で重複排除できる
    assert jira.created[0]["idempotency_key"] == jira2.created[0]["idempotency_key"]


def test_when_false_skips_step():
    empty_todos = json.dumps({"items": []}, ensure_ascii=False)
    engine, jira, _ = build_engine([MINUTES, empty_todos])
    template = loader.load_file(ROOT / "templates/examples/meeting_minutes.yaml")

    ctx = engine.run(template, trigger={"meeting_id": "MTG-002"})

    assert ctx.results["register_jira"].status == "skipped"
    assert jira.created == []


def test_missing_required_key_fails_early():
    broken = json.dumps({"title": "x"}, ensure_ascii=False)
    engine, _, _ = build_engine([broken])
    template = loader.load_file(ROOT / "templates/examples/meeting_minutes.yaml")

    with pytest.raises(StepFailure, match="minutes"):
        engine.run(template, trigger={"meeting_id": "MTG-003"})


def test_retry_then_success():
    class FlakyAdapter(MockSlackAdapter):
        name = "slack"

        def __init__(self):
            super().__init__()
            self.attempts = 0

        def post_message(self, channel, text, thread_ts=None):
            self.attempts += 1
            if self.attempts < 2:
                raise RuntimeError("一時的な障害")
            return super().post_message(channel, text, thread_ts)

    adapters = AdapterRegistry()
    adapters.register(FlakyAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())

    raw = {
        "name": "retry_demo",
        "steps": [{
            "id": "post", "adapter": "slack", "action": "post_message",
            "inputs": {"channel": "#x", "text": "hi"},
            "retry": {"max_attempts": 3, "backoff_seconds": 0},
        }],
    }
    ctx = Engine(adapters, llms).run(loader.load_dict(raw))
    assert ctx.results["post"].status == "success"
    assert ctx.results["post"].attempts == 2


# --- 人が読む文への差し込み / interpolation into prose ----------------------

def test_a_list_of_values_becomes_bullets_not_json():
    """通知に角括弧と引用符を並べない。読むのは人なので。"""
    scope = {"steps": {"a": {"output": ["設計レビューを完了する", "負荷試験を実施する"]}}}
    text = render("決定事項:\n{{ steps.a.output }}", scope)

    assert "[" not in text and '"' not in text
    assert "- 設計レビューを完了する" in text
    assert "- 負荷試験を実施する" in text


def test_a_single_item_list_is_not_bulleted():
    scope = {"steps": {"a": {"output": ["ひとつだけ"]}}}
    assert render("{{ steps.a.output }} です", scope) == "ひとつだけ です"


def test_an_empty_list_renders_as_nothing():
    scope = {"steps": {"a": {"output": []}}}
    assert render("結果:{{ steps.a.output }}", scope) == "結果:"


def test_nested_structures_stay_as_json():
    """平らにすると何を見ているのか分からなくなる。"""
    scope = {"steps": {"a": {"output": [{"task": "x", "assignee": "佐藤"}]}}}
    text = render("内容: {{ steps.a.output }}", scope)   # 文中なので文字列化される
    assert "assignee" in text


def test_whole_placeholder_still_preserves_type():
    """後続ステップへ構造を渡す経路は変えていない。"""
    scope = {"steps": {"a": {"output": ["x", "y"]}}}
    assert render("{{ steps.a.output }}", scope) == ["x", "y"]


# --- 繰り返し / iteration ---------------------------------------------------

def _looping_engine(slack=None):
    from aipmo.adapters.base import AdapterRegistry
    from aipmo.adapters.mock import MockSlackAdapter
    from aipmo.llm.base import EchoProvider
    from aipmo.llm.registry import LLMRegistry

    adapters = AdapterRegistry()
    adapters.register(slack or MockSlackAdapter())
    llms = LLMRegistry()
    llms.register("default", EchoProvider())
    return Engine(adapters, llms), adapters.get("slack")


def _loop_template(**overrides):
    step = {
        "id": "chase", "for_each": "{{ params.people }}", "as": "person",
        "adapter": "slack", "action": "post_message",
        "inputs": {"channel": "{{ person.id }}", "text": "{{ person.name }} さんへ"},
    }
    step.update(overrides)
    return loader.load_dict({"name": "loop", "steps": [step]})


def test_a_step_runs_once_per_element():
    engine, slack = _looping_engine()
    ctx = engine.run(_loop_template(), params={"people": [
        {"id": "U1", "name": "田中"}, {"id": "U2", "name": "佐藤"},
    ]})

    assert ctx.results["chase"].output["count"] == 2
    assert [m["channel"] for m in slack.posted] == ["U1", "U2"]
    assert "田中 さんへ" in slack.posted[0]["text"]


def test_an_empty_list_is_not_a_failure():
    """遅延が0件なら催促は0通で正しい。"""
    engine, slack = _looping_engine()
    ctx = engine.run(_loop_template(), params={"people": []})

    assert ctx.results["chase"].status == "success"
    assert ctx.results["chase"].output["count"] == 0
    assert slack.posted == []


def test_one_failure_does_not_stop_the_rest():
    """3人目で失敗しても、4人目と5人目には届くこと。"""
    from aipmo.adapters.mock import MockSlackAdapter

    class Picky(MockSlackAdapter):
        def post_message(self, channel, text, thread_ts=None):
            if channel == "U2":
                raise RuntimeError("no such user")
            return super().post_message(channel, text, thread_ts)

    engine, slack = _looping_engine(Picky())
    ctx = engine.run(_loop_template(), params={"people": [
        {"id": "U1", "name": "a"}, {"id": "U2", "name": "b"}, {"id": "U3", "name": "c"},
    ]})

    output = ctx.results["chase"].output
    assert ctx.results["chase"].status == "success"
    assert output["count"] == 2 and output["failed"] == 1
    assert [m["channel"] for m in slack.posted] == ["U1", "U3"]


def test_all_failing_marks_the_step_failed():
    from aipmo.adapters.mock import MockSlackAdapter

    class Broken(MockSlackAdapter):
        def post_message(self, channel, text, thread_ts=None):
            raise RuntimeError("down")

    engine, _ = _looping_engine(Broken())
    template = _loop_template(continue_on_error=True)
    ctx = engine.run(template, params={"people": [{"id": "U1", "name": "a"}]})

    assert ctx.results["chase"].status == "failed"


def test_the_item_cap_stops_runaway_sends():
    """500通の DM を誤って送るより、途中で止めて気づける方がよい。"""
    engine, slack = _looping_engine()
    template = _loop_template(max_items=3)
    ctx = engine.run(template, params={
        "people": [{"id": f"U{i}", "name": str(i)} for i in range(10)]})

    assert ctx.results["chase"].output["count"] == 3
    assert ctx.results["chase"].output["truncated"] is True
    assert len(slack.posted) == 3


def test_the_loop_index_is_available():
    engine, slack = _looping_engine()
    template = _loop_template(inputs={
        "channel": "#x", "text": "{{ loop.index }}/{{ loop.total }}"})
    engine.run(template, params={"people": [{"id": "a"}, {"id": "b"}]})

    assert slack.posted[0]["text"] == "0/2"
    assert slack.posted[1]["text"] == "1/2"


def test_a_non_list_is_rejected_clearly():
    engine, _ = _looping_engine()
    ctx = engine.run(_loop_template(continue_on_error=True),
                     params={"people": "not a list"})
    assert "list" in ctx.results["chase"].error


def test_reserved_names_cannot_be_used_as_the_loop_variable():
    with pytest.raises(loader.TemplateError, match="reserved"):
        _loop_template(**{"as": "steps"})


def test_the_cap_is_bounded_in_the_template():
    with pytest.raises(loader.TemplateError, match="max_items"):
        _loop_template(max_items=5000)


def test_later_steps_can_read_the_loop_results():
    engine, slack = _looping_engine()
    template = loader.load_dict({"name": "loop", "steps": [
        {"id": "chase", "for_each": "{{ params.people }}", "as": "person",
         "adapter": "slack", "action": "post_message",
         "inputs": {"channel": "{{ person.id }}", "text": "hi"}},
        {"id": "summary", "adapter": "slack", "action": "post_message",
         "inputs": {"channel": "#log",
                    "text": "{{ steps.chase.output.count }} 件送信"}},
    ]})
    engine.run(template, params={"people": [{"id": "U1"}, {"id": "U2"}]})

    assert slack.posted[-1]["text"] == "2 件送信"


def test_where_filters_each_element():
    """要素ごとの条件。when はループの前に一度しか評価されない。"""
    engine, slack = _looping_engine()
    template = _loop_template(where="{{ person.ok }} == true")
    ctx = engine.run(template, params={"people": [
        {"id": "U1", "name": "a", "ok": True},
        {"id": "U2", "name": "b", "ok": False},
        {"id": "U3", "name": "c", "ok": True},
    ]})

    assert [m["channel"] for m in slack.posted] == ["U1", "U3"]
    assert ctx.results["chase"].output["skipped"] == 1


def test_where_can_compare_against_a_parameter():
    engine, slack = _looping_engine()
    template = _loop_template(
        where="{{ person.score }} >= {{ params.threshold }}")
    engine.run(template, params={
        "threshold": 0.8,
        "people": [{"id": "U1", "name": "a", "score": 0.95},
                   {"id": "U2", "name": "b", "score": 0.55}]})

    assert [m["channel"] for m in slack.posted] == ["U1"]


def test_an_unevaluable_condition_skips_rather_than_writes():
    """判定できないまま書き込むより、飛ばして数に残す方が安全。"""
    engine, slack = _looping_engine()
    template = _loop_template(where="{{ person.missing }} > 1")
    ctx = engine.run(template, params={"people": [{"id": "U1", "name": "a"}]})

    assert slack.posted == []
    assert ctx.results["chase"].output["skipped"] == 1


def test_where_requires_for_each():
    with pytest.raises(loader.TemplateError, match="for_each"):
        loader.load_dict({"name": "x", "steps": [
            {"id": "a", "where": "{{ params.x }} > 1",
             "adapter": "slack", "action": "post_message",
             "inputs": {"channel": "#a", "text": "x"}}]})


def test_a_step_depending_on_a_skipped_step_is_skipped_not_failed():
    """先行が飛ばされたら、その出力を見る工程も飛ばす。

    条件付きの工程を連ねれば必ず起きる形。ここで実行ごと止めるのは誤り。
    Chaining conditional steps makes this inevitable; aborting the run for it
    is the wrong answer.
    """
    engine, slack = _looping_engine()
    template = loader.load_dict({"name": "chain", "steps": [
        {"id": "first", "when": "{{ params.go }} == true",
         "adapter": "slack", "action": "post_message",
         "inputs": {"channel": "#a", "text": "x"}},
        {"id": "second", "when": "{{ steps.first.output.ok }} == true",
         "adapter": "slack", "action": "post_message",
         "inputs": {"channel": "#b", "text": "y"}},
    ]})
    ctx = engine.run(template, params={"go": False})

    assert ctx.results["first"].status == "skipped"
    assert ctx.results["second"].status == "skipped"
    assert slack.posted == []


# --- 組み込み変換 / built-in transforms -------------------------------------

def test_days_between_counts_for_the_template():
    """テンプレートに計算の仕組みは無い。渡さないとモデルが数えることになる。"""
    from aipmo.engine.runner import BUILTIN_TRANSFORMS

    assert BUILTIN_TRANSFORMS["days_between"]("2026-08-28", "2026-09-30") == 33


def test_days_between_handles_a_timestamp():
    from aipmo.engine.runner import BUILTIN_TRANSFORMS

    assert BUILTIN_TRANSFORMS["days_between"](
        "2026-08-28T09:00:00Z", "2026-08-30") == 2


def test_a_past_date_gives_a_negative_number():
    """過ぎていることを 0 に丸めない。まだ間に合うように読める。"""
    from aipmo.engine.runner import BUILTIN_TRANSFORMS

    assert BUILTIN_TRANSFORMS["days_between"]("2026-09-30", "2026-08-28") < 0


def test_an_unreadable_date_yields_nothing_rather_than_stopping_the_run():
    from aipmo.engine.runner import BUILTIN_TRANSFORMS

    assert BUILTIN_TRANSFORMS["days_between"]("2026-08-28", "来週") is None


def test_a_transform_step_runs_in_a_template():
    engine, slack = _looping_engine()
    template = loader.load_dict({"name": "t", "steps": [
        {"id": "left", "expression": "days_between",
         "inputs": {"start": "{{ run.date }}", "end": "2099-01-01"}},
        {"id": "say", "adapter": "slack", "action": "post_message",
         "inputs": {"channel": "#x", "text": "残り {{ steps.left.output }} 日"}},
    ]})
    engine.run(template)

    assert "残り" in slack.posted[0]["text"]
    assert "{{" not in slack.posted[0]["text"]
