# エージェント / Agents

決められた工程を流すのではなく、**AI が道具を選んで自分で呼びます**。
何をすればよいかが事前に決まらない仕事に向きます。

Instead of running a fixed sequence, **the model chooses which tools to call**.
This suits work whose shape is not known in advance.

| | 向くもの |
|---|---|
| 通常の工程 (`llm` / `adapter`) | 手順が決まっている。議事録を作る、課題を登録する |
| エージェント (`agent`) | 手順が決まらない。原因を調べる、状況をまとめる |

---

## 書き方 / Writing one

```yaml
steps:
  - id: investigate
    agent:
      tools:
        - jira.find_overdue        # 使ってよい道具を列挙する
      allow_writes: false          # 既定。外の世界を変えることは許さない
      max_iterations: 5            # 往復の上限
    llm:
      profile: default
    prompt_inline: |
      プロジェクトの遅延状況を調べ、報告してください。
      道具を使って実際のデータを確認してから答えてください。
      推測で書かないこと。
```

`tools` は2通りの書き方ができます。

- `jira` — そのアダプタの**読み取り**アクション全部
- `jira.find_overdue` — 単一のアクションだけ

---

## 中で何が起きているか / What happens inside

`aipmo/engine/agent.py` の `run_agent` は、次の4段階を道具を呼ばなくなるまで
繰り返します。

`run_agent` in `aipmo/engine/agent.py` repeats four phases until the model
stops calling tools:

| 段階 | すること | コード上の名前 |
|---|---|---|
| ① RECOGNIZE 認識 | ここまでの会話履歴をモデルに渡す | `_recognize` |
| ② DECIDE 判断 | 道具を呼ぶか、答えを返すかを応答から読み取る | `_decided_to_act` |
| ③ ACT 行動 | 呼ぶと決まった道具を実際に実行する | `_act` |
| ④ OBSERVE 観測 | 結果を会話履歴に積み、続けるか終えるかを決める | `_observe` |

②で「道具を呼ばない」と判断したときだけ輪を抜け、それ以外は④の結果を
持って①に戻ります。これは新しい仕組みではなく、以前からの動きを
4つの名前で明示しただけです — 挙動は変わっていません。

The loop is left only when ② finds no tool call; otherwise ④'s outcome
carries back into the next ①. This is not new behaviour — it names four
phases that were already happening, without changing what they do.

③ ACT には、それを行ってよいかという判断も含まれます。書き込み系の道具で
`require_approval: true` が立っている工程では、実行の直前に人の承認を
求めます（後述「書き込みごとに人の承認を求める」）。

③ ACT also includes the question of whether it is allowed to happen at all:
a write-tool call on a step with `require_approval: true` asks a human right
before it runs (see "Requiring approval per write" below).

```
ユーザー要求
     │
     ▼
① RECOGNIZE ──▶ ② DECIDE ──「答えが出た」──▶ 完了・回答 ──▶ ユーザー
     ▲                │
     │           「道具を呼ぶ」
     │                ▼
     └────── ④ OBSERVE ◀── ③ ACT
```

---

## 許可の設計 / How permission works

### 道具は必ず列挙する

`tools` の省略はできません。テンプレートは第三者が書いて配布される前提なので、
**その中の一節に、無制限に Jira と Slack を触れる AI を置かせない**ためです。

`tools` cannot be omitted. Templates are authored by third parties and
distributed; a clause inside one must not be able to place a model with
unrestricted access to Jira and Slack.

### 書き込みは別に許可が要る

`tools: [jira]` と書いても、課題は作られません。外の世界を変える操作
（課題の起票、通知の送信、行の書き込み）には `allow_writes: true` が要ります。

読み違いはやり直せますが、**書いた誤りはやり直せません。** AI の判断だけで
課題を作り、通知を送ることを、既定にはしていません。

Naming an adapter does not grant its write actions. Creating issues, sending
notifications and writing rows need `allow_writes: true`. A mistaken read can
be retried; a mistaken write cannot be taken back, so it is not the default.

**推奨する形** — 調査はエージェント、実行は決め打ちの工程に分ける。
何を送るかは AI が決め、送るかどうかはテンプレートが決めます。

The recommended shape: investigation by the agent, action by a fixed step. The
model decides what to say; the template decides whether it goes out.

