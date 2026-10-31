"""Human verification for Cortex backed by kcd_core clinician releases (contract C5).

    cortex = Cortex(url, human_verifier=release_verifier(clinician_keys))

A confirm / reject / re-verify / retire / erase is allowed only when the actor carries a
kcd_core Release that is signed by them (Ed25519, kcd_core.signing), names their role, and
covers this exact target: the rule id, or "erase:<patient id>" for an erasure.
"""

from __future__ import annotations

from collections.abc import Iterable

from kcd_core.contracts import Release
from kcd_core.signing import KeyRing, SignatureError, verify_release

from kcd.cortex.store import HumanActor, HumanVerifier, is_human_role


def release_verifier(clinician_keys: KeyRing, roles: Iterable[str] | None = None) -> HumanVerifier:
    allowed = frozenset(roles) if roles is not None else None

    def verify(actor: HumanActor, target: str) -> bool:
        if not isinstance(actor, HumanActor) or actor.id.startswith("seat:"):
            return False
        release = actor.release
        if not isinstance(release, Release):
            return False
        if release.clinician_id != actor.id or release.role != actor.role or release.artifact_id != target:
            return False
        if not is_human_role(actor.role) or (allowed is not None and actor.role not in allowed):
            return False
        try:
            verify_release(release, clinician_keys)
        except SignatureError:
            return False
        return True

    return verify
