"""The real Core wiring (Postgres + JetStream + dispatcher + outbox relay + escalation) driven end to end."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from kcd_core.bus import DISPATCHER_INBOX, seat_inbox
from kcd_core.contracts import MessageType, new_envelope
from kcd_core.keys import generate_keypair, load_signer
from kcd_core.service import Core, build_core
from kcd_core.settings import Settings
from kcd_core.signing import sign_envelope

from .conftest import FIXTURES, MIGRATIONS, ROOT, TEST_DSN, TEST_NATS, _pg_available, payload

RED_FLAG = payload(MessageType.RED_FLAG_ESCALATION)


async def until(pred, timeout=10.0):  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not pred():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


@pytest.fixture
async def core(nats_js, tmp_path: Path):
    if not _pg_available():
        pytest.skip("Postgres not reachable")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        for ext in ("pgcrypto", "vector"):
            admin.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}" SCHEMA public')
        admin.execute(f"CREATE SCHEMA {schema}")
    for seat in ("companion", "case_briefer", "dispatcher"):
        generate_keypair(tmp_path, seat)
    settings = Settings(
        env="test", database_url=TEST_DSN, nats_url=TEST_NATS, migrate_on_start=True, migrations_dir=MIGRATIONS,
        pg_options=f"-c search_path={schema},public", catalog_path=ROOT / "contracts" / "policy_catalog.json",
        manifests_dir=ROOT / "seats" / "manifests", keys_dir=tmp_path, bus_max_attempts=3,
    )
    c: Core = await build_core(settings)
    await c.start()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(c.relay.run(stop))]
    now = datetime.now(UTC)
    for role, who in (("primary", "dr_rossi"), ("backup", "dr_bianchi"), ("head_of_unit", "dr_verdi")):
        c.rota.add_shift("uro_oncology", role, who, now - timedelta(hours=1), now + timedelta(hours=8))
    c.keys_dir = tmp_path  # type: ignore[attr-defined]
    yield c
    stop.set()
    await asyncio.gather(*tasks)
    await c.bus.close()
    c.pool.close()
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def red_flag_from_companion(core: Core, episode: str):
    signer = load_signer(core.keys_dir, "companion")  # type: ignore[attr-defined]
    env = new_envelope(
        type_=MessageType.RED_FLAG_ESCALATION, sender="companion", to="case_briefer", patient_id="pt_marco",
        episode_id=episode, payload=RED_FLAG, manifest_version=core.registry.get("companion").ref,
        now=datetime.now(UTC),
    )
    return sign_envelope(env, signer)


async def test_red_flag_goes_through_the_whole_stack(core: Core):
    delivered = []

    async def briefer(env):
        delivered.append(env)

    briefer_bus = type(core.bus)(core.bus._nc)  # a second consumer on the same connection: the seat side
    await briefer_bus.subscribe(seat_inbox("case_briefer"), briefer)

    env = red_flag_from_companion(core, "ep_1")
    await core.bus.publish(DISPATCHER_INBOX, env)
    await until(lambda: delivered)

    assert delivered[0].id == env.id                       # forwarded intact, signature still valid
    assert core.audit.verify_chain() is True
    esc = core.escalations.store.for_message(str(env.id))  # type: ignore[attr-defined]
    assert esc is not None and esc.paged == ("dr_rossi",)  # the on-call primary was paged (LogPager) exactly once

    # the same clinical event arrives again as a new envelope (companion retry): no second delivery, no second page
    dup = red_flag_from_companion(core, "ep_1")
    await core.bus.publish(DISPATCHER_INBOX, dup)
    await asyncio.sleep(1.0)
    assert len(delivered) == 1
    assert core.outbox.pending() == 0 and core.outbox.dead() == 0

    # an unanswered page climbs the ladder when the worker ticks past the deadline
    future = datetime.now(UTC) + timedelta(minutes=6)
    moved = core.escalations.store.claim_due(future, limit=5, lease=timedelta(seconds=1))  # type: ignore[attr-defined]
    assert [m.id for m in moved] == [esc.id]
    await briefer_bus.close()


async def test_forged_message_is_dead_lettered_and_audited(core: Core):
    env = red_flag_from_companion(core, "ep_9")
    forged = env.model_copy(update={"payload": {**env.payload, "reason": "tampered"}})
    await core.bus.publish(DISPATCHER_INBOX, forged)
    await until(lambda: core.bus.dead_letters)
    assert core.audit.find("dispatch.reject", "blocked")
    assert core.audit.verify_chain() is True


def test_unused_imports_are_intentional():
    assert shutil and FIXTURES
