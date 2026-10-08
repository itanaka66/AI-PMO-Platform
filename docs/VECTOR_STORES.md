# ベクトルストア / Vector stores

`meeting_to_tasks` のような業務テンプレートには要らない。過去の知見を
検索・蓄積する `generalize_knowledge` のようなテンプレートでだけ使う。
5種類から選べる: **Qdrant・pgvector・Chroma・Milvus・Weaviate。**

Not needed for a workflow template like `meeting_to_tasks`. Only used by a
template that searches or accumulates past knowledge, like
`generalize_knowledge`. Choose from **five backends: Qdrant, pgvector,
Chroma, Milvus, Weaviate.**

---

## どれか1つを選ぶ / Pick exactly one

```yaml
adapters:
  qdrant:                       # 例: Qdrant / example: Qdrant
    url: http://localhost:6333
    embedding:
      provider: openai
      model: text-embedding-3-small
      dimension: 1536
```

`adapters:` の下に `qdrant` / `pgvector` / `chroma` / `milvus` / `weaviate`
のうち1つだけを書く。テンプレートからは `<名前>.search` /
`<名前>.upsert` / `<名前>.submit_candidate` として使える。

Write exactly one of `qdrant` / `pgvector` / `chroma` / `milvus` /
`weaviate` under `adapters:`. A template can call it as `<name>.search` /
`<name>.upsert` / `<name>.submit_candidate`.

### 論理名 `vector_store` — バックエンドを乗り換えてもテンプレートは変わらない

ちょうど1つだけ設定すると、同じアダプタが論理名 `vector_store` でも
使えるようになる。新しく書くテンプレートは `vector_store.search` /
`vector_store.submit_candidate` を使うことを推奨する — 後で Qdrant から
pgvector に乗り換えても、この名前を使ったテンプレートは書き換えが要らない。
`templates/examples/generalize_knowledge.yaml` がその実例。
（2つ以上設定した場合は曖昧になるため、この論理名は登録されない。
 各バックエンド固有の名前では引き続き使える。）

### The logical name `vector_store` — switch backends without touching templates

