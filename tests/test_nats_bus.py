"""NatsJetStreamBus against a real JetStream server (skipped when NATS is unreachable)."""

from __future__ import annotations

import asyncio

from kcd_core.adapters.nats_bus import NatsJetStreamBus
from kcd_core.bus import DISPATCHER_INBOX, PermanentReject, dead_letter, seat_inbox
from kcd_core.contracts import Envelope, MessageType
from kcd_core.signing import verify_envelope

from .conftest import payload

CONFLICT = payload(MessageType.THERAPY_CONFLICT)
INBOX = seat_inbox("safety_verifier")


def make(nc, **kw):
    kw.setdefault("base_backoff_s", 0.05)
    return NatsJetStreamBus(nc, **kw)


async def until(pred, timeout=8.0):  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not pred():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def env_for(desk, n=0):
    return desk.envelope(MessageType.THERAPY_CONFLICT, "renal_guardian", "safety_verifier", CONFLICT, episode=f"ep_{n}")


async def dlq_messages(nc, subject):
    psub = await nc.jetstream().pull_subscribe(dead_letter(subject), stream="KCD_DLQ")
    try:
        return await psub.fetch(10, timeout=1)
    except Exception:  # noqa: BLE001
        return []


async def test_delivers_and_the_signature_survives_the_wire(nats_js, desk):
    bus, got = make(nats_js), []

    async def handler(env: Envelope) -> None:
        got.append(env)

    await bus.subscribe(INBOX, handler)
    sent = env_for(desk)
    await bus.publish(INBOX, sent)
    await until(lambda: got)
    assert got[0] == sent
    verify_envelope(got[0], desk.seat_keys)
    await bus.close()


async def test_retries_with_backoff_then_succeeds(nats_js, desk):
    bus, attempts = make(nats_js), []

    async def flaky(env: Envelope) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("db hiccup")

    await bus.subscribe(INBOX, flaky)
    await bus.publish(INBOX, env_for(desk))
    await until(lambda: len(attempts) == 3)
    await asyncio.sleep(0.3)
    assert len(attempts) == 3 and not bus.dead_letters
    await bus.close()


async def test_exhausted_retries_land_in_the_durable_dlq(nats_js, desk):
    bus = make(nats_js, max_attempts=3)

    async def always(env: Envelope) -> None:
        raise RuntimeError("boom")

    await bus.subscribe(INBOX, always)
    await bus.publish(INBOX, env_for(desk))
    await until(lambda: bus.dead_letters)
    assert bus.dead_letters[0].attempts == 3
    await bus.close()  # a restart loses the in-memory list, not the stream
    msgs = await dlq_messages(nats_js, INBOX)
    assert len(msgs) == 1
    assert msgs[0].headers["KCD-Attempts"] == "3" and "boom" in msgs[0].headers["KCD-Error"]
    assert msgs[0].headers["KCD-Original-Subject"] == INBOX
    assert Envelope.model_validate_json(msgs[0].data).id is not None  # replayable


async def test_permanent_reject_is_dead_lettered_on_the_first_attempt(nats_js, desk):
    bus, calls = make(nats_js), []

    async def reject(env: Envelope) -> None:
        calls.append(1)
        raise PermanentReject("bad signature")

    await bus.subscribe(INBOX, reject)
    await bus.publish(INBOX, env_for(desk))
    await until(lambda: bus.dead_letters)
    assert calls == [1] and bus.dead_letters[0].error == "rejected: bad signature"
    await bus.close()


async def test_garbage_on_the_wire_is_parked_not_lost(nats_js, desk):
    bus = make(nats_js)

    async def never(env: Envelope) -> None:
        raise AssertionError("must not be called")

    await bus.subscribe(INBOX, never)
    await nats_js.jetstream().publish(INBOX, b"{not json")
    for _ in range(50):
        if msgs := await dlq_messages(nats_js, INBOX):
            break
    assert msgs and msgs[0].data == b"{not json" and "undecodable" in msgs[0].headers["KCD-Error"]
    await bus.close()


async def test_unfinished_message_is_redelivered_after_a_crash(nats_js, desk):
    first, second, started, got = make(nats_js, ack_wait_s=1.0), make(nats_js, ack_wait_s=1.0), asyncio.Event(), []

    async def hangs(env: Envelope) -> None:
        started.set()
        await asyncio.Event().wait()

    async def ok(env: Envelope) -> None:
        got.append(env)

    await first.subscribe(INBOX, hangs)
    await first.publish(INBOX, env_for(desk))
    await asyncio.wait_for(started.wait(), 5)
    await first.close()  # the worker dies mid-handler: no ack, no nak
    await second.subscribe(INBOX, ok)
    await until(lambda: got, 10)
    await second.close()


async def test_a_message_that_keeps_crashing_workers_is_dead_lettered(nats_js, desk):
    started = asyncio.Event()

    async def hangs(env: Envelope) -> None:
        started.set()
        await asyncio.Event().wait()

    for i in range(2):  # two workers die on it (max_attempts=2)
        bus = make(nats_js, max_attempts=2, ack_wait_s=1.0)
        started.clear()
        await bus.subscribe(INBOX, hangs)
        if i == 0:
            await bus.publish(INBOX, env_for(desk))
        await asyncio.wait_for(started.wait(), 6)
        await bus.close()

    calls = []

    async def must_not_run(env: Envelope) -> None:
        calls.append(1)

    last = make(nats_js, max_attempts=2, ack_wait_s=1.0)
    await last.subscribe(INBOX, must_not_run)
    await until(lambda: last.dead_letters, 10)
    assert calls == [] and "crashed" in last.dead_letters[0].error
    await last.close()


async def test_two_replicas_share_the_work_exactly_once(nats_js, desk):
    a, b, seen = make(nats_js), make(nats_js), []

    async def handler(env: Envelope) -> None:
        seen.append(env.id)
        await asyncio.sleep(0.01)

    await a.subscribe(INBOX, handler)
    await b.subscribe(INBOX, handler)
    for n in range(20):
        await a.publish(INBOX, env_for(desk, n))
    await until(lambda: len(seen) >= 20)
    await asyncio.sleep(0.3)
    assert len(seen) == 20 and len(set(seen)) == 20
    await a.close()
    await b.close()


async def test_same_envelope_on_two_hops_is_not_mistaken_for_a_duplicate(nats_js, desk):
    # seat -> dispatcher -> seat carries the same envelope id. JetStream dedupes on message id across the stream,
    # so the id must include the subject, otherwise the second hop is silently dropped.
    bus, hops = make(nats_js), []

    async def record(env: Envelope) -> None:
        hops.append(1)

    await bus.subscribe(DISPATCHER_INBOX, record)
    await bus.subscribe(INBOX, record)
    env = env_for(desk)
    await bus.publish(DISPATCHER_INBOX, env)
    await bus.publish(INBOX, env)
    await until(lambda: len(hops) == 2)
    await bus.publish(INBOX, env)  # the genuine duplicate of the same hop is dropped by the server
    await asyncio.sleep(0.4)
    assert len(hops) == 2
    await bus.close()
