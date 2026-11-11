"""Postgres implementations of the Core ports: idempotency, audit, escalations, on-call rota, gate queue.

The ports are synchronous (seat code and the dispatcher call them inline), so these use a sync
``psycopg_pool.ConnectionPool`` built by ``make_pool`` (statement + connect timeouts, UTC session). Calls are
short single statements; a stuck database fails the call within seconds instead of freezing the event loop.
Anything that runs in the background (outbox relay, escalation worker) moves its DB calls off the loop with
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg.errors
from psycopg import Connection
from psycopg_pool import ConnectionPool

from ..audit import GENESIS, AuditEvent, content_hash
from ..contracts.envelope import Envelope
from ..contracts.messages import CasePackReady
from ..escalation import Escalation, EscalationConflict
from ._pg import clean_json

AUDIT_LOCK_KEY = 0x4B434401  # advisory lock that serialises appends to the audit chain

# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class PostgresIdempotencyStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def has(self, key: str) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("SELECT 1 FROM idempotency WHERE key = %s", (key,)).fetchone() is not None

    def mark(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO idempotency (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))

    def prune(self, older_than: timedelta) -> int:
        """Drop keys older than the longest possible redelivery window (keep this well above max bus retry time)."""
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM idempotency WHERE marked_at < now() - %s", (older_than,))
            return cur.rowcount


# ---------------------------------------------------------------------------
# Audit (hash chain)
# ---------------------------------------------------------------------------

_AUDIT_COLS = (
    "id::text, ts, actor, manifest_version, action, decision, patient_id, correlation_id, "
    "inputs_hash, outputs_hash, detail, prev_hash, hash"
)


def _audit_event(row: tuple[Any, ...]) -> AuditEvent:
    detail = row[10] if isinstance(row[10], dict) else json.loads(row[10])
    return AuditEvent(
        id=row[0], ts=row[1].astimezone(UTC), actor=row[2], manifest_version=row[3], action=row[4],
        decision=row[5], patient_id=row[6], correlation_id=row[7], inputs_hash=row[8], outputs_hash=row[9],
        detail=detail, prev_hash=row[11], hash=row[12],
    )


class PostgresAuditSink:
    """Append-only, hash-chained. The chain is extended under an advisory lock so concurrent writers queue
    instead of forking it; the table itself rejects UPDATE / DELETE / TRUNCATE (migration 002)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record(
        self,
        *,
        ts: datetime,
        actor: str,
        action: str,
        decision: Literal["allowed", "blocked", "info", "error"],
        manifest_version: str | None = None,
        patient_id: str | None = None,
        correlation_id: str | None = None,
        inputs: Any = None,
        outputs: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        if ts.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        # Normalise before hashing: the hash covers the serialised timestamp, and the database hands back UTC.
        ts = ts.astimezone(UTC)
        with self._pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK_KEY,))
            row = conn.execute("SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1").fetchone()
            body: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "ts": ts,
                "actor": actor,
                "manifest_version": manifest_version,
                "action": action,
                "decision": decision,
                "patient_id": patient_id,
                "correlation_id": correlation_id,
                "inputs_hash": content_hash(inputs) if inputs is not None else None,
                "outputs_hash": content_hash(outputs) if outputs is not None else None,
                "detail": clean_json(detail or {}),
                "prev_hash": row[0] if row else GENESIS,
            }
            draft = AuditEvent(**body, hash=GENESIS)
            event = draft.model_copy(update={"hash": content_hash(draft.model_dump(mode="json", exclude={"hash"}))})
            conn.execute(
                "INSERT INTO audit_event (id, ts, actor, manifest_version, action, decision, patient_id, "
                "correlation_id, inputs_hash, outputs_hash, detail, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (event.id, event.ts, event.actor, event.manifest_version, event.action, event.decision,
                 event.patient_id, event.correlation_id, event.inputs_hash, event.outputs_hash,
                 json.dumps(event.detail), event.prev_hash, event.hash),
            )
        return event

    def verify_chain(self) -> bool:
        """Re-check every link. Streams through a server-side cursor, so memory stays flat on a big table."""
        prev = GENESIS
        with self._pool.connection() as conn, conn.cursor(name="audit_verify") as cur:
            cur.itersize = 500
            cur.execute(f"SELECT {_AUDIT_COLS} FROM audit_event ORDER BY seq")  # noqa: S608 - constant columns
            for row in cur:
                event = _audit_event(row)
                body = event.model_dump(mode="json", exclude={"hash"})
                if event.prev_hash != prev or content_hash(body) != event.hash:
                    return False
                prev = event.hash
        return True

    def find(self, action: str, decision: str | None = None) -> list[AuditEvent]:
        sql = f"SELECT {_AUDIT_COLS} FROM audit_event WHERE action = %s"  # noqa: S608
        params: tuple[Any, ...] = (action,)
        if decision is not None:
            sql += " AND decision = %s"
            params = (action, decision)
        with self._pool.connection() as conn:
            return [_audit_event(r) for r in conn.execute(sql + " ORDER BY seq", params).fetchall()]


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------

_ESC_COLS = "id, message_id, patient_id, episode_id, unit, level, deadline, status, paged, acknowledged_by"


