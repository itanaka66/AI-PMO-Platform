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


def test_repeated_identical_failure_stops_the_loop():
    """同じ道具に同じ引数で失敗し続けたら、上限まで待たず打ち切る。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {}),
        calls("jira__find_overdue", {}),
        calls("jira__find_overdue", {}),  # ここまで来ない
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")

    assert result.stopped_because == "repeated_failure"
    assert len(result.tool_calls) == 2
    assert len(provider.conversations) == 2


def test_failures_with_different_arguments_do_not_count_as_repeated():
    """引数が違えば「同じ失敗の繰り返し」ではないので止めない。"""
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {}),
        calls("jira__find_overdue", {"project": "does-not-exist"}),
        says("あきらめます"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")

    assert result.stopped_because == "finished"


def test_unexpected_adapter_exception_stops_the_loop_as_fatal():
    """AdapterError でも引数エラーでもない例外は、繰り返させず即座に止める。"""
    import aipmo.adapters.mock as mock_module

    adapters = registry()

    def _boom(self, project: str, as_of: str | None = None):
        raise RuntimeError("jira api is down")

    original = mock_module.MockJiraAdapter.find_overdue
    _boom._aipmo_action = original._aipmo_action
    _boom._aipmo_writes = original._aipmo_writes
    mock_module.MockJiraAdapter.find_overdue = _boom
    try:
        provider = EchoProvider(script=[
            calls("jira__find_overdue", {"project": "PROJ"}),
            says("ここには来ない"),
        ])
        result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                           prompt="調べて")
    finally:
        mock_module.MockJiraAdapter.find_overdue = original

    assert result.stopped_because == "fatal_tool_failure"
    assert result.tool_calls[0].fatal is True
    assert len(provider.conversations) == 1  # 次の RECOGNIZE に進んでいない


def test_tool_calls_in_one_round_run_concurrently():
    """1周内の複数の道具呼び出しは直列に待たず並行して実行される。"""
    import time as time_module

    from aipmo.adapters.base import Adapter, action

    class SlowAdapter(Adapter):
        name = "slow"

        @action()
        def wait(self, seconds: float) -> dict:
            time_module.sleep(seconds)
            return {"waited": seconds}

    adapters = AdapterRegistry()
    adapters.register(SlowAdapter())

    response = LLMResponse(
        text="", model="s",
        tool_calls=[
            ToolCall(id="a", name="slow__wait", arguments={"seconds": 0.2}),
            ToolCall(id="b", name="slow__wait", arguments={"seconds": 0.2}),
        ],
    )
    provider = EchoProvider(script=[response, says("完了")])

    start = time_module.monotonic()
    result = run_agent(provider, adapters, AgentSpec(tools=["slow"]),
                       prompt="待って")
    elapsed = time_module.monotonic() - start

    assert result.stopped_because == "finished"
    assert elapsed < 0.35  # 直列なら 0.4 秒以上かかる


def test_cancel_check_stops_the_loop_before_the_next_recognize():
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"project": "PROJ"}, f"c{i}")
        for i in range(5)
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて", cancel_check=lambda: True)

    assert result.stopped_because == "cancelled"
    assert result.iterations == 1
    assert len(provider.conversations) == 0


def test_reflection_warns_before_the_repeated_failure_cutoff():
    from aipmo.engine.agent import REPEATED_FAILURE_THRESHOLD

    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {}),
        says("直します"),
    ])

    run_agent(provider, adapters, AgentSpec(tools=["jira"]), prompt="調べて")

    if REPEATED_FAILURE_THRESHOLD - 1 >= 1:
        last_conversation = provider.conversations[-1]
        assert any(
            m.get("role") == "user" and "OBSERVE" in str(m.get("content"))
            for m in last_conversation
        )


def test_malformed_arguments_are_handed_back():
    adapters = registry()
    provider = EchoProvider(script=[
        calls("jira__find_overdue", {"__malformed__": "{not json"}),
        says("やり直します"),
    ])

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")
    assert "valid JSON" in result.tool_calls[0].error


# --- 4段階の輪 / the four-phase loop ----------------------------------------
#
# run_agent は RECOGNIZE → DECIDE → ACT → OBSERVE を繰り返す。ここでは
# その各段階を担う関数を個別に確かめる — 輪全体のふるまいは他のテストが
# すでに確認している。
#
# run_agent repeats RECOGNIZE → DECIDE → ACT → OBSERVE. These tests check
# each phase's own function directly; the loop's overall behaviour is already
# covered elsewhere in this file.

def test_recognize_request_carries_the_system_and_user_messages():
    from aipmo.engine.agent import _recognize_request

    messages = _recognize_request("あなたは PMO です", "調べて")

    assert messages == [
        {"role": "system", "content": "あなたは PMO です"},
        {"role": "user", "content": "調べて"},
    ]


def test_recognize_request_omits_the_system_message_when_none_is_given():
    from aipmo.engine.agent import _recognize_request

    messages = _recognize_request(None, "調べて")

    assert messages == [{"role": "user", "content": "調べて"}]


class _FlakyProvider(EchoProvider):
    """converse が最初の数回だけ例外を投げるスタブ。"""

    def __init__(self, fail_times: int, then: LLMResponse) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.then = then
        self.attempts = 0

    def converse(self, messages, tools=None, temperature=0.2, max_tokens=4096):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionError("temporary network failure")
        return self.then


def test_recognize_retries_a_transient_provider_failure(monkeypatch):
    from aipmo.engine import agent as agent_module

    monkeypatch.setattr(agent_module.time, "sleep", lambda seconds: None)

    provider = _FlakyProvider(fail_times=2, then=says("復旧しました"))
    adapters = registry()

    result = run_agent(provider, adapters, AgentSpec(tools=["jira"]),
                       prompt="調べて")

    assert result.answer == "復旧しました"
    assert result.stopped_because == "finished"
    assert provider.attempts == 3


def test_recognize_gives_up_after_max_attempts(monkeypatch):
    from aipmo.engine import agent as agent_module

    monkeypatch.setattr(agent_module.time, "sleep", lambda seconds: None)

    provider = _FlakyProvider(fail_times=99, then=says("到達しない"))
    adapters = registry()

    with pytest.raises(AgentError):
        run_agent(provider, adapters, AgentSpec(tools=["jira"]), prompt="調べて")

    assert provider.attempts == agent_module.RECOGNIZE_MAX_ATTEMPTS


def test_decide_reads_a_tool_call_as_wanting_to_act():
    from aipmo.engine.agent import _decided_to_act

    assert _decided_to_act(calls("jira__find_overdue", {"project": "PROJ"})) is True


def test_decide_reads_no_tool_call_as_the_answer_having_arrived():
    from aipmo.engine.agent import _decided_to_act

    assert _decided_to_act(says("遅延はありません")) is False


def test_act_runs_the_tool_and_returns_its_record():
    from aipmo.engine.agent import _act

    adapters = registry()
    box = ToolBox(adapters, AgentSpec(tools=["jira"]))
    call = ToolCall(id="c1", name="jira__find_overdue", arguments={"project": "PROJ"})

    record = _act(box, call)

    assert record.ok is True
    assert record.name == "jira__find_overdue"


def test_observe_shapes_a_successful_record_as_a_tool_message():
    from aipmo.engine.agent import ToolRecord, _observe

    call = ToolCall(id="c1", name="jira__find_overdue", arguments={})
    record = ToolRecord(name="jira__find_overdue", arguments={}, ok=True,
                        result={"count": 0})

    message = _observe(call, record)

    assert message["role"] == "tool"
    assert message["tool_call_id"] == "c1"
    assert "count" in message["content"]


def test_observe_shapes_a_failed_record_as_an_error_payload():
    from aipmo.engine.agent import ToolRecord, _observe

    call = ToolCall(id="c1", name="jira__find_overdue", arguments={})
    record = ToolRecord(name="jira__find_overdue", arguments={}, ok=False,
                        error="project が足りません")

    message = _observe(call, record)

    assert message["role"] == "tool"
    assert "project が足りません" in message["content"]


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
