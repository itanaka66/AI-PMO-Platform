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
