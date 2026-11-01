import json
import subprocess
import sys

import pytest
from kcd_core.contracts import PAYLOADS, Manifest, MessageType
from kcd_core.registry import ManifestError, ManifestRegistry, check_against_catalog
from pydantic import ValidationError

from .conftest import FIXTURES, MANIFEST_DIR, ROOT


def test_exactly_ten_message_types():
    assert len(MessageType) == 10
    assert set(PAYLOADS) == set(MessageType)


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


def test_manifest_with_unknown_tool_is_rejected(registry, catalog):
    bad = registry.get("companion").model_copy(update={"tools": ["fhir.drop_database"]})
    with pytest.raises(ManifestError, match="unknown tools"):
        check_against_catalog(bad, catalog)


def test_tool_on_ungranted_surface_is_rejected(registry, catalog):
    bad = registry.get("renal_guardian").model_copy(update={"tools": ["channel.send_message"]})
    with pytest.raises(ManifestError, match="surface"):
        check_against_catalog(bad, catalog)


def test_manifest_rejects_extra_fields():
    body = json.loads((MANIFEST_DIR / "companion.manifest.json").read_text(encoding="utf-8"))
    body["can_do_anything"] = True
    with pytest.raises(ValidationError):
        Manifest.model_validate(body)


def test_every_seat_except_companion_refuses_patient_contact(registry):
    for seat in registry.seats():
        if seat != "companion":
            assert "no_patient_contact" in registry.get(seat).refuses, seat


def test_every_seat_holds_the_clinical_refusals(registry):
    floor = {"no_diagnosis", "no_prescribing", "no_result_disclosure", "no_identifiable_data_to_cloud"}
    for seat in registry.seats():
        assert floor <= set(registry.get(seat).refuses), seat
