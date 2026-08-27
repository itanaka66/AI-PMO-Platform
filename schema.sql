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
