-- 003: egress approvals are single-use. The primary key makes "first use wins" atomic across replicas.
CREATE TABLE egress_approval_use (
    approval_id TEXT PRIMARY KEY,
    used_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX egress_approval_use_expires ON egress_approval_use (expires_at);
