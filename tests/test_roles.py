"""役割特化テンプレートのテスト / role-specialised template tests.

確かめるのは 3 点: (1) 役割ごとに渡る道具が決まった範囲を出ないこと、
(2) 役割の憲章とパラメータがモデルに届くこと、(3) 書き込みの経路が
役割ごとの約束どおりであること（開発・テストは承認つきのコメントだけ、
ほかは読み取り専用で、外へ出るのは社内チャンネルへの投稿だけ）。

Three things: each role's tools stay inside its remit; the role charter and
params reach the model; and the write paths match each role's promise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aipmo.adapters.base import Adapter, AdapterRegistry, action
from aipmo.adapters.mock import MockSlackAdapter
from aipmo.dsl import loader
from aipmo.engine.agent import ToolBox
from aipmo.engine.runner import Engine, PromptLibrary
from aipmo.llm.base import EchoProvider, LLMResponse, ToolCall
from aipmo.llm.registry import LLMRegistry

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "templates" / "roles"


class FakeJira(Adapter):
    name = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.comments: list[dict[str, Any]] = []

    @action()
    def search(self, jql: str, limit: int = 50) -> dict[str, Any]:
        return {"items": [{"key": "PROJ-1", "summary": "ログイン改善"}], "count": 1}

    @action(writes=True)
    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        self.comments.append({"issue_key": issue_key, "text": text})
        return {"ok": True}

    @action(writes=True)
    def update_issue(self, issue_key: str, summary: str | None = None) -> dict[str, Any]:
        raise AssertionError("roles must never be handed update_issue")

    @action(writes=True)
    def create_issues(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        raise AssertionError("roles must never be handed create_issues")


class FakeVectorStore(Adapter):
    name = "vector_store"

    @action()
    def search(self, text: str | None = None, limit: int = 5) -> dict[str, Any]:
        return {"items": [], "count": 0}

    @action(writes=True)
    def upsert(self, documents: list[dict[str, Any]]) -> dict[str, Any]:
        raise AssertionError("roles must never be handed upsert")


class FakeCrawler(Adapter):
    name = "crawler"

    @action()
    def fetch_page(self, url: str) -> dict[str, Any]:
        return {"html": "<p>x</p>"}

    @action()
    def extract_text(self, html: str) -> dict[str, Any]:
        return {"text": "x"}


def says(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="scripted")


def calls(name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(text="", model="scripted",
                       tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)])


def build(script, approve=None):
    adapters = AdapterRegistry()
    jira, slack = FakeJira(), MockSlackAdapter()
    for adapter in (jira, slack, FakeVectorStore(), FakeCrawler()):
        adapters.register(adapter)
    llm = EchoProvider(script=script)
    llms = LLMRegistry()
    llms.register("default", llm)
    engine = Engine(adapters, llms, PromptLibrary(ROOT / "prompts"), approve=approve)
    return engine, adapters, jira, slack, llm


def load(name: str):
    return loader.load_file(ROLES / f"{name}.yaml")


def agent_step(template):
    (step,) = [s for s in template.steps if s.agent]
    return step


def tools_of(name: str) -> set[str]:
    template = load(name)
    _, adapters, *_ = build([])
    box = ToolBox(adapters, agent_step(template).agent)
    return {d["function"]["name"] for d in box.definitions()}


# ===== (1) 道具の範囲 / tool remit ============================================

EXPECTED_TOOLS = {
    "role_developer": {"jira__search", "jira__add_comment"},
    "role_tester": {"jira__search", "jira__add_comment"},
    "role_researcher": {"crawler__fetch_page", "crawler__extract_text",
                        "vector_store__search"},
    "role_writer": {"jira__search", "vector_store__search"},
    "role_sales": {"jira__search", "vector_store__search"},
}


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_each_role_gets_exactly_its_tools(name):
    assert tools_of(name) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", ["role_researcher", "role_writer", "role_sales"])
def test_read_only_roles_cannot_write(name):
    step = agent_step(load(name))
    assert step.agent.allow_writes is False


@pytest.mark.parametrize("name", ["role_developer", "role_tester"])
def test_roles_that_comment_need_approval_for_every_write(name):
    spec = agent_step(load(name)).agent
    assert spec.allow_writes and spec.require_approval


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_every_role_is_bounded(name):
    spec = agent_step(load(name)).agent
    assert 0 < spec.max_iterations <= 8 and spec.max_tokens_total <= 80000


# ===== (2) 憲章とパラメータ / charter and params =============================

@pytest.mark.parametrize("name,charter", [
    ("role_developer", "開発AI"), ("role_tester", "テストAI"),
    ("role_researcher", "調査AI"), ("role_writer", "文書AI"),
    ("role_sales", "営業AI"),
])
def test_role_charter_and_params_reach_the_model(name, charter):
    engine, _, _, slack, llm = build([says("結果")])
    engine.run(load(name), params={"issue_key": "PROJ-42", "question": "何が新しい?",
                                   "customer": "ACME"})
    system, user = llm.conversations[0][0], llm.conversations[0][1]
    assert system["role"] == "system" and charter in system["content"]
    assert "{{" not in user["content"]
    marker = {"role_developer": "PROJ-42", "role_tester": "PROJ-42",
              "role_researcher": "何が新しい?", "role_writer": "status_report",
              "role_sales": "ACME"}[name]
    assert marker in user["content"]


# ===== (3) 書き込みの経路 / write paths =====================================

def test_developer_comment_is_refused_without_an_approver_but_run_completes():
    script = [calls("jira__add_comment", {"issue_key": "PROJ-1", "text": "メモ"}),
              says("## 要約\nログイン改善")]
    engine, _, jira, slack, _ = build(script)          # approve 無し
    ctx = engine.run(load("role_developer"), params={"issue_key": "PROJ-1"})
    assert jira.comments == []                          # 断られた
    assert "ログイン改善" in ctx.results["analyse"].output["answer"]
    assert not ctx.results["analyse"].output["tool_calls"][0]["ok"]
    assert slack.posted and "ログイン改善" in slack.posted[0]["text"]


def test_developer_comment_goes_through_when_a_human_approves():
    seen = []

    def approve(tool, arguments):
        seen.append((tool, arguments["issue_key"]))
        return True

    script = [calls("jira__add_comment", {"issue_key": "PROJ-1", "text": "メモ"}),
              says("完了")]
    engine, _, jira, _, _ = build(script, approve=approve)
    engine.run(load("role_tester"), params={"issue_key": "PROJ-1"})
    assert seen == [("jira.add_comment", "PROJ-1")]
    assert jira.comments == [{"issue_key": "PROJ-1", "text": "メモ"}]


@pytest.mark.parametrize("name,channel", [
    ("role_writer", "#docs-review"), ("role_sales", "#sales-review"),
    ("role_researcher", "#research"),
])
def test_read_only_roles_leave_only_an_internal_post(name, channel):
    script = [calls("jira__search" if name != "role_researcher" else "vector_store__search",
                    {"jql": "project = PROJ"} if name != "role_researcher"
                    else {"text": "新機能"}),
              says("下書き本文")]
    engine, _, jira, slack, _ = build(script)
    engine.run(load(name))
    assert jira.comments == []
    assert [m["channel"] for m in slack.posted] == [channel]
    assert "下書き本文" in slack.posted[0]["text"]


def test_a_read_only_role_cannot_call_a_write_tool_even_if_the_model_tries():
    script = [calls("jira__add_comment", {"issue_key": "PROJ-1", "text": "勝手に"}),
              says("終了")]
    engine, _, jira, _, _ = build(script)
    ctx = engine.run(load("role_sales"))
    assert jira.comments == []
    record = ctx.results["draft"].output["tool_calls"][0]
    assert not record["ok"]


def test_review_posts_are_marked_as_drafts():
    engine, _, _, slack, _ = build([says("本文")])
    engine.run(load("role_sales"))
    assert "未送信の下書き" in slack.posted[0]["text"]
    engine, _, _, slack, _ = build([says("本文")])
    engine.run(load("role_writer"))
    assert "下書き" in slack.posted[0]["text"]
