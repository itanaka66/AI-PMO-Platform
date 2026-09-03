-- AI-PMO Platform — PostgreSQL schema
-- 実行履歴、ナレッジ昇格ワークフロー、テナント利用許諾を保持する。
-- Holds run history, the knowledge promotion workflow, and tenant consent.

CREATE TABLE IF NOT EXISTS tenant_consent (
    tenant        TEXT PRIMARY KEY,
    -- A: 二次利用不可 / no secondary use
    -- B: 匿名化ノウハウとして利用可 / anonymized knowledge may be reused
    -- C: 事例公開可 / case study may be published
    level         CHAR(1) NOT NULL DEFAULT 'A' CHECK (level IN ('A','B','C')),
    contract_ref  TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    template        TEXT NOT NULL,
    template_version TEXT,
    trigger         JSONB,
    status          TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS step_results (
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    output      JSONB,
    error       TEXT,
    attempts    INT NOT NULL DEFAULT 1,
    duration_ms INT,
    PRIMARY KEY (run_id, step_id)
);

-- ナレッジ昇格ワークフロー / knowledge promotion workflow.
-- L0-L2 は保存しない。ここに入るのは一般化済みの候補のみ。
-- L0-L2 are never stored here; only generalized candidates reach this table.
CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id                  TEXT PRIMARY KEY,
    tenant              TEXT NOT NULL,
    knowledge_level     INT NOT NULL CHECK (knowledge_level BETWEEN 3 AND 6),
    category            TEXT,
    pattern_name        TEXT,
    body                JSONB NOT NULL,
    publicability_score NUMERIC(5,2),
    review_status       TEXT NOT NULL DEFAULT 'pending'
                        CHECK (review_status IN ('pending','approved','rejected')),
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_key          TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_candidates_pending
    ON knowledge_candidates (review_status) WHERE review_status = 'pending';

-- WBS 再計画の承認ワークフロー / WBS replan approval workflow.
-- WBS再計画AIはここにしか書けない。生WBSへの書き込み権限は与えない
-- （engine.adapters に渡す postgres アダプタの queries を、再計画系テンプレートには
-- このテーブル向けの named query しか渡さないことで担保する）。
--
-- The WBS-replanning AI can only ever write here. It is never handed a named
-- query that touches the live WBS tables — the boundary is enforced by which
-- queries a given template's postgres adapter config exposes, the same
-- mechanism that keeps templates off raw SQL and off other tenants' data.
CREATE TABLE IF NOT EXISTS wbs_replan_proposals (
    id              TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    wbs_version_from TEXT,
    -- 提案は「全体書き換え」ではなく「差分」。変更されていない項目には
    -- 一切触れない（TODO更新の update_issue と同じ設計判断）。
    -- A diff, never a full rewrite — untouched items are never mentioned,
    -- the same choice made for update_issue.
    diff            JSONB NOT NULL,
    rationale       TEXT,
    assumptions     JSONB,
    -- Tier 1: 軽微・自動反映可 / minor, safe to auto-apply
    -- Tier 2: 重大・要承認 / significant, approval required
    -- Tier 3: 危機的・個別即時通知 / critical, notified individually and immediately
    tier            INT NOT NULL CHECK (tier IN (1, 2, 3)),
    confidence      NUMERIC(3,2),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','stale','superseded')),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    -- 却下理由・承認時の補足など。必須にはしない
    -- （理由を強制すると、急ぎの却下が「後で書く」で止まってしまう）。
    -- An optional note (a rejection reason, or a caveat on an approval).
    -- Not required — forcing one would let an urgent rejection stall on
    -- "I'll fill this in later".
    decision_note   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 同じドリフトに対する重複提案を防ぐための冪等キー
    -- (例: "{wbs_id}:{drift_signature}")。新しい差分が同じ根拠から
    -- 出た場合は、新規行を作らずこのキーで既存の pending 行を更新する
    -- （supersede）。
    -- Idempotency key preventing duplicate proposals for the same drift
    -- (e.g. "{wbs_id}:{drift_signature}"). A new diff from the same root
    -- cause updates the existing pending row under this key instead of
    -- inserting a second one.
    source_key      TEXT UNIQUE
);

-- 同じ tier に対して複数の代替案（A/B）を並べて提示する場合のラベル。
-- 単一案のときは NULL のまま（既存の提案・テンプレートはこの列を知らない）。
-- source_key に含めることで、同じ wbs_id・tier でもラベルが違えば
-- 別の行として共存できる -- supersede の対象は「同じラベルの前回案」だけ。
--
-- Label for presenting multiple alternative proposals (A/B) side by side
-- for the same tier. Stays NULL for a single-option proposal (existing
-- proposals/templates do not know this column). Folding it into
-- source_key lets differently-labelled alternatives for the same
-- wbs_id/tier coexist as separate rows -- supersede only replaces a
-- previous proposal under the *same* label.
ALTER TABLE wbs_replan_proposals ADD COLUMN IF NOT EXISTS option_label TEXT;

CREATE INDEX IF NOT EXISTS idx_wbs_proposals_pending
    ON wbs_replan_proposals (tenant, status) WHERE status = 'pending';

-- Risk/Forecast の予測履歴 / forecast snapshot history.
-- classify_drift のヒステリシス判定（前回のドリフト量との比較）に使う。
-- 1 wbs あたり最新1件だけ保持すれば足りるので upsert する。
--
-- Feeds classify_drift's hysteresis (comparison against the last recorded
-- drift). Only the latest snapshot per wbs is needed, hence the upsert.
CREATE TABLE IF NOT EXISTS wbs_forecast_snapshots (
    wbs_id          TEXT NOT NULL,
    tenant          TEXT NOT NULL,
    drift_days      NUMERIC(6,2),
    tier            INT,
    forecast        JSONB NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, wbs_id)
);

