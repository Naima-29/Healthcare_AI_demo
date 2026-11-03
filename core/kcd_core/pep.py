"""Policy enforcement point. Deny by default; every decision is audited.

The model never decides what is allowed. It only ever sees the tools this returns from visible_tools(),
and every call is re-checked here anyway, so a prompt-injected model still cannot reach a refused tool.
"""

from __future__ import annotations

from collections.abc import Callable

from .audit import AuditSink
from .clock import Clock
from .contracts import Manifest, MemoryLayer, MessageType, PolicyCatalog, Release
from .signing import KeyRing, SignatureError, verify_release


class PolicyViolation(Exception):
    def __init__(self, seat: str, kind: str, target: str, reason: str, refusal: str | None = None) -> None:
        super().__init__(f"{seat}: {kind} '{target}' blocked: {reason}")
        self.seat = seat
        self.kind = kind
        self.target = target
        self.reason = reason
        self.refusal = refusal


class PolicyEnforcementPoint:
    def __init__(
        self,
        manifest_source: Callable[[], Manifest],
        catalog: PolicyCatalog,
        audit: AuditSink,
        clock: Clock,
        clinician_keys: KeyRing,
    ) -> None:
        self._manifest = manifest_source  # read on every check, so a hot-reloaded manifest applies at once
        self.catalog = catalog
        self.audit = audit
        self.clock = clock
        self.clinician_keys = clinician_keys

    @property
    def manifest(self) -> Manifest:
        return self._manifest()

    # --- helpers -------------------------------------------------------------

    def _refused_actions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rid in self.manifest.refuses:
            for action in self.catalog.refusals[rid].actions:
                out.setdefault(action, rid)
        return out

    def _gated_content(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rid in self.manifest.refuses:
            for cls in self.catalog.refusals[rid].content:
                out.setdefault(cls, rid)
        return out

    def _tool_refusal(self, tool: str) -> str | None:
        refused = self._refused_actions()
        return next((refused[a] for a in self.catalog.tool_actions.get(tool, []) if a in refused), None)

    def _decide(self, kind: str, target: str, reason: str | None, refusal: str | None = None,
                patient_id: str | None = None) -> None:
        m = self.manifest
        self.audit.record(
            ts=self.clock.now(),
            actor=m.seat,
            manifest_version=m.ref,
            action=f"pep.{kind}",
            decision="allowed" if reason is None else "blocked",
            patient_id=patient_id,
            detail={"target": target, "reason": reason, "refusal": refusal},
        )
        if reason is not None:
            raise PolicyViolation(m.seat, kind, target, reason, refusal)

    # --- checks --------------------------------------------------------------

    def visible_tools(self) -> list[str]:
        known = self.catalog.all_tools()
        return [t for t in self.manifest.tools if t in known and self._tool_refusal(t) is None]

    def check_tool(self, tool: str, patient_id: str | None = None) -> None:
        if refusal := self._tool_refusal(tool):  # a refusal wins even if the tool is listed: safety floor by union
            return self._decide("tool", tool, "refused by policy", refusal, patient_id)
        if tool not in self.manifest.tools:
            return self._decide("tool", tool, "not in manifest", None, patient_id)
        if tool not in self.catalog.all_tools():
            return self._decide("tool", tool, "not in catalogue", None, patient_id)
        surface = self.catalog.surface_of(tool)
        if surface and surface not in self.manifest.surfaces:
            return self._decide("tool", tool, f"surface '{surface}' not granted", None, patient_id)
        self._decide("tool", tool, None, None, patient_id)

    def check_action(self, action: str, patient_id: str | None = None) -> None:
        """For actions that are not a tool call (e.g. case.decide, verifier.override)."""
        refusal = self._refused_actions().get(action)
        self._decide("action", action, "refused by policy" if refusal else None, refusal, patient_id)

    def check_emit(self, type_: MessageType, to: str) -> None:
        if type_ not in self.manifest.emits:
            return self._decide("emit", str(type_), "type not in manifest.emits")
        if to not in self.manifest.a2a_peers:
            return self._decide("emit", f"{type_}->{to}", f"peer '{to}' not in manifest.a2a_peers")
        self._decide("emit", f"{type_}->{to}", None)

    def check_memory(self, layer: MemoryLayer, mode: str) -> None:
        allowed = self.manifest.memory.read if mode == "read" else self.manifest.memory.write
        reason = None if layer in allowed else f"memory.{mode} does not include L{layer}"
        self._decide("memory", f"{mode}:L{layer}", reason)

    def check_content(self, content_class: str, artifact_hash: str | None = None,
                      release: Release | None = None, patient_id: str | None = None) -> None:
        """Release-gated content (diagnosis, results, doses) needs a valid clinician release for this exact artifact."""
        gated = self._gated_content()
        if content_class not in gated:
            return self._decide("content", content_class, None, None, patient_id)
        refusal = gated[content_class]
        if release is None:
            return self._decide("content", content_class, "no clinician release", refusal, patient_id)
        try:
            verify_release(release, self.clinician_keys)
        except SignatureError as exc:
            return self._decide("content", content_class, f"invalid release: {exc}", refusal, patient_id)
        if artifact_hash is None or release.artifact_hash != artifact_hash:
            return self._decide("content", content_class, "release does not cover this artifact", refusal,
                                patient_id)
        if content_class not in release.content_classes:
            return self._decide("content", content_class, "release does not cover this content class", refusal,
                                patient_id)
        self._decide("content", content_class, None, None, patient_id)
