"""Dispatcher: the only component that addresses seats.

For each inbound message: verify signature -> validate schema -> check route against both manifests ->
dedupe -> side effects (paging, gate) -> deliver to the receiver's inbox. The sender's signature travels
end to end; the dispatcher does not re-sign, so receivers can verify who really wrote the message.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from .audit import AuditSink
from .bus import DISPATCHER_INBOX, Bus, PermanentReject, seat_inbox
from .clock import Clock
from .contracts import Envelope, MessageType
from .contracts.messages import CasePackReady, RedFlagEscalation
from .escalation import EscalationService
from .registry import DISPATCHER, ManifestRegistry, RoutingError
from .signing import KeyRing, SignatureError, verify_envelope


class IdempotencyStore(Protocol):
    """In Postgres: a unique index on the key, marked in the same transaction as the outbox row."""

    def has(self, key: str) -> bool: ...
    def mark(self, key: str) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._keys: set[str] = set()

    def has(self, key: str) -> bool:
        return key in self._keys

    def mark(self, key: str) -> None:
        self._keys.add(key)


class GatePort(Protocol):
    """Gate service (review queue on the Clinical Board). Implemented with Person 5.

    Must be idempotent on (case_id, version): a bus retry can call it again for the same message.
    """

    async def open_gate(self, env: Envelope, pack: CasePackReady) -> None: ...


class Dispatcher:
    def __init__(
        self,
        *,
        bus: Bus,
        registry: ManifestRegistry,
        seat_keys: KeyRing,
        audit: AuditSink,
        clock: Clock,
        escalations: EscalationService,
        gates: GatePort,
        idempotency: IdempotencyStore | None = None,
        default_unit: str = "uro_oncology",
    ) -> None:
        self.bus, self.registry, self.seat_keys = bus, registry, seat_keys
        self.audit, self.clock = audit, clock
        self.escalations, self.gates = escalations, gates
        self.idempotency = idempotency or InMemoryIdempotencyStore()
        self.default_unit = default_unit

    async def start(self) -> None:
        await self.bus.subscribe(DISPATCHER_INBOX, self.on_message)

    async def on_message(self, env: Envelope) -> None:
        try:
            verify_envelope(env, self.seat_keys)
            payload = env.typed_payload()
            self.registry.check_route(env.from_, env.to, env.type)
        except (SignatureError, ValidationError, RoutingError) as exc:
            self._audit(env, "dispatch.reject", "blocked", {"error": str(exc)})
            raise PermanentReject(str(exc)) from exc

        if self.idempotency.has(env.idempotency_key):
            self._audit(env, "dispatch.duplicate", "info")
            return

        if isinstance(payload, RedFlagEscalation):
            esc = await self.escalations.open(env, unit=payload.unit or self.default_unit)
            self._audit(env, "dispatch.escalation_opened", "info", {"escalation_id": esc.id})

        if env.to == DISPATCHER:
            await self._consume(env)
        else:
            await self.bus.publish(seat_inbox(env.to), env)
            self._audit(env, "dispatch.routed", "allowed", {"to": env.to})
        # Marked only after delivery succeeded: if publish fails, the bus retry must not be dropped as a duplicate.
        self.idempotency.mark(env.idempotency_key)

    async def _consume(self, env: Envelope) -> None:
        if env.type == MessageType.CASE_PACK_READY:
            pack = CasePackReady.model_validate(env.payload)
            await self.gates.open_gate(env, pack)
            self._audit(env, "dispatch.gate_opened", "info", {"case_id": pack.case_id, "version": pack.version})
        else:
            self._audit(env, "dispatch.consumed", "info")

    def _audit(self, env: Envelope, action: str, decision: str, detail: dict[str, object] | None = None) -> None:
        self.audit.record(
            ts=self.clock.now(), actor=DISPATCHER, action=action, decision=decision,  # type: ignore[arg-type]
            patient_id=env.patient_id, correlation_id=str(env.correlation_id), inputs=env.wire(),
            detail={"type": str(env.type), "from": env.from_, "to": env.to, **(detail or {})},
        )
