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

### 判断の参考情報（似た過去の候補の内訳）/ Reference stats for the decision

承認待ちの候補を見るとき（`aipmo knowledge show ID`・Web の「ナレッジ」）、
テキストが似た過去の候補から、人間がどう判断したか・LLM がどう判定したか
（`llm_verdict`。自己学習サイクルなどが付けたときだけ存在する）の内訳を
件数・割合つきで一緒に表示する。選択肢は承認/却下の2択に決め打たない
——実際に `review_status` / `llm_verdict` に現れた値をそのまま集計するので、
3択以上でも自然文の指示がそのまま値でも同じように働く。決定済みの候補
には出さない（判断はもう終わっているので、参考情報は不要）。

```bash
aipmo knowledge show ID
# ...
# 参考 / reference — 似た過去の候補 4 件
#   人間の判断 / human decisions:
#     approved: 3 件 (75.0%)
#     rejected: 1 件 (25.0%)
#   LLM の判断 / LLM verdicts:
#     ok: 3 件 (75.0%)
#     needs_fix: 1 件 (25.0%)
```

Shown when viewing a pending candidate (`aipmo knowledge show ID` / the web
"Knowledge" screen): among past candidates with similar text, how humans
decided and how an LLM verdicted (`llm_verdict`, present only when something
like the self-learning cycle set it) — counts and percentages, never
hard-coded to an approve/reject pair. Not shown on an already-decided
candidate, since there is nothing left to reference it for.

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

**中文**：像 `meeting_to_tasks` 这类业务模板不需要向量存储；只有像 `generalize_knowledge` 这种检索/积累过往知识的模板才会用到。可从 5 种中选择：**Qdrant・pgvector・Chroma・Milvus・Weaviate。**公开（向 public 集合写入）必须经过人工审核流程——`submit_candidate` 只会把候选提交到私有集合等待审核，`aipmo knowledge`（CLI）或 Web「知识库」画面才能一览、修改、批准或拒绝；批准或拒绝都会把"谁、何时、如何判断"写入该候选的私有记录中保留（从不删除）。`self_learning_cycle` 模板用本地 LLM 反复进行练习：生成虚构课题→自我判断→另一个本地模型验证→通过验证的才提交候选，全部不写入真实的工作跟踪系统；只有运营者通过 UI 复选框或 `aipmo learning enable` 明确选择"完全信任 RAG"后，才会对该流程提交的候选自动批准，且可随时关闭。人工审核候选时，还会显示相似历史候选的参考统计（人工判断与 LLM 判断的件数与比例）。

**한국어**：`meeting_to_tasks` 같은 업무 템플릿에는 필요 없습니다. 과거 지식을 검색·축적하는 `generalize_knowledge` 같은 템플릿에서만 씁니다. 5종 중에서 고를 수 있습니다: **Qdrant·pgvector·Chroma·Milvus·Weaviate.** 공개(public 컬렉션에 쓰기)는 반드시 사람의 검토를 거칩니다——`submit_candidate`는 비공개 컬렉션에 후보를 올려 검토를 기다리게 할 뿐이며, 일람·수정·승인·거부는 `aipmo knowledge`(CLI)나 웹 "지식" 화면에서만 할 수 있습니다. 승인·거부 모두 "누가·언제·어떻게 판단했는지"를 그 후보의 비공개 기록에 남기고 지우지 않습니다. `self_learning_cycle` 템플릿은 로컬 LLM으로 연습을 반복합니다: 가상의 과제를 만들고→스스로 판단하고→다른 로컬 모델이 검증하여→통과한 것만 후보로 제출하며, 실제 트래커에는 전혀 쓰지 않습니다. 운영자가 UI 체크박스나 `aipmo learning enable`로 "RAG를 전적으로 신뢰"하겠다고 명시적으로 선택했을 때만 이 사이클의 후보가 자동 승인되며, 언제든 끌 수 있습니다. 후보를 검토할 때는 비슷한 과거 후보들의 참고 통계(인간 판단·LLM 판단의 건수와 비율)도 함께 보여줍니다.

**Español**：No se necesita para una plantilla de trabajo como `meeting_to_tasks`. Solo lo usa una plantilla que busca o acumula conocimiento pasado, como `generalize_knowledge`. Se puede elegir entre 5 opciones: **Qdrant, pgvector, Chroma, Milvus, Weaviate.** Publicar (escribir en la colección `public`) siempre pasa por una revisión humana: `submit_candidate` solo deja el candidato en la colección privada a la espera; solo `aipmo knowledge` (CLI) o la pantalla web "Knowledge" permiten listarlos, editarlos, aprobarlos o rechazarlos. Tanto aprobar como rechazar añaden quién decidió qué y cuándo al registro privado del candidato, sin borrarlo nunca. La plantilla `self_learning_cycle` practica con un LLM local en bucle: inventa una tarea ficticia, se juzga a sí misma, un segundo modelo local lo verifica, y solo lo verificado se envía como candidato — nunca se escribe en un rastreador real. Solo cuando un operador elige explícitamente, con una casilla en la web o `aipmo learning enable`, "confiar por completo en el RAG", los candidatos de este ciclo se aprueban automáticamente, y puede desactivarse en cualquier momento. Al revisar un candidato también se muestran estadísticas de referencia de candidatos pasados similares (decisiones humanas y veredictos del LLM, con conteos y porcentajes).

