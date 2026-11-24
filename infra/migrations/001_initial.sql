-- Kidney Care Desk: initial schema for the edge-node Postgres.
-- Run against the 'kcd' database created by docker-compose.
-- Requires pgvector extension (the image installs it; we just enable it).

CREATE EXTENSION IF NOT EXISTS "pgcrypto";    -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";      -- for KM retrieval later

-- ---------------------------------------------------------------------------
-- Outbox (transactional outbox pattern for the A2A bus)
-- ---------------------------------------------------------------------------

CREATE TABLE outbox (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject     TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ
);

CREATE INDEX idx_outbox_unsent ON outbox (created_at) WHERE sent_at IS NULL;

-- ---------------------------------------------------------------------------
-- Idempotency (deduplication of A2A message delivery)
-- ---------------------------------------------------------------------------

CREATE TABLE idempotency (
    key         TEXT PRIMARY KEY,
    marked_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Audit log (hash-chained, append-only)
-- ---------------------------------------------------------------------------

CREATE TABLE audit_event (
    id                UUID PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL,
    actor             TEXT NOT NULL,
    manifest_version  TEXT,
    action            TEXT NOT NULL,
    decision          TEXT NOT NULL CHECK (decision IN ('allowed', 'blocked', 'info', 'error')),
    patient_id        TEXT,
    correlation_id    TEXT,
    inputs_hash       TEXT,
    outputs_hash      TEXT,
    detail            JSONB NOT NULL DEFAULT '{}',
    prev_hash         TEXT NOT NULL,
    hash              TEXT NOT NULL,
    seq               BIGSERIAL NOT NULL
);

CREATE INDEX idx_audit_action ON audit_event (action);
CREATE INDEX idx_audit_patient ON audit_event (patient_id) WHERE patient_id IS NOT NULL;
CREATE INDEX idx_audit_ts ON audit_event (ts);

-- ---------------------------------------------------------------------------
-- Escalations (paging ladder state)
-- ---------------------------------------------------------------------------

CREATE TABLE escalation (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    patient_id      TEXT NOT NULL,
    episode_id      TEXT NOT NULL,
    unit            TEXT NOT NULL,
    level           INT NOT NULL DEFAULT 0,
    deadline        TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'exhausted')),
    paged           JSONB NOT NULL DEFAULT '[]',
    acknowledged_by TEXT
);

CREATE INDEX idx_escalation_due ON escalation (deadline) WHERE status = 'open';
CREATE INDEX idx_escalation_message ON escalation (message_id);

-- ---------------------------------------------------------------------------
-- Manifests (versioned seat configuration)
-- ---------------------------------------------------------------------------

CREATE TABLE manifest (
    seat            TEXT NOT NULL,
    version         INT NOT NULL,
    body            JSONB NOT NULL,
    diff_summary    TEXT,
    compiled_by     TEXT,
    approved_by     TEXT,
    active          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (seat, version)
);

CREATE INDEX idx_manifest_active ON manifest (seat) WHERE active;