-- Project Digital Twin / プロジェクトの状態を横断的に保持する。
-- 「タスク一覧」ではなく「状態」として捉え直すための11のテーブルと、
-- その診断結果を保持する dt_health_diagnostics。
--
-- id は他の多くのテーブル（runs・knowledge_candidates 等）と違い、
-- DEFAULT gen_random_uuid()::text を持つ（型はそれらと同じく TEXT のまま。
-- UUID 型にはしない）。他のテーブルはアプリ側の Python コードが id を
-- 発行してから書き込むが、ここは postgres アダプタの汎用 execute から
-- テンプレート経由で書き込む。テンプレートの式評価には関数呼び出しが無く、
-- for_each の1要素につき道具呼び出しは1回だけなので、要素ごとに新しい id を
-- 組み立てる手段がテンプレート側に無い — そこで DB 側にゆだねる。
-- 冪等性は id ではなく、各テーブルの ON CONFLICT が使う自然キー
-- （tenant + jira_*_key）で保証する。詳しくは docs/PROJECT-DIGITAL-TWIN.md。
--
-- Project Digital Twin — holds a project's full state, not just its task
-- list: eleven state tables plus dt_health_diagnostics for diagnosis
-- results.
--
-- Unlike most other tables here (runs, knowledge_candidates, ...), id
-- defaults to gen_random_uuid()::text (still TEXT, not a UUID column —
-- matching every other table's type). Those other tables have Python
-- application code mint an id before writing; these are written through
-- postgres's generic execute action from a template instead. The
-- template expression evaluator has no function calls, and a for_each
-- element gets exactly one tool call, so a template has no way to build a
-- fresh id per element — the database does it instead. Idempotency comes
-- not from id but from each table's own natural key (tenant +
-- jira_*_key), which its ON CONFLICT clause uses. See
-- docs/PROJECT-DIGITAL-TWIN.md.
--
-- dt_ を接頭辞にしているのは、queries.yaml の overdue_tasks が参照する
-- （が、このスキーマにはもともと定義の無い）"tasks" という名前との衝突を
-- 避けるため。overdue_tasks はこの変更の対象外。
--
-- The dt_ prefix avoids colliding with the name "tasks" that
-- queries.yaml's own overdue_tasks query already references (to a table
-- this schema has never actually defined). overdue_tasks is untouched by
-- this change.
CREATE TABLE IF NOT EXISTS dt_projects (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT,
    jira_project_key    TEXT,
    status              TEXT NOT NULL DEFAULT 'Active'
                        CHECK (status IN ('Active','At Risk','Critical','Closed')),
    health_score        INT CHECK (health_score BETWEEN 0 AND 100),
    health_status       TEXT CHECK (health_status IN ('Green','Yellow','Red')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_analyzed       TIMESTAMPTZ,
    last_synced         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant, jira_project_key)
);

CREATE INDEX IF NOT EXISTS idx_dt_projects_tenant ON dt_projects (tenant);
CREATE INDEX IF NOT EXISTS idx_dt_projects_health ON dt_projects (tenant, health_status);

