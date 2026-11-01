"""Chief of Staff safety floor. The first tests are the demo's compiler tests, ported."""

import pytest
from kcd_core.chief_of_staff import BASE_FLOOR, amend, compile_new, enforce_floor, rule_refusals
from kcd_core.clock import FakeClock
from kcd_core.pep import PolicyViolation

from .test_pep import pep_for

NOW = FakeClock().now()


@pytest.fixture
def companion_v1(registry):
    # The demo seed, before the Director adds the imaging refusal live (golden path step 1).
    m = registry.get("companion")
    return m.model_copy(update={"refuses": [r for r in m.refuses if r != "disclose_imaging_before_review"]})


@pytest.mark.parametrize("sentence", [
    "non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.",
    "Never disclose an imaging result to the patient before the doctor has reviewed it.",
])
def test_amend_turns_a_sentence_into_a_hard_block(companion_v1, catalog, sentence):
    draft = amend(companion_v1, sentence, catalog, now=NOW)
    assert draft.diff.refuses == ["disclose_imaging_before_review"]
    assert draft.manifest.version == companion_v1.version + 1
    assert draft.manifest.compiled_by == "chief_of_staff"
    assert draft.understood


def test_french_and_other_refusals():
    french = "Ne jamais communiquer le résultat de l’imagerie au patient."
    assert "disclose_imaging_before_review" in rule_refusals(french)
    assert "label_as_diagnosis" in rule_refusals("Never call it a diagnosis.")


def test_a_prohibition_never_grants_tools(companion_v1, catalog):
    draft = amend(companion_v1, "Never send the CT scan to the patient on Telegram.", catalog, now=NOW)
    assert draft.diff.tools == []
    assert "pacs.fetch_study" not in draft.manifest.tools


def test_new_seats_inherit_the_clinical_safety_floor(catalog):
    draft = compile_new(
        "Crea un agente per il follow-up telefonico di nefrologia: chiama via WhatsApp i pazienti con CKD e "
        "raccoglie la pressione dagli indossabili. Ogni consiglio deve essere approvato dal nefrologo.",
        catalog, now=NOW,
    )
    m = draft.manifest
    assert set(BASE_FLOOR) <= set(m.refuses)
    assert "nephrologist_review" in m.gates
    assert "wearables.read" in m.tools and "wearables" in m.surfaces
    assert m.version == 1 and m.a2a_peers == []  # no routes until someone adds them


def test_hospital_seat_without_channel_cannot_message_the_patient(catalog):
    draft = compile_new("Create an agent for lab follow-up: read the lab system and the FHIR record.", catalog, now=NOW)
    assert "send_to_patient" in draft.manifest.refuses


def test_model_draft_cannot_drop_refusals_or_gates(registry, catalog):
    live = registry.get("renal_guardian")
    # A model "helpfully" proposes a version with no refusals and an automatic gate.
    proposed = live.model_copy(update={"refuses": [], "gates": ["none"]})
    merged = enforce_floor(proposed, live.job_description, catalog, previous=live)
    assert set(live.refuses) <= set(merged.refuses)
    assert set(BASE_FLOOR) <= set(merged.refuses)
    assert "nephrologist_review" in merged.gates and "none" not in merged.gates
    assert merged.version == live.version + 1


def test_model_draft_with_unknown_tool_is_rejected(registry, catalog):
    from kcd_core.registry import ManifestError

    live = registry.get("renal_guardian")
    with pytest.raises(ManifestError, match="unknown tools"):
        enforce_floor(live.model_copy(update={"tools": ["shell.exec"]}), "", catalog, previous=live)


def test_chief_of_staff_cannot_activate_a_manifest():
    pep, _ = pep_for("chief_of_staff")
    pep.check_tool("control_plane.propose_manifest")
    with pytest.raises(PolicyViolation) as err:
        pep.check_action("manifest.activate")
    assert err.value.refusal == "activate_without_approval"
