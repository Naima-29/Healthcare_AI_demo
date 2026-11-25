"""Deterministic safety-floor NLU for patient messages (English and Italian).

This is a deliberately small rule layer ported from the v2 demo. It is not a
general-purpose clinical language model; a future edge SLM may add findings,
but must never remove deterministic findings.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

IT_HINT = re.compile(
    r"\b(ho|sono|mi|non|dolore|fianco|che|della|perso|testa|giorno|prendo|cosa|esame|"
    r"niente|rosate|ieri|stasera|posso|buonasera)\b",
    re.IGNORECASE,
)

MINIMIZER = re.compile(
    r"(niente di che|un po'|un po’|a bit|a little|sometimes|a volte|not too bad|"
    r"nothing serious|sopportabile|non è niente|it's fine|va bene)",
    re.IGNORECASE,
)

def detect_language(text: str) -> str:
    return "it" if IT_HINT.search(text or "") else "en"


def extract(text: str, classify_drug: Callable[[str], dict[str, str] | None] | None = None) -> dict[str, Any]:
    """Extract a conservative subset of symptoms, pain, weight loss, medication and intent."""
    t = (text or "").lower()
    symptoms: list[dict[str, Any]] = []

    def add(code: str, label: str) -> None:
        if not any(item["code"] == code for item in symptoms):
            symptoms.append({"code": code, "label": label, "source_text": text[:500]})

    if (re.search(r"(fianco|lombar|schiena|flank|lower back|\bside\b|sotto le costole|under the ribs)", t)
            and re.search(r"(dolor|male|pain|ache|fa male|hurts)", t)):
        add("flank_pain", "flank pain")
    if re.search(
        r"(sangue nelle urine|urine[^.]{0,20}(rosa|rosate|rosse|scure|sangue)|rosso scuro|ematuria|"
        r"blood in (the |my )?urine|pink(ish)? urine|urine (is|are) (pink|red|dark)|dark red|hematuria|haematuria)",
        t,
    ):
        add("visible_hematuria", "visible blood in urine")
    if re.search(r"(gira la testa|giramenti|vertigin|capogir|dizz)", t):
        add("dizziness", "dizziness")
    if re.search(r"(nausea|nauseous|vomit)", t):
        add("nausea", "nausea")
    if re.search(r"(febbre|fever|brividi|chills)", t):
        add("fever", "fever / chills")
    if re.search(r"(stanc|fatigue|exhaust|spossat)", t):
        add("fatigue", "fatigue")

    pain_match = re.search(r"\b(\d{1,2})\s*(su|/|out of|di)\s*10\b", t)
    if pain_match is None:
        pain_match = re.search(r"\b(?:direi|say|about|circa)\s+(\d{1,2})\b", t)
    pain_score = min(10, int(pain_match.group(1))) if pain_match else None

    weight_match = re.search(r"(?:perso|lost|calato di)\s*(?:circa\s*)?(\d{1,3})\s*(kg|chil|kilo)", t)
    weight_loss_kg = int(weight_match.group(1)) if weight_match else None
    if weight_loss_kg is not None:
        add("weight_loss", f"weight loss {weight_loss_kg} kg")

    minimizer = bool(MINIMIZER.search(t))
    drug = classify_drug(text) if classify_drug else None
    intents: list[str] = []
    if re.search(r"(tac|\bct\b|scan|risonanza|immagin|imaging)", t) and re.search(
        r"(mostrat|result|esito|risultat|show|uscit|cosa ha|what did|com'è andat|referto)", t
    ):
        intents.append("ask_imaging_result")
    if re.search(r"(dna|genetic|genetica|gene\b|mutazion)", t) and re.search(r"\?|cosa|what", t):
        intents.append("ask_genetics")
    if drug:
        intents.append("ask_medication" if re.search(r"(posso|can i|devo|should i)", t) else "report_medication")

    verbal = 0.0
    if pain_score is not None:
        verbal = pain_score / 10
    elif re.search(r"(fortissim|insopportabil|terribil|severe|unbearable|atroce)", t):
        verbal = 0.85
    elif re.search(r"(dolor|pain|male)", t):
        verbal = 0.45
    if minimizer:
        verbal = max(0.0, verbal - 0.2)

    return {
        "symptoms": symptoms,
        "pain_score": pain_score,
        "weight_loss_kg": weight_loss_kg,
        "minimizer": minimizer,
        "drug": drug,
        "intents": intents,
        "verbal": verbal,
        "language": detect_language(text),
    }