CREATE TABLE IF NOT EXISTS dt_wbs_nodes (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    parent_id           TEXT REFERENCES dt_wbs_nodes(id) ON DELETE SET NULL,
    name                TEXT NOT NULL,
    description         TEXT,
    level               INT NOT NULL CHECK (level >= 0),
    jira_epic_key       TEXT,
    planned_start       DATE,
    planned_end         DATE,
    actual_completion   NUMERIC(5,2) NOT NULL DEFAULT 0
                        CHECK (actual_completion BETWEEN 0 AND 100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant, project_id, jira_epic_key)
);

CREATE INDEX IF NOT EXISTS idx_dt_wbs_project ON dt_wbs_nodes (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_wbs_parent ON dt_wbs_nodes (parent_id);

CREATE TABLE IF NOT EXISTS dt_tasks (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    wbs_node_id         TEXT REFERENCES dt_wbs_nodes(id) ON DELETE SET NULL,
    jira_issue_key      TEXT NOT NULL,
    jira_issue_type     TEXT,
    title               TEXT NOT NULL,
    description         TEXT,
    status              TEXT,
    assignee            TEXT,
    reporter            TEXT,
    priority            TEXT,
    story_points        INT CHECK (story_points IS NULL OR story_points > 0),
    planned_end         DATE,
    actual_end          DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced         TIMESTAMPTZ,
    UNIQUE (tenant, jira_issue_key)
);

CREATE INDEX IF NOT EXISTS idx_dt_tasks_project ON dt_tasks (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_tasks_status ON dt_tasks (tenant, status);
CREATE INDEX IF NOT EXISTS idx_dt_tasks_assignee ON dt_tasks (tenant, assignee);

CREATE TABLE IF NOT EXISTS dt_schedule_forecast (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    planned_start       DATE,
    planned_end         DATE,
    current_forecast    DATE,
    as_of_date          DATE NOT NULL,
    variance_days       INT,
    variance_percent    NUMERIC(5,2),
    confidence          NUMERIC(3,2) CHECK (confidence BETWEEN 0 AND 1),
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dt_schedule_project ON dt_schedule_forecast (tenant, project_id);

CREATE TABLE IF NOT EXISTS dt_resources (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    email               TEXT,
    slack_id            TEXT,
    jira_account_id     TEXT,
    role                TEXT,
    allocation_percent  NUMERIC(5,2) CHECK (allocation_percent BETWEEN 0 AND 100),
    start_date          DATE,
    end_date            DATE,
    status              TEXT NOT NULL DEFAULT 'Active'
                        CHECK (status IN ('Active','On Leave','Off-boarded')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dt_resources_project ON dt_resources (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_resources_role ON dt_resources (tenant, role);

-- リスクは今のところ Jira からの自動抽出をしない。確率・影響度を LLM に
-- 推定させるには、説明責任のある設計（根拠をどう残すか）をまだ詰めていない。
-- 手入力と、将来の同期実装のためにテーブルだけ用意する。
--
-- Risks are not auto-extracted from Jira yet: having an LLM estimate
-- probability and impact needs an accountable design (how the reasoning is
-- kept) that isn't settled. The table exists for manual entry and a future
-- sync implementation.
CREATE TABLE IF NOT EXISTS dt_risks (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT,
    category            TEXT,
    probability         NUMERIC(3,2) NOT NULL CHECK (probability BETWEEN 0 AND 1),
    impact              NUMERIC(3,2) NOT NULL CHECK (impact BETWEEN 0 AND 1),
    exposure            NUMERIC(5,3) GENERATED ALWAYS AS (probability * impact) STORED,
    status              TEXT NOT NULL DEFAULT 'Active'
                        CHECK (status IN ('Active','Mitigated','Closed','Accepted')),
    mitigation_strategy TEXT,
    mitigation_owner    TEXT,
    identified_date     DATE,
    mitigation_start_date DATE,
    mitigation_end_date DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dt_risks_project ON dt_risks (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_risks_status ON dt_risks (tenant, status);
CREATE INDEX IF NOT EXISTS idx_dt_risks_exposure ON dt_risks (tenant, exposure DESC);

CREATE TABLE IF NOT EXISTS dt_issues (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    jira_issue_key      TEXT,
    title               TEXT NOT NULL,
    description         TEXT,
    severity            TEXT CHECK (severity IN ('Low','Medium','High','Critical')),
    status              TEXT,
    is_blocker          BOOLEAN NOT NULL DEFAULT false,
    blocks_tasks        JSONB,
    assignee            TEXT,
    owner               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    opened_date         DATE,
    resolved_date       DATE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant, jira_issue_key)
);

CREATE INDEX IF NOT EXISTS idx_dt_issues_project ON dt_issues (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_issues_status ON dt_issues (tenant, status);
CREATE INDEX IF NOT EXISTS idx_dt_issues_blocker ON dt_issues (tenant, is_blocker) WHERE is_blocker;

-- 依存関係は今のところ Jira の issue link からの同期をしない
-- （jira / jira_agile アダプタに issue link を読むアクションがまだ無い）。
-- 手入力と、将来の同期実装のためにテーブルだけ用意する。
--
-- Dependencies are not synced from Jira issue links yet — neither the jira
-- nor jira_agile adapter has an action that reads them. The table exists
-- for manual entry and a future sync implementation.
CREATE TABLE IF NOT EXISTS dt_dependencies (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    from_task_id        TEXT NOT NULL REFERENCES dt_tasks(id) ON DELETE CASCADE,
    to_task_id          TEXT NOT NULL REFERENCES dt_tasks(id) ON DELETE CASCADE,
    dep_type            TEXT NOT NULL DEFAULT 'finish-to-start'
                        CHECK (dep_type IN ('finish-to-start','start-to-start','finish-to-finish')),
    lag_days            INT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dt_no_self_dependency CHECK (from_task_id != to_task_id)
);

CREATE INDEX IF NOT EXISTS idx_dt_dependencies_from ON dt_dependencies (from_task_id);
CREATE INDEX IF NOT EXISTS idx_dt_dependencies_to ON dt_dependencies (to_task_id);

CREATE TABLE IF NOT EXISTS dt_budget (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    planned_amount      NUMERIC(14,2) NOT NULL,
    actual_amount       NUMERIC(14,2) NOT NULL DEFAULT 0,
    forecast_amount     NUMERIC(14,2),
    variance            NUMERIC(14,2) GENERATED ALWAYS AS (forecast_amount - planned_amount) STORED,
    variance_percent    NUMERIC(6,2) GENERATED ALWAYS AS (
        CASE WHEN planned_amount > 0
             THEN ((forecast_amount - planned_amount) / planned_amount) * 100
             ELSE NULL
        END
    ) STORED,
    as_of_date          DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 予算は現在値の1行のみ（履歴ではない）。プロジェクトごとに upsert する。
    -- Current-state only, one row per project (not a history) — upserted.
    UNIQUE (tenant, project_id)
);

CREATE INDEX IF NOT EXISTS idx_dt_budget_project ON dt_budget (tenant, project_id);

CREATE TABLE IF NOT EXISTS dt_decisions (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    description         TEXT,
    context             TEXT,
    decision_text       TEXT NOT NULL,
    rationale           TEXT,
    decision_date       DATE NOT NULL,
    decided_by          TEXT,
    status              TEXT NOT NULL DEFAULT 'Approved'
                        CHECK (status IN ('Proposed','Approved','Rejected','Superseded')),
    impact_areas        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dt_decisions_project ON dt_decisions (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_decisions_date ON dt_decisions (tenant, decision_date);

-- content_summary までがこのテーブルの持ち分。全文の意味検索は、この基盤の
-- 既存の役割分担どおり vector_store アダプタ（Qdrant / pgvector / Chroma /
-- Milvus / Weaviate から選択、docs/VECTOR_STORES.md）に委ねる — embedding
-- 列をここに直接持たせない。
--
-- This table's job stops at content_summary. Full semantic search is left
-- to the vector_store adapter (Qdrant/pgvector/Chroma/Milvus/Weaviate,
-- docs/VECTOR_STORES.md), matching this platform's existing separation of
-- concerns — no embedding column lives here.
CREATE TABLE IF NOT EXISTS dt_documents (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    description         TEXT,
    url                 TEXT,
    doc_type            TEXT,
    doc_status          TEXT CHECK (doc_status IN ('Draft','Review','Approved','Archived')),
    content_summary     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at          TIMESTAMPTZ,
    version             INT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_dt_documents_project ON dt_documents (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_documents_type ON dt_documents (tenant, doc_type);

CREATE TABLE IF NOT EXISTS dt_health_diagnostics (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant              TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES dt_projects(id) ON DELETE CASCADE,
    diagnosed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    health_score        INT CHECK (health_score BETWEEN 0 AND 100),
    health_status       TEXT CHECK (health_status IN ('Green','Yellow','Red')),
    rule_scores         JSONB,
    blockers            JSONB,
    recommendations     JSONB,
    confidence          NUMERIC(3,2) CHECK (confidence BETWEEN 0 AND 1),
    analysis_summary    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dt_diagnostics_project ON dt_health_diagnostics (tenant, project_id);
CREATE INDEX IF NOT EXISTS idx_dt_diagnostics_date
    ON dt_health_diagnostics (tenant, project_id, diagnosed_at DESC);
