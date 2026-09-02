# AI の提供元 / AI providers

テンプレートは `profile: default` としか書きません。どの提供元・どのモデルに
割り当てるかは設定側で決めます。**提供元を乗り換えても、テンプレートは
1文字も変わりません。**

A template only ever says `profile: default`. Which provider and model that
resolves to is configuration, so **switching providers changes no template**.

---

## 対応している提供元 / Supported providers

| 提供元 | 種別 | 埋め込み | 鍵の環境変数 |
|---|---|---|---|
| `openai` | クラウド | あり | `OPENAI_API_KEY` |
| `gemini` | クラウド | あり | `GEMINI_API_KEY` |
| `groq` | クラウド | **なし** | `GROQ_API_KEY` |
| `openrouter` | クラウド | **なし** | `OPENROUTER_API_KEY` |
| `ollama` | ローカル | あり | 不要（エージェント工程は不可） |
| `vllm` | ローカル | あり | 不要 |
| `lmstudio` | ローカル | あり | 不要 |
| `llamacpp` | ローカル | — | 不要 |

Gemini・Groq・OpenRouter・vLLM・LM Studio・llama.cpp は、いずれも
OpenAI 互換の API を出しています。実装は1つで、違いは
`aipmo/llm/presets.py` にデータとして置いてあります。

All of these except Ollama speak an OpenAI-compatible API. There is one
implementation; the differences live as data in `aipmo/llm/presets.py`.

---

## 設定例 / Configuration

### Gemini

```yaml
llm:
  default:
    provider: gemini
    model: gemini-3.5-flash
```
```bash
export GEMINI_API_KEY=...    # aistudio.google.com で取得
```

### Groq

速度が要る場合に向きます。**埋め込み API を持っていません**（下記参照）。
Fast. **It has no embeddings API** — see below.

```yaml
llm:
  default:
    provider: groq
    model: openai/gpt-oss-120b
```

### OpenRouter

多数のモデルに1つの鍵で届きます。モデル名は `provider/model` 形式です。
One key, many models. Model names take the form `provider/model`.

```yaml
llm:
  default:
    provider: openrouter
    model: openai/gpt-4o-mini
```

### ローカル / Local

```yaml
# Ollama
llm:
  default:
    provider: ollama
    model: qwen2.5:14b
    host: http://localhost:11434

# vLLM — --served-model-name に渡した名前をそのまま書く
llm:
  default:
    provider: vllm
    model: Qwen/Qwen2.5-14B-Instruct
    base_url: http://192.168.1.50:8000/v1

# LM Studio — Local Server を有効にしてから
llm:
  default:
    provider: lmstudio
    model: qwen2.5-14b-instruct
```

ローカルの提供元は**モデル名を必ず指定してください**。何を載せているかは
こちらからは分からないので、既定値を勝手に決めません。

Local providers **require an explicit model name**: what you have loaded is not
something this software can guess, so it does not invent a default.

---

## 複数の提供元を同時に呼ぶ / Calling several providers at once

`profile` の代わりに `profiles`（複数形）を並びで書くと、同じプロンプトを
Ollama・Gemini・ChatGPT のような複数の提供元に**同時に**投げて、
結果を並べて比較できます。速さのためではなく、書きぶりを見比べたり、
人が選んだりする用途を想定しています。

Write `profiles` (plural) instead of `profile` and the same prompt goes out to
several providers — Ollama, Gemini, ChatGPT, say — **at the same time**, with
every answer kept for comparison. This is for weighing wording side by side or
letting a person pick, not for speed.

```yaml
- id: draft_minutes
  llm:
    profiles: [local, gemini, openai]   # Docker のローカル + クラウド2つ
  prompt: minutes_ja
  inputs:
    transcript: "{{ steps.fetch_transcript.output.text }}"
```
```yaml
# config 側 / in config
llm:
  local:
    provider: ollama
    model: qwen2.5:14b
  gemini:
    provider: gemini
    model: gemini-3.5-flash
  openai:
    provider: openai
    model: gpt-4o-mini
```

結果は `steps.draft_minutes.output.results` に、宣言した順の並びで入ります。
各要素は `{ profile, model, ok, text }`（JSON 出力なら `text` の代わりに
`data`）で、失敗した要素は `{ profile, ok: false, error }` になります。
`count` が成功数、`failed` が失敗数です。

Results land in `steps.draft_minutes.output.results`, in the order declared.
Each entry is `{ profile, model, ok, text }` (or `data` instead of `text` when
`output_format: json`); a failed one is `{ profile, ok: false, error }`.
`count` and `failed` give the totals.

**1つが落ちても他は止まりません。** 全滅したときだけステップが失敗になり、
通常のリトライに乗ります。`profile`（単数）と `profiles` は同時に指定でき
ません。エージェント (`agent:`) ステップでは使えません — 1本の会話で道具を
呼び続けるエージェントには、そもそも「並行した複数の答え」という概念が
成立しないためです。

