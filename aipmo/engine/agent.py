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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..adapters.base import AdapterError, AdapterRegistry
from ..dsl.schema import AgentSpec
from ..llm.base import LLMProvider, ToolCall

logger = logging.getLogger("aipmo.agent")

# 道具の結果をそのままモデルに返すと、長い出力で文脈を食い潰す。
# Returning a whole tool result can exhaust the context on one long output.
RESULT_CHAR_LIMIT = 6000

# ① RECOGNIZE がプロバイダ呼び出しで失敗したときの再試行回数と待ち時間。
# ネットワーク瞬断やレート制限は一過性のことが多く、1回失敗しただけで
# 実行全体を落とすのは惜しい。
# Retries and backoff for a failed provider call during RECOGNIZE. A network
# blip or rate limit is often transient; failing the whole run on one error
# throws away work unnecessarily.
RECOGNIZE_MAX_ATTEMPTS = 3
RECOGNIZE_RETRY_BASE_SECONDS = 1.0

# ④ OBSERVE が「同じ道具に同じ引数で連続して失敗している」と見なす回数。
# 到達したら② DECIDE 側で輪を止める。モデルに任せると、直らない失敗を
# 何度も繰り返して上限まで回し続けることがある。
# How many consecutive identical-and-failing calls OBSERVE tolerates before
# DECIDE cuts the loop short. Left to the model alone, a call that cannot
# succeed gets retried verbatim until the iteration ceiling is hit.
REPEATED_FAILURE_THRESHOLD = 2

