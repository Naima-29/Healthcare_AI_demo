"""Contract C1: the seat manifest, the single source of truth for what a seat may do.

Shape follows the v2 demo (`contracts/manifestSchema.mjs`). Core adds `consumes` / `emits`: the Dispatcher
derives its routing table from them, so there is no hand-written routing switch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .envelope import SEAT_ID_PATTERN
from .messages import MessageType

MemoryLayer = Literal[0, 1, 2, 3]
"""Cortex layers: 0-1 licensed knowledge (read-only for seats), 2 hospital partition, 3 patient partition."""

READ_ONLY_LAYERS: frozenset[int] = frozenset({0, 1})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSpec(_Strict):
    edge: str = Field(description="Edge model id, or 'none' for a deterministic seat")
    fallback: str | None = Field(default=None, description="Cloud fallback; only ever sees de-identified data")


class Humanized(_Strict):
    audio: str | None = None
    video: str | None = None
    text: str | None = None
    fuzzy: str | None = None


class MemoryScopes(_Strict):
    read: list[MemoryLayer] = Field(default_factory=list)
    write: list[MemoryLayer] = Field(default_factory=list)
    partition: str | None = None

    @model_validator(mode="after")
    def _scopes(self) -> MemoryScopes:
        if missing := set(self.write) - set(self.read):
            raise ValueError(f"cannot write layers {sorted(missing)} without reading them")
        if licensed := set(self.write) & READ_ONLY_LAYERS:
            raise ValueError(f"layers {sorted(licensed)} are licensed knowledge, read-only for seats")
        return self


class Unit(_Strict):
    kind: str = Field(description="Billing unit, e.g. 'per_check'")
    tc: float = Field(ge=0, description="Token-credits per unit")


class Manifest(_Strict):
    seat: str = Field(pattern=SEAT_ID_PATTERN)
    name: str = Field(min_length=2)
    role: str | None = None
    version: int = Field(ge=1)
    location: Literal["patient_edge", "hospital_edge"]
    desk: str | None = None
    model: ModelSpec
    humanized: Humanized | None = None
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    memory: MemoryScopes = Field(default_factory=MemoryScopes)
    refuses: list[str] = Field(default_factory=list)
    safety_script: str | None = None
    gates: list[str] = Field(min_length=1, description="Gate ids from the catalogue; ['none'] if automatic")
    a2a_peers: list[str] = Field(default_factory=list, description="Seats this seat may address (via the Dispatcher)")
    consumes: list[MessageType] = Field(default_factory=list)
    emits: list[MessageType] = Field(default_factory=list)
    artifact: str | None = None
    unit: Unit
    job_description: str = Field(min_length=20)
    compiled_by: str | None = None
    compiled_at: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.seat}@v{self.version}"

    @model_validator(mode="after")
    def _no_duplicates(self) -> Manifest:
        for name in ("tools", "surfaces", "refuses", "gates", "a2a_peers", "consumes", "emits"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate entries in '{name}'")
        return self
