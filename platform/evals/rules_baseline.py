"""Port of the demo's keyword rules, used as the red-flag baseline.

Sources: backend/humanized/nlu.mjs (extract) and backend/knowledge/km.mjs (redFlags).
Kept deliberately faithful to the demo, weaknesses included: this is the floor the
real Companion (rules + language model) has to beat.
"""
from __future__ import annotations

import re

_PAIN_SITE = re.compile(r"(fianco|lombar|schiena|flank|lower back|\bside\b|sotto le costole|under the ribs)")
_PAIN_WORD = re.compile(r"(dolor|male|pain|ache|fa male|hurts)")
_HEMATURIA = re.compile(
    r"(sangue nelle urine|urine[^.]{0,20}(rosa|rosate|rosse|scure|sangue)|rosso scuro|ematuria|"
    r"blood in (the )?urine|pink(ish)? urine|urine (is|are) (pink|red|dark)|dark red|hematuria|haematuria)"
)
_DIZZY = re.compile(r"(gira la testa|giramenti|vertigin|capogir|dizz)")
_NAUSEA = re.compile(r"(nausea|nauseous|vomit)")
_FEVER = re.compile(r"(febbre|fever|brividi|chills)")
_FATIGUE = re.compile(r"(stanc|fatigue|exhaust|spossat)")
_PAIN_SCORE = (
    re.compile(r"\b(\d{1,2})\s*(su|/|out of|di)\s*10\b"),
    re.compile(r"\b(?:direi|say|about|circa)\s+(\d{1,2})\b"),
)
_WEIGHT = re.compile(r"(?:perso|lost|calato di)\s*(?:circa\s*)?(\d{1,3})\s*(kg|chil|kilo)")


def extract(text: str) -> dict:
    t = text.lower()
    symptoms: set[str] = set()
    if _PAIN_SITE.search(t) and _PAIN_WORD.search(t):
        symptoms.add("flank_pain")
    if _HEMATURIA.search(t):
        symptoms.add("visible_hematuria")
    if _DIZZY.search(t):
        symptoms.add("dizziness")
    if _NAUSEA.search(t):
        symptoms.add("nausea")
    if _FEVER.search(t):
        symptoms.add("fever")
    if _FATIGUE.search(t):
        symptoms.add("fatigue")

    pain = None
    for rx in _PAIN_SCORE:
        m = rx.search(t)
        if m:
            pain = min(10, int(m.group(1)))
            break

    weight = None
    m = _WEIGHT.search(t)
    if m:
        weight = int(m.group(1))
        symptoms.add("weight_loss")

    return {"symptoms": symptoms, "pain_score": pain, "weight_loss_kg": weight}


def red_flags(findings: dict, context: dict | None = None) -> dict:
    s = findings["symptoms"]
    pain = findings["pain_score"] or 0
    weight = findings["weight_loss_kg"] or 0
    rcc = bool((context or {}).get("history", {}).get("rcc"))

    evidence = sorted(s)
    core = "visible_hematuria" in s and "flank_pain" in s and pain >= 7
    systemic = weight >= 5 or "dizziness" in s or "fever" in s
    if core and (systemic or rcc):
        return {"urgency": "high", "score": 1.0, "evidence": evidence}
    if "visible_hematuria" in s or ("flank_pain" in s and pain >= 7):
        return {"urgency": "medium", "score": 0.5, "evidence": evidence}
    return {"urgency": "none", "score": 0.0, "evidence": evidence}


def predict(item: dict) -> dict:
    return red_flags(extract(item["text"]), item.get("context"))