Configuring exactly one backend also registers it under the logical name
`vector_store`. New templates should prefer `vector_store.search` /
`vector_store.submit_candidate` — switching from Qdrant to pgvector later
needs no template changes. `templates/examples/generalize_knowledge.yaml`
does exactly this. (With two or more backends configured this logical name
is skipped as ambiguous; each backend's own name still works.)

これは LLM の `profile` と同じ考え方 — 詳しくは [docs/PROVIDERS.md](PROVIDERS.md)。

Same idea as an LLM `profile` — see [docs/PROVIDERS.md](PROVIDERS.md).

---

## 共通の振る舞い / What is shared across all five

どれを選んでも同じ:

- スコープは `private` / `public` の2つだけ。実コレクション名（テナント名や
  テーブル名）はテンプレートから見えない
- `public` への直接書き込みは拒否される。`submit_candidate` で候補として
  提出し、人間のレビューを経てはじめて公開される
- 公開可能性スコアは自動算出。テンプレートは数値を用意しなくてよい
- コレクション／テーブルは事前に作成されている前提。このアダプタ自身は
  作成しない

The same regardless of which one you pick:

- Only two scopes exist, `private` and `public`. The concrete collection or
  table name (tenant name, table name) is never visible to a template
- Direct writes to `public` are refused. `submit_candidate` submits a
  candidate; publication only happens after human review
- The publicability score is computed automatically; a template need not
  supply one
- The collection or table is assumed to already exist. The adapter itself
  never creates one

---

## 人間の承認フロー（レビュー・修正・公開）/ The human review workflow

`submit_candidate` が約束する「人間が承認するレビュー」の実体。候補の
一覧・修正・承認（公開）・却下は、CLI（`aipmo knowledge`）か Web 画面
（「ナレッジ」）からだけ行える——アダプタ側のこれらのメソッドには
`@action` を付けていないため、テンプレートからは絶対に呼べない。

```bash
aipmo knowledge                              # 承認待ちの一覧（score 降順）
aipmo knowledge show ID                      # 候補の中身と判定の根拠
aipmo knowledge edit ID --text "書き直した内容"  # 承認待ちのものだけ修正できる
aipmo knowledge approve ID [--note メモ]      # 承認 → public コレクションへ複製
aipmo knowledge reject ID [--note メモ]       # 却下（public には何も書かない）
```

承認・却下のどちらも、private 側のその行に「誰が・いつ・どう判断したか」
（`reviewed_by` / `reviewed_at` / `review_note`）を書き足して残す。**消さない。**
これが「人間の判断の記録」そのもの。承認したときだけ、審査用の項目を
除いた内容を `public` コレクションへ複製する（`promoted_from` / `promoted_at`
だけは出どころとして残す）。決定済みの候補は編集できない——判断の記録を
あとから書き換えさせないため。

This is the actual "human-approved review" that `submit_candidate` promises.
Listing, editing, approving, and rejecting candidates only happens through
the CLI (`aipmo knowledge`) or the web screen ("Knowledge") — the adapter
methods behind this are not `@action`-decorated, so no template can ever
call them.

Both approving and rejecting append who decided what and when
(`reviewed_by` / `reviewed_at` / `review_note`) onto the private record —
never deleting it. That record is the recorded human decision. Only on
approval is a copy, stripped of review-only fields, written to the `public`
collection (keeping just `promoted_from` / `promoted_at` as provenance).
An already-decided candidate cannot be edited — that would let a recorded
decision be rewritten after the fact.

---

## 自己学習サイクル（ローカル LLM・自己判断・別モデル検証）/ The self-learning cycle

`templates/examples/self_learning_cycle.yaml` は、`aipmo schedule` が繰り返す
練習用のループ：ローカル LLM が架空の（実在しない）PMO 業務課題を作り、
**同じモデルが自己判断**し、**別のローカル LLM が検証**（`verdict: ok` /
`needs_fix`）し、妥当だったものだけを上の人間承認フローへ`submit_candidate`
で提出する。実際のトラッカー・外部サービスへは一切書き込まない。

```yaml
llm:
  self_learner: {provider: ollama, model: llama3.1}     # 生成・自己判断
  self_verifier: {provider: ollama, model: qwen2.5:14b}  # 検証（別モデル）
```

提出された候補は、ほかのテンプレートが作るものとまったく同じ、承認待ちの
private な行——既定では、ここでも人間が `aipmo knowledge` / Web の
「ナレッジ」で決める。

### オプトインの自動承認 / Opt-in auto-approval

運用者が「RAG を包括的に信用する」と明示的に選んだときだけ、**この
サイクルが提出した候補に限って**自動で承認される（ほかの経路の候補には
一切触れない）。いつでも無効化でき、無効化は新しい候補の自動承認を
止めるだけで、それまでに自動承認した判断（誰が・いつ）は記録されたまま
消えない。

```bash
aipmo learning                # 今の状態
aipmo learning enable         # 有効にする（Web 画面にも同じチェックボックスがある）
aipmo learning disable        # いつでも無効化できる
```

Only turned on when an operator explicitly opts in, and only for candidates
*this specific cycle* submitted — `aipmo/self_learning.py` matches by
template name before checking the toggle, so no other source's candidates
are ever touched by it.

---

## Qdrant

```yaml
adapters:
  qdrant:
    url: http://localhost:6333
    api_key: ${QDRANT_API_KEY:-}
    embedding: {provider: openai, model: text-embedding-3-small, dimension: 1536}
```

導入 / install: `pip install "aipmo[data]"`

コレクションは `tenant_<tenant名>` と `public_pmo_knowledge`（既定名）を
事前に作成しておく。[docs/DEPLOY-ORACLE.md](DEPLOY-ORACLE.md) が具体例。

Create the `tenant_<tenant>` and `public_pmo_knowledge` (default name)
collections beforehand. [docs/DEPLOY-ORACLE.md](DEPLOY-ORACLE.md) has a
worked example.

---

## pgvector

すでに PostgreSQL を運用していて、別のサーバーを増やしたくない構成に向く。
`postgres` アダプタと同じ Postgres に相乗りできる。

Fits a shop that already runs PostgreSQL and would rather not stand up
another server. Can share the same Postgres instance as the `postgres`
adapter.

```yaml
adapters:
  pgvector:
    dsn: ${PGVECTOR_DSN}
    table: pmo_vectors            # 既定値 / default
    embedding: {provider: openai, model: text-embedding-3-small, dimension: 1536}
```

導入 / install: `pip install "aipmo[vector-pgvector]"`

テーブルは事前に用意する / create the table beforehand:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE pmo_vectors (
    id         text PRIMARY KEY,
    collection text NOT NULL,
    embedding  vector(1536),      -- 埋め込みの次元に合わせる / match the embedding dimension
    payload    jsonb NOT NULL
);
CREATE INDEX ON pmo_vectors (collection);
```

---

## Chroma

自前サーバー（`chromadb.HttpClient` で接続する構成）を前提にする。

Assumes a self-hosted server reachable as `chromadb.HttpClient`.

```yaml
adapters:
  chroma:
    url: http://localhost:8000
    api_key: ${CHROMA_TOKEN:-}
    embedding: {provider: openai, model: text-embedding-3-small, dimension: 1536}
```

導入 / install: `pip install "aipmo[vector-chroma]"`

コレクションは `client.get_or_create_collection` などで事前に作る。

Create the collection beforehand, e.g. with `get_or_create_collection`.

---

## Milvus

`pymilvus.MilvusClient`（高レベル API）で接続する。コレクションは
動的フィールドを有効にして事前に作成しておく。フィルタは等価一致のみ
対応（`filters: {pattern: "key_person_dependency"}` のような dict）。

Connects via `pymilvus.MilvusClient`, the high-level API. Create the
collection beforehand with dynamic fields enabled. Filters only support
equality (a dict like `filters: {pattern: "key_person_dependency"}`).

```yaml
adapters:
  milvus:
    url: http://localhost:19530
    api_key: ${MILVUS_TOKEN:-}
    embedding: {provider: openai, model: text-embedding-3-small, dimension: 1536}
```

導入 / install: `pip install "aipmo[vector-milvus]"`

---

## Weaviate

v4 クライアントで接続する。REST と gRPC の両方のポートが要る
（既定の gRPC ポートは 50051）。コレクション（Weaviate 用語の
"collection"）は事前に作成しておく。

Connects via the v4 client. Needs both the REST and the gRPC port (gRPC
defaults to 50051). Create the collection beforehand.

```yaml
adapters:
  weaviate:
    url: http://localhost:8080
    grpc_port: 50051               # 既定値 / default
    api_key: ${WEAVIATE_API_KEY:-}
    embedding: {provider: openai, model: text-embedding-3-small, dimension: 1536}
```

導入 / install: `pip install "aipmo[vector-weaviate]"`

---

## 埋め込みの次元を変えたら / Changing the embedding dimension

どのバックエンドでも同じ制約: 埋め込みの提供元やモデルを変えると次元が
変わることがあり、既存のコレクション／テーブルの次元は固定なので、
**作り直しと再投入が要る。** 詳しくは
[docs/PROVIDERS.md](PROVIDERS.md#埋め込みの次元が変わると既存のベクトルは使えません)。

Same constraint regardless of backend: changing the embedding provider or
model can change the vector dimension, and an existing collection or
table's dimension is fixed, so switching means **recreating it and
re-indexing.** See [docs/PROVIDERS.md](PROVIDERS.md).

---

### 他言語の要約 / Summary in other languages

**中文**：像 `meeting_to_tasks` 这类业务模板不需要向量存储；只有像 `generalize_knowledge` 这种检索/积累过往知识的模板才会用到。可从 5 种中选择：**Qdrant・pgvector・Chroma・Milvus・Weaviate。**

**한국어**：`meeting_to_tasks` 같은 업무 템플릿에는 필요 없습니다. 과거 지식을 검색·축적하는 `generalize_knowledge` 같은 템플릿에서만 씁니다. 5종 중에서 고를 수 있습니다: **Qdrant·pgvector·Chroma·Milvus·Weaviate.**

**Español**：No se necesita para una plantilla de trabajo como `meeting_to_tasks`. Solo lo usa una plantilla que busca o acumula conocimiento pasado, como `generalize_knowledge`. Se puede elegir entre 5 opciones: **Qdrant, pgvector, Chroma, Milvus, Weaviate.**

**Français**：Pas nécessaire pour un modèle métier comme `meeting_to_tasks`. Utilisé seulement par un modèle qui recherche ou accumule des connaissances passées, comme `generalize_knowledge`. Choix entre 5 options : **Qdrant, pgvector, Chroma, Milvus, Weaviate.**

**Deutsch**：Nicht nötig für eine Geschäftsvorlage wie `meeting_to_tasks`. Wird nur von einer Vorlage genutzt, die vergangenes Wissen durchsucht oder ansammelt, wie `generalize_knowledge`. Wahl zwischen 5 Optionen: **Qdrant, pgvector, Chroma, Milvus, Weaviate.**

**Português**：Não é necessário para um modelo de negócio como `meeting_to_tasks`. Usado apenas por um modelo que busca ou acumula conhecimento passado, como `generalize_knowledge`. Pode-se escolher entre 5 opções: **Qdrant, pgvector, Chroma, Milvus, Weaviate.**