**One provider going down does not stop the others.** The step only fails when
every provider does, and that failure goes through the normal retry path.
`profile` (singular) and `profiles` cannot both be set. Agent steps
(`agent:`) cannot use `profiles`: an agent is a single ongoing conversation
driving tools, and "several parallel answers" has no meaning there.

---

## 注意すべき差 / Differences that bite

### Groq と OpenRouter には埋め込み API がありません

ベクトル検索を使う場合、埋め込みだけ別の提供元に向ける必要があります。
設定を読む段階でエラーになるので、実行時に気づくことはありません。

If you use vector search, embeddings must come from somewhere else. This is
rejected while the config is read, not at the moment of first use.

```yaml
llm:
  default:
    provider: groq              # チャットは Groq
    model: openai/gpt-oss-120b

adapters:
  qdrant:                       # pgvector / chroma / milvus / weaviate でも同じ
    embedding:
      provider: openai          # 埋め込みだけ別
      model: text-embedding-3-small
      dimension: 1536
```

（`embedding` の書き方は5種類のベクトルストアで共通。詳しくは
[docs/VECTOR_STORES.md](VECTOR_STORES.md)。）

(The `embedding` block is the same shape across all five vector-store
backends. See [docs/VECTOR_STORES.md](VECTOR_STORES.md).)

この構成では鍵が2つ要ります（`GROQ_API_KEY` と `OPENAI_API_KEY`）。
セットアップウィザードで Groq を選ぶと、この点を警告します。

This needs two keys. The setup wizard warns about it when you pick Groq.

### Ollama ではエージェント工程は動きません

Ollama の経路は `/api/generate` だけで、道具を渡す対話 (`converse`) がありません。
エージェント工程を Ollama プロファイルに向けると、実行時に失敗します。
議事録のような1往復の工程はそのまま使えます。

The Ollama path is `/api/generate` only — there is no tool-using conversation.
Pointing an agent step at an Ollama profile fails at run time. Single-turn
work such as minutes still runs.

```yaml
llm:
  default:                      # 議事録など決め打ちの工程
    provider: ollama
    model: qwen2.5:14b
  agent:                        # 判断が要る工程
    provider: openai
    model: gpt-4o-mini
```

### JSON モードの扱いが違います

このソフトのテンプレートは、議事録や TODO の抽出で JSON 出力を使います。
`response_format` を受け付けない提供元に送ると、無視されるのではなく
**400 で落ちる**ことがあります。

そこで提供元ごとに対応可否を持たせ、非対応ならプロンプト側で JSON を要求し、
返答は寛容に解析します（```json の囲みや前置きが混ざっても読めます）。

Templates rely on JSON output for minutes and task extraction. An endpoint that
does not accept `response_format` may answer with a **400 rather than ignoring
it**, so support is tracked per provider: where it is absent, JSON is requested
in the prompt and the reply is parsed leniently — fenced blocks and preambles
are handled.

OpenRouter は経路のモデル次第なので、既定では送りません。

For OpenRouter this depends on the routed model, so it is not sent by default.

### 埋め込みの次元が変わると、既存のベクトルは使えません

提供元を乗り換えると次元が変わることがあります（OpenAI 1536 と別のモデルなど）。
どのベクトルストアもコレクション・テーブルの次元は固定なので、
**作り直しと再投入が要ります**。チャットの提供元は自由に変えられますが、
埋め込みはそうではありません。

Changing embedding provider can change the vector dimension, and every
vector store here has a fixed dimension per collection or table. Switching
means **recreating it and re-indexing**. Chat providers can be swapped
freely; embedding providers cannot.

---

## モデル名は短命です / Model names are short-lived

ここに書いた既定値は 2026年8月時点のものです。
Groq は 2026年8月16日に Llama 系のチャットモデルを停止しました。

**既定値に固執しないでください。** 動かなくなったら、提供元のモデル一覧で
現行の ID を確認して `model:` を書き換えれば済みます。

The defaults here are a snapshot from August 2026. Groq shut down its Llama
chat models on 16 August 2026. **Do not build on a default.** When one stops
working, check the provider's model list and change `model:`.

- Groq: `https://api.groq.com/openai/v1/models` で現行の一覧が引けます
- OpenRouter / Gemini: 各提供元のモデル一覧ページ

---

## 使い分け / Choosing

| 状況 | 向くもの |
|---|---|
| とりあえず試す | `openai` — 埋め込みも揃っていて設定が1つで済む |
| 長い会議記録を安く回す | `gemini` — 入力が長い用途に向く |
| 速度が要る | `groq` — ただし埋め込みは別に用意 |
| モデルを比べたい | `openrouter` — 1つの鍵で多数に届く |
| 記録を外に出せない | `ollama` / `vllm` — GPU のある機械が要る |

会議 Transcript は機微情報を含みます。社外に出せない場合は、
**AI サーバーを自前で用意してください。** GPU のない機械では実用速度が
出ないので、そこは設備の問題として扱う必要があります。

Meeting transcripts are sensitive. If they cannot leave your network, run the
AI yourself — and note that without a GPU the speed will not be usable, which
makes it a hardware question rather than a configuration one.