# ④ OBSERVE が「同じ道具に同じ引数を連続して呼んでいる」と見なす回数。
# 失敗が続く場合だけでなく、成功が続いていても新しい情報を得ていない
# （足踏みしている）ことがある。
# How many consecutive identical calls OBSERVE treats as worth flagging —
# not only when they keep failing, but also when they keep succeeding
# without moving the work forward.
REPEATED_CALL_THRESHOLD = REPEATED_FAILURE_THRESHOLD


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
    # AdapterError（引数が悪い等、モデルが直せる見込みがある）と区別するため。
    # 想定外の例外は「引数を変えれば直る」種類ではないことが多く、
    # そのまま繰り返させても意味がない。
    # Distinguished from AdapterError (a bad-argument sort of failure the model
    # can plausibly fix). An unexpected exception is usually not the kind that
    # different arguments would fix, so letting the loop keep retrying it
    # blindly does not help.
    fatal: bool = False


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

    def writes_tool(self, name: str) -> bool:
        """この道具が外の世界を変える（書き込み系の）道具かどうか。

        並行実行の範囲を決めるのに使う — 読むだけの道具同士は競合しないが、
        書き込み系はアダプタ内部の状態やリモート側で競合しうる。

        Used to decide the scope of concurrent execution: read-only tools
        cannot conflict with one another, but write actions can race, either
        against adapter-internal state or on the remote side.
        """
        entry = self._allowed.get(name)
        if entry is None:
            return False
        adapter_name, action_name = entry
        return self.adapters.get(adapter_name).writes(action_name)

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
        except TypeError as exc:
            # 必須引数の不足・型違いなど、呼び出しの形が悪かっただけ。
            # AdapterError と同様、引数を直せば通る見込みがある。
            # A missing or mistyped argument — the call's shape was wrong.
            # Like AdapterError, fixable by sending different arguments.
            return ToolRecord(name=call.name, arguments=call.arguments,
                              ok=False, error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            # AdapterError でも TypeError でもないもの（接続断、認証切れ、
            # バグ）は、引数を変えて呼び直しても直らない見込みが高いので
            # 致命的として扱う。
            # Anything else (a dropped connection, expired auth, a bug) is
            # unlikely to be fixed by retrying with different arguments, so
            # it is marked fatal.
            return ToolRecord(name=call.name, arguments=call.arguments,
                              ok=False, error=f"{type(exc).__name__}: {exc}",
                              fatal=True)

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
    cancel_check: "Callable[[], bool] | None" = None,
) -> AgentResult:
    """`cancel_check` — 外から中断したいときに渡す。呼ぶたびに問い合わせ、
    True が返ったらその時点で輪を抜ける。長時間の道具呼び出しの合間に
    ユーザーが止められる経路が無いと、実行が終わるまで待つしかなくなる。

    `cancel_check`, when given, is polled at points where stopping early is
    safe; returning True ends the loop there. Without an external stop path,
    a caller has no way to interrupt a run short of killing the process.
    """
    toolbox = ToolBox(adapters, spec)
    messages = _recognize_request(system, prompt)

    records: list[ToolRecord] = []
    used_in, used_out = 0, 0

    def cancelled() -> bool:
        return cancel_check is not None and cancel_check()

    for iteration in range(1, spec.max_iterations + 1):
        if cancelled():
            return AgentResult(
                answer=_last_text(messages), iterations=iteration,
                tool_calls=records, stopped_because="cancelled",
                input_tokens=used_in, output_tokens=used_out,
            )

        # ① RECOGNIZE — ここまでの文脈をそのままモデルに渡す。
        # Hand everything gathered so far to the model as-is.
        response = _recognize(provider, messages, toolbox, temperature, max_tokens)
        used_in += response.input_tokens or 0
        used_out += response.output_tokens or 0

        # トークン上限は、分かった直後（③ ACT で道具を呼んで無駄にする前）
        # に見る。輪の終わりまで待つと、上限超過に気づく前に1周分の道具
        # 呼び出しを使い切ってしまう。
        # The token ceiling is checked as soon as it is known — before ACT
        # spends a round of tool calls — rather than at the end of the loop,
        # where a full round would already have run before the overage
        # was noticed.
        if used_in + used_out >= spec.max_tokens_total:
            return AgentResult(
                answer=response.text, iterations=iteration, tool_calls=records,
                stopped_because="token_limit",
                input_tokens=used_in, output_tokens=used_out,
            )

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

        if cancelled():
            return AgentResult(
                answer=_last_text(messages), iterations=iteration,
                tool_calls=records, stopped_because="cancelled",
                input_tokens=used_in, output_tokens=used_out,
            )

        # ③ ACT — この周に呼ぶと決まった道具を実行する。読み取り系は並行に、
        # 書き込み系は互いに順番に（cancel_check も見ながら）実行する。
        # Run this round's tool calls: reads concurrently, writes one at a
        # time relative to each other, watching cancel_check as it goes.
        batch = _act_batch(toolbox, response.tool_calls, cancel_check)

        fatal_record: ToolRecord | None = None
        was_cancelled = False
        for call, record in zip(response.tool_calls, batch):
            records.append(record)
            # ④ OBSERVE — 結果を、次の①で読める形にして積む。
            # Feed the result back in a shape the next RECOGNIZE can read.
            messages.append(_observe(call, record))
            logger.info("agent tool %s ok=%s fatal=%s",
                       record.name, record.ok, record.fatal)

            if record.error == CANCELLED_TOOL_ERROR:
                # キャンセルされた呼び出しは、実際に失敗したわけではない。
                # 致命的判定にも繰り返し失敗判定にも数えない。
                # A cancelled call did not actually fail — it should not
                # count toward the fatal or repeated-failure checks below.
                was_cancelled = True
                continue

            if record.fatal and fatal_record is None:
                fatal_record = record

            # ② DECIDE の一部 — 同じ道具に同じ引数で連続して失敗している場合、
            # モデルに任せず輪を止める。直らない失敗を上限まで繰り返させない。
            # Part of DECIDE: a call that keeps failing identically is cut
            # short rather than left to repeat until the iteration ceiling.
            if _is_repeated_failure(records):
                return AgentResult(
                    answer=(
                        f"道具 '{record.name}' が同じ引数で {REPEATED_FAILURE_THRESHOLD} "
                        f"回連続して失敗したため中断しました / stopped after "
                        f"'{record.name}' failed identically "
                        f"{REPEATED_FAILURE_THRESHOLD} times in a row: {record.error}"
                    ),
                    iterations=iteration, tool_calls=records,
                    stopped_because="repeated_failure",
                    input_tokens=used_in, output_tokens=used_out,
                )

        if was_cancelled:
            return AgentResult(
                answer=_last_text(messages), iterations=iteration,
                tool_calls=records, stopped_because="cancelled",
                input_tokens=used_in, output_tokens=used_out,
            )

        if fatal_record is not None:
            # ④ OBSERVE — 想定外の失敗（バグ・認証切れ・接続断）は、
            # モデルに次を選ばせても直らない見込みが高いのでここで止める。
            # An unexpected failure (a bug, expired auth, a dropped
            # connection) is unlikely to be fixed by letting the model try
            # again, so the loop ends here instead of continuing regardless.
            return AgentResult(
                answer=(
                    f"道具 '{fatal_record.name}' が回復不能なエラーで失敗した"
                    f"ため中断しました / stopped after '{fatal_record.name}' "
                    f"failed with an unrecoverable error: {fatal_record.error}"
                ),
                iterations=iteration, tool_calls=records,
                stopped_because="fatal_tool_failure",
                input_tokens=used_in, output_tokens=used_out,
            )

        # ④ OBSERVE の反省 — この周の結果が目的の助けになっているかを見て、
        # 次の① RECOGNIZE に短い所見を渡す。結果をただ積むだけでは、
        # モデルは「あと一歩で打ち切られる」ことに気づけない。
        # OBSERVE's reflection: a short note on whether this round helped,
        # fed into the next RECOGNIZE. Without it, the model has no signal
        # that it is one repeated failure away from being cut off.
        note = _reflect(records)
        if note:
            messages.append({"role": "user", "content": note})

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

    プロバイダ呼び出しは一過性の理由（レート制限、瞬断）で落ちることがある。
    そのまま伝播させると、ここまでの道具呼び出しの積み重ねごと実行全体を
    失うので、指数バックオフで数回まで再試行する。ただし認証エラーや
    不正なリクエストのような恒久的な失敗は、待っても直らないので
    即座に諦める。

    Hands the conversation so far and the available tools to the model and
    returns its response. A provider call can fail for transient reasons
    (rate limits, a network blip); propagating that immediately would throw
    away every tool call made so far, so this retries a few times with
    exponential backoff before giving up. A permanent failure — bad
    credentials, a malformed request — will not fix itself by waiting, so
    that is raised immediately instead of being retried.
    """
    last_error: Exception | None = None
    for attempt in range(1, RECOGNIZE_MAX_ATTEMPTS + 1):
        try:
            return provider.converse(
                messages, tools=toolbox.definitions(),
                temperature=temperature, max_tokens=max_tokens,
            )
        except Exception as exc:
            last_error = exc
            if not _is_transient_error(exc):
                logger.warning("agent recognize failed permanently: %s", exc)
                break
            if attempt == RECOGNIZE_MAX_ATTEMPTS:
                break
            delay = RECOGNIZE_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "agent recognize failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt, RECOGNIZE_MAX_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)

    raise AgentError(
        f"agent: モデルへの問い合わせが失敗しました "
        f"/ the provider call failed: {type(last_error).__name__}: {last_error}"
    ) from last_error


# HTTP ステータスを持つ例外のうち、待っても直らないもの。
# 4xx はおおむね「リクエストの側が悪い」— 鍵、権限、リクエスト内容。
# 429（レート制限）だけは例外で、待てば直る。
# HTTP-status-bearing exceptions that will not fix themselves by waiting.
# 4xx generally means the request itself was wrong — credentials,
# permissions, malformed content. 429 (rate limit) is the one exception,
# since that does resolve by waiting.
_PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 422})


def _is_transient_error(exc: Exception) -> bool:
    """一過性の失敗（再試行する価値がある）かどうかを判定する。

    Decides whether a failure is transient — worth retrying — or permanent.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status not in _PERMANENT_STATUS_CODES

    try:
        import openai
    except ImportError:
        openai = None  # type: ignore[assignment]

    if openai is not None:
        permanent = (
            openai.AuthenticationError, openai.PermissionDeniedError,
            openai.BadRequestError, openai.NotFoundError,
            openai.UnprocessableEntityError, openai.ConflictError,
        )
        if isinstance(exc, permanent):
            return False
        transient = (
            openai.RateLimitError, openai.APIConnectionError,
            openai.APITimeoutError, openai.InternalServerError,
        )
        if isinstance(exc, transient):
            return True

    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    # 種類の分からない例外は、実運用でよく見るネットワーク系だとは
    # 断定できない。再試行に価値があるとは限らないので、既定は
    # 「恒久的」— 3回無駄に待たせるより、早く諦めて伝える方が良い。
    # An exception of an unrecognized kind cannot be assumed to be one of
    # the usual network hiccups. The default leans permanent — surfacing it
    # promptly beats spending three rounds of backoff on something waiting
    # will not fix.
    return False


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


