"""Contract C3: the policy catalogue (refusals, gates, surfaces, tools). Clinically owned and versioned."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Refusal(_Strict):
    description: str
    blocked_tools: list[str] = Field(default_factory=list)
    release_gated_content: list[str] = Field(
        default_factory=list,
        description="Content classes the seat may only emit when backed by a valid clinician release",
    )


class ToolSpec(_Strict):
    server: str = Field(description="MCP server that serves the tool")
    surface: str | None = Field(default=None, description="Surface the tool writes to, if any")


class PolicyCatalog(_Strict):
    version: str
    refusals: dict[str, Refusal]
    tools: dict[str, ToolSpec]
    surfaces: list[str]
    gate_roles: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> PolicyCatalog:
        for rid, refusal in self.refusals.items():
            unknown = set(refusal.blocked_tools) - self.tools.keys()
            if unknown:
                raise ValueError(f"refusal '{rid}' blocks unknown tools: {sorted(unknown)}")
        for name, tool in self.tools.items():
            if tool.surface is not None and tool.surface not in self.surfaces:
                raise ValueError(f"tool '{name}' references unknown surface '{tool.surface}'")
        return self

    @classmethod
    def load(cls, path: Path) -> PolicyCatalog:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
