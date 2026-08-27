"""エージェントのテスト / agent tests.

主眼は「止まること」と「許可した範囲を出ないこと」。
Focus: that it stops, and that it stays inside what was permitted.
"""
from __future__ import annotations

import pytest

from aipmo.adapters.base import AdapterRegistry
from aipmo.adapters.mock import MockJiraAdapter, MockSlackAdapter, MockTeamsAdapter
from aipmo.dsl import loader
from aipmo.dsl.schema import AgentSpec
from aipmo.engine.agent import AgentError, ToolBox, run_agent
from aipmo.engine.runner import Engine
from aipmo.llm.base import EchoProvider, LLMResponse, ToolCall
from aipmo.llm.presets import ProviderError, require_tools
from aipmo.llm.registry import LLMRegistry


def registry() -> AdapterRegistry:
    adapters = AdapterRegistry()
    adapters.register(MockTeamsAdapter())
    adapters.register(MockJiraAdapter())
    adapters.register(MockSlackAdapter())
    return adapters


def says(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="scripted")


def calls(name: str, arguments: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(text="", model="scripted",
                       tool_calls=[ToolCall(id=call_id, name=name,
                                            arguments=arguments)])


# --- 道具の選別 / tool selection -------------------------------------------

def test_only_named_adapters_are_offered():
    box = ToolBox(registry(), AgentSpec(tools=["jira"]))
    names = {d["function"]["name"] for d in box.definitions()}
    assert names == {"jira__find_overdue"}      # create_issues は書き込みなので除外


def test_a_single_action_can_be_named():
    box = ToolBox(registry(), AgentSpec(tools=["teams.get_transcript"]))
    assert [d["function"]["name"] for d in box.definitions()] == ["teams__get_transcript"]


def test_write_actions_need_explicit_permission():
    """アダプタ名を書いただけで課題を作られては困る。"""
    read_only = ToolBox(registry(), AgentSpec(tools=["jira"]))
    assert "jira__create_issues" not in {
        d["function"]["name"] for d in read_only.definitions()}

    writable = ToolBox(registry(), AgentSpec(tools=["jira"], allow_writes=True))
    assert "jira__create_issues" in {
        d["function"]["name"] for d in writable.definitions()}


def test_unknown_tool_is_rejected_with_the_registered_list():
    with pytest.raises(AgentError, match="registered"):
        ToolBox(registry(), AgentSpec(tools=["asana"]))


def test_no_usable_tools_explains_the_write_permission():
    """slack は書き込みのみ。理由を言わずに落とすと原因が分からない。"""
    with pytest.raises(AgentError, match="allow_writes"):
        ToolBox(registry(), AgentSpec(tools=["slack"]))


def test_tool_schema_comes_from_the_signature():
    box = ToolBox(registry(), AgentSpec(tools=["jira.find_overdue"]))
    params = box.definitions()[0]["function"]["parameters"]
    assert params["properties"]["project"]["type"] == "string"
    assert params["required"] == ["project"]


# --- 呼び出しの制御 / call gating ------------------------------------------

def test_calling_an_unpermitted_tool_is_refused_not_fatal():
    """断ってエージェントに続けさせる。実行ごと失うより結果が良い。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("slack__post_message", {"channel": "#x", "text": "hi"}),
        says("別の方法を取ります"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")

    assert result.tool_calls[0].ok is False
    assert "not permitted" in result.tool_calls[0].error
    assert result.stopped_because == "finished"
    assert adapters.get("slack").posted == []


def test_permitted_tool_actually_runs():
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"project": "PROJ"}),
        says("遅延はありません"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="遅延を調べて")

    assert result.tool_calls[0].ok is True
    assert result.answer == "遅延はありません"


def test_adapter_error_is_returned_to_the_agent():
    """道具が失敗しても実行を落とさず、モデルに直す機会を与える。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {}),          # project が足りない
        says("引数を直して再試行します"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")

    assert result.tool_calls[0].ok is False
    assert result.stopped_because == "finished"


def test_malformed_arguments_are_handed_back():
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"__malformed__": "{not json"}),
        says("やり直します"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")
    assert "valid JSON" in result.tool_calls[0].error


# --- 止まること / stopping --------------------------------------------------

def test_iteration_limit_stops_the_loop():
    """エージェントは自分では止まらない。上限が無いと利用者の残高で回り続ける。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"project": "PROJ"}, f"c{i}")
        for i in range(20)
    ])

    result = run_agent(provider, adapters,
                       AgentSpec(tools=["jira"], max_iterations=3),
                       prompt="ずっと調べて")

    assert result.iterations == 3
    assert result.stopped_because == "iteration_limit"
    assert len(result.tool_calls) == 3


def test_truncation_is_reported_not_disguised_as_completion():
    """打ち切りと完了を同じ顔で返すと、読む側が判断を誤る。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"project": "PROJ"}, f"c{i}")
        for i in range(10)
    ])
    result = run_agent(provider, adapters,
                       AgentSpec(tools=["jira"], max_iterations=2),
                       prompt="x")
    assert result.stopped_because != "finished"