CANCELLED_TOOL_ERROR = "cancelled before this tool call ran"


def _cancelled_record(call: ToolCall) -> ToolRecord:
    return ToolRecord(name=call.name, arguments=call.arguments, ok=False,
                      error=CANCELLED_TOOL_ERROR)


def _act_batch(
    toolbox: "ToolBox",
    calls: list[ToolCall],
    cancel_check: "Callable[[], bool] | None" = None,
) -> list[ToolRecord]:
    """③ ACT — この周の道具呼び出しを実行する。

    読み取り系の道具どうしは互いに独立しているので並行に走らせる。
    書き込み系は、アダプタ内部の状態やリモート側で競合しうるので、
    書き込みどうしは順番に実行する（読み取りとは並行して進む）。
    結果は `calls` と同じ順で返し、会話履歴の順序を呼ばれた順に保つ。

    途中で `cancel_check` が True を返したら、まだ実行していない
    書き込み呼び出しは実行せず、キャンセル済みの記録を返す。

    Read-only tool calls are independent of one another, so they run
    concurrently. Write actions can race — against adapter-internal state or
    on the remote side — so writes run one at a time relative to each other
    (while still overlapping with the concurrent reads). Results come back
    in the same order as `calls`, keeping the conversation history in call
    order.

    If `cancel_check` starts returning True partway through, any write call
    not yet started is skipped and recorded as cancelled instead of run.
    """
    if len(calls) <= 1:
        if calls and cancel_check is not None and cancel_check():
            return [_cancelled_record(calls[0])]
        return [_act(toolbox, call) for call in calls]

    write_flags = [toolbox.writes_tool(call.name) for call in calls]
    read_indices = [i for i, w in enumerate(write_flags) if not w]
    write_indices = [i for i, w in enumerate(write_flags) if w]

    results: list[ToolRecord | None] = [None] * len(calls)

    def run_writes() -> None:
        for i in write_indices:
            if cancel_check is not None and cancel_check():
                for j in write_indices[write_indices.index(i):]:
                    results[j] = _cancelled_record(calls[j])
                return
            results[i] = _act(toolbox, calls[i])

    if not read_indices:
        run_writes()
        return results  # type: ignore[return-value]

    with ThreadPoolExecutor(max_workers=len(read_indices)) as pool:
        futures = {pool.submit(_act, toolbox, calls[i]): i for i in read_indices}
        run_writes()
        for future, i in futures.items():
            results[i] = future.result()

    return results  # type: ignore[return-value]


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


