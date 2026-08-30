"""エージェント実行 / the agent loop.

決められた工程を流すのではなく、LLM が道具を選んで自分で呼ぶ。
何をするかが事前に決まらない仕事（原因を調べる、状況をまとめる）に向く。

Instead of running a fixed sequence, the model chooses which tools to call.
This suits work whose shape is not known in advance: investigating a cause,
assembling a picture of where something stands.

1周の構成 / One cycle of the loop
----------------------------------
`run_agent` は次の4段階を、道具を呼ばなくなるまで繰り返す。

    ① RECOGNIZE 認識 — ここまでの会話履歴を踏まえてモデルに問い合わせる
    ② DECIDE    判断 — 道具を呼ぶか、答えを返すかを応答から読み取る
    ③ ACT       行動 — 呼ぶと決まった道具を実際に実行する
    ④ OBSERVE   観測 — 結果を会話履歴に積み、続けるか終えるかを決める
                        (② で「答えが出た」と判断していればここには来ない)

呼ぶ道具が無くなった（②で答えが出たと判断した）ときだけ、この輪を抜けて
利用者に結果を返す。それ以外は④の結果を持って①に戻る。

`run_agent` repeats four phases until the model stops calling tools:
RECOGNIZE (ask the model given everything so far), DECIDE (read from its
response whether it wants a tool or is done), ACT (actually run whichever
tool it asked for), and OBSERVE (feed the result back in, then decide whether
to continue). The loop is left only when DECIDE finds no tool call — that is
the model's answer; otherwise OBSERVE's outcome carries back into the next
RECOGNIZE.

止め方について / On stopping
-----------------------------
エージェントは自分では止まらない。上限が無ければ、利用者自身の API 残高で
回り続ける。ここでは3つの止め方を持たせている。

  - 往復回数の上限
  - 累計トークンの上限
  - 道具を呼ばなくなったら終了（=答えが出た、② DECIDE の結果）

An agent does not stop on its own; with no ceiling it keeps going on the user's
own API balance. Three stopping conditions are enforced: a turn limit, a
cumulative token limit, and the model ceasing to call tools (② DECIDE finding
no tool call — the answer has arrived).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..adapters.base import AdapterError, AdapterRegistry
from ..dsl.schema import AgentSpec
from ..llm.base import LLMProvider, ToolCall

logger = logging.getLogger("aipmo.agent")

# 道具の結果をそのままモデルに返すと、長い出力で文脈を食い潰す。
# Returning a whole tool result can exhaust the context on one long output.
RESULT_CHAR_LIMIT = 6000


class AgentError(Exception):
    pass


@dataclass
class ToolRecord:
    """1回の道具呼び出しの記録 / one tool call, as it happened."""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None


@dataclass
class AgentResult:
    answer: str
    iterations: int
    tool_calls: list[ToolRecord] = field(default_factory=list)
    stopped_because: str = "finished"
    input_tokens: int = 0
    output_tokens: int = 0


class ToolBox:
    """エージェントに渡す道具の集合。許可されたものだけを入れる。

    The set of tools handed to an agent — only the permitted ones.
    """

    def __init__(self, adapters: AdapterRegistry, spec: AgentSpec) -> None:
        self.adapters = adapters
        self.spec = spec
        self._allowed: dict[str, tuple[str, str]] = {}
        self._definitions: list[dict[str, Any]] = []
        self._build()

    def _build(self) -> None:
        wanted = set(self.spec.tools)
        blocked_writes: list[str] = []

        for adapter_name in self.adapters.names():
            adapter = self.adapters.get(adapter_name)
            for action_name, described in adapter.describe().items():
                qualified = f"{adapter_name}.{action_name}"

                if adapter_name not in wanted and qualified not in wanted:
                    continue

                if described["writes"] and not self.spec.allow_writes:
                    # 許可の指定がアダプタ単位でも、書き込みは別に許可が要る。
                    # "jira" と書いただけで課題を作られては困る。
                    # Naming an adapter does not grant its write actions:
                    # allowing "jira" must not by itself create issues.
                    blocked_writes.append(qualified)
                    continue

                # 道具名は OpenAI の命名規則に合わせる / conform to the tool name rules
                tool_name = qualified.replace(".", "__")
                self._allowed[tool_name] = (adapter_name, action_name)
                self._definitions.append({
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": described["description"] or qualified,
                        "parameters": described["parameters"],
                    },
                })

        unknown = [
            name for name in wanted
            if not any(
                name == adapter or name.startswith(f"{adapter}.")
                for adapter in self.adapters.names()
            )
        ]
        if unknown:
            raise AgentError(
                f"agent: 存在しない道具が指定されています "
                f"/ unknown tools requested: {', '.join(sorted(unknown))}\n"
                f"  登録済み / registered: {', '.join(self.adapters.names())}"
            )

        if not self._definitions:
            hint = ""
            if blocked_writes:
                hint = (f"\n  書き込み操作は allow_writes: true が要ります "
                        f"/ write actions need allow_writes: true: "
                        f"{', '.join(sorted(blocked_writes))}")
            raise AgentError(
                f"agent: 使える道具がありません / no usable tools{hint}"
            )

        logger.info("agent toolbox: %s", ", ".join(sorted(self._allowed)))

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions)

    def call(self, call: ToolCall) -> ToolRecord:
        if call.name not in self._allowed:
            # 許可外を呼ぼうとしたら、断ってエージェントに続けさせる。
            # 実行を落とすより、モデルに別の手を選ばせた方が結果が良い。
            # Refuse and let the agent continue: giving the model a chance to
            # choose differently beats aborting the run.
            return ToolRecord(
                name=call.name, arguments=call.arguments, ok=False,
                error=f"tool '{call.name}' is not permitted in this step",
            )

        if "__malformed__" in call.arguments:
            return ToolRecord(
                name=call.name, arguments=call.arguments, ok=False,
                error="arguments were not valid JSON; send them again",
            )

        adapter_name, action_name = self._allowed[call.name]
        try:
            result = self.adapters.get(adapter_name).invoke(
                action_name, dict(call.arguments))
        except AdapterError as exc:
            return ToolRecord(name=call.name, arguments=call.arguments,
                              ok=False, error=str(exc))
        except Exception as exc:
            return ToolRecord(name=call.name, arguments=call.arguments,
                              ok=False, error=f"{type(exc).__name__}: {exc}")

        return ToolRecord(name=call.name, arguments=call.arguments,
                          ok=True, result=result)


def run_agent(
    provider: LLMProvider,
    adapters: AdapterRegistry,
    spec: AgentSpec,
    prompt: str,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> AgentResult:
    toolbox = ToolBox(adapters, spec)
    messages = _recognize_request(system, prompt)

    records: list[ToolRecord] = []
    used_in, used_out = 0, 0

    for iteration in range(1, spec.max_iterations + 1):
        # ① RECOGNIZE — ここまでの文脈をそのままモデルに渡す。
        # Hand everything gathered so far to the model as-is.
        response = _recognize(provider, messages, toolbox, temperature, max_tokens)
        used_in += response.input_tokens or 0
        used_out += response.output_tokens or 0

        # ② DECIDE — 道具を呼ぶか、答えを返すかは応答そのものが示す。
        # The response itself says whether it wants a tool or has an answer.
        if not _decided_to_act(response):
            # 道具を呼ばなくなった = 答えが出た
            # No more tool calls means the agent has its answer.
            return AgentResult(
                answer=response.text, iterations=iteration, tool_calls=records,
                stopped_because="finished",
                input_tokens=used_in, output_tokens=used_out,
            )

        messages.append(_assistant_turn(response))

        for call in response.tool_calls:
            # ③ ACT — 呼ぶと決まった道具を実際に実行する。
            # Actually run whichever tool the model asked for.
            record = _act(toolbox, call)
            records.append(record)
            # ④ OBSERVE — 結果を、次の①で読める形にして積む。
            # Feed the result back in a shape the next RECOGNIZE can read.
            messages.append(_observe(call, record))
            logger.info("agent tool %s ok=%s", record.name, record.ok)

        if used_in + used_out >= spec.max_tokens_total:
            # ④ OBSERVE — 続けるかどうかの判定も観測の一部。
            # Whether to continue is also part of what OBSERVE decides.
            return AgentResult(
                answer=response.text, iterations=iteration, tool_calls=records,
                stopped_because="token_limit",
                input_tokens=used_in, output_tokens=used_out,
            )

    # 上限に達した。途中で打ち切ったことを隠さない。
    # 「終わった」と「打ち切った」を同じ顔で返すと、読む側が判断を誤る。
    # Hitting the ceiling is reported as such: presenting a truncated run as a
    # finished one would mislead whoever reads the result.
    return AgentResult(
        answer=_last_text(messages), iterations=spec.max_iterations,
        tool_calls=records, stopped_because="iteration_limit",
        input_tokens=used_in, output_tokens=used_out,
    )


def _recognize_request(system: str | None, prompt: str) -> list[dict[str, Any]]:
    """① RECOGNIZE の起点 — 依頼そのものを会話履歴の最初の形にする。

    The starting point of RECOGNIZE: turns the request itself into the first
    shape of the conversation history.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _recognize(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    toolbox: "ToolBox",
    temperature: float,
    max_tokens: int,
) -> Any:
    """① RECOGNIZE — 会話履歴と使える道具の一覧をモデルに渡し、応答を得る。

    Hands the conversation so far and the available tools to the model and
    returns its response.
    """
    return provider.converse(
        messages, tools=toolbox.definitions(),
        temperature=temperature, max_tokens=max_tokens,
    )