def test_token_ceiling_stops_the_loop():
    adapters = registry()
    heavy = LLMResponse(text="", model="s", input_tokens=5000, output_tokens=5000,
                        tool_calls=[ToolCall(id="c", name="jira__find_overdue",
                                             arguments={"project": "PROJ"})])
    provider = EchoProvider(script=[heavy for _ in range(10)])

    result = run_agent(provider, adapters,
                       AgentSpec(tools=["jira"], max_iterations=10,
                                 max_tokens_total=12000),
                       prompt="x")

    assert result.stopped_because == "token_limit"
    assert result.iterations < 10


def test_no_tool_call_ends_immediately():
    provider = EchoProvider(script=[says("すぐ答えられます")])
    result = run_agent(provider, registry(), AgentSpec(tools=["jira"]), prompt="x")

    assert result.iterations == 1
    assert result.tool_calls == []


# --- テンプレートからの利用 / use from a template ---------------------------

def test_agent_step_parses_and_runs():
    adapters = registry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider(script=[
        calls("jira__find_overdue", {"project": "PROJ"}),
        says("遅延なし"),
    ]))

    template = loader.load_dict({
        "name": "agent_demo",
        "steps": [{
            "id": "investigate",
            "agent": {"tools": ["jira.find_overdue"], "max_iterations": 4},
            "prompt_inline": "PROJ の遅延を調べてください",
        }],
    })

    ctx = Engine(adapters, llms).run(template)
    output = ctx.results["investigate"].output

    assert output["answer"] == "遅延なし"
    assert output["stopped_because"] == "finished"
    assert output["tool_calls"][0]["name"] == "jira__find_overdue"


def test_agent_step_requires_tools():
    """道具を列挙させないと、配布テンプレートに無制限の AI を置ける。"""
    with pytest.raises(loader.TemplateError, match="tools"):
        loader.load_dict({"name": "x", "steps": [
            {"id": "a", "agent": {}, "prompt_inline": "p"}]})


def test_agent_step_requires_a_prompt():
    with pytest.raises(loader.TemplateError, match="prompt"):
        loader.load_dict({"name": "x", "steps": [
            {"id": "a", "agent": {"tools": ["jira"]}}]})


def test_iteration_limit_is_bounded_in_the_template():
    with pytest.raises(loader.TemplateError, match="max_iterations"):
        loader.load_dict({"name": "x", "steps": [{
            "id": "a", "agent": {"tools": ["jira"], "max_iterations": 500},
            "prompt_inline": "p"}]})


def test_agent_output_is_referenceable_by_later_steps():
    adapters = registry()
    llms = LLMRegistry()
    llms.register("default", EchoProvider(script=[says("要約です")]))

    template = loader.load_dict({
        "name": "chained",
        "steps": [
            {"id": "look", "agent": {"tools": ["jira"]},
             "prompt_inline": "調べて"},
            {"id": "tell", "adapter": "slack", "action": "post_message",
             "inputs": {"channel": "#x", "text": "{{ steps.look.output.answer }}"}},
        ],
    })

    Engine(adapters, llms).run(template)
    assert adapters.get("slack").posted[0]["text"] == "要約です"


# --- 提供元の対応 / provider capability ------------------------------------

def test_provider_without_tool_support_is_refused_early():
    """非対応の相手に道具を送ると、無視されるか 400 になる。
    どちらも『動いていないのに気づかない』形なので、先に落とす。"""
    with pytest.raises(ProviderError, match="tool calling"):
        require_tools("llamacpp")


@pytest.mark.parametrize("name", ["openai", "gemini", "groq", "openrouter"])
def test_hosted_providers_support_tools(name):
    assert require_tools(name).supports_tools
