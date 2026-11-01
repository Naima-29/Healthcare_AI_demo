"""Every refusal in the catalogue gets a test proving it blocks, for every seat that holds it."""

import pytest
from kcd_core.audit import InMemoryAudit
from kcd_core.clock import FakeClock
from kcd_core.contracts import MessageType, PolicyCatalog
from kcd_core.pep import PolicyEnforcementPoint, PolicyViolation
from kcd_core.registry import ManifestRegistry
from kcd_core.signing import KeyRing, Signer, sign_release

from .conftest import CATALOG_PATH, MANIFEST_DIR, sha

CATALOG = PolicyCatalog.load(CATALOG_PATH)
REGISTRY = ManifestRegistry.from_dir(CATALOG, MANIFEST_DIR)
CLINICIAN = Signer.generate("dr_rossi")


def pep_for(seat: str, manifest=None) -> tuple[PolicyEnforcementPoint, InMemoryAudit]:
    audit = InMemoryAudit()
    m = manifest or REGISTRY.get(seat)
    return PolicyEnforcementPoint(lambda: m, CATALOG, audit, FakeClock(),
                                  KeyRing({"dr_rossi": CLINICIAN.public_key})), audit


TOOL_CASES = [
    (seat, rid, tool)
    for seat in REGISTRY.seats()
    for rid in REGISTRY.get(seat).refuses
    for tool in CATALOG.refusals[rid].blocked_tools
]
CONTENT_CASES = [
    (seat, rid, cls)
    for seat in REGISTRY.seats()
    for rid in REGISTRY.get(seat).refuses
    for cls in CATALOG.refusals[rid].release_gated_content
]


def test_every_refusal_is_exercised():
    covered = {rid for _, rid, _ in TOOL_CASES} | {rid for _, rid, _ in CONTENT_CASES}
    assert covered == set(CATALOG.refusals), "a refusal no seat holds, or one with nothing to block"


@pytest.mark.parametrize(("seat", "refusal", "tool"), TOOL_CASES)
def test_refused_tool_is_blocked_even_if_listed(seat, refusal, tool):
    # Put the tool in the manifest on purpose: the refusal must still win (safety floor by union).
    m = REGISTRY.get(seat)
    tampered = m.model_copy(update={"tools": [*m.tools, tool], "surfaces": list(CATALOG.surfaces)})
    pep, audit = pep_for(seat, tampered)
    with pytest.raises(PolicyViolation) as err:
        pep.check_tool(tool)
    assert err.value.refusal == refusal
    assert tool not in pep.visible_tools()
    assert audit.find("pep.tool", "blocked")


@pytest.mark.parametrize(("seat", "refusal", "cls"), CONTENT_CASES)
def test_gated_content_without_release_is_blocked(seat, refusal, cls):
    pep, _ = pep_for(seat)
    with pytest.raises(PolicyViolation) as err:
        pep.check_content(cls, artifact_hash=sha("x"))
    assert err.value.refusal == refusal


def _release(artifact_hash, classes, signer=CLINICIAN):
    from datetime import UTC, datetime

    from kcd_core.contracts import Release

    r = Release(id="rel_1", artifact_id="cp_1", artifact_hash=artifact_hash, clinician_id="dr_rossi",
                role="clinician:nephrology", content_classes=classes, signed_at=datetime.now(UTC))
    return sign_release(r, signer) if signer is CLINICIAN else r.model_copy(update={"sig": signer.sign_bytes(b"x")})


def test_released_content_passes_for_matching_artifact():
    pep, _ = pep_for("companion")
    h = sha("case pack v2")
    pep.check_content("diagnosis", artifact_hash=h, release=_release(h, ["diagnosis"]))


def test_release_for_another_artifact_is_blocked():
    pep, _ = pep_for("companion")
    with pytest.raises(PolicyViolation, match="does not cover this artifact"):
        pep.check_content("diagnosis", artifact_hash=sha("v3"), release=_release(sha("v2"), ["diagnosis"]))


def test_release_for_another_content_class_is_blocked():
    pep, _ = pep_for("companion")
    h = sha("v2")
    with pytest.raises(PolicyViolation, match="content class"):
        pep.check_content("dose", artifact_hash=h, release=_release(h, ["diagnosis"]))


def test_forged_release_is_blocked():
    pep, _ = pep_for("companion")
    h = sha("v2")
    with pytest.raises(PolicyViolation, match="invalid release"):
        pep.check_content("diagnosis", artifact_hash=h, release=_release(h, ["diagnosis"], Signer.generate("x")))


def test_unlisted_tool_is_blocked():
    pep, _ = pep_for("renal_guardian")
    with pytest.raises(PolicyViolation, match="not in manifest"):
        pep.check_tool("pacs.fetch_study")


def test_allowed_tool_passes_and_is_audited():
    pep, audit = pep_for("renal_guardian")
    pep.check_tool("fhir.read_observations")
    assert audit.find("pep.tool", "allowed")


def test_memory_scope():
    pep, _ = pep_for("imaging_reader")
    pep.check_memory("L1", "read")
    with pytest.raises(PolicyViolation):
        pep.check_memory("L2", "read")
    with pytest.raises(PolicyViolation):
        pep.check_memory("L3", "write")


def test_emit_to_wrong_peer_is_blocked():
    pep, _ = pep_for("companion")
    pep.check_emit(MessageType.SYMPTOM_REPORT, "renal_guardian")
    with pytest.raises(PolicyViolation, match="peer"):
        pep.check_emit(MessageType.SYMPTOM_REPORT, "imaging_reader")
    with pytest.raises(PolicyViolation, match="emits"):
        pep.check_emit(MessageType.CLINICIAN_DECISION, "renal_guardian")
