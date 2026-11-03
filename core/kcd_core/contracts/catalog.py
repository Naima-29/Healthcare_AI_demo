"""Contract C3: the policy catalogue. Vocabulary from the v2 demo (`contracts/catalog.mjs`); clinically owned.

- A refusal blocks *actions* (checked on every tool call and on ctx.act) and release-gated *content classes*.
- A tool performs zero or more actions (`tool_actions`); a tool whose action is refused is blocked.
- Surfaces are MCP servers; a seat may call a server's tools only if the surface is in its manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Refusal(_Strict):
    label: str
    actions: list[str] = Field(default_factory=list)
    content: list[str] = Field(default_factory=list, description="Content classes allowed only with a valid release")


class Gate(_Strict):
    label: str
    signer: str | None = Field(description="Role that must sign; null means no human gate")


class PolicyCatalog(_Strict):
    version: str
    source: str | None = None
    refusals: dict[str, Refusal]
    content_classes: list[str]
    gates: dict[str, Gate]
    surfaces: dict[str, list[str]] = Field(description="MCP server -> its tools")
    internal_tools: list[str] = Field(default_factory=list, description="Runtime tools with no MCP surface")
    tool_actions: dict[str, list[str]] = Field(default_factory=dict, description="Tool -> actions it performs")

    @model_validator(mode="after")
    def _consistent(self) -> PolicyCatalog:
        seen: set[str] = set()
        for tools in self.surfaces.values():
            for tool in tools:
                if tool in seen:
                    raise ValueError(f"tool '{tool}' is listed under two surfaces")
                seen.add(tool)
        if dup := seen & set(self.internal_tools):
            raise ValueError(f"internal tools also listed under a surface: {sorted(dup)}")
        if unknown := set(self.tool_actions) - self.all_tools():
            raise ValueError(f"tool_actions for unknown tools: {sorted(unknown)}")
        for rid, refusal in self.refusals.items():
            if unknown := set(refusal.content) - set(self.content_classes):
                raise ValueError(f"refusal '{rid}' gates unknown content classes: {sorted(unknown)}")
        return self

    def all_tools(self) -> set[str]:
        return {t for tools in self.surfaces.values() for t in tools} | set(self.internal_tools)

    def surface_of(self, tool: str) -> str | None:
        return next((s for s, tools in self.surfaces.items() if tool in tools), None)

    @classmethod
    def load(cls, path: Path) -> PolicyCatalog:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
