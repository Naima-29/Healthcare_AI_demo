from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from kcd_core.audit import InMemoryAudit
from kcd_core.bus import InMemoryBus
from kcd_core.clock import FakeClock
from kcd_core.contracts import Envelope, MessageType, PolicyCatalog, Release, new_envelope
from kcd_core.contracts.messages import CasePackReady
from kcd_core.dispatcher import Dispatcher
from kcd_core.escalation import Escalation, EscalationService, InMemoryEscalationStore
from kcd_core.registry import ManifestRegistry
from kcd_core.runtime import Seat, SeatRuntime, ToolRegistry
from kcd_core.signing import KeyRing, Signer, sign_envelope, sign_release

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "contracts" / "policy_catalog.json"
MANIFEST_DIR = ROOT / "seats" / "manifests"
FIXTURES = ROOT / "contracts" / "fixtures"


def payload(type_: MessageType | str, index: int = 0) -> dict[str, Any]:
    """A valid example payload from contracts/fixtures (taken from the demo's golden path)."""
    import json

    name = type_.value if isinstance(type_, MessageType) else type_
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["valid"][index]
    return data


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class FakeRota:
    def __init__(self, table: dict[tuple[str, str], str | None] | None = None) -> None:
        self.table = table if table is not None else {
            ("uro_oncology", "primary"): "dr_rossi",
            ("uro_oncology", "backup"): "dr_bianchi",
            ("uro_oncology", "head_of_unit"): "dr_verdi",
        }

    async def who(self, unit: str, role: str, at: datetime) -> str | None:
        return self.table.get((unit, role))


@dataclass
class FakePager:
    pages: list[tuple[str, str]] = field(default_factory=list)

    async def page(self, target: str, escalation: Escalation) -> None:
        self.pages.append((target, escalation.id))


@dataclass
class FakeGates:
    opened: list[CasePackReady] = field(default_factory=list)

    async def open_gate(self, env: Envelope, pack: CasePackReady) -> None:
        self.opened.append(pack)


@dataclass
class Desk:
    """A whole desk in memory: registry, keys, bus, dispatcher. Seats are added per test."""

    catalog: PolicyCatalog
    registry: ManifestRegistry
    clock: FakeClock
    audit: InMemoryAudit
    bus: InMemoryBus
    signers: dict[str, Signer]
    seat_keys: KeyRing
    clinician: Signer
    clinician_keys: KeyRing
    pager: FakePager
    gates: FakeGates
    escalations: EscalationService
    dispatcher: Dispatcher
    runtimes: dict[str, SeatRuntime] = field(default_factory=dict)

    async def add_seat(self, seat: Seat, tools: dict[str, Any] | None = None, timeout_s: float = 5.0) -> SeatRuntime:
        rt = SeatRuntime(
            seat, registry=self.registry, catalog=self.catalog, signer=self.signers[seat.seat_id],
            seat_keys=self.seat_keys, clinician_keys=self.clinician_keys, bus=self.bus, audit=self.audit,
            clock=self.clock, tools=ToolRegistry(tools), timeout_s=timeout_s,
        )
        await rt.start()
        self.runtimes[seat.seat_id] = rt
        return rt

    def envelope(self, type_: MessageType, sender: str, to: str, payload: dict[str, Any],
                 *, sign: bool = True, episode: str = "ep_1") -> Envelope:
        env = new_envelope(
            type_=type_, sender=sender, to=to, patient_id="pt_marco", episode_id=episode, payload=payload,
            manifest_version=self.registry.get(sender).ref, now=self.clock.now(),
        )
        return sign_envelope(env, self.signers[sender]) if sign else env

    def release(self, artifact_hash: str, classes: list[str]) -> Release:
        r = Release(id="rel_1", artifact_id="cp_1", artifact_hash=artifact_hash, clinician_id=self.clinician.key_id,
                    role="clinician:nephrology", content_classes=classes, signed_at=self.clock.now())
        return sign_release(r, self.clinician)


@pytest.fixture
def catalog() -> PolicyCatalog:
    return PolicyCatalog.load(CATALOG_PATH)


@pytest.fixture
def registry(catalog: PolicyCatalog) -> ManifestRegistry:
    return ManifestRegistry.from_dir(catalog, MANIFEST_DIR)


@pytest_asyncio.fixture
async def desk(catalog: PolicyCatalog, registry: ManifestRegistry):  # type: ignore[no-untyped-def]
    clock, audit = FakeClock(), InMemoryAudit()
    bus = InMemoryBus(max_attempts=3)
    signers = {s: Signer.generate(s) for s in registry.seats()}
    seat_keys = KeyRing()
    for s, signer in signers.items():
        seat_keys.add(s, signer.public_key)
    clinician = Signer.generate("dr_rossi")
    clinician_keys = KeyRing({"dr_rossi": clinician.public_key})
    pager, gates = FakePager(), FakeGates()
    escalations = EscalationService(rota=FakeRota(), pager=pager, store=InMemoryEscalationStore(),
                                    clock=clock, audit=audit)
    dispatcher = Dispatcher(bus=bus, registry=registry, seat_keys=seat_keys, audit=audit, clock=clock,
                            escalations=escalations, gates=gates)
    await dispatcher.start()
    d = Desk(catalog, registry, clock, audit, bus, signers, seat_keys, clinician, clinician_keys, pager, gates,
             escalations, dispatcher)
    yield d
    await bus.close()


# ---------------------------------------------------------------------------
# Real infrastructure (Postgres, NATS). Skipped automatically when not reachable, so `pytest` still runs anywhere.
#   docker compose -f infra/docker-compose.yml up -d postgres nats
#   KCD_TEST_DSN / KCD_TEST_NATS_URL override the defaults. Tests never touch the dev `kcd` database: every test
#   gets its own throwaway schema with the real migrations applied.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import uuid  # noqa: E402

TEST_DSN = os.environ.get("KCD_TEST_DSN", "postgresql://kcd:kcd_dev@localhost:5432/kcd_test")
TEST_NATS = os.environ.get("KCD_TEST_NATS_URL", "nats://localhost:4222")
MIGRATIONS = ROOT / "infra" / "migrations"


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(TEST_DSN, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def pg():  # type: ignore[no-untyped-def]
    """A pool on a fresh schema with all migrations applied. Dropped afterwards."""
    if not _pg_available():
        pytest.skip(f"Postgres not reachable at {TEST_DSN}")
    import psycopg
    from kcd_core.adapters._pg import make_pool
    from kcd_core.migrate import apply_migrations

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        for ext in ("pgcrypto", "vector"):
            admin.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}" SCHEMA public')
        admin.execute(f"CREATE SCHEMA {schema}")
    options = f"-c search_path={schema},public"
    apply_migrations(TEST_DSN, MIGRATIONS, options=options)
    pool = make_pool(TEST_DSN, options=options, max_size=8, name="kcd-test")
    yield pool
    pool.close()
    with psycopg.connect(TEST_DSN, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest_asyncio.fixture
async def nats_js():  # type: ignore[no-untyped-def]
    """A clean JetStream: the KCD streams are deleted before and after each test."""
    import nats

    try:
        nc = await nats.connect(TEST_NATS, connect_timeout=1, max_reconnect_attempts=0)
    except Exception:  # noqa: BLE001
        pytest.skip(f"NATS not reachable at {TEST_NATS}")
    js = nc.jetstream()

    async def wipe() -> None:
        for name in ("KCD_A2A", "KCD_DLQ"):
            try:
                await js.delete_stream(name)
            except Exception:  # noqa: BLE001
                pass

    await wipe()
    yield nc
    await wipe()
    await nc.close()
