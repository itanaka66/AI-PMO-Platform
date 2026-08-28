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
| `ollama` | ローカル | あり | 不要 |
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
  qdrant:
    embedding:
      provider: openai          # 埋め込みだけ別
      model: text-embedding-3-small
      dimension: 1536
```

この構成では鍵が2つ要ります（`GROQ_API_KEY` と `OPENAI_API_KEY`）。
セットアップウィザードで Groq を選ぶと、この点を警告します。

This needs two keys. The setup wizard warns about it when you pick Groq.

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
Qdrant のコレクションは次元が固定なので、**作り直しと再投入が要ります**。
チャットの提供元は自由に変えられますが、埋め込みはそうではありません。

Changing embedding provider can change the vector dimension, and a Qdrant
collection's dimension is fixed. Switching means **recreating the collection and
re-indexing**. Chat providers can be swapped freely; embedding providers cannot.

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
