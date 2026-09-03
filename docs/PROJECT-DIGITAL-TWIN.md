# Project Digital Twin

プロジェクトを「タスクの一覧」ではなく、状態そのもの（WBS・タスク・
スケジュール予測・リソース・リスク・課題・依存関係・予算・意思決定・
ドキュメント）として保持し、決定論的なルール採点 + LLM の所見という
ハイブリッド構成で「このプロジェクトは大丈夫か」に答える。

Holds a project as full state — not just a task list — across WBS, tasks,
schedule forecast, resources, risks, issues, dependencies, budget,
decisions, and documents, and answers "is this project okay?" through a
hybrid of deterministic rule scoring and an LLM narrative.

## なぜこの形か / Why this shape

- **ルールがスコアを決め、LLM は書くだけ。** [aipmo/health.py](../aipmo/health.py)
  の `assess_project_health` が5軸（schedule・resources・risks・budget・
  blockers）を重み付き加算で採点する。この数字は再現可能で、後から
  「なぜこのスコアなのか」を説明できる。LLM はこの確定した数字を渡され、
  所見と推奨アクションの文章を書くだけで、スコアを計算し直すことはしない
  ——[aipmo/portfolio.py](../aipmo/portfolio.py) と
  [prompts/sprint_health_ja.md](../prompts/sprint_health_ja.md) が既に
  確立しているのと同じ原則。

  Rules score, the LLM only writes. `assess_project_health` in
  `aipmo/health.py` computes a weighted sum across five dimensions —
  reproducible, and explainable after the fact. The LLM receives that
  finished number and writes narrative/recommendations from it; it never
  recomputes the score, mirroring the rule already established by
  `aipmo/portfolio.py` and `prompts/sprint_health_ja.md`.

- **テーブルは `dt_` 接頭辞。** `queries.yaml` には昔からある未使用の
  `overdue_tasks` クエリが暗に前提とする `tasks` テーブル名があり、
  それと衝突しないための接頭辞。12個のテーブルは
  [sql/schema.sql](../sql/schema.sql) の該当箇所を参照。

  Tables use a `dt_` prefix to avoid colliding with the `tasks` table name
  implied (but never defined) by the pre-existing, unused `overdue_tasks`
  query in `queries.yaml`. See `sql/schema.sql` for all 12 tables.

- **`id` だけ `DEFAULT gen_random_uuid()::text`。** 他のテーブル
  （`runs`・`wbs_replan_proposals` など）は Python 側で ID を発行するが、
  `dt_` テーブルは汎用の `postgres.execute` から書き込むテンプレートで、
  テンプレート側に ID を発行する手段が無い。冪等性は `id` ではなく、
  各テーブルの自然キー（`tenant` + `jira_*_key` など）への
  `ON CONFLICT` で保証する。詳しくは `sql/schema.sql` の該当コメント。

  `id` alone gets `DEFAULT gen_random_uuid()::text`. Every other table
  mints its id in Python; the `dt_` tables are written from templates via
  the generic `postgres.execute` step, which has no way to mint an id
  inline. Idempotency instead comes from each table's natural key (tenant
  + a Jira key) via `ON CONFLICT`, not from `id`.

## 構成 / What's here

| 何 / What | どこ / Where |
|---|---|
| スキーマ（12テーブル） | [sql/schema.sql](../sql/schema.sql) |
| named query | [queries.yaml](../queries.yaml)（`dt_` で始まるもの） |
| ルール採点 | [aipmo/health.py](../aipmo/health.py) |
| 同期テンプレート（毎日9時） | [templates/examples/digital_twin_sync.yaml](../templates/examples/digital_twin_sync.yaml) |
| 診断テンプレート | [templates/examples/digital_twin_diagnose.yaml](../templates/examples/digital_twin_diagnose.yaml) |
| 診断プロンプト | [prompts/digital_twin_diagnosis_ja.md](../prompts/digital_twin_diagnosis_ja.md) |
| 読み取り API | `GET /api/v1/projects/{id}/state`・`GET /api/v1/projects/{id}/diagnose`（[aipmo/web/server.py](../aipmo/web/server.py)） |

