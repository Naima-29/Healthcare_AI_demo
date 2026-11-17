"""The egress gate: the only code path that may send anything to a cloud model.

Principle (ENGINEERING_PLAN, #7): identifiable data never leaves the hospital; the cloud accepts only payloads
the Safety Verifier has stamped as de-identified. The stamp is a signed `EgressApproval` over the exact payload.
The gate sends only when ALL of these hold, and blocks (and audits) otherwise:

  approved decision, signed by the Safety Verifier's key, for this seat and this target, target allowed by the
  seat's manifest (`model.fallback`), seat manifest unchanged since approval, not expired and not valid for
  longer than policy allows, payload hash equals the bytes being sent, none of the known patient identifiers
  appears in the payload (tripwire), and the approval has not been used before.

Fail closed: any unexpected error while checking is a block. The audit trail records hashes and reason codes,
never the payload. An approval is consumed just before sending, so a cloud failure means "ask again", never
"send twice".
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Protocol

import rfc8785

from .audit import AuditSink, content_hash
from .clock import Clock
from .contracts import EgressApproval, Manifest
from .registry import ManifestRegistry
from .signing import KeyRing, SignatureError, Signer, canonical

log = logging.getLogger(__name__)

REASONS = (
    "rejected_by_verifier", "wrong_approver", "bad_signature", "wrong_seat", "wrong_target", "seat_not_allowed_cloud",
    "stale_manifest", "not_yet_valid", "expired", "ttl_too_long", "payload_changed", "identifier_in_payload",
    "already_used", "internal_error",
)


class EgressBlocked(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class CloudClient(Protocol):
    async def send(self, target: str, payload: dict[str, Any]) -> Any: ...


class ApprovalUseStore(Protocol):
    def consume(self, approval_id: str, expires_at: datetime) -> bool:
        """Atomically record the approval as used. True only for the first caller."""
        ...


class InMemoryApprovalUses:
    def __init__(self) -> None:
        self.used: dict[str, datetime] = {}

    def consume(self, approval_id: str, expires_at: datetime) -> bool:
        if approval_id in self.used:
            return False
        self.used[approval_id] = expires_at
        return True


def sign_approval(approval: EgressApproval, signer: Signer) -> EgressApproval:
    if signer.key_id != approval.approved_by:
        raise SignatureError(f"signer '{signer.key_id}' cannot sign as '{approval.approved_by}'")
    return approval.model_copy(update={"sig": signer.sign_bytes(canonical(approval.model_dump(mode="json")))})


def verify_approval(approval: EgressApproval, keyring: KeyRing) -> None:
    keyring.verify_bytes(approval.approved_by, canonical(approval.model_dump(mode="json")), approval.sig)


def cloud_target_of(manifest: Manifest) -> str | None:
    """The cloud id a seat may use. `model.fallback` may carry a human note ("kimi (de-identified only)");
    the id is the part before it."""
    fallback = manifest.model.fallback
    return fallback.split(" (")[0].strip() if fallback else None


class EgressGate:
    def __init__(
        self,
        *,
        registry: ManifestRegistry,
        keyring: KeyRing,
        client: CloudClient,
        uses: ApprovalUseStore,
        audit: AuditSink,
        clock: Clock,
        approver: str = "safety_verifier",
        max_ttl: timedelta = timedelta(minutes=10),
        clock_skew: timedelta = timedelta(seconds=30),
    ) -> None:
        self.registry, self.keyring, self.client, self.uses = registry, keyring, client, uses
        self.audit, self.clock, self.approver = audit, clock, approver
        self.max_ttl, self.skew = max_ttl, clock_skew

    async def send(
        self,
        *,
        seat: str,
        target: str,
        payload: dict[str, Any],
        approval: EgressApproval,
        known_identifiers: Collection[str] = (),
    ) -> Any:
        """Send `payload` to `target` for `seat`, or raise EgressBlocked. `known_identifiers` are values that must
        never appear (the patient id, name, record number...): a last tripwire, not a substitute for anonymisation."""
        try:
            self._check(seat, target, payload, approval, known_identifiers)
            if not self.uses.consume(approval.id, approval.expires_at):
                raise EgressBlocked("already_used")
        except EgressBlocked as blocked:
            self._audit(seat, "egress.blocked", "blocked", blocked.reason, approval, target)
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
            log.exception("egress check failed unexpectedly")
            self._audit(seat, "egress.blocked", "blocked", "internal_error", approval, target)
            raise EgressBlocked("internal_error") from exc
        try:
            result = await self.client.send(target, payload)
        except Exception:
            self._audit(seat, "egress.failed", "error", "cloud_error", approval, target)
            raise
        self._audit(seat, "egress.sent", "allowed", "ok", approval, target)
        return result

    # ------------------------------------------------------------------------------------------------------

    def _check(self, seat: str, target: str, payload: dict[str, Any], approval: EgressApproval,
               known_identifiers: Collection[str]) -> None:
        if approval.decision != "approved":
            raise EgressBlocked("rejected_by_verifier", approval.reason or "")
        if approval.approved_by != self.approver:
            raise EgressBlocked("wrong_approver")
        try:
            verify_approval(approval, self.keyring)
        except SignatureError as exc:
            raise EgressBlocked("bad_signature") from exc
        if approval.requesting_seat != seat:
            raise EgressBlocked("wrong_seat")
        if approval.target != target:
            raise EgressBlocked("wrong_target")
        manifest = self.registry.get(seat) if seat in self.registry.seats() else None
        if manifest is None or cloud_target_of(manifest) != target:
            raise EgressBlocked("seat_not_allowed_cloud")
        if approval.manifest_version != manifest.ref:
            raise EgressBlocked("stale_manifest")
        now = self.clock.now()
        if now < approval.issued_at - self.skew:
            raise EgressBlocked("not_yet_valid")
        if now >= approval.expires_at:
            raise EgressBlocked("expired")
        if approval.expires_at - approval.issued_at > self.max_ttl:
            raise EgressBlocked("ttl_too_long")
        if content_hash(payload) != approval.payload_hash:
            raise EgressBlocked("payload_changed")
        text = rfc8785.dumps(payload).decode().lower()
        if any(len(i) >= 3 and i.lower() in text for i in known_identifiers):
            raise EgressBlocked("identifier_in_payload")

    def _audit(self, seat: str, action: str, decision: str, reason: str, approval: EgressApproval,
               target: str) -> None:
        self.audit.record(
            ts=self.clock.now(), actor=seat, action=action, decision=decision,  # type: ignore[arg-type]
            detail={"reason": reason, "approval_id": approval.id, "payload_hash": approval.payload_hash,
                    "target": target},
        )
