"""AI-PMO テンプレート DSL のスキーマ定義。

テンプレート = 実行可能なノウハウ。
1つの YAML が、1つの PMO 業務ワークフローを完全に記述する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepKind(str, Enum):
    """ステップの種別。エンジンはこの値でディスパッチする。"""

    ADAPTER = "adapter"   # 外部ツール呼び出し (jira / slack / teams ...)
    LLM = "llm"           # LLM 呼び出し
    TRANSFORM = "transform"  # 純粋なデータ変換 (LLM を使わない)
    AGENT = "agent"       # LLM が道具を選んで自分で呼ぶ / the LLM drives


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


@dataclass
class TriggerSpec:
    """テンプレートの起動条件。

    type:
      - manual   : UI / CLI からの手動実行
      - schedule : cron 式
      - event    : 外部イベント (teams:meeting_ended など)
    """

    type: str = "manual"
    cron: str | None = None
    event: str | None = None
    timezone: str = "Asia/Tokyo"


@dataclass
class RetrySpec:
    max_attempts: int = 1
    backoff_seconds: float = 2.0


@dataclass
class LLMSpec:
    """LLM の指定。

    provider に具体名 (openai / anthropic / ollama ...) ではなく
    論理名 (default / fast / local) を書けるようにして、
    実環境ごとの割り当ては設定ファイル側で解決する。
    これにより「Docker 版=ローカル LLM / 非 Docker 版=クラウド」を
    テンプレートを書き換えずに切り替えられる。
    """

    profile: str = "default"
    temperature: float = 0.2
    max_tokens: int = 4096

    # 複数の提供元に同じプロンプトを同時に投げ、比較する。
    # 指定があれば profile より優先される。
    # Fan out the same prompt to several providers at once for comparison.
    # Takes priority over `profile` when non-empty.
    profiles: list[str] = field(default_factory=list)


@dataclass
class AgentSpec:
    """エージェントに許すこと / what an agent is permitted to do.

    既定は「読むだけ・5回まで」。テンプレートは第三者が書いて配布される。
    その中の一節に、無制限に Jira と Slack を触れる LLM を置かせない。
    許可は明示的に列挙させる。

    Read-only and five turns by default. Templates are authored by third
    parties and distributed; a clause inside one must not be able to place an
    LLM with unrestricted access to Jira and Slack. Permissions are enumerated.
    """

    # 使ってよい道具。"jira" でアダプタ全体、"jira.find_overdue" で単一アクション。
    # Allowed tools: "jira" for a whole adapter, "jira.find_overdue" for one.
    tools: list[str] = field(default_factory=list)

    # 外の世界を変える操作を許すか。既定は不可。
    # 課題を作る・通知を送るを AI の判断だけで行わせない。
    # Whether world-changing actions are permitted. Off by default: creating
    # issues and sending messages should not rest on the model's judgement alone.
    allow_writes: bool = False

    # 往復の上限。エージェントは自分では止まらない。
    # 止め方を書いておかないと、利用者の API 残高で回り続ける。
    # Turn limit. An agent does not stop on its own, and without a ceiling it
    # keeps going on the user's own API balance.
    max_iterations: int = 5

    # 1回の実行で使ってよいトークンの上限（概算）。超えたら打ち切る。
    # An approximate token ceiling for one run; exceeding it ends the loop.
    max_tokens_total: int = 60000


@dataclass
class Step:
    id: str
    kind: StepKind

    # --- kind == ADAPTER ---
    adapter: str | None = None
    action: str | None = None

    # --- kind == LLM ---
    llm: LLMSpec | None = None
    prompt: str | None = None          # プロンプトテンプレート名 (prompts/ 配下)
    prompt_inline: str | None = None   # インラインで書く場合

    # --- kind == TRANSFORM ---
    expression: str | None = None

    # --- kind == AGENT ---
    agent: AgentSpec | None = None

    # --- 繰り返し / iteration ---
    # 値の並びに対して、同じ工程を1件ずつ実行する。
    # 担当者ごとに1通ずつ送る、といった処理はこれが無いと書けない。
    # Runs the same step once per element. Without it, work like "one message
    # per assignee" cannot be expressed at all.
    for_each: str | None = None
    as_name: str = "item"
    # 要素ごとの条件。when はループの前に一度だけ評価されるので、
    # 「確信度が高いものだけ実行する」といった絞り込みには別の口が要る。
    # A per-element condition: `when` is evaluated once before the loop, so
    # filtering on each element's own values needs its own field.
    where: str | None = None
    max_items: int = 50

    # --- 共通 ---
    inputs: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    output_format: OutputFormat = OutputFormat.TEXT
    output_schema: dict[str, Any] | None = None  # JSON 出力時の検証用
    when: str | None = None            # 条件式。False なら skip
    retry: RetrySpec = field(default_factory=RetrySpec)
    continue_on_error: bool = False


@dataclass
class Template:
    name: str
    version: str = "1"
    industry: str = "generic"
    description: str = ""
    trigger: TriggerSpec = field(default_factory=TriggerSpec)
    params: dict[str, Any] = field(default_factory=dict)   # 実行時パラメータの既定値
    steps: list[Step] = field(default_factory=list)

    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]