```yaml
  - id: investigate
    agent:
      tools: [jira.find_overdue]
      allow_writes: false
    prompt_inline: 遅延を調べて報告してください

  - id: notify                       # 送信は決め打ち
    adapter: slack
    action: post_message
    inputs:
      text: "{{ steps.investigate.output.answer }}"
```

### 書き込みごとに人の承認を求める

`allow_writes: true` は「この工程は書き込みをしてよい」という、
テンプレート作成時の一括判断です。1回ごとに人が見て判断したい場合は
`require_approval: true` を重ねます。

```yaml
- id: file_issue
  agent:
    tools: [jira]
    allow_writes: true
    require_approval: true   # 書き込みの直前に承認を求める
  prompt_inline: 内容を確認し、必要なら課題を起票してください
```

承認する側（人に尋ねる方法）は、テンプレートではなく実行環境が決めます。
`aipmo run` は対話端末があれば、その場でその都度尋ねます。**承認する相手を
用意していない場合（スケジューラ・Web サーバーからの実行など）、
その書き込みは常に断られます。** 黙って通ることはありません。

Who does the approving is decided by the runtime, not the template. `aipmo
run` asks at the terminal, each time, when one is attached. **With no
approver configured — a scheduled or web-triggered run, say — the write is
always refused.** It is never silently let through.

読み取り系の道具はこの対象外です。`require_approval` は書き込みだけを
対象にするので、`allow_writes: false` の工程で立てても何も起きません。

Read-only tools are unaffected: `require_approval` only ever gates writes, so
setting it on a step with `allow_writes: false` has no effect.

**Slack で承認する / Approving over Slack**

対話端末を使わずに承認したい場合（スケジューラや Web サーバーからの
実行がこれに当たる）は、`config.yaml` に `approval.slack` を設定します。

```yaml
adapters:
  slack:
    token: ${SLACK_BOT_TOKEN}

approval:
  slack:
    channel: "#approvals"     # 必須。承認を求めるチャンネル
    timeout_seconds: 300      # 既定 300 秒。反応が無ければ断る
    poll_seconds: 5           # 既定 5 秒おきに反応を確認する
    approver_ids: []          # 空なら誰の反応でもよい。絞るなら Slack ユーザー ID を列挙する
```

書き込みが提案されるたびに、その内容（道具名と引数）が `channel` へ
投稿されます。:white_check_mark: の反応で承認、:x: の反応で却下、
`timeout_seconds` 以内にどちらも付かなければ、対話端末の場合と同じく
断ります。

`approval.slack` を設定すると、`aipmo run` の対話端末での確認より
優先されます。`adapters.slack` の設定も別途必要です — 承認の投稿・
反応の確認は、そのアダプタをそのまま使って行われます。

Slack の Events API（Webhook）は使いません。ボットトークンだけで
動くように、一定間隔で反応を確認する方式にしています。反応が届くまで
`poll_seconds` 分の遅れが出ますが、公開エンドポイントや Slack App の
Event Subscriptions の用意は要りません。

To approve without an interactive terminal — the case for a scheduler or a
web-triggered run — set `approval.slack` in `config.yaml` (see the example
above). Every proposed write posts its tool name and arguments to `channel`;
a `:white_check_mark:` reaction approves it, a `:x:` reaction declines it, and
no reaction within `timeout_seconds` refuses it, the same as the terminal
path. `approval.slack`, once configured, takes priority over `aipmo run`'s
own terminal prompt. `adapters.slack` needs its own configuration too — the
same adapter is used to post the proposal and read the reaction.

This deliberately polls rather than using Slack's Events API/webhooks: it
trades up to `poll_seconds` of latency for needing nothing beyond the bot
token already in use elsewhere — no public endpoint or Slack App event
configuration to stand up.

---

## 止め方 / Stopping

**エージェントは自分では止まりません。** 上限が無ければ、利用者自身の
API 残高で回り続けます。3つの止め方があります。

An agent does not stop on its own; with no ceiling it keeps going on the user's
own API balance. Three conditions end the loop:

| | 既定 | 意味 |
|---|---|---|
| `max_iterations` | 5 | 往復の上限（1〜25） |
| `max_tokens_total` | 60000 | 累計トークンの上限 |
| — | — | 道具を呼ばなくなったら終了 |