def _decided_to_act(response: Any) -> bool:
    """② DECIDE — 応答が道具呼び出しを含むかどうかを読み取る。

    道具呼び出しが無い = モデルは答えを返すと決めた、という合図。

    Reads whether the response carries a tool call. No tool call means the
    model decided to answer instead.
    """
    return bool(response.tool_calls)


def _act(toolbox: "ToolBox", call: ToolCall) -> ToolRecord:
    """③ ACT — 呼ぶと決まった道具を実際に実行する。

    Actually runs whichever tool was decided on.
    """
    return toolbox.call(call)


def _observe(call: ToolCall, record: ToolRecord) -> dict[str, Any]:
    """④ OBSERVE — 実行結果を、次の① RECOGNIZE で読める会話履歴の形にする。

    Turns the outcome of ACT into the conversation-history shape the next
    RECOGNIZE will read.
    """
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": _render(record),
    }


def _assistant_turn(response: Any) -> dict[str, Any]:
    """道具呼び出しを含む応答を、会話履歴に戻せる形にする。"""
    if response.raw_message is not None:
        message = response.raw_message
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name,
                             "arguments": json.dumps(call.arguments,
                                                     ensure_ascii=False)},
            }
            for call in response.tool_calls
        ],
    }


def _render(record: ToolRecord) -> str:
    if not record.ok:
        return json.dumps({"error": record.error}, ensure_ascii=False)

    text = json.dumps(record.result, ensure_ascii=False, default=str)
    if len(text) > RESULT_CHAR_LIMIT:
        # 切り詰めたことを本人に伝える。黙って削ると、
        # モデルは全部を見たつもりで結論を出す。
        # Say that it was truncated: cutting silently leaves the model
        # concluding from what it believes was the whole result.
        text = (text[:RESULT_CHAR_LIMIT]
                + f'… [truncated at {RESULT_CHAR_LIMIT} characters]')
    return text


def _last_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""
