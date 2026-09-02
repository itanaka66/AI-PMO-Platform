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
