-- ─────────────────────────────────────────────────────────────────────────────
-- Job Discovery System — PostgreSQL schema initialisation
-- Executed automatically by the postgres Docker container on first start.
-- Safe to re-run (all statements are idempotent).
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Core jobs table ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                SERIAL       PRIMARY KEY,
    title             VARCHAR      NOT NULL,
    company           VARCHAR      NOT NULL,
    date_posted       VARCHAR,
    url               VARCHAR,
    salary            VARCHAR,
    source            VARCHAR,                              -- primary source
    sources           JSON         NOT NULL DEFAULT '[]',  -- all platforms seen on
    search_term       VARCHAR,
    first_seen        TIMESTAMP    NOT NULL DEFAULT NOW(),
    last_seen         TIMESTAMP    NOT NULL DEFAULT NOW(),
    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    fingerprint       VARCHAR      NOT NULL UNIQUE,        -- MD5(title|company)

    -- Phase 3 — Legitimacy scoring (0-100)
    legitimacy_score  INTEGER,
    score_breakdown   JSON,                                -- {signal: points}
    suspected_ghost   BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Phase 4 — AI layer
    description       TEXT,
    embedding         JSON,                                -- text-embedding-3-small (1536 floats)
    relevance_score   FLOAT,                              -- 0-10 profile match
    ghost_probability FLOAT,                              -- 0.0-1.0
    ai_reasoning      JSON,                               -- {relevance: "...", ghost: "..."}
    combined_score    FLOAT,                              -- legitimacy*0.6 + relevance*10*0.4

    -- Phase 5 — Telegram alerts
    tg_alerted        BOOLEAN      NOT NULL DEFAULT FALSE
);

-- ── Application tracking (kanban) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS application_tracker (
    id         SERIAL    PRIMARY KEY,
    job_id     INTEGER   NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status     VARCHAR   NOT NULL DEFAULT 'saved',  -- saved/applied/interview/offer/rejected
    notes      TEXT,
    applied_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (job_id)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint     ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_is_active       ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_combined_score  ON jobs(combined_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen      ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_source          ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_suspected_ghost ON jobs(suspected_ghost);
CREATE INDEX IF NOT EXISTS idx_jobs_tg_alerted      ON jobs(tg_alerted) WHERE tg_alerted = FALSE;
CREATE INDEX IF NOT EXISTS idx_tracker_job_id       ON application_tracker(job_id);
CREATE INDEX IF NOT EXISTS idx_tracker_status       ON application_tracker(status);