**Français**：Pas nécessaire pour un modèle métier comme `meeting_to_tasks`. Utilisé seulement par un modèle qui recherche ou accumule des connaissances passées, comme `generalize_knowledge`. Choix entre 5 options : **Qdrant, pgvector, Chroma, Milvus, Weaviate.** La publication (écriture dans la collection `public`) passe toujours par une revue humaine : `submit_candidate` ne fait que déposer le candidat dans la collection privée en attente ; seuls `aipmo knowledge` (CLI) ou l'écran web « Knowledge » permettent de lister, modifier, approuver ou rejeter. Approuver comme rejeter ajoutent qui a décidé quoi et quand sur l'enregistrement privé du candidat, sans jamais le supprimer. Le modèle `self_learning_cycle` s'entraîne en boucle avec un LLM local : invente une tâche fictive, se juge lui-même, un second modèle local vérifie, et seul ce qui est vérifié est soumis comme candidat — rien n'est jamais écrit dans un vrai gestionnaire de tickets. Ce n'est que lorsqu'un opérateur choisit explicitement, via une case à cocher web ou `aipmo learning enable`, de « faire entièrement confiance au RAG » que les candidats de ce cycle sont approuvés automatiquement, et cela peut être désactivé à tout moment. Lors de l'examen d'un candidat, des statistiques de référence sur des candidats passés similaires (décisions humaines et verdicts du LLM, avec comptes et pourcentages) sont également affichées.

**Deutsch**：Nicht nötig für eine Geschäftsvorlage wie `meeting_to_tasks`. Wird nur von einer Vorlage genutzt, die vergangenes Wissen durchsucht oder ansammelt, wie `generalize_knowledge`. Wahl zwischen 5 Optionen: **Qdrant, pgvector, Chroma, Milvus, Weaviate.** Veröffentlichung (Schreiben in die `public`-Sammlung) läuft immer über eine menschliche Prüfung: `submit_candidate` legt den Kandidaten nur wartend in der privaten Sammlung ab; nur `aipmo knowledge` (CLI) oder der Web-Bildschirm „Knowledge" erlauben Auflisten, Bearbeiten, Genehmigen oder Ablehnen. Sowohl Genehmigen als auch Ablehnen fügen dem privaten Datensatz des Kandidaten hinzu, wer was wann entschieden hat, und löschen ihn nie. Die Vorlage `self_learning_cycle` übt mit einem lokalen LLM in einer Schleife: erfindet eine fiktive Aufgabe, beurteilt sich selbst, ein zweites lokales Modell prüft, und nur Geprüftes wird als Kandidat eingereicht — es wird nie in ein echtes Tracking-System geschrieben. Erst wenn ein Betreiber über eine Web-Checkbox oder `aipmo learning enable` ausdrücklich wählt, „dem RAG vollständig zu vertrauen", werden Kandidaten dieses Zyklus automatisch genehmigt, was sich jederzeit wieder abschalten lässt. Beim Prüfen eines Kandidaten werden zudem Referenzstatistiken ähnlicher früherer Kandidaten angezeigt (menschliche Entscheidungen und LLM-Urteile, mit Anzahl und Prozent).

**Português**：Não é necessário para um modelo de negócio como `meeting_to_tasks`. Usado apenas por um modelo que busca ou acumula conhecimento passado, como `generalize_knowledge`. Pode-se escolher entre 5 opções: **Qdrant, pgvector, Chroma, Milvus, Weaviate.** A publicação (escrita na coleção `public`) sempre passa por revisão humana: `submit_candidate` apenas deixa o candidato na coleção privada aguardando; somente `aipmo knowledge` (CLI) ou a tela web "Knowledge" permitem listar, editar, aprovar ou rejeitar. Tanto aprovar quanto rejeitar acrescentam quem decidiu o quê e quando ao registro privado do candidato, sem nunca apagá-lo. O modelo `self_learning_cycle` pratica em loop com um LLM local: inventa uma tarefa fictícia, julga a si mesmo, um segundo modelo local verifica, e só o que é verificado é enviado como candidato — nada é escrito em um rastreador real. Somente quando um operador escolhe explicitamente, por uma caixa de seleção na web ou `aipmo learning enable`, "confiar totalmente no RAG", os candidatos desse ciclo são aprovados automaticamente, podendo ser desativado a qualquer momento. Ao revisar um candidato, também são exibidas estatísticas de referência de candidatos semelhantes anteriores (decisões humanas e veredictos do LLM, com contagens e porcentagens).
