"""Seat runtime: what every seat runs on. Seat authors implement Seat.handle and nothing else."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from .audit import AuditSink
from .bus import DISPATCHER_INBOX, Bus, PermanentReject, seat_inbox
from .clock import Clock
from .contracts import Envelope, Manifest, MemoryLayer, MessageType, PolicyCatalog, Release, new_envelope
from .pep import PolicyEnforcementPoint, PolicyViolation
from .registry import ManifestRegistry
from .signing import KeyRing, SignatureError, Signer, sign_envelope, verify_envelope

Tool = Callable[..., Awaitable[Any]]


class ToolRegistry:
    """Tool name -> async callable. In production each entry is an MCP client call."""

    def __init__(self, tools: dict[str, Tool] | None = None) -> None:
        self._tools = dict(tools or {})

    def register(self, name: str, fn: Tool) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise LookupError(f"tool '{name}' has no implementation bound")
        return self._tools[name]


class SeatContext:
    """Everything a handler may touch. Every side effect goes through the PEP."""

    def __init__(self, runtime: SeatRuntime, inbound: Envelope | None) -> None:
        self._rt = runtime
        self.inbound = inbound
        self.manifest = runtime.manifest
        self.pep = runtime.pep

    def visible_tools(self) -> list[str]:
        return self.pep.visible_tools()

    async def tool(self, name: str, **kwargs: Any) -> Any:
        patient = self.inbound.patient_id if self.inbound else None
        self.pep.check_tool(name, patient_id=patient)
        result = await self._rt.tools.get(name)(**kwargs)
        self._rt.audit.record(
            ts=self._rt.clock.now(), actor=self.manifest.seat, manifest_version=self.manifest.ref,
            action=f"tool.{name}", decision="info", patient_id=patient,
            correlation_id=str(self.inbound.correlation_id) if self.inbound else None,
            inputs=_jsonable(kwargs), outputs=_jsonable(result),
        )
        return result

    def act(self, action: str) -> None:
        """Declare an action that is not a tool call (e.g. 'case.decide'); raises if a refusal blocks it."""
        self.pep.check_action(action, patient_id=self.inbound.patient_id if self.inbound else None)

    def check_memory(self, layer: MemoryLayer, mode: str) -> None:
        self.pep.check_memory(layer, mode)

    def guard_content(self, content_class: str, artifact_hash: str | None = None,
                      release: Release | None = None) -> None:
        self.pep.check_content(content_class, artifact_hash, release,
                               patient_id=self.inbound.patient_id if self.inbound else None)

    async def send(
        self,
        type_: MessageType,
        to: str,
        payload: BaseModel | dict[str, Any],
        *,
        patient_id: str | None = None,
        episode_id: str | None = None,
        correlation_id: UUID | None = None,
    ) -> Envelope:
        """Send a typed message. It always goes to the dispatcher, which routes it to `to`."""
        self.pep.check_emit(type_, to)
        src = self.inbound
        env = new_envelope(
            type_=type_,
            sender=self.manifest.seat,
            to=to,
            patient_id=patient_id or (src.patient_id if src else _required("patient_id")),
            episode_id=episode_id or (src.episode_id if src else _required("episode_id")),
            payload=payload,
            manifest_version=self.manifest.ref,
            now=self._rt.clock.now(),
            correlation_id=correlation_id or (src.correlation_id if src else None),
            causation_id=src.id if src else None,
        )
        env = sign_envelope(env, self._rt.signer)
        await self._rt.bus.publish(DISPATCHER_INBOX, env)
        return env


class Seat(ABC):
    seat_id: str

    @abstractmethod
    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None: ...

    async def fallback(self, env: Envelope, ctx: SeatContext, error: BaseException) -> None:  # noqa: B027
        """Called when handle times out or fails for good. Default: leave it for a human, loudly."""


class SeatRuntime:
    def __init__(
        self,
        seat: Seat,
        *,
        registry: ManifestRegistry,
        catalog: PolicyCatalog,
        signer: Signer,
        seat_keys: KeyRing,
        clinician_keys: KeyRing,
        bus: Bus,
        audit: AuditSink,
        clock: Clock,
        tools: ToolRegistry | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.seat = seat
        self.registry = registry
        self.signer = signer
        self.seat_keys = seat_keys
        self.bus = bus
        self.audit = audit
        self.clock = clock
        self.tools = tools or ToolRegistry()
        self.timeout_s = timeout_s
        self.pep = PolicyEnforcementPoint(lambda: self.manifest, catalog, audit, clock, clinician_keys)
        if signer.key_id != seat.seat_id:
            raise ValueError("signer key id must equal the seat id")

    @property
    def manifest(self) -> Manifest:
        # Looked up per access: a manifest activated in the registry applies from the next message on.
        return self.registry.get(self.seat.seat_id)

    def context(self, inbound: Envelope | None = None) -> SeatContext:
        return SeatContext(self, inbound)

    async def start(self) -> None:
        await self.bus.subscribe(seat_inbox(self.seat.seat_id), self._on_message)

    async def _on_message(self, env: Envelope) -> None:
        m = self.manifest
        try:
            verify_envelope(env, self.seat_keys)  # defence in depth: the dispatcher already verified
            if env.to != m.seat or env.type not in m.consumes:
                raise PermanentReject(f"{m.ref} does not consume {env.type}")
            payload = env.typed_payload()
        except (SignatureError, ValidationError) as exc:
            self._audit(env, "seat.reject", "blocked", {"error": str(exc)})
            raise PermanentReject(str(exc)) from exc

        ctx = self.context(env)
        try:
            await asyncio.wait_for(self.seat.handle(env, payload, ctx), timeout=self.timeout_s)
            self._audit(env, "seat.handled", "info")
        except PolicyViolation as exc:
            # A blocked action is not retried: the same input would be blocked again.
            self._audit(env, "seat.policy_violation", "blocked", {"error": str(exc)})
            await self.seat.fallback(env, ctx, exc)
        except TimeoutError as exc:
            self._audit(env, "seat.timeout", "error", {"timeout_s": self.timeout_s})
            await self.seat.fallback(env, ctx, exc)

    def _audit(self, env: Envelope, action: str, decision: Any, detail: dict[str, Any] | None = None) -> None:
        self.audit.record(
            ts=self.clock.now(), actor=self.seat.seat_id, manifest_version=self.manifest.ref, action=action,
            decision=decision, patient_id=env.patient_id, correlation_id=str(env.correlation_id),
            inputs=env.wire(), detail=detail,
        )


def _required(name: str) -> str:
    raise ValueError(f"{name} is required when sending outside of a message handler")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)
