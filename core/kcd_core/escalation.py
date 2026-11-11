"""Escalation ladder for red flags: page on-call, and climb the ladder until someone acknowledges.

Durable in production: EscalationStore is a Postgres table and tick() runs in a worker (EscalationWorker) that
claims due rows with SELECT ... FOR UPDATE SKIP LOCKED plus a short lease, so several replicas never page the same
doctor twice and a crashed worker's rows are picked up again when the lease expires.

Safety properties, in order of importance:
- never miss a page: a failed page, an unknown rota or a crashed worker all end in "page the next level", never silence
- a duplicate page is acceptable, a missing one is not (at-least-once, like the bus)
- terminal states (acknowledged, exhausted) never move back: every write is compare-and-set on status = 'open'

Everything time-based goes through Clock, so tests run in milliseconds.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from .audit import AuditSink
from .clock import Clock
from .contracts import Envelope

log = logging.getLogger(__name__)

Status = Literal["open", "acknowledged", "exhausted"]


class EscalationConflict(Exception):
    """Another process already opened an escalation for this message (unique on message_id)."""


@dataclass(frozen=True)
class Escalation:
    id: str
    message_id: str
    patient_id: str
    episode_id: str
    unit: str
    level: int
    deadline: datetime
    status: Status
    paged: tuple[str, ...] = ()
    acknowledged_by: str | None = None


@dataclass(frozen=True)
class LadderStep:
    role: str  # rota role at this level, e.g. "primary", "backup", "head_of_unit"
    ack_window: timedelta


DEFAULT_LADDER: tuple[LadderStep, ...] = (
    LadderStep("primary", timedelta(minutes=5)),
    LadderStep("backup", timedelta(minutes=5)),
    LadderStep("head_of_unit", timedelta(minutes=10)),
)


class OnCallRota(Protocol):
    async def who(self, unit: str, role: str, at: datetime) -> str | None: ...


class Pager(Protocol):
    async def page(self, target: str, escalation: Escalation) -> None: ...


class EscalationStore(Protocol):
    def put(self, esc: Escalation) -> bool:
        """Insert, or update an escalation that is still open. Returns False if it had already left 'open'
        (compare-and-set: an acknowledgement is never overwritten by a late tick). Clears any lease.
        Raises EscalationConflict if another escalation exists for the same message."""
        ...

    def get(self, esc_id: str) -> Escalation | None: ...
    def due(self, now: datetime) -> list[Escalation]:
        """Read-only view of overdue escalations (monitoring, tests). Workers use claim_due."""
        ...

    def claim_due(self, now: datetime, *, limit: int, lease: timedelta) -> list[Escalation]:
        """Atomically take up to `limit` overdue escalations for `lease`. Two callers never get the same row."""
        ...

    def for_message(self, message_id: str) -> Escalation | None: ...


@dataclass
class InMemoryEscalationStore:
    rows: dict[str, Escalation] = field(default_factory=dict)
    leases: dict[str, datetime] = field(default_factory=dict)

    def put(self, esc: Escalation) -> bool:
        current = self.rows.get(esc.id)
        if current is not None and current.status != "open":
            return False
        other = self.for_message(esc.message_id)
        if other is not None and other.id != esc.id:
            raise EscalationConflict(esc.message_id)
        self.rows[esc.id] = esc
        self.leases.pop(esc.id, None)
        return True

    def get(self, esc_id: str) -> Escalation | None:
        return self.rows.get(esc_id)

    def due(self, now: datetime) -> list[Escalation]:
        return [e for e in self.rows.values() if e.status == "open" and e.deadline <= now]

    def claim_due(self, now: datetime, *, limit: int, lease: timedelta) -> list[Escalation]:
        claimed: list[Escalation] = []
        for e in sorted(self.due(now), key=lambda x: x.deadline):
            if len(claimed) >= limit:
                break
            if self.leases.get(e.id, now) > now:
                continue  # leased by another worker
            self.leases[e.id] = now + lease
            claimed.append(e)
        return claimed

    def for_message(self, message_id: str) -> Escalation | None:
        return next((e for e in self.rows.values() if e.message_id == message_id), None)


class EscalationService:
    def __init__(
        self,
        *,
        rota: OnCallRota,
        pager: Pager,
        store: EscalationStore,
        clock: Clock,
        audit: AuditSink,
        ladder: tuple[LadderStep, ...] = DEFAULT_LADDER,
        final_fallback: str = "emergency_desk",
        lease: timedelta = timedelta(seconds=30),
        batch_size: int = 50,
    ) -> None:
        if not ladder:
            raise ValueError("ladder needs at least one step")
        self.rota, self.pager, self.store, self.clock, self.audit = rota, pager, store, clock, audit
        self.ladder = ladder
        self.final_fallback = final_fallback
        self.lease = lease
        self.batch_size = batch_size

    async def open(self, env: Envelope, unit: str) -> Escalation:
        if existing := self.store.for_message(str(env.id)):
            return existing  # a redelivered red flag must not page the doctor twice
        now = self.clock.now()
        esc = Escalation(
            id=str(uuid.uuid4()), message_id=str(env.id), patient_id=env.patient_id, episode_id=env.episode_id,
            unit=unit, level=0, deadline=now + self.ladder[0].ack_window, status="open",
        )
        esc = await self._page_level(esc)
        try:
            self.store.put(esc)
        except EscalationConflict:
            # Two deliveries raced; both paged (a duplicate page is acceptable), only one row may exist.
            existing = self.store.for_message(str(env.id))
            if existing is None:
                raise
            return existing
        return esc

    async def acknowledge(self, esc_id: str, clinician_id: str) -> Escalation:
        esc = self.store.get(esc_id)
        if esc is None:
            raise KeyError(esc_id)
        if esc.status != "open":
            return esc  # idempotent: a late second ack changes nothing
        acked = replace(esc, status="acknowledged", acknowledged_by=clinician_id)
        if not self.store.put(acked):
            return self.store.get(esc_id) or esc  # someone else (or the ladder) got there first
        self._audit(acked, "escalation.acknowledged", {"by": clinician_id})
        return acked

    async def tick(self) -> list[Escalation]:
        """Advance every overdue escalation one level. Safe to run from several workers at once."""
        now = self.clock.now()
        moved: list[Escalation] = []
        for esc in self.store.claim_due(now, limit=self.batch_size, lease=self.lease):
            try:
                advanced = await self._advance(esc, now)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stall the others
                # The lease expires and the row is claimed again: retried, never dropped.
                log.exception("escalation %s could not advance; will retry after the lease", esc.id)
                self._audit(esc, "escalation.tick_failed", {"error": repr(exc)}, decision="error")
                continue
            if advanced is not None:
                moved.append(advanced)
        return moved

    async def _advance(self, esc: Escalation, now: datetime) -> Escalation | None:
        fresh = self.store.get(esc.id)
        if fresh is None or fresh.status != "open":
            return None  # acknowledged between the claim and now: do not page again
        nxt = esc.level + 1
        if nxt >= len(self.ladder):
            new = replace(esc, status="exhausted", paged=(*esc.paged, self.final_fallback))
            # Raises on failure on purpose: the row stays open, the lease expires, the next tick pages again.
            await self.pager.page(self.final_fallback, new)
            self._audit(new, "escalation.exhausted", {"paged": self.final_fallback})
        else:
            new = replace(esc, level=nxt, deadline=now + self.ladder[nxt].ack_window)
            new = await self._page_level(new)
        return new if self.store.put(new) else None

    async def _page_level(self, esc: Escalation) -> Escalation:
        step = self.ladder[esc.level]
        now = self.clock.now()
        try:
            target = await self.rota.who(esc.unit, step.role, now)
        except Exception as exc:  # noqa: BLE001 - a rota outage must climb the ladder, not stop it
            log.exception("rota lookup failed for %s/%s", esc.unit, step.role)
            self._audit(esc, "escalation.rota_error", {"role": step.role, "error": repr(exc)}, decision="error")
            return replace(esc, deadline=now)
        if target is None:
            # Nobody on the rota for this level: don't wait out the window, go up now.
            self._audit(esc, "escalation.no_one_on_call", {"role": step.role})
            return replace(esc, deadline=now)
        try:
            await self.pager.page(target, esc)
        except Exception as exc:  # noqa: BLE001
            # The page may not have reached anyone: treat the level as unanswered and fail up immediately.
            log.exception("paging %s failed", target)
            self._audit(esc, "escalation.page_failed", {"target": target, "error": repr(exc)}, decision="error")
            return replace(esc, deadline=now)
        self._audit(esc, "escalation.paged", {"target": target, "role": step.role, "level": esc.level})
        return replace(esc, paged=(*esc.paged, target))

    def _audit(self, esc: Escalation, action: str, detail: dict[str, object], decision: str = "info") -> None:
        self.audit.record(ts=self.clock.now(), actor="dispatcher", action=action, decision=decision,  # type: ignore[arg-type]
                          patient_id=esc.patient_id, detail={"escalation_id": esc.id, **detail})
