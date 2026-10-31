"""A whole Kidney Care Desk in memory, built from the repo's catalogue and seed manifests.

Used by this slice's tests and by `python -m kcd.demo`. It wires kcd_core exactly like the Core
test fixture (tests/conftest.py at the repo root): registry, Ed25519 keys for every seat and one
clinician, in-memory bus, Dispatcher with escalation, and a gate port that records opened gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kcd_core.audit import InMemoryAudit
from kcd_core.bus import DISPATCHER_INBOX, InMemoryBus, seat_inbox
from kcd_core.clock import FakeClock
from kcd_core.contracts import Envelope, MessageType, PolicyCatalog, Release, new_envelope
from kcd_core.contracts.messages import CasePackReady
from kcd_core.dispatcher import Dispatcher
from kcd_core.escalation import Escalation, EscalationService, InMemoryEscalationStore
from kcd_core.registry import ManifestRegistry
from kcd_core.runtime import Seat, SeatRuntime, ToolRegistry
from kcd_core.signing import KeyRing, Signer, sign_envelope, sign_release

REPO = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO / "contracts" / "policy_catalog.json"
MANIFEST_DIR = REPO / "seats" / "manifests"
CLINICIAN_ID = "dr_rossi"


class _Rota:
    async def who(self, unit: str, role: str, at: datetime) -> str | None:
        return {"primary": "dr_rossi", "backup": "dr_bianchi", "head_of_unit": "dr_verdi"}.get(role)


@dataclass
class _Pager:
    pages: list[tuple[str, str]] = field(default_factory=list)

    async def page(self, target: str, escalation: Escalation) -> None:
        self.pages.append((target, escalation.id))


@dataclass
class RecordingGates:
    """Gate port: records the review items the Dispatcher opens (Clinical Board stand-in)."""

    opened: list[tuple[Envelope, CasePackReady]] = field(default_factory=list)

    async def open_gate(self, env: Envelope, pack: CasePackReady) -> None:
        self.opened.append((env, pack))


@dataclass
class Desk:
    catalog: PolicyCatalog
    registry: ManifestRegistry
    clock: FakeClock
    audit: InMemoryAudit
    bus: InMemoryBus
    signers: dict[str, Signer]
    seat_keys: KeyRing
    clinician: Signer
    clinician_keys: KeyRing
    gates: RecordingGates
    dispatcher: Dispatcher
    runtimes: dict[str, SeatRuntime] = field(default_factory=dict)

    @classmethod
    async def create(cls) -> Desk:
        catalog = PolicyCatalog.load(CATALOG_PATH)
        registry = ManifestRegistry.from_dir(catalog, MANIFEST_DIR)
        clock, audit, bus = FakeClock(), InMemoryAudit(), InMemoryBus(max_attempts=3)
        signers = {s: Signer.generate(s) for s in registry.seats()}
        seat_keys = KeyRing()
        for s, signer in signers.items():
            seat_keys.add(s, signer.public_key)
        clinician = Signer.generate(CLINICIAN_ID)
        clinician_keys = KeyRing({CLINICIAN_ID: clinician.public_key})
        gates = RecordingGates()
        escalations = EscalationService(rota=_Rota(), pager=_Pager(), store=InMemoryEscalationStore(),
                                        clock=clock, audit=audit)
        dispatcher = Dispatcher(bus=bus, registry=registry, seat_keys=seat_keys, audit=audit, clock=clock,
                                escalations=escalations, gates=gates)
        await dispatcher.start()
        return cls(catalog, registry, clock, audit, bus, signers, seat_keys, clinician, clinician_keys, gates,
                   dispatcher)

    async def add_seat(self, seat: Seat, tools: dict[str, Any] | None = None, timeout_s: float = 10.0) -> SeatRuntime:
        rt = SeatRuntime(seat, registry=self.registry, catalog=self.catalog, signer=self.signers[seat.seat_id],
                         seat_keys=self.seat_keys, clinician_keys=self.clinician_keys, bus=self.bus, audit=self.audit,
                         clock=self.clock, tools=ToolRegistry(tools), timeout_s=timeout_s)
        await rt.start()
        self.runtimes[seat.seat_id] = rt
        return rt

    def envelope(self, type_: MessageType, sender: str, to: str, payload: Any, *, patient: str = "pt_marco",
                 episode: str = "ep_1") -> Envelope:
        env = new_envelope(type_=type_, sender=sender, to=to, patient_id=patient, episode_id=episode, payload=payload,
                           manifest_version=self.registry.get(sender).ref, now=self.clock.now())
        return sign_envelope(env, self.signers[sender])

    async def deliver(self, env: Envelope) -> None:
        """Send through the Dispatcher, like a seat would, and wait until everything settles."""
        await self.bus.publish(DISPATCHER_INBOX, env)
        await self.bus.drain()

    def release(self, artifact_id: str, artifact_hash: str, role: str = "clinician:nephrology",
                classes: list[str] | None = None, release_id: str = "rel_1") -> Release:
        r = Release(id=release_id, artifact_id=artifact_id, artifact_hash=artifact_hash, clinician_id=CLINICIAN_ID,
                    role=role, content_classes=classes or [], signed_at=self.clock.now())
        return sign_release(r, self.clinician)

    def delivered_to(self, seat: str) -> list[Envelope]:
        return [env for subject, env in self.bus.published if subject == seat_inbox(seat)]

    async def close(self) -> None:
        await self.bus.close()