打ち切られた場合、`stopped_because` が `iteration_limit` か `token_limit` に
なります。**完了と打ち切りを同じ顔で返しません。** 途中で止まった結果を
「終わった」として読むと、判断を誤るためです。

When truncated, `stopped_because` says so. A truncated run is not presented as
a finished one: reading a partial result as complete leads to a wrong call.

```yaml
- id: notify
  when: "{{ steps.investigate.output.stopped_because }} == 'finished'"
```

---

## 出力 / Output

```yaml
{{ steps.investigate.output.answer }}           # 最終的な答え
{{ steps.investigate.output.stopped_because }}  # finished / iteration_limit / token_limit
{{ steps.investigate.output.iterations }}       # 往復した回数
{{ steps.investigate.output.tool_calls }}       # 何をどう呼んだか
{{ steps.investigate.output.tokens }}           # 使ったトークン
```

`tool_calls` には失敗した呼び出しも残ります。**AI が何を見て結論を出したかを
後から確認できる**必要があるためで、PMO の用途ではここが効きます。

Failed calls are kept too: being able to see what the model actually looked at
before concluding is what makes the result usable in a PMO context.

---

## 提供元の対応 / Provider support

ツール呼び出しに対応した提供元が要ります。非対応の相手に道具を送ると、
無視されるか 400 で落ちます。どちらも**動いていないのに気づきにくい**形なので、
設定を読む段階で弾きます。

| 提供元 | ツール呼び出し |
|---|---|
| `openai` / `gemini` / `groq` / `openrouter` | 対応 |
| `vllm` / `lmstudio` | モデル次第（OpenAI 互換の chat API） |
| `ollama` | **非対応**。`/api/generate` のみ。エージェント工程は実行時に失敗する |
| `llamacpp` | 既定では当てにしない |

Ollama の経路は `/api/generate` だけで、道具を渡す対話がありません。
エージェント工程を Ollama に向けると実行時に失敗します。議事録のような
1往復の工程はそのまま使えます。vLLM / LM Studio は OpenAI 互換なら道具を
渡せますが、ローカルの小さいモデルは呼べても**引数を間違えます**。
間違いは差し戻して直させますが、往復が増えます。
エージェントにはクラウドの大きめのモデルを割り当てる方が現実的です。

The Ollama path is `/api/generate` only — it has no tool-using conversation.
An agent step pointed at Ollama fails at run time. Single-turn work such as
minutes still runs. vLLM and LM Studio can pass tools if they speak the
OpenAI chat API, but small local models get the arguments wrong. Mistakes are
handed back for correction, which costs turns. Assigning a larger hosted
model to the agent profile is the realistic choice.

```yaml
llm:
  default:                      # 議事録など決め打ちの工程
    provider: ollama
    model: qwen2.5:14b
  agent:                        # 判断が要る工程
    provider: openai
    model: gpt-4o-mini
```

```yaml
- id: investigate
  agent: { tools: [jira] }
  llm: { profile: agent }       # プロファイルで割り当てを変える
```

---

## 例 / Example

`templates/examples/overdue_triage.yaml` に動く例があります。

```bash
aipmo validate templates/examples/overdue_triage.yaml
aipmo run templates/examples/overdue_triage.yaml
```

書き込みを許可したエージェントの例は
`templates/examples/generalize_knowledge.yaml`。社内固有の知見を読み、
識別情報を落として書き直し、`vector_store.submit_candidate` で提出する
（提出は「レビュー待ちに載せる」ところまでで、公開そのものではない）。
渡している道具はそれ1つだけで、`allow_writes: true` を明示している。
`vector_store` は設定したベクトルストア（Qdrant・pgvector・Chroma・Milvus・
Weaviate のいずれか）の論理名で、詳しくは
[docs/VECTOR_STORES.md](VECTOR_STORES.md)。

An example with write access allowed: `templates/examples/generalize_knowledge.yaml`.
It reads an internal insight, rewrites it with identifying detail stripped
out, and submits the result via `vector_store.submit_candidate` — submitting
only means landing in the review queue, not publishing. It is handed exactly
that one tool, with `allow_writes: true` stated explicitly. `vector_store` is
the logical name for whichever backend is configured (Qdrant, pgvector,
Chroma, Milvus, or Weaviate); see [docs/VECTOR_STORES.md](VECTOR_STORES.md).
