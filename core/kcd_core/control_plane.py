"""Control plane: how a manifest change goes live. Propose -> diff -> Director approves -> activate -> reload.

Rules this module enforces (the HTTP layer only authenticates and calls it):
- only a `director` may propose, approve, reject or roll back, and every decision is audited with who and what
- a draft is approved against the live version it was made from; if the seat changed since, it is stale and refused
- activation is all-or-nothing: catalogue check, topology check (routes still consistent), then the store; a failure
  leaves the live manifest untouched
- other processes (seat containers) pick the change up by polling the store (ManifestReloader): no restart, and no
  pub/sub message that could be missed
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from psycopg_pool import ConnectionPool

from .audit import AuditSink
from .chief_of_staff.diff import manifest_diff
from .chief_of_staff.llm import CompiledDraft, DraftModel, compile_draft
from .clock import Clock
from .contracts import Manifest
from .registry import ManifestError, ManifestRegistry, RoutingError

log = logging.getLogger(__name__)

DraftStatus = Literal["pending", "approved", "rejected", "superseded"]


class ControlPlaneError(Exception):
    """Base for errors the API maps to 4xx."""


class Forbidden(ControlPlaneError):
    pass


class NotFound(ControlPlaneError):
    pass


class Conflict(ControlPlaneError):
    pass


@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str]

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise Forbidden(f"'{self.id}' lacks the '{role}' role")


@dataclass(frozen=True)
class StoredDraft:
    id: str
    seat: str
    base_version: int | None
    manifest: Manifest
    diff: dict[str, Any]
    instruction: str
    used_model: bool
    proposed_by: str
    created_at: datetime
    status: DraftStatus = "pending"
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "seat": self.seat, "base_version": self.base_version, "status": self.status,
            "instruction": self.instruction, "used_model": self.used_model, "proposed_by": self.proposed_by,
            "created_at": self.created_at.isoformat(), "decided_by": self.decided_by, "note": self.note,
            "diff": self.diff, "manifest": self.manifest.model_dump(mode="json"),
        }


class ManifestStore(Protocol):
    def add_draft(self, draft: StoredDraft) -> None: ...
    def get_draft(self, draft_id: str) -> StoredDraft | None: ...
    def list_drafts(self, status: DraftStatus | None = None) -> list[StoredDraft]: ...
    def decide(self, draft_id: str, to: DraftStatus, by: str, at: datetime, note: str | None) -> bool:
        """pending -> to, compare-and-set. False if the draft was no longer pending."""
        ...

    def activate(self, manifest: Manifest, approved_by: str, diff_summary: str) -> None: ...
    def active(self, seat: str) -> Manifest | None: ...
    def all_active(self) -> list[Manifest]: ...
    def previous(self, seat: str) -> Manifest | None:
        """The version live before the current one (for rollback)."""
        ...


@dataclass
class InMemoryManifestStore:
    drafts: dict[str, StoredDraft] = field(default_factory=dict)
    versions: dict[str, list[tuple[Manifest, str]]] = field(default_factory=dict)

    def add_draft(self, draft: StoredDraft) -> None:
        self.drafts[draft.id] = draft

    def get_draft(self, draft_id: str) -> StoredDraft | None:
        return self.drafts.get(draft_id)

    def list_drafts(self, status: DraftStatus | None = None) -> list[StoredDraft]:
        return sorted((d for d in self.drafts.values() if status in (None, d.status)), key=lambda d: d.created_at)

    def decide(self, draft_id: str, to: DraftStatus, by: str, at: datetime, note: str | None) -> bool:
        d = self.drafts.get(draft_id)
        if d is None or d.status != "pending":
            return False
        from dataclasses import replace

        self.drafts[draft_id] = replace(d, status=to, decided_by=by, decided_at=at, note=note)
        return True

    def activate(self, manifest: Manifest, approved_by: str, diff_summary: str) -> None:
        self.versions.setdefault(manifest.seat, []).append((manifest, approved_by))

    def active(self, seat: str) -> Manifest | None:
        v = self.versions.get(seat)
        return v[-1][0] if v else None

    def all_active(self) -> list[Manifest]:
        return [v[-1][0] for v in self.versions.values() if v]

    def previous(self, seat: str) -> Manifest | None:
        v = self.versions.get(seat, [])
        return v[-2][0] if len(v) > 1 else None


class PostgresManifestStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    _D = "id, seat, base_version, body, diff, instruction, used_model, status, proposed_by, created_at, " \
         "decided_by, decided_at, decision_note"

    @staticmethod
    def _draft(r: tuple[Any, ...]) -> StoredDraft:
        return StoredDraft(
            id=str(r[0]), seat=r[1], base_version=r[2], manifest=Manifest.model_validate(r[3]), diff=r[4],
            instruction=r[5], used_model=r[6], status=r[7], proposed_by=r[8], created_at=r[9], decided_by=r[10],
            decided_at=r[11], note=r[12],
        )

    def add_draft(self, draft: StoredDraft) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO manifest_draft ({self._D}) "  # noqa: S608
                "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)",
                (draft.id, draft.seat, draft.base_version, draft.manifest.model_dump_json(), json.dumps(draft.diff),
                 draft.instruction.replace("\x00", ""), draft.used_model, draft.status, draft.proposed_by,
                 draft.created_at, draft.decided_by, draft.decided_at, draft.note),
            )

    def get_draft(self, draft_id: str) -> StoredDraft | None:
        with self._pool.connection() as conn:
            r = conn.execute(f"SELECT {self._D} FROM manifest_draft WHERE id = %s", (draft_id,)).fetchone()  # noqa: S608
        return self._draft(r) if r else None

    def list_drafts(self, status: DraftStatus | None = None) -> list[StoredDraft]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {self._D} FROM manifest_draft WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at",  # noqa: S608
                (status, status),
            ).fetchall()
        return [self._draft(r) for r in rows]

    def decide(self, draft_id: str, to: DraftStatus, by: str, at: datetime, note: str | None) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE manifest_draft SET status = %s, decided_by = %s, decided_at = %s, decision_note = %s "
                "WHERE id = %s AND status = 'pending' RETURNING 1", (to, by, at, note, draft_id)).fetchone()
        return row is not None

    def activate(self, manifest: Manifest, approved_by: str, diff_summary: str) -> None:
        with self._pool.connection() as conn:  # one transaction: there is never zero or two active versions
            conn.execute("UPDATE manifest SET active = FALSE WHERE seat = %s AND active", (manifest.seat,))
            conn.execute(
                "INSERT INTO manifest "
                "(seat, version, body, diff_summary, compiled_by, approved_by, active, activated_at) "
                "VALUES (%s, %s, %s::jsonb, %s, %s, %s, TRUE, now())",
                (manifest.seat, manifest.version, manifest.model_dump_json(), diff_summary, manifest.compiled_by,
                 approved_by),
            )

    def active(self, seat: str) -> Manifest | None:
        with self._pool.connection() as conn:
            r = conn.execute("SELECT body FROM manifest WHERE seat = %s AND active", (seat,)).fetchone()
        return Manifest.model_validate(r[0]) if r else None

    def all_active(self) -> list[Manifest]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT body FROM manifest WHERE active ORDER BY seat").fetchall()
        return [Manifest.model_validate(r[0]) for r in rows]

    def previous(self, seat: str) -> Manifest | None:
        with self._pool.connection() as conn:
            r = conn.execute(
                "SELECT body FROM manifest WHERE seat = %s AND NOT active ORDER BY version DESC LIMIT 1", (seat,)
            ).fetchone()
        return Manifest.model_validate(r[0]) if r else None


class ControlPlane:
    def __init__(self, *, registry: ManifestRegistry, store: ManifestStore, audit: AuditSink, clock: Clock,
                 model: DraftModel | None = None) -> None:
        self.registry, self.store, self.audit, self.clock, self.model = registry, store, audit, clock, model

    # -- propose ---------------------------------------------------------------

    async def propose(self, actor: Actor, instruction: str, seat: str | None = None, name: str | None = None
                      ) -> tuple[StoredDraft, CompiledDraft]:
        actor.require("director")
        instruction = instruction.strip()
        if len(instruction) < 8 or "\x00" in instruction:
            raise ControlPlaneError("instruction is empty or invalid")
        current = self._live(seat) if seat else None
        compiled = await compile_draft(instruction, self.registry.catalog, now=self.clock.now(), model=self.model,
                                       current=current, name=name)
        if current is None and compiled.manifest.seat in self.registry.seats():
            raise Conflict(f"a seat named '{compiled.manifest.seat}' already exists; pass seat= to amend it")
        diff = manifest_diff(current, compiled.manifest)
        draft = StoredDraft(
            id=str(uuid.uuid4()), seat=compiled.manifest.seat, base_version=current.version if current else None,
            manifest=compiled.manifest, diff=diff.to_json(), instruction=instruction, used_model=compiled.used_model,
            proposed_by=actor.id, created_at=self.clock.now(),
        )
        self.store.add_draft(draft)
        self._audit(actor, "control_plane.proposed", "info", draft, {"needs_attention": diff.needs_attention,
                                                                     "used_model": compiled.used_model})
        return draft, compiled

    # -- decide ----------------------------------------------------------------

    def approve(self, actor: Actor, draft_id: str) -> StoredDraft:
        actor.require("director")
        draft = self._draft(draft_id)
        if draft.status == "approved":
            return draft  # idempotent: a double click does not activate twice
        if draft.status != "pending":
            raise Conflict(f"draft is {draft.status}")
        live = self.registry.get(draft.seat) if draft.seat in self.registry.seats() else None
        if (live.version if live else None) != draft.base_version:
            self.store.decide(draft_id, "superseded", actor.id, self.clock.now(), "live manifest changed")
            raise Conflict("the live manifest changed since this draft was made; propose again")
        if not self.store.decide(draft_id, "approved", actor.id, self.clock.now(), None):
            return self._draft(draft_id)  # lost a race with another approval: same outcome
        try:
            self._activate(draft.manifest, actor.id, draft.diff)
        except (ManifestError, RoutingError) as exc:
            self._audit(actor, "control_plane.activation_failed", "blocked", draft, {"error": str(exc)})
            raise Conflict(f"draft cannot be activated: {exc}") from exc
        self._audit(actor, "control_plane.activated", "allowed", draft, {"to": draft.manifest.ref})
        return self._draft(draft_id)

    def reject(self, actor: Actor, draft_id: str, reason: str) -> StoredDraft:
        actor.require("director")
        draft = self._draft(draft_id)
        if not self.store.decide(draft_id, "rejected", actor.id, self.clock.now(), reason[:500]):
            raise Conflict(f"draft is {draft.status}")
        self._audit(actor, "control_plane.rejected", "info", draft, {"reason": reason[:500]})
        return self._draft(draft_id)

    def rollback(self, actor: Actor, seat: str) -> Manifest:
        actor.require("director")
        prev = self.store.previous(seat)
        live = self._live(seat)
        if prev is None:
            raise Conflict(f"'{seat}' has no earlier version to roll back to")
        # A rollback is a new, higher version carrying the old content, so history only ever moves forward.
        restored = Manifest.model_validate({**prev.model_dump(), "version": live.version + 1,
                                            "compiled_by": f"rollback by {actor.id}"})
        self._activate(restored, actor.id, {"rollback_of": live.ref})
        self.audit.record(ts=self.clock.now(), actor=actor.id, action="control_plane.rolled_back", decision="allowed",
                          detail={"seat": seat, "from": live.ref, "to": restored.ref, "content_of": prev.ref})
        return restored

    # -- helpers ---------------------------------------------------------------

    def _activate(self, manifest: Manifest, by: str, diff: dict[str, Any]) -> None:
        before = self.registry.get(manifest.seat) if manifest.seat in self.registry.seats() else None
        self.registry.activate(manifest)  # validates against the catalogue; raises before anything changes
        try:
            self.registry.check_topology()
            if before is not None and self.store.active(manifest.seat) is None:
                self.store.activate(before, "boot", "manifest loaded from disk at start-up")  # so rollback has a target
            self.store.activate(manifest, by, json.dumps(diff.get("changes", []))[:2000])
        except BaseException:  # whatever went wrong, the live registry must end up exactly as it was
            self._restore(manifest.seat, before)
            raise

    def _restore(self, seat: str, before: Manifest | None) -> None:
        self.registry.undo_activation(seat)

    def _live(self, seat: str) -> Manifest:
        if seat not in self.registry.seats():
            raise NotFound(f"no such seat '{seat}'")
        return self.registry.get(seat)

    def _draft(self, draft_id: str) -> StoredDraft:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise NotFound("no such draft")
        return draft

    def _audit(self, actor: Actor, action: str, decision: str, draft: StoredDraft, extra: dict[str, Any]) -> None:
        self.audit.record(ts=self.clock.now(), actor=actor.id, action=action, decision=decision,  # type: ignore[arg-type]
                          detail={"draft_id": draft.id, "seat": draft.seat, "manifest": draft.manifest.ref, **extra})


class ManifestReloader:
    """Keeps this process's registry equal to the store: every process that runs seats starts one."""

    def __init__(self, registry: ManifestRegistry, store: ManifestStore) -> None:
        self.registry, self.store = registry, store

    def sync_once(self) -> list[str]:
        changed: list[str] = []
        for m in self.store.all_active():
            have = self.registry.get(m.seat) if m.seat in self.registry.seats() else None
            if have is None or have.version != m.version:
                try:
                    self.registry.activate(m)
                    changed.append(m.ref)
                except ManifestError:
                    log.exception("refusing store manifest %s that fails the catalogue check", m.ref)
        return changed

    async def run(self, stop: asyncio.Event, interval_s: float = 2.0) -> None:
        from .workers import run_periodically

        await run_periodically("manifest reload", lambda: asyncio.to_thread(self.sync_once), interval_s, stop)
