"""Transactional outbox + relay on a real Postgres."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from kcd_core.adapters.pg_outbox import OutboxRelay, PostgresOutbox, msg_id_for
from kcd_core.contracts import Envelope, MessageType
from kcd_core.signing import verify_envelope

from .conftest import payload

SUBJECT = "a2a.seat.safety_verifier"
CONFLICT = payload(MessageType.THERAPY_CONFLICT)


class Publisher:
    """Fake bus publisher. `fail` lists subjects that raise."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail or set()
        self.lock = asyncio.Lock()

    async def publish_raw(self, subject: str, data: bytes, *, msg_id: str) -> None:
        if subject in self.fail:
            raise ConnectionError("bus down")
        async with self.lock:
            self.sent.append((subject, msg_id))


def env_n(desk, n: int, subject_to: str = "safety_verifier"):
    return desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", subject_to, CONFLICT, episode=f"ep_{n}")


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


async def test_route_once_claims_the_key_and_queues_exactly_one_row(pg, desk):
    outbox = PostgresOutbox(pg)
    env = env_n(desk, 1)
    assert await outbox.route_once(env.idempotency_key, SUBJECT, env) is True
    assert await outbox.route_once(env.idempotency_key, SUBJECT, env) is False  # redelivery
    assert outbox.pending() == 1
    with pg.connection() as conn:
        assert conn.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 1


async def test_failed_enqueue_does_not_leave_the_key_claimed(pg, desk):
    outbox = PostgresOutbox(pg)
    env = env_n(desk, 1)
    with pg.connection() as conn:  # a row already owns this message id, so the INSERT will violate the unique index
        conn.execute("INSERT INTO outbox (id, subject, payload, msg_id) VALUES (gen_random_uuid(), 'x', '{}', %s)",
                     (msg_id_for(env, SUBJECT),))
    with pytest.raises(psycopg.errors.UniqueViolation):
        await outbox.route_once(env.idempotency_key, SUBJECT, env)
    with pg.connection() as conn:  # the key insert was rolled back with it: the retry can still deliver
        assert conn.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0


async def test_relay_publishes_in_order_and_marks_sent(pg, desk):
    outbox, pub = PostgresOutbox(pg), Publisher()
    envs = [env_n(desk, n) for n in range(5)]
    for e in envs:
        await outbox.route_once(e.idempotency_key, SUBJECT, e)
    relay = OutboxRelay(pg, pub)
    assert await relay.relay_once() == 5
    assert [m for _, m in pub.sent] == [msg_id_for(e, SUBJECT) for e in envs]
    assert outbox.pending() == 0
    assert await relay.relay_once() == 0


async def test_a_failing_row_does_not_block_the_rows_behind_it(pg, desk):
    # Head-of-line blocking would hold a red flag hostage to one bad row; each row backs off on its own instead.
    outbox, clock = PostgresOutbox(pg), Clock()
    pub = Publisher(fail={"a2a.seat.imaging_reader"})
    good1, bad, good2 = env_n(desk, 1), env_n(desk, 2), env_n(desk, 3)
    await outbox.route_once(good1.idempotency_key, SUBJECT, good1)
    await outbox.route_once(bad.idempotency_key, "a2a.seat.imaging_reader", bad)
    await outbox.route_once(good2.idempotency_key, SUBJECT, good2)
    relay = OutboxRelay(pg, pub, now=clock)

    assert await relay.relay_once() == 2
    assert {m for _, m in pub.sent} == {msg_id_for(good1, SUBJECT), msg_id_for(good2, SUBJECT)}
    assert outbox.pending() == 1

    pub.fail.clear()
    assert await relay.relay_once() == 0  # still inside its own backoff
    clock.t += timedelta(seconds=10)
    assert await relay.relay_once() == 1
    assert pub.sent[-1][1] == msg_id_for(bad, "a2a.seat.imaging_reader")
    assert outbox.pending() == 0


async def test_a_dead_bus_stops_the_batch_after_a_few_failures(pg, desk):
    outbox, clock = PostgresOutbox(pg), Clock()
    attempts: list[str] = []

    class Down:
        async def publish_raw(self, subject: str, data: bytes, *, msg_id: str) -> None:
            attempts.append(msg_id)
            raise ConnectionError("bus down")

    for n in range(10):
        e = env_n(desk, n)
        await outbox.route_once(e.idempotency_key, SUBJECT, e)
    relay = OutboxRelay(pg, Down(), now=clock, max_consecutive_failures=3)
    assert await relay.relay_once() == 0
    assert len(attempts) == 3  # bounded stall: three publish timeouts, not ten
    assert outbox.pending() == 10
    # once the bus is back, the untouched rows go out immediately (they were released, not backed off)
    pub = Publisher()
    assert await OutboxRelay(pg, pub, now=clock).relay_once() == 7


async def test_a_poison_row_is_parked_and_does_not_block_the_queue(pg, desk):
    outbox, clock = PostgresOutbox(pg), Clock()
    pub = Publisher(fail={"a2a.seat.imaging_reader"})
    bad, good = env_n(desk, 1), env_n(desk, 2)
    await outbox.route_once(bad.idempotency_key, "a2a.seat.imaging_reader", bad)
    await outbox.route_once(good.idempotency_key, SUBJECT, good)
    relay = OutboxRelay(pg, pub, now=clock, max_attempts=3)
    for _ in range(3):
        await relay.relay_once()
        clock.t += timedelta(minutes=5)
    assert outbox.dead() == 1
    await relay.relay_once()
    assert [m for _, m in pub.sent] == [msg_id_for(good, SUBJECT)]


async def test_two_relays_never_publish_the_same_row_twice(pg, desk):
    outbox, pub = PostgresOutbox(pg), Publisher()
    for n in range(30):
        e = env_n(desk, n)
        await outbox.route_once(e.idempotency_key, SUBJECT, e)
    relays = [OutboxRelay(pg, pub, batch_size=7) for _ in range(3)]
    for _ in range(6):
        await asyncio.gather(*(r.relay_once() for r in relays))
    ids = [m for _, m in pub.sent]
    assert len(ids) == 30 and len(set(ids)) == 30


async def test_a_crashed_relays_rows_are_picked_up_after_the_lease(pg, desk):
    outbox, clock, pub = PostgresOutbox(pg), Clock(), Publisher()
    e = env_n(desk, 1)
    await outbox.route_once(e.idempotency_key, SUBJECT, e)
    crashed = OutboxRelay(pg, pub, now=clock, lease_s=30)
    assert len(await asyncio.to_thread(crashed._claim)) == 1  # claimed, then the process died
    survivor = OutboxRelay(pg, pub, now=clock, lease_s=30)
    assert await survivor.relay_once() == 0
    clock.t += timedelta(seconds=31)
    assert await survivor.relay_once() == 1


async def test_signature_survives_the_outbox_round_trip(pg, desk):
    # A float and a NUL in the payload: the exact bytes that were signed must still verify after storage.
    forged = {**CONFLICT}
    env = env_n(desk, 1)
    from kcd_core.signing import sign_envelope

    env = env.model_copy(update={"payload": {**env.payload, "note": "a\x00b", "score": 0.1 + 0.2}, "sig": None})
    env = sign_envelope(env, desk.signers["renal_guardian"])
    outbox = PostgresOutbox(pg)
    assert await outbox.route_once(env.idempotency_key, SUBJECT, env) is True
    with pg.connection() as conn:
        body = conn.execute("SELECT payload FROM outbox").fetchone()[0]
    verify_envelope(Envelope.model_validate_json(body), desk.seat_keys)
    assert forged == CONFLICT
