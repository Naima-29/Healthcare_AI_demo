"""Integration tests for Postgres adapters.

Require a running Postgres instance (``docker compose -f infra/docker-compose.yml up -d postgres``).
Skipped automatically when Postgres is unreachable.

Run explicitly::

    uv run pytest tests/test_pg_adapters.py -q
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from kcd_core.escalation import Escalation

try:
    import psycopg
    from psycopg_pool import ConnectionPool

    _TEST_DSN = "postgresql://kcd:kcd_dev@localhost:5432/kcd"
    try:
        # Quick connectivity check — fail fast if Postgres is not up.
        with psycopg.connect(_TEST_DSN, connect_timeout=2) as _conn:
            _conn.execute("SELECT 1")
        _PG_AVAILABLE = True
    except Exception:
        _PG_AVAILABLE = False
except ImportError:
    _PG_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _PG_AVAILABLE, reason="Postgres not available or psycopg not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS idempotency (
    key         TEXT PRIMARY KEY,
    marked_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_event (
    id                UUID PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL,
    actor             TEXT NOT NULL,
    manifest_version  TEXT,
    action            TEXT NOT NULL,
    decision          TEXT NOT NULL,
    patient_id        TEXT,
    correlation_id    TEXT,
    inputs_hash       TEXT,
    outputs_hash      TEXT,
    detail            JSONB NOT NULL DEFAULT '{}',
    prev_hash         TEXT NOT NULL,
    hash              TEXT NOT NULL,
    seq               BIGSERIAL NOT NULL
);

CREATE TABLE IF NOT EXISTS escalation (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    patient_id      TEXT NOT NULL,
    episode_id      TEXT NOT NULL,
    unit            TEXT NOT NULL,
    level           INT NOT NULL DEFAULT 0,
    deadline        TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    paged           JSONB NOT NULL DEFAULT '[]',
    acknowledged_by TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject     TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ
);
"""


@pytest.fixture(scope="module")
def pool() -> ConnectionPool:  # type: ignore[misc]
    p = ConnectionPool(_TEST_DSN, min_size=1, max_size=4, open=True)
    # Ensure tables exist.
    with p.connection() as conn:
        conn.execute(_SCHEMA_SQL)
    yield p  # type: ignore[misc]
    p.close()


@pytest.fixture(autouse=True)
def _clean_tables(pool: ConnectionPool) -> None:  # type: ignore[misc]
    """Truncate all tables before each test."""
    with pool.connection() as conn:
        conn.execute("TRUNCATE idempotency, audit_event, escalation, outbox")
    yield  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PostgresIdempotencyStore
# ---------------------------------------------------------------------------


class TestPostgresIdempotencyStore:
    def test_mark_and_has(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresIdempotencyStore

        store = PostgresIdempotencyStore(pool)
        assert store.has("k1") is False
        store.mark("k1")
        assert store.has("k1") is True

    def test_mark_idempotent(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresIdempotencyStore

        store = PostgresIdempotencyStore(pool)
        store.mark("k1")
        store.mark("k1")  # no error
        assert store.has("k1") is True


# ---------------------------------------------------------------------------
# PostgresAuditSink
# ---------------------------------------------------------------------------


class TestPostgresAuditSink:
    def test_record_and_verify_chain(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresAuditSink

        sink = PostgresAuditSink(pool)
        now = datetime.now(UTC)
        e1 = sink.record(ts=now, actor="companion", action="tool.fhir.get_patient", decision="info")
        e2 = sink.record(ts=now, actor="companion", action="seat.handled", decision="info")
        assert e1.prev_hash != e2.prev_hash  # chain links differ
        assert e2.prev_hash == e1.hash  # e2 follows e1
        assert sink.verify_chain() is True

    def test_find_filters(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresAuditSink

        sink = PostgresAuditSink(pool)
        now = datetime.now(UTC)
        sink.record(ts=now, actor="a", action="dispatch.routed", decision="allowed")
        sink.record(ts=now, actor="a", action="dispatch.reject", decision="blocked")
        sink.record(ts=now, actor="a", action="dispatch.routed", decision="allowed")
        assert len(sink.find("dispatch.routed")) == 2
        assert len(sink.find("dispatch.reject", "blocked")) == 1


# ---------------------------------------------------------------------------
# PostgresEscalationStore
# ---------------------------------------------------------------------------


class TestPostgresEscalationStore:
    def _esc(self, **overrides: object) -> Escalation:
        from kcd_core.escalation import Escalation

        defaults: dict[str, object] = {
            "id": "esc_1",
            "message_id": "msg_1",
            "patient_id": "pt_1",
            "episode_id": "ep_1",
            "unit": "nephrology",
            "level": 0,
            "deadline": datetime.now(UTC),
            "status": "open",
            "paged": (),
            "acknowledged_by": None,
        }
        defaults.update(overrides)
        return Escalation(**defaults)  # type: ignore[arg-type]

    def test_put_and_get(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresEscalationStore

        store = PostgresEscalationStore(pool)
        esc = self._esc()
        store.put(esc)
        got = store.get("esc_1")
        assert got is not None
        assert got.id == "esc_1"
        assert got.paged == ()

    def test_put_upserts(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresEscalationStore

        store = PostgresEscalationStore(pool)
        esc = self._esc(paged=("dr_a",))
        store.put(esc)
        esc2 = self._esc(level=1, paged=("dr_a", "dr_b"))
        store.put(esc2)
        got = store.get("esc_1")
        assert got is not None
        assert got.level == 1
        assert got.paged == ("dr_a", "dr_b")

    def test_due(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresEscalationStore

        store = PostgresEscalationStore(pool)
        now = datetime.now(UTC)
        store.put(self._esc(id="e1", deadline=now - timedelta(minutes=1)))
        store.put(self._esc(id="e2", deadline=now + timedelta(minutes=10)))
        store.put(self._esc(id="e3", deadline=now - timedelta(minutes=5), status="acknowledged"))
        due = store.due(now)
        assert len(due) == 1
        assert due[0].id == "e1"

    def test_for_message(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_stores import PostgresEscalationStore

        store = PostgresEscalationStore(pool)
        store.put(self._esc(id="e1", message_id="msg_x"))
        assert store.for_message("msg_x") is not None
        assert store.for_message("msg_y") is None


# ---------------------------------------------------------------------------
# OutboxWriter
# ---------------------------------------------------------------------------


class TestOutboxWriter:
    @pytest.mark.asyncio
    async def test_publish_inserts_row(self, pool: ConnectionPool) -> None:
        from kcd_core.adapters.pg_outbox import OutboxWriter
        from kcd_core.contracts import MessageType, new_envelope

        writer = OutboxWriter(pool)
        env = new_envelope(
            type_=MessageType.ACKNOWLEDGMENT,
            sender="dispatcher",
            to="companion",
            patient_id="pt_1",
            episode_id="ep_1",
            payload={"status": "ok", "ref": "123"},
            manifest_version="dispatcher@v1",
            now=datetime.now(UTC),
        )
        await writer.publish("a2a.seat.companion", env)

        with pool.connection() as conn:
            row = conn.execute("SELECT subject, payload FROM outbox WHERE id = %s", (str(env.id),)).fetchone()
        assert row is not None
        assert row[0] == "a2a.seat.companion"
        # payload round-trips through JSONB
        payload_dict = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        assert payload_dict["type"] == "acknowledgment"
