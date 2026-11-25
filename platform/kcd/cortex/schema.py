"""Loop Cortex tables (SQLAlchemy Core). Portable: SQLite for development, Postgres in the pilot.

Layers (deck slide 24): L0 medical knowledge & law, L1 specialty playbook, L2 the hospital,
L3 the patient. A partition is (layer, key): L3 key = patient id, L2 key = hospital id,
L0/L1 key = "global" / specialty name.
"""
from sqlalchemy import Column, Float, Index, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()

# What proves: immutable, append-only, hash-chained per partition.
evidence = Table(
    "cortex_evidence", metadata,
    Column("id", String(64), primary_key=True),
    Column("layer", Integer, nullable=False),
    Column("partition", String(200), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(100), nullable=False),
    Column("source", String(200), nullable=False),
    Column("recorded_at", String(40), nullable=False),
    Column("content", Text, nullable=False),            # canonical JSON
    Column("content_sha256", String(64), nullable=False),
    Column("blob_sha256", String(64)),                   # optional binary (image, panel file) in the blob store
    Column("meta", Text, nullable=False),                # canonical JSON
    Column("prev_hash", String(64), nullable=False),
    Column("chain_hash", String(64), nullable=False),
    UniqueConstraint("layer", "partition", "seq", name="uq_evidence_chain_position"),
)
Index("ix_evidence_partition_kind", evidence.c.layer, evidence.c.partition, evidence.c.kind)

# What is believed: typed, versioned, trial (shadow) until a human confirms.
knowledge = Table(
    "cortex_knowledge", metadata,
    Column("id", String(64), primary_key=True),
    Column("layer", Integer, nullable=False),
    Column("partition", String(200), nullable=False),
    Column("key", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("value", Text, nullable=False),               # canonical JSON
    Column("confidence", Float, nullable=False),
    Column("half_life_days", Float, nullable=False),
    Column("source", String(200), nullable=False),
    Column("evidence_ids", Text, nullable=False),        # JSON list
    Column("status", String(20), nullable=False),        # shadow | confirmed | rejected | superseded | retired
    Column("proposed_by", String(200), nullable=False),
    Column("proposed_at", String(40), nullable=False),
    Column("decided_by", String(200)),
    Column("decided_role", String(100)),
    Column("decided_at", String(40)),
    Column("last_verified", String(40)),
    Column("note", Text),
    UniqueConstraint("layer", "partition", "key", "version", name="uq_knowledge_version"),
)
Index("ix_knowledge_partition_status", knowledge.c.layer, knowledge.c.partition, knowledge.c.status)

# Idempotency keys for seats (e.g. a study or a decision already processed).
processed = Table(
    "cortex_processed", metadata,
    Column("key", String(300), primary_key=True),
    Column("layer", Integer, nullable=False),
    Column("partition", String(200), nullable=False),
    Column("result", Text),
    Column("at", String(40), nullable=False),
)

# Proof that an erasure happened, without keeping the patient's identity.
erasure_receipts = Table(
    "cortex_erasure_receipts", metadata,
    Column("id", String(64), primary_key=True),
    Column("subject_hash", String(64), nullable=False),
    Column("requested_by", String(200), nullable=False),
    Column("requested_role", String(100), nullable=False),
    Column("reason", Text, nullable=False),
    Column("erased_at", String(40), nullable=False),
    Column("evidence_rows", Integer, nullable=False),
    Column("knowledge_rows", Integer, nullable=False),
    Column("blobs_deleted", Integer, nullable=False),
    Column("hook_errors", Text, nullable=False),         # JSON list
)
