"""Every refusal in the catalogue gets a test proving it blocks, for every seat that holds it."""

from datetime import UTC, datetime

import pytest
from kcd_core.audit import InMemoryAudit
from kcd_core.clock import FakeClock
from kcd_core.contracts import MessageType, PolicyCatalog, Release
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


HELD = [(seat, rid) for seat in REGISTRY.seats() for rid in REGISTRY.get(seat).refuses]
ACTION_CASES = [(seat, rid, a) for seat, rid in HELD for a in CATALOG.refusals[rid].actions]
CONTENT_CASES = [(seat, rid, c) for seat, rid in HELD for c in CATALOG.refusals[rid].content]
TOOL_CASES = [
    (seat, rid, tool)
    for seat, rid in HELD
    for tool, actions in CATALOG.tool_actions.items()
    if set(actions) & set(CATALOG.refusals[rid].actions)
]


def test_every_refusal_blocks_something():
    for rid, refusal in CATALOG.refusals.items():
        assert refusal.actions or refusal.content, rid


@pytest.mark.parametrize(("seat", "refusal", "action"), ACTION_CASES)
def test_refused_action_is_blocked(seat, refusal, action):
    pep, audit = pep_for(seat)
    with pytest.raises(PolicyViolation) as err:
        pep.check_action(action)
    assert err.value.refusal in {r for r in REGISTRY.get(seat).refuses if action in CATALOG.refusals[r].actions}
    assert audit.find("pep.action", "blocked")


@pytest.mark.parametrize(("seat", "refusal", "tool"), TOOL_CASES)
def test_tool_performing_a_refused_action_is_blocked_even_if_listed(seat, refusal, tool):
    # Put the tool and its surface in the manifest on purpose: the refusal must still win.
    m = REGISTRY.get(seat)
    tampered = m.model_copy(update={"tools": [*m.tools, tool], "surfaces": list(CATALOG.surfaces)})
    pep, _ = pep_for(seat, tampered)
    with pytest.raises(PolicyViolation, match="refused by policy"):
        pep.check_tool(tool)
    assert tool not in pep.visible_tools()


@pytest.mark.parametrize(("seat", "refusal", "cls"), CONTENT_CASES)
def test_gated_content_without_release_is_blocked(seat, refusal, cls):
    pep, _ = pep_for(seat)
    with pytest.raises(PolicyViolation, match="no clinician release"):
        pep.check_content(cls, artifact_hash=sha("x"))


def test_unrefused_action_is_allowed():
    pep, audit = pep_for("companion")
    pep.check_action("routing.rank_people")  # the Companion does not hold rank_clinicians
    assert audit.find("pep.action", "allowed")


def _release(artifact_hash, classes, signer=CLINICIAN):
    r = Release(id="dec-1", artifact_id="case-1", artifact_hash=artifact_hash, clinician_id="dr_rossi",
                role="treating_clinician", content_classes=classes, signed_at=datetime.now(UTC))
    return sign_release(r, signer) if signer is CLINICIAN else r.model_copy(update={"sig": signer.sign_bytes(b"x")})


def test_released_content_passes_for_matching_artifact():
    pep, _ = pep_for("companion")
    h = sha("case pack v2")
    pep.check_content("imaging_result", artifact_hash=h, release=_release(h, ["imaging_result"]))


def test_release_for_another_artifact_is_blocked():
    pep, _ = pep_for("companion")
    with pytest.raises(PolicyViolation, match="does not cover this artifact"):
        pep.check_content("imaging_result", artifact_hash=sha("v3"), release=_release(sha("v2"), ["imaging_result"]))


def test_release_for_another_content_class_is_blocked():
    # The demo releases every content class on any signature; here a release names what it unlocks.
    pep, _ = pep_for("companion")
    h = sha("v2")
    with pytest.raises(PolicyViolation, match="content class"):
        pep.check_content("dose_instruction", artifact_hash=h, release=_release(h, ["imaging_result"]))


def test_forged_release_is_blocked():
    pep, _ = pep_for("companion")
    h = sha("v2")
    with pytest.raises(PolicyViolation, match="invalid release"):
        forged = _release(h, ["imaging_result"], Signer.generate("x"))
        pep.check_content("imaging_result", artifact_hash=h, release=forged)


def test_unlisted_tool_is_blocked():
    pep, _ = pep_for("renal_guardian")
    with pytest.raises(PolicyViolation, match="not in manifest"):
        pep.check_tool("pacs.fetch_study")


def test_allowed_tool_passes_and_is_audited():
    pep, audit = pep_for("renal_guardian")
    pep.check_tool("fhir.get_observations")
    assert audit.find("pep.tool", "allowed")


def test_memory_scope():
    pep, _ = pep_for("renal_guardian")  # demo seed: read 0-3, write 3
    pep.check_memory(2, "read")
    pep.check_memory(3, "write")
    with pytest.raises(PolicyViolation):
        pep.check_memory(2, "write")


def test_emit_to_wrong_peer_is_blocked():
    pep, _ = pep_for("companion")
    pep.check_emit(MessageType.SYMPTOM_REPORT, "case_briefer")
    with pytest.raises(PolicyViolation, match="peer"):
        pep.check_emit(MessageType.SYMPTOM_REPORT, "imaging_reader")
    with pytest.raises(PolicyViolation, match="emits"):
        pep.check_emit(MessageType.CLINICIAN_DECISION, "case_briefer")
