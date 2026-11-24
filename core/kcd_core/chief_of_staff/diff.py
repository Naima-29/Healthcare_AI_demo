"""Manifest diff: what a Director is actually approving, field by field, with the risky parts called out.

Order inside lists is irrelevant, so lists are compared as sets. A change is *attention-worthy* when it widens
what the seat can do (new tools, surfaces, routes, memory, cloud fallback, location) or loosens a safety rail
(a removed refusal or gate). The console highlights those; the approval API returns them with every draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..contracts import Manifest

_QUIET = {"version", "compiled_at", "compiled_by"}  # change on every draft, never interesting on their own
_GRANTS = {"tools", "surfaces", "a2a_peers", "consumes", "emits", "memory.read", "memory.write", "skills"}
_RAILS = {"refuses", "gates"}
_WIDENING_SCALARS = {"model.fallback", "location", "model.edge"}


@dataclass(frozen=True)
class FieldChange:
    path: str
    kind: Literal["added", "removed", "changed"]
    before: Any = None
    after: Any = None
    flag: Literal["grants", "loosens", None] = None

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "before": self.before, "after": self.after, "flag": self.flag}


@dataclass(frozen=True)
class ManifestDiff:
    seat: str
    from_ref: str | None
    to_ref: str
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes

    @property
    def needs_attention(self) -> bool:
        return any(c.flag for c in self.changes)

    def render(self) -> str:
        head = f"{self.from_ref or '(new seat)'} -> {self.to_ref}"
        if self.empty:
            return head + "\n  no changes"
        lines = [head]
        for c in self.changes:
            mark = {"grants": "  [WIDENS]", "loosens": "  [LOOSENS A SAFETY RAIL]", None: ""}[c.flag]
            if c.kind == "changed":
                lines.append(f"  ~ {c.path}: {c.before!r} -> {c.after!r}{mark}")
            else:
                sign, value = ("+", c.after) if c.kind == "added" else ("-", c.before)
                lines.append(f"  {sign} {c.path}: {value}{mark}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {"seat": self.seat, "from": self.from_ref, "to": self.to_ref, "needs_attention": self.needs_attention,
                "changes": [c.to_json() for c in self.changes]}


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    else:
        out[prefix] = value


def manifest_diff(old: Manifest | None, new: Manifest) -> ManifestDiff:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if old is not None:
        _flatten("", old.model_dump(mode="json"), before)
    _flatten("", new.model_dump(mode="json"), after)
    changes: list[FieldChange] = []
    for path in sorted(before.keys() | after.keys()):
        if path in _QUIET:
            continue
        b, a = before.get(path), after.get(path)
        if isinstance(a, list) or isinstance(b, list):
            bs, as_ = set(map(str, b or [])), set(map(str, a or []))
            for item in sorted(as_ - bs):
                changes.append(FieldChange(path, "added", None, item, "grants" if path in _GRANTS else None))
            for item in sorted(bs - as_):
                changes.append(FieldChange(path, "removed", item, None, "loosens" if path in _RAILS else None))
        elif a != b:
            flag: Literal["grants", "loosens", None] = (
                "grants" if path in _WIDENING_SCALARS and old is not None else None
            )
            kind: Literal["added", "removed", "changed"] = "changed" if old is not None else "added"
            changes.append(FieldChange(path, kind, b, a, flag))
    return ManifestDiff(new.seat, old.ref if old else None, new.ref, changes)
