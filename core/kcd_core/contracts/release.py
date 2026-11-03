"""Contract C5: a clinician's signed release of an artifact."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    artifact_id: str
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    clinician_id: str
    role: str
    content_classes: list[str] = Field(description="Content classes this release lets seats disclose")
    signed_at: datetime
    sig: str | None = None
