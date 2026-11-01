"""Contract C1: the seat manifest, the single source of truth for what a seat may do."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .envelope import SEAT_ID_PATTERN
from .messages import MessageType

MemoryLayer = Literal["L0", "L1", "L2", "L3"]
"""L0 episode working memory, L1 patient evidence, L2 patient knowledge, L3 shared hospital knowledge."""

SEMVER = r"^\d+\.\d+\.\d+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSpec(_Strict):
    provider: Literal["vllm", "ollama", "cloud_deidentified", "none"]
    name: str


class MemoryScopes(_Strict):
    read: list[MemoryLayer] = Field(default_factory=list)
    write: list[MemoryLayer] = Field(default_factory=list)


class GateSpec(_Strict):
    artifact: str = Field(description="Artifact kind held for review, e.g. 'case_pack'")
    signer_role: str = Field(description="Role that must sign, e.g. 'clinician:radiology'")


class Manifest(_Strict):
    seat: str = Field(pattern=SEAT_ID_PATTERN)
    version: str = Field(pattern=SEMVER)
    location: Literal["edge", "device", "cloud"]
    model: ModelSpec
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)
    memory: MemoryScopes = Field(default_factory=MemoryScopes)
    refuses: list[str] = Field(default_factory=list)
    gates: list[GateSpec] = Field(default_factory=list)
    a2a_peers: list[str] = Field(default_factory=list)
    # consumes / emits are an addition to the demo's C1: the dispatcher derives its routing table from them.
    consumes: list[MessageType] = Field(default_factory=list)
    emits: list[MessageType] = Field(default_factory=list)
    artifact: str | None = None
    unit: str
    job_description: str

    @property
    def ref(self) -> str:
        return f"{self.seat}@{self.version}"

    @model_validator(mode="after")
    def _no_duplicates(self) -> Manifest:
        for name in ("tools", "surfaces", "refuses", "a2a_peers", "consumes", "emits"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate entries in '{name}'")
        if not set(self.memory.write) <= set(self.memory.read) | {"L0"}:
            # Writing a layer you cannot read makes the seat blind to its own output; almost always a typo.
            raise ValueError("memory.write must be a subset of memory.read (L0 excepted)")
        return self
