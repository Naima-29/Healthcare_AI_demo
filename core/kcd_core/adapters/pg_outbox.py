"""Transactional outbox: the Dispatcher's "mark processed" and "send onward" commit together.

Why it exists. Dispatching a message is: dedupe -> hand it to the receiver's inbox -> remember we did. Doing
those as separate steps leaves a crash window: publish succeeded, process died before remembering, the bus
redelivers, the receiver sees the message twice. Here the idempotency key and the outbox row are one Postgres
transaction (`PostgresOutbox.route_once`). A relay then publishes the rows to JetStream with a message id, so
a relay crash between publish and bookkeeping is also absorbed (JetStream drops the repeat).

Seats publish straight to JetStream (a JetStream ack means durably stored); they have no database transaction
to be atomic with, so they do not use the outbox.

Delivery is at-least-once and NOT globally ordered, on purpose: a row that keeps failing backs off on its own and
later rows go past it. Strict order would let one bad row hold a red-flag escalation hostage (head-of-line
blocking). Nothing in the protocol depends on order: receivers are idempotent and the bus itself reorders on retry.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg_pool import ConnectionPool

from ..contracts import Envelope

log = logging.getLogger(__name__)


class RawPublisher(Protocol):
    async def publish_raw(self, subject: str, data: bytes, *, msg_id: str) -> None: ...


def msg_id_for(env: Envelope, subject: str) -> str:
    """JetStream deduplicates on message id *per stream*, across subjects. The same envelope legitimately travels
    seat -> dispatcher and dispatcher -> seat, so the subject is part of the id (otherwise the second hop would be
    silently dropped as a duplicate)."""
    return f"{env.id}@{subject}"


class PostgresOutbox:
    """Implements the Dispatcher's ``AtomicRouter``."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def route_once(self, key: str, subject: str, env: Envelope) -> bool:
        """Atomically: claim the idempotency key and queue the message. False if the key was already taken."""
        return await asyncio.to_thread(self._route_once, key, subject, env)

    def _route_once(self, key: str, subject: str, env: Envelope) -> bool:
        body = env.model_dump_json(by_alias=True)
        with self._pool.connection() as conn:  # one transaction: commits on exit, rolls back on error
            claimed = conn.execute(
                "INSERT INTO idempotency (key) VALUES (%s) ON CONFLICT DO NOTHING RETURNING 1", (key,)
            ).fetchone()
            if claimed is None:
                return False
            conn.execute(
                "INSERT INTO outbox (id, subject, payload, msg_id) VALUES (%s, %s, %s, %s)",
                (uuid.uuid4(), subject, body, msg_id_for(env, subject)),
            )
        return True

    def pending(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL AND dead_at IS NULL").fetchone()
        return int(row[0]) if row else 0

    def dead(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT count(*) FROM outbox WHERE dead_at IS NOT NULL").fetchone()
        return int(row[0]) if row else 0

    def prune_sent(self, older_than: timedelta) -> int:
        with self._pool.connection() as conn:
            return conn.execute("DELETE FROM outbox WHERE sent_at < now() - %s", (older_than,)).rowcount


@dataclass(frozen=True)
class _Row:
    seq: int
    subject: str
    body: str
    msg_id: str
    attempts: int


class OutboxRelay:
    """Publishes outbox rows to the bus, oldest first.

    No transaction is held open across the network call: rows are claimed with a lease (SELECT ... FOR UPDATE
    SKIP LOCKED inside one short UPDATE), published, then marked sent. A crashed relay's lease expires and
    another picks the rows up. A failing row backs off exponentially on its own; after `max_attempts` it is
    parked (`dead_at`) and surfaces in `PostgresOutbox.dead()` so it can be alerted on. If `max_consecutive_failures`
    rows fail in a row the bus is assumed down: the batch stops (bounded stall) and the rest is released.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        publisher: RawPublisher,
        *,
        batch_size: int = 100,
        poll_interval_s: float = 0.2,
        lease_s: float = 30.0,
        max_attempts: int = 20,
        max_consecutive_failures: int = 3,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pool, self._publisher = pool, publisher
        self._batch, self._poll, self._lease, self._max_attempts, self._now = (
            batch_size, poll_interval_s, lease_s, max_attempts, now,
        )
        self._max_consecutive = max_consecutive_failures

    async def relay_once(self) -> int:
        rows = await asyncio.to_thread(self._claim)
        sent: list[int] = []
        failures = 0
        for i, row in enumerate(rows):
            try:
                await self._publisher.publish_raw(row.subject, row.body.encode(), msg_id=row.msg_id)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.warning("outbox publish failed seq=%s subject=%s: %r", row.seq, row.subject, exc)
                rest = [r.seq for r in rows[i + 1:]] if failures >= self._max_consecutive else []
                await asyncio.to_thread(self._fail, row, repr(exc), rest)
                if rest:
                    break  # the bus looks down: stop burning publish timeouts, retry the rest next round
                continue
            failures = 0
            sent.append(row.seq)
        if sent:
            await asyncio.to_thread(self._mark_sent, sent)
        return len(sent)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                n = await self.relay_once()
            except Exception:
                log.exception("outbox relay iteration failed")
                n = 0
            if n == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
                except TimeoutError:
                    pass

    # -- sync DB steps (run in threads) ---------------------------------------

    def _claim(self) -> list[_Row]:
        now = self._now()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE outbox o SET claimed_until = %s, attempts = o.attempts + 1 FROM ("
                "  SELECT seq FROM outbox WHERE sent_at IS NULL AND dead_at IS NULL "
                "  AND (claimed_until IS NULL OR claimed_until <= %s) "
                "  ORDER BY seq LIMIT %s FOR UPDATE SKIP LOCKED"
                ") pick WHERE o.seq = pick.seq RETURNING o.seq, o.subject, o.payload, o.msg_id, o.attempts",
                (now + timedelta(seconds=self._lease), now, self._batch),
            ).fetchall()
        return sorted((_Row(*r) for r in rows), key=lambda r: r.seq)

    def _mark_sent(self, seqs: list[int]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outbox SET sent_at = %s, claimed_until = NULL WHERE seq = ANY(%s)", (self._now(), seqs)
            )

    def _fail(self, row: _Row, error: str, release: list[int]) -> None:
        now = self._now()
        with self._pool.connection() as conn:
            if row.attempts >= self._max_attempts:
                log.critical("outbox row seq=%s parked after %d attempts: %s", row.seq, row.attempts, error)
                conn.execute("UPDATE outbox SET dead_at = %s, last_error = %s WHERE seq = %s", (now, error, row.seq))
            else:
                backoff = timedelta(seconds=min(2 ** row.attempts, 60))
                conn.execute(
                    "UPDATE outbox SET claimed_until = %s, last_error = %s WHERE seq = %s",
                    (now + backoff, error, row.seq),
                )
            if release:
                conn.execute("UPDATE outbox SET claimed_until = NULL WHERE seq = ANY(%s)", (release,))
