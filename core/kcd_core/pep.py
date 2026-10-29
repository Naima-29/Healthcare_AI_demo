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

    def _refused_tools(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rid in self.manifest.refuses:
            for tool in self.catalog.refusals[rid].blocked_tools:
                out[tool] = rid
        return out

    def _gated_content(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rid in self.manifest.refuses:
            for cls in self.catalog.refusals[rid].release_gated_content:
                out[cls] = rid
        return out

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
        refused = self._refused_tools()
        return [t for t in self.manifest.tools if t in self.catalog.tools and t not in refused]

    def check_tool(self, tool: str, patient_id: str | None = None) -> None:
        refused = self._refused_tools()
        if tool in refused:  # a refusal wins even if the tool is also listed: safety floor by union
            return self._decide("tool", tool, "refused by policy", refused[tool], patient_id)
        if tool not in self.manifest.tools:
            return self._decide("tool", tool, "not in manifest", None, patient_id)
        if tool not in self.catalog.tools:
            return self._decide("tool", tool, "not in catalogue", None, patient_id)
        surface = self.catalog.tools[tool].surface
        if surface and surface not in self.manifest.surfaces:
            return self._decide("tool", tool, f"surface '{surface}' not granted", None, patient_id)
        self._decide("tool", tool, None, None, patient_id)

    def check_emit(self, type_: MessageType, to: str) -> None:
        if type_ not in self.manifest.emits:
            return self._decide("emit", str(type_), "type not in manifest.emits")
        if to not in self.manifest.a2a_peers:
            return self._decide("emit", f"{type_}->{to}", f"peer '{to}' not in manifest.a2a_peers")
        self._decide("emit", f"{type_}->{to}", None)

    def check_memory(self, layer: MemoryLayer, mode: str) -> None:
        allowed = self.manifest.memory.read if mode == "read" else self.manifest.memory.write
        reason = None if layer in allowed else f"memory.{mode} does not include {layer}"
        self._decide("memory", f"{mode}:{layer}", reason)

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
