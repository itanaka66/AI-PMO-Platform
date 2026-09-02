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
