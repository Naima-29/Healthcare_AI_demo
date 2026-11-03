import json
import subprocess
import sys

import pytest
from kcd_core.contracts import PAYLOADS, Manifest, MemoryScopes, MessageType
from kcd_core.registry import ManifestError, ManifestRegistry, check_against_catalog
from pydantic import ValidationError

from .conftest import FIXTURES, MANIFEST_DIR, ROOT

DEMO = ROOT / "demo" / "kidney-care-desk"


def test_exactly_ten_message_types():
    assert len(MessageType) == 10
    assert set(PAYLOADS) == set(MessageType)


def test_message_types_match_the_demo():
    src = (DEMO / "contracts" / "catalog.mjs").read_text(encoding="utf-8")
    block = src[src.index("A2A_TYPES"):src.index("SEAT_IDS")]
    assert all(f"'{t.value}'" in block for t in MessageType)


@pytest.mark.parametrize("type_", list(MessageType), ids=str)
def test_golden_fixtures(type_):
    fx = json.loads((FIXTURES / f"{type_.value}.json").read_text(encoding="utf-8"))
    assert fx["valid"] and fx["invalid"], "every type needs valid and invalid fixtures"
    for good in fx["valid"]:
        PAYLOADS[type_].model_validate(good)
    for bad in fx["invalid"]:
        with pytest.raises(ValidationError):
            PAYLOADS[type_].model_validate(bad)


def test_schemas_are_up_to_date():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_schemas.py"), "--check"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_seed_manifests_load(registry: ManifestRegistry):
    assert len(registry.seats()) == len(list(MANIFEST_DIR.glob("*.manifest.json"))) == 9


def test_seed_manifests_match_the_demo_import():
    """seats/manifests is generated from the demo; rerun scripts/import_demo_manifests.py if this fails."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from import_demo_manifests import DEMO_SEAT_IDS, DEMO_SEATS, build
    finally:
        sys.path.pop(0)
    for seat in DEMO_SEAT_IDS:
        demo = json.loads((DEMO_SEATS / f"{seat}.manifest.json").read_text(encoding="utf-8"))
        ours = json.loads((MANIFEST_DIR / f"{seat}.manifest.json").read_text(encoding="utf-8"))
        assert build(demo) == ours, seat


def test_only_the_companion_can_reach_the_patient(registry):
    for seat in registry.seats():
        m = registry.get(seat)
        if seat == "companion":
            assert "channel" in m.surfaces
        else:
            assert "channel" not in m.surfaces and "channel.send" not in m.tools, seat


def test_companion_never_discloses_imaging_before_review(registry):
    assert "disclose_imaging_before_review" in registry.get("companion").refuses


def test_manifest_with_unknown_tool_is_rejected(registry, catalog):
    bad = registry.get("companion").model_copy(update={"tools": ["fhir.drop_database"]})
    with pytest.raises(ManifestError, match="unknown tools"):
        check_against_catalog(bad, catalog)


def test_tool_without_its_surface_is_rejected(registry, catalog):
    bad = registry.get("renal_guardian").model_copy(update={"tools": ["channel.send"]})
    with pytest.raises(ManifestError, match="needs surface"):
        check_against_catalog(bad, catalog)


def test_manifest_rejects_extra_fields():
    body = json.loads((MANIFEST_DIR / "companion.manifest.json").read_text(encoding="utf-8"))
    body["can_do_anything"] = True
    with pytest.raises(ValidationError):
        Manifest.model_validate(body)


def test_licensed_memory_layers_are_read_only():
    with pytest.raises(ValidationError, match="read-only"):
        MemoryScopes(read=[0, 1, 3], write=[1])
    with pytest.raises(ValidationError, match="without reading"):
        MemoryScopes(read=[0], write=[3])