同期を起動するのは既存の汎用 `/api/runs` エンドポイント
（`digital_twin_sync` テンプレート）で、専用の `/sync` エンドポイントは
作っていない。診断も同様に `digital_twin_diagnose` テンプレートの実行
から行う。

Sync is triggered through the existing generic `/api/runs` endpoint
(running the `digital_twin_sync` template) — no dedicated `/sync` route was
added, since one would just duplicate it. Diagnosis works the same way via
`digital_twin_diagnose`.

## 今回やらないこと / What this does not do yet

意図的に外した3点。理由は「設計上の判断がまだ無い」または
「今のアダプタでは取れない」のどちらか。テーブルとスキーマは用意して
あるので、実装はいつでも追加できる。

Three things left out on purpose — either the design doesn't make the call
yet, or the current adapter can't fetch the data. Every table is already in
the schema, so implementing these later needs no migration.

1. **リスクの自動抽出（LLM で Jira の説明文から抽出）** —
   元設計は「LLM で抽出」としか書いておらず、その呼び出しの入出力形が
   決まっていない。`dt_risks` はスキーマだけ用意し、同期では書き込まない。

   **Risk auto-extraction from Jira descriptions via LLM** — the source
   design says only "extract via LLM" with no schema for that call's
   input/output. `dt_risks` ships with its table but nothing writes to it.

2. **依存関係の同期（Jira の課題間リンクから）** — `aipmo/adapters/jira.py`
   にも `jira_agile.py` にも issue link を返す手段が無く、新しいアダプタ
   アクションが要る。`dt_dependencies` も同様にスキーマのみ。

   **Dependency sync from Jira issue links** — neither `jira.py` nor
   `jira_agile.py` exposes issue links; this needs a new adapter action.
   `dt_dependencies` ships schema-only as well.

3. **`jira.search()` が返さないフィールド** — `担当者以外の詳細・
   優先度・課題種別・カスタムフィールド・課題リンク` は、Jira API から
   取得可能でも `JiraAdapter.search()` の出力マッピングが
   `key`・`summary`・`status`・`assignee`・`assignee_id`・`due_date`・
   `labels` の7項目に固定されているため取り出せない。このため
   `dt_resources`・`dt_budget`・`dt_issues` への同期、および
   タスクの `reporter`・`priority`・`story_points`・親 Epic との紐付けは
   すべて未実装（`digital_twin_sync.yaml` 内のコメントにも明記）。
   アダプタのマッピング自体を拡張しない限り、テンプレート側だけでは
   直しようがない制約。

   **Fields `jira.search()` never returns** — reporter, priority, issue
   type, custom fields, and issue links are all available from the Jira
   API but `JiraAdapter.search()`'s Python code hardcodes its output
   mapping to just 7 fields (key, summary, status, assignee, assignee_id,
   due_date, labels). That blocks syncing `dt_resources`, `dt_budget`, and
   `dt_issues`, plus a task's reporter/priority/story_points/parent-Epic
   link (documented inline in `digital_twin_sync.yaml`). Nothing a
   template can work around — it needs the adapter's own mapping extended.

## テストの見方 / Where the tests live

- [tests/test_health.py](../tests/test_health.py) — ルール採点の純粋関数
  テスト（重み付け・境界値・`STATUS_RED` に実際に到達できることの確認）。
- [tests/test_digital_twin.py](../tests/test_digital_twin.py) — named
  query の束縛（`FakeConnection`）、テンプレートの構造、2つの読み取り API
  のテナント分離・権限・404/503。
- テンプレートのロード・プロンプト存在確認は
  [tests/test_templates.py](../tests/test_templates.py) が全テンプレート
  共通で自動カバーする。
