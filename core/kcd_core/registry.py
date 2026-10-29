"""Manifest registry: loads, validates against the catalogue, and derives the routing table.

In-memory here; the control-plane service backs the same interface with Postgres and versions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .contracts import Manifest, MessageType, PolicyCatalog

DISPATCHER = "dispatcher"

ReloadListener = Callable[[Manifest], None]


class ManifestError(ValueError):
    pass


class RoutingError(Exception):
    pass


def check_against_catalog(m: Manifest, catalog: PolicyCatalog) -> None:
    problems: list[str] = []
    if unknown := set(m.tools) - catalog.tools.keys():
        problems.append(f"unknown tools {sorted(unknown)}")
    if unknown := set(m.refuses) - catalog.refusals.keys():
        problems.append(f"unknown refusals {sorted(unknown)}")
    if unknown := set(m.surfaces) - set(catalog.surfaces):
        problems.append(f"unknown surfaces {sorted(unknown)}")
    if unknown := {g.signer_role for g in m.gates} - set(catalog.gate_roles):
        problems.append(f"unknown gate roles {sorted(unknown)}")
    for tool in m.tools:
        surface = catalog.tools[tool].surface if tool in catalog.tools else None
        if surface and surface not in m.surfaces:
            problems.append(f"tool '{tool}' writes to surface '{surface}' the seat does not have")
    if problems:
        raise ManifestError(f"{m.ref}: " + "; ".join(problems))


class ManifestRegistry:
    def __init__(self, catalog: PolicyCatalog) -> None:
        self.catalog = catalog
        self._active: dict[str, Manifest] = {}
        self._history: dict[str, list[Manifest]] = {}
        self._listeners: list[ReloadListener] = []

    @classmethod
    def from_dir(cls, catalog: PolicyCatalog, directory: Path) -> ManifestRegistry:
        reg = cls(catalog)
        for path in sorted(directory.glob("*.manifest.json")):
            reg.activate(Manifest.model_validate(json.loads(path.read_text(encoding="utf-8"))))
        reg.check_topology()
        return reg

    def on_reload(self, listener: ReloadListener) -> None:
        self._listeners.append(listener)

    def activate(self, manifest: Manifest) -> None:
        check_against_catalog(manifest, self.catalog)
        self._active[manifest.seat] = manifest
        self._history.setdefault(manifest.seat, []).append(manifest)
        for listener in self._listeners:
            listener(manifest)

    def rollback(self, seat: str) -> Manifest:
        history = self._history.get(seat, [])
        if len(history) < 2:
            raise ManifestError(f"no previous version of '{seat}'")
        history.pop()
        previous = history.pop()
        self.activate(previous)
        return previous

    def get(self, seat: str) -> Manifest:
        try:
            return self._active[seat]
        except KeyError:
            raise RoutingError(f"unknown seat '{seat}'") from None

    def seats(self) -> list[str]:
        return sorted(self._active)

    def check_route(self, sender: str, to: str, type_: MessageType) -> None:
        """Hub-and-spoke rule: sender must emit the type and list the receiver as a peer; receiver must consume it."""
        s = self.get(sender)
        r = self.get(to)
        if type_ not in s.emits:
            raise RoutingError(f"{s.ref} does not emit {type_}")
        if to not in s.a2a_peers:
            raise RoutingError(f"{s.ref} may not address '{to}'")
        if type_ not in r.consumes:
            raise RoutingError(f"{r.ref} does not consume {type_}")

    def check_topology(self) -> None:
        """Every emitted type has somewhere to go, and every peer exists."""
        problems: list[str] = []
        for m in self._active.values():
            for peer in m.a2a_peers:
                if peer not in self._active:
                    problems.append(f"{m.ref} lists unknown peer '{peer}'")
            for t in m.emits:
                if not any(t in self._active[p].consumes for p in m.a2a_peers if p in self._active):
                    problems.append(f"{m.ref} emits {t} but no peer consumes it")
        if problems:
            raise ManifestError("; ".join(problems))
