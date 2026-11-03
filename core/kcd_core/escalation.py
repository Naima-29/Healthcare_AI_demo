"""Escalation ladder for red flags: page on-call, and climb the ladder until someone acknowledges.

Durable in production: EscalationStore is a Postgres table and tick() runs in a worker that claims due rows
with SELECT ... FOR UPDATE SKIP LOCKED. Everything time-based goes through Clock, so tests run in milliseconds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from .audit import AuditSink
from .clock import Clock
from .contracts import Envelope

Status = Literal["open", "acknowledged", "exhausted"]


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
    def put(self, esc: Escalation) -> None: ...
    def get(self, esc_id: str) -> Escalation | None: ...
    def due(self, now: datetime) -> list[Escalation]: ...
    def for_message(self, message_id: str) -> Escalation | None: ...


@dataclass
class InMemoryEscalationStore:
    rows: dict[str, Escalation] = field(default_factory=dict)

    def put(self, esc: Escalation) -> None:
        self.rows[esc.id] = esc

    def get(self, esc_id: str) -> Escalation | None:
        return self.rows.get(esc_id)

    def due(self, now: datetime) -> list[Escalation]:
        return [e for e in self.rows.values() if e.status == "open" and e.deadline <= now]

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
    ) -> None:
        if not ladder:
            raise ValueError("ladder needs at least one step")
        self.rota, self.pager, self.store, self.clock, self.audit = rota, pager, store, clock, audit
        self.ladder = ladder
        self.final_fallback = final_fallback

    async def open(self, env: Envelope, unit: str) -> Escalation:
        if existing := self.store.for_message(str(env.id)):
            return existing  # a redelivered red flag must not page the doctor twice
        now = self.clock.now()
        esc = Escalation(
            id=str(uuid.uuid4()), message_id=str(env.id), patient_id=env.patient_id, episode_id=env.episode_id,
            unit=unit, level=0, deadline=now + self.ladder[0].ack_window, status="open",
        )
        esc = await self._page_level(esc)
        self.store.put(esc)
        return esc

    async def acknowledge(self, esc_id: str, clinician_id: str) -> Escalation:
        esc = self.store.get(esc_id)
        if esc is None:
            raise KeyError(esc_id)
        if esc.status != "open":
            return esc  # idempotent: a late second ack changes nothing
        esc = replace(esc, status="acknowledged", acknowledged_by=clinician_id)
        self.store.put(esc)
        self._audit(esc, "escalation.acknowledged", {"by": clinician_id})
        return esc

    async def tick(self) -> list[Escalation]:
        """Advance every overdue escalation one level. Call from a scheduler every few seconds."""
        now = self.clock.now()
        moved: list[Escalation] = []
        for esc in self.store.due(now):
            nxt = esc.level + 1
            if nxt >= len(self.ladder):
                esc = replace(esc, status="exhausted", paged=(*esc.paged, self.final_fallback))
                await self.pager.page(self.final_fallback, esc)
                self._audit(esc, "escalation.exhausted", {"paged": self.final_fallback})
            else:
                esc = replace(esc, level=nxt, deadline=now + self.ladder[nxt].ack_window)
                esc = await self._page_level(esc)
            self.store.put(esc)
            moved.append(esc)
        return moved

    async def _page_level(self, esc: Escalation) -> Escalation:
        step = self.ladder[esc.level]
        target = await self.rota.who(esc.unit, step.role, self.clock.now())
        if target is None:
            # Nobody on the rota for this level: don't wait out the window, go up now.
            self._audit(esc, "escalation.no_one_on_call", {"role": step.role})
            return replace(esc, deadline=self.clock.now())
        await self.pager.page(target, esc)
        self._audit(esc, "escalation.paged", {"target": target, "role": step.role, "level": esc.level})
        return replace(esc, paged=(*esc.paged, target))

    def _audit(self, esc: Escalation, action: str, detail: dict[str, object]) -> None:
        self.audit.record(ts=self.clock.now(), actor="dispatcher", action=action, decision="info",
                          patient_id=esc.patient_id, detail={"escalation_id": esc.id, **detail})
