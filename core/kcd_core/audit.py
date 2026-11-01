"""Audit events, hash-chained so that editing or dropping a past event is detectable."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Literal, Protocol

import rfc8785
from pydantic import BaseModel, ConfigDict

GENESIS = "sha256:" + "0" * 64


def content_hash(obj: Any) -> str:
    data = rfc8785.dumps(obj) if not isinstance(obj, bytes) else obj
    return "sha256:" + hashlib.sha256(data).hexdigest()


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    ts: datetime
    actor: str
    manifest_version: str | None
    action: str
    decision: Literal["allowed", "blocked", "info", "error"]
    patient_id: str | None = None
    correlation_id: str | None = None
    inputs_hash: str | None = None
    outputs_hash: str | None = None
    detail: dict[str, Any] = {}
    prev_hash: str
    hash: str


class AuditSink(Protocol):
    def record(
        self,
        *,
        ts: datetime,
        actor: str,
        action: str,
        decision: Literal["allowed", "blocked", "info", "error"],
        manifest_version: str | None = None,
        patient_id: str | None = None,
        correlation_id: str | None = None,
        inputs: Any = None,
        outputs: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent: ...


class InMemoryAudit:
    """Reference sink. The Postgres sink keeps the same chain in an append-only table."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(
        self,
        *,
        ts: datetime,
        actor: str,
        action: str,
        decision: Literal["allowed", "blocked", "info", "error"],
        manifest_version: str | None = None,
        patient_id: str | None = None,
        correlation_id: str | None = None,
        inputs: Any = None,
        outputs: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        prev = self.events[-1].hash if self.events else GENESIS
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "ts": ts,
            "actor": actor,
            "manifest_version": manifest_version,
            "action": action,
            "decision": decision,
            "patient_id": patient_id,
            "correlation_id": correlation_id,
            "inputs_hash": content_hash(inputs) if inputs is not None else None,
            "outputs_hash": content_hash(outputs) if outputs is not None else None,
            "detail": detail or {},
            "prev_hash": prev,
        }
        draft = AuditEvent(**body, hash=GENESIS)
        event = draft.model_copy(update={"hash": content_hash(draft.model_dump(mode="json", exclude={"hash"}))})
        self.events.append(event)
        return event

    def verify_chain(self) -> bool:
        prev = GENESIS
        for e in self.events:
            body = e.model_dump(mode="json", exclude={"hash"})
            if e.prev_hash != prev or content_hash(body) != e.hash:
                return False
            prev = e.hash
        return True

    def find(self, action: str, decision: str | None = None) -> list[AuditEvent]:
        return [e for e in self.events if e.action == action and (decision is None or e.decision == decision)]