def _is_repeated_failure(records: list[ToolRecord]) -> bool:
    """④ OBSERVE — 末尾が同じ道具・同じ引数の失敗で連続しているかを見る。

    Looks at whether the trailing records are the same tool, same arguments,
    all failing — the signature of a call that will not succeed by retrying.
    """
    if len(records) < REPEATED_FAILURE_THRESHOLD:
        return False

    tail = records[-REPEATED_FAILURE_THRESHOLD:]
    latest = tail[-1]
    if latest.ok:
        return False
    return all(
        not r.ok and r.name == latest.name and r.arguments == latest.arguments
        for r in tail
    )


def _reflect(records: list[ToolRecord]) -> str | None:
    """④ OBSERVE の反省 — 生の結果を積むだけでなく、その周を短く評価する。

    2つのことを見る:
      - 同じ失敗があと1回続いたら打ち切られる状態か
        （直らない失敗を上限まで繰り返させないための警告）
      - 同じ呼び出しが成功し続けているのに、進捗が止まっていないか
        （成功しているからといって、目的に近づいているとは限らない）

    道具の結果そのものは既に _observe で各メッセージに入っているので、
    ここで繰り返す必要はない — 足りないのはモデルへのメタな所見であって、
    結果の再掲ではない。

    Beyond appending raw results, this gives the round a short assessment,
    watching for two things:
      - being one identical failure away from the repeated-failure cutoff
        (so a call that cannot succeed does not get retried until the
        ceiling), and
      - the same call succeeding repeatedly without the work moving forward
        (success is not proof of progress).

    The tool result itself is already in each message via _observe; what
    was missing was a meta-level note to the model, not a restatement of
    the result.
    """
    threshold = REPEATED_FAILURE_THRESHOLD
    if threshold < 2 or len(records) < threshold - 1:
        return None

    tail = records[-(threshold - 1):]
    latest = tail[-1]
    same_call = all(
        r.name == latest.name and r.arguments == latest.arguments
        for r in tail
    )
    if not same_call:
        return None

    if not latest.ok:
        if not all(not r.ok for r in tail):
            return None
        return (
            f"[OBSERVE] '{latest.name}' はこの引数で {threshold - 1} 回連続して"
            f"失敗しています。もう一度同じ引数で呼ぶと打ち切られます — 引数を"
            f"変えるか、別の道具を検討してください。"
            f" / '{latest.name}' has failed {threshold - 1} time(s) in a row "
            f"with these exact arguments. Calling it again unchanged will end "
            f"this run — change the arguments or try a different tool."
        )

    if not all(r.ok for r in tail):
        return None
    return (
        f"[OBSERVE] '{latest.name}' をこの引数で {threshold - 1} 回連続して"
        f"呼び、同じ結果を得ています。新しい情報は増えていない可能性が"
        f"あります — 目的に対して次に何を確かめるべきか考え直してください。"
        f" / '{latest.name}' has been called {threshold - 1} time(s) in a row "
        f"with these exact arguments, returning the same kind of result each "
        f"time. This may not be adding new information — reconsider what "
        f"would actually move the goal forward before calling it again."
    )


def _last_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""