def _escalation(row: tuple[Any, ...]) -> Escalation:
    paged = row[8] if isinstance(row[8], list) else json.loads(row[8])
    return Escalation(
        id=row[0], message_id=row[1], patient_id=row[2], episode_id=row[3], unit=row[4], level=row[5],
        deadline=row[6].astimezone(UTC), status=row[7], paged=tuple(paged), acknowledged_by=row[9],
    )


class PostgresEscalationStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def put(self, esc: Escalation) -> bool:
        """Upsert, but only while the stored row is still 'open' (compare-and-set); see EscalationStore.put."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    f"INSERT INTO escalation ({_ESC_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s) "  # noqa: S608
                    "ON CONFLICT (id) DO UPDATE SET level = EXCLUDED.level, deadline = EXCLUDED.deadline, "
                    "status = EXCLUDED.status, paged = EXCLUDED.paged, acknowledged_by = EXCLUDED.acknowledged_by, "
                    "lease_until = NULL WHERE escalation.status = 'open' RETURNING 1",
                    (esc.id, esc.message_id, esc.patient_id, esc.episode_id, esc.unit, esc.level, esc.deadline,
                     esc.status, json.dumps(list(esc.paged)), esc.acknowledged_by),
                ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "escalation_message_key":
                raise EscalationConflict(esc.message_id) from exc
            raise
        return row is not None

    def get(self, esc_id: str) -> Escalation | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_ESC_COLS} FROM escalation WHERE id = %s", (esc_id,)).fetchone()  # noqa: S608
        return _escalation(row) if row else None

    def due(self, now: datetime) -> list[Escalation]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ESC_COLS} FROM escalation WHERE status = 'open' AND deadline <= %s ORDER BY deadline",  # noqa: S608
                (now,),
            ).fetchall()
        return [_escalation(r) for r in rows]

    def claim_due(self, now: datetime, *, limit: int, lease: timedelta) -> list[Escalation]:
        """SELECT ... FOR UPDATE SKIP LOCKED picks the rows, the lease keeps them ours while we page (we never
        hold a transaction open across the pager call). A crashed worker's lease simply expires."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"UPDATE escalation e SET lease_until = %s FROM ("  # noqa: S608
                "  SELECT id FROM escalation WHERE status = 'open' AND deadline <= %s "
                "  AND (lease_until IS NULL OR lease_until <= %s) "
                "  ORDER BY deadline LIMIT %s FOR UPDATE SKIP LOCKED"
                ") due WHERE e.id = due.id "
                f"RETURNING {', '.join('e.' + c.strip() for c in _ESC_COLS.split(','))}",
                (now + lease, now, now, limit),
            ).fetchall()
        return sorted((_escalation(r) for r in rows), key=lambda e: e.deadline)

    def for_message(self, message_id: str) -> Escalation | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_ESC_COLS} FROM escalation WHERE message_id = %s", (message_id,)  # noqa: S608
            ).fetchone()
        return _escalation(row) if row else None


# ---------------------------------------------------------------------------
# On-call rota (the escalation ladder's source of truth; the `oncall` MCP server reads the same table)
# ---------------------------------------------------------------------------


class PostgresOnCallRota:
    """Implements ``OnCallRota``. If shifts overlap, the one that started last wins (deterministic)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def who(self, unit: str, role: str, at: datetime) -> str | None:
        import asyncio

        return await asyncio.to_thread(self._who, unit, role, at)

    def _who(self, unit: str, role: str, at: datetime) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT clinician_id FROM oncall_shift WHERE unit = %s AND role = %s "
                "AND starts_at <= %s AND ends_at > %s ORDER BY starts_at DESC LIMIT 1",
                (unit, role, at, at),
            ).fetchone()
        return str(row[0]) if row else None

    def add_shift(self, unit: str, role: str, clinician_id: str, starts_at: datetime, ends_at: datetime) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO oncall_shift (unit, role, clinician_id, starts_at, ends_at) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (unit, role, clinician_id, starts_at, ends_at),
            ).fetchone()
        assert row is not None
        return int(row[0])


# ---------------------------------------------------------------------------
# Gate queue (durable stand-in for the gate service until Person 5's lands; same GatePort)
# ---------------------------------------------------------------------------


class PostgresGateQueue:
    """Implements ``GatePort``. Idempotent on (case_id, version): a bus retry cannot open a gate twice."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def open_gate(self, env: Envelope, pack: CasePackReady) -> None:
        import asyncio

        await asyncio.to_thread(self._open, env, pack)

    def _open(self, env: Envelope, pack: CasePackReady) -> None:
        body = clean_json(pack.model_dump(mode="json", by_alias=True))
        with self._pool.connection() as conn:
            _open_gate(conn, env, pack.case_id, pack.version, body)

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT case_id, version, patient_id, episode_id, pack FROM gate_item "
                "WHERE status = 'pending_review' ORDER BY opened_at LIMIT %s", (limit,),
            ).fetchall()
        return [{"case_id": r[0], "version": r[1], "patient_id": r[2], "episode_id": r[3], "pack": r[4]} for r in rows]


def _open_gate(conn: Connection[Any], env: Envelope, case_id: str, version: int, body: Any) -> None:
    conn.execute(
        "INSERT INTO gate_item (case_id, version, message_id, patient_id, episode_id, pack) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (case_id, version) DO NOTHING",
        (case_id, version, str(env.id), env.patient_id, env.episode_id, json.dumps(body)),
    )
