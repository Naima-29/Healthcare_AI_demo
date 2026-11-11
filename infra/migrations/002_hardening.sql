-- 002: make the draft schema safe to run in production.
--  * outbox: body as TEXT (JSONB cannot store \u0000), ordering, lease-based claiming, poison-row handling
--  * audit_event: append-only (UPDATE / DELETE / TRUNCATE are rejected by the database itself)
--  * escalation: lease column for the tick worker, one escalation per red-flag message
--  * manifest: at most one active version per seat; manifest_draft for the Director approval flow
--  * gate_item: durable queue of case packs waiting for a clinician (the gate service reads it)
--  * oncall_shift: the rota the escalation ladder pages from

-- ---------------------------------------------------------------- outbox
ALTER TABLE outbox ALTER COLUMN payload TYPE TEXT USING payload::text;
ALTER TABLE outbox ADD COLUMN seq           BIGSERIAL;
ALTER TABLE outbox ADD COLUMN msg_id        TEXT;
ALTER TABLE outbox ADD COLUMN claimed_until TIMESTAMPTZ;
ALTER TABLE outbox ADD COLUMN attempts      INT NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN last_error    TEXT;
ALTER TABLE outbox ADD COLUMN dead_at       TIMESTAMPTZ;
UPDATE outbox SET msg_id = id::text WHERE msg_id IS NULL;
ALTER TABLE outbox ALTER COLUMN msg_id SET NOT NULL;
CREATE UNIQUE INDEX outbox_msg_id_key ON outbox (msg_id);
DROP INDEX idx_outbox_unsent;
CREATE INDEX outbox_pending ON outbox (seq) WHERE sent_at IS NULL AND dead_at IS NULL;

-- ---------------------------------------------------------------- idempotency
CREATE INDEX idempotency_marked_at ON idempotency (marked_at);

-- ---------------------------------------------------------------- audit (append-only)
CREATE UNIQUE INDEX audit_event_seq_key ON audit_event (seq);

CREATE FUNCTION audit_event_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only (% rejected)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END
$$;
CREATE TRIGGER audit_event_no_change BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();
CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event
    FOR EACH STATEMENT EXECUTE FUNCTION audit_event_immutable();

-- ---------------------------------------------------------------- escalation
ALTER TABLE escalation ADD COLUMN lease_until TIMESTAMPTZ;
DROP INDEX idx_escalation_message;
CREATE UNIQUE INDEX escalation_message_key ON escalation (message_id);

-- ---------------------------------------------------------------- manifests
CREATE UNIQUE INDEX manifest_one_active ON manifest (seat) WHERE active;
ALTER TABLE manifest ADD COLUMN activated_at TIMESTAMPTZ;

CREATE TABLE manifest_draft (
    id            UUID PRIMARY KEY,
    seat          TEXT NOT NULL,
    base_version  INT,                       -- live version the draft was made against; NULL for a new seat
    body          JSONB NOT NULL,
    diff          JSONB NOT NULL,
    instruction   TEXT NOT NULL,
    used_model    BOOLEAN NOT NULL DEFAULT FALSE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    proposed_by   TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    decision_note TEXT
);
CREATE INDEX manifest_draft_pending ON manifest_draft (created_at) WHERE status = 'pending';

-- ---------------------------------------------------------------- gate queue
CREATE TABLE gate_item (
    case_id     TEXT NOT NULL,
    version     INT NOT NULL,
    message_id  TEXT NOT NULL,
    patient_id  TEXT NOT NULL,
    episode_id  TEXT NOT NULL,
    pack        JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending_review',
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, version)
);
CREATE INDEX gate_item_pending ON gate_item (opened_at) WHERE status = 'pending_review';

-- ---------------------------------------------------------------- on-call rota
CREATE TABLE oncall_shift (
    id           BIGSERIAL PRIMARY KEY,
    unit         TEXT NOT NULL,
    role         TEXT NOT NULL,              -- primary | backup | head_of_unit
    clinician_id TEXT NOT NULL,
    starts_at    TIMESTAMPTZ NOT NULL,
    ends_at      TIMESTAMPTZ NOT NULL,
    CHECK (ends_at > starts_at)
);
CREATE INDEX oncall_shift_lookup ON oncall_shift (unit, role, starts_at DESC);
