"""Contract C2: the envelope every A2A message travels in."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .messages import PAYLOADS, MessageType

SCHEMA_VERSION = "1.0.0"
SeatId = str  # validated by SEAT_ID_PATTERN; the set of seats lives in the manifest registry, not in code
SEAT_ID_PATTERN = r"^[a-z][a-z0-9_]{1,40}$"


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: UUID
    type: MessageType
    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^\d+\.\d+\.\d+$")
    from_: SeatId = Field(alias="from", pattern=SEAT_ID_PATTERN)
    to: SeatId = Field(pattern=SEAT_ID_PATTERN)
    patient_id: str
    episode_id: str
    correlation_id: UUID
    causation_id: UUID | None = None
    idempotency_key: str
    created_at: datetime
    manifest_version: str = Field(description="'<seat>@<semver>' of the sender's active manifest")
    payload: dict[str, Any]
    sig: str | None = Field(default=None, description="Ed25519 signature (base64url) over the JCS form without sig")

    def typed_payload(self) -> BaseModel:
        """Validate the payload against its type's schema. Raises pydantic.ValidationError."""
        return PAYLOADS[self.type].model_validate(self.payload)

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def idempotency_key(sender: str, type_: MessageType, episode_id: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(rfc8785.dumps({"f": sender, "t": str(type_), "e": episode_id, "p": payload}))
    return f"sha256:{digest.hexdigest()}"


def new_envelope(
    *,
    type_: MessageType,
    sender: str,
    to: str,
    patient_id: str,
    episode_id: str,
    payload: BaseModel | dict[str, Any],
    manifest_version: str,
    now: datetime,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> Envelope:
    """Build an unsigned envelope. The payload is validated against its type before it is wrapped."""
    model = payload if isinstance(payload, BaseModel) else PAYLOADS[type_].model_validate(payload)
    if not isinstance(model, PAYLOADS[type_]):
        raise TypeError(f"payload {type(model).__name__} does not match message type {type_}")
    body = model.model_dump(mode="json")
    return Envelope(
        id=uuid.uuid4(),
        type=type_,
        **{"from": sender},
        to=to,
        patient_id=patient_id,
        episode_id=episode_id,
        correlation_id=correlation_id or uuid.uuid4(),
        causation_id=causation_id,
        idempotency_key=idempotency_key(sender, type_, episode_id, body),
        created_at=now,
        manifest_version=manifest_version,
        payload=body,
    )
