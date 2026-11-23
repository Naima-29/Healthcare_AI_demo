"""Shared classification of release-gated patient-facing content."""

from __future__ import annotations

import re
from collections.abc import Iterable

from kcd.textguard import is_diagnosis_claim

# ---------------------------------------------------------------------- patient-bound text
_I = re.IGNORECASE
_MED_WORDS = (r"medicines?|medications?|tablets?|pills?|capsules?|doses?|dosage|painkillers?|anti-?inflammator\w*"
              r"|farmac\w*|medicin\w*|compress\w*|pastigli\w*|capsul\w*|dosaggio|antidolorific\w*"
              r"|antinfiammator\w*")
_DOSE_VERBS = (r"take|taking|stop|start|increase|reduce|lower|double|halve|skip|avoid"
               r"|prend\w*|assum\w*|smett\w*|sospend\w*|interromp\w*|aument\w*|ridu\w*|raddopp\w*|evit\w*|discontinu\w*")
_DOSE_AMOUNT = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:mg|mcg|µg|g|ml|iu|ui|units?|unità|tablets?|pills?|capsules?"
                          r"|compress[ae]|pastigli[ae]|gocce|drops)\b", _I)
_DOSE_FREQUENCY = re.compile(r"\b(?:once|twice|three times|\d+ times)\s+(?:a|per)\s+day\b|\bevery\s+\d+\s+hours\b"
                             r"|\bvolt[ae]\s+al\s+giorno\b|\bogni\s+\d+\s+ore\b", _I)
# "stop taking ..." is an instruction whatever the drug, even one the Knowledge Model does not know
_TAKING_INSTRUCTION = re.compile(r"\b(?:stop|start|keep|continue)\s+taking\b"
                                 r"|\b(?:smett|inizi|continu)\w*\s+(?:di|a)\s+prendere\b", _I)
_PATIENT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "diagnosis_claim": [
        re.compile(r"\byou\s+have\s+(?:a\s+|an\s+)?(?:\w+\s+){0,2}(?:cancer|tumou?r|carcinoma|malignan\w*)", _I),
        re.compile(r"\bdiagnosi\s+(?:di|è|:)|\bdiagnosticat\w*|\bha\s+(?:un|una)\s+(?:tumore|cancro|carcinoma)"
                   r"|\b(?:è|si\s+tratta\s+di)\s+(?:un|una)\s+(?:tumore|cancro|carcinoma|neoplasia)"
                   r"|\bcompatibile\s+con\s+(?:un\s+|una\s+)?(?:tumore|cancro|carcinoma|neoplasia)", _I),
    ],
    "imaging_result": [
        re.compile(r"\b(?:scan|ct|mri|x-?ray|ultrasound|images?|tac|risonanza|ecografia|immagin\w*|lastr\w*)\b"
                   r"[^.;:]{0,30}\b(?:shows?|showed|found|revealed|detected|is\s+(?:normal|clear)|mostra\w*"
                   r"|evidenzi\w*|rilev\w*|ha\s+trovato|è\s+normale)\b", _I),
        re.compile(r"\b(?:mass|lesion|tumou?r|nodule|cyst|massa|lesione|nodulo|cisti)\b[^.;:]{0,20}"
                   r"\b\d+(?:[.,]\d+)?\s?(?:mm|cm)\b", _I),
        re.compile(r"\b(?:probabilit\w*|likelihood|confidence|heat\s?map|mappa\s+di\s+calore)\b", _I),
    ],
    "prescription": [re.compile(r"\b(?:prescrib\w*|prescription|prescriv\w*|ricetta)\b", _I)],
    "genetic_result": [
        re.compile(r"\b(?:pathogenic|variants?|mutations?|carrier|patogen\w*|variant[ei]|mutazion[ei]"
                   r"|portat(?:ore|rice)|VHL|APOL1|BRCA\d?)\b", _I),
    ],
}


def patient_content_classes(text: str | None, drug_names: Iterable[str] = ()) -> set[str]:
    """The release-gated content classes in a message to the patient (empty set: safe without a release).

    drug_names (e.g. the Knowledge Model's names) let "avoid ibuprofen" count as a dose instruction too.
    """
    text = text or ""
    found = {cls for cls, patterns in _PATIENT_PATTERNS.items() if any(p.search(text) for p in patterns)}
    if is_diagnosis_claim(text):
        found.add("diagnosis_claim")
    objects = _MED_WORDS + "".join("|" + re.escape(n) for n in drug_names)
    instruction = re.compile(rf"\b(?:{_DOSE_VERBS})\b[^.;:]{{0,40}}?\b(?:{objects})\b", _I)
    passive_instruction = re.compile(
        rf"\b(?:{objects})\b[^.;:]{{0,40}}\b(?:should|must|need to|needs to|have to|has to)\s+"
        rf"(?:be\s+)?(?:stopped|discontinued|avoided)\b",
        _I,
    )
    passive_instruction_it = re.compile(
        rf"\b(?:{objects}|terapia)\b[^.;:]{{0,40}}\b(?:deve|devono|dovrebbe|dovrebbero|va|vanno)\s+(?:essere\s+)?(?:sospes[oaie]|interrott[oaie]|evitat[oaie])\b",
        _I,
    )
    if any(
        p.search(text)
        for p in (
            instruction,
            passive_instruction,
            passive_instruction_it,
            _DOSE_AMOUNT,
            _DOSE_FREQUENCY,
            _TAKING_INSTRUCTION,
        )
    ):
        found.add("dose_instruction")
    return found
