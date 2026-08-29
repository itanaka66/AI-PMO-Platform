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
