"""Keyword reading of patient messages (English, Italian) for the Companion stand-in.

Port of the demo's humanized/nlu.mjs, with its limits: keyword patterns only, weak on paraphrases and negation
(see evals/datasets/red_flags). Person 2's real Companion replaces it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

IT_HINT = re.compile(r"\b(ho|sono|mi|non|dolore|fianco|che|della|perso|testa|giorno|prendo|cosa|esame|niente|rosate"
                     r"|ieri|stasera|posso|buonasera)\b", re.IGNORECASE)
_SYMPTOMS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("visible_hematuria", "visible blood in urine", re.compile(
        r"sangue nelle urine|urine[^.]{0,20}(rosa|rosate|rosse|scure|sangue)|rosso scuro|ematuria"
        r"|blood in (the |my )?urine|pink(ish)? urine|urine (is|are) (pink|red|dark)|dark red|ha?ematuria")),
    ("dizziness", "dizziness", re.compile(r"gira la testa|giramenti|vertigin|capogir|dizz")),
    ("nausea", "nausea", re.compile(r"nausea|nauseous|vomit")),
    ("fever", "fever / chills", re.compile(r"febbre|fever|brividi|chills")),
    ("fatigue", "fatigue", re.compile(r"stanc|fatigue|exhaust|spossat")),
)
_SITE = re.compile(r"fianco|lombar|schiena|flank|lower back|\bside\b|sotto le costole|under the ribs")
_PAIN = re.compile(r"dolor|male|pain|ache|hurts")
_SCORE = (re.compile(r"\b(\d{1,2})\s*(?:su|/|out of|di)\s*10\b"), re.compile(r"\b(?:direi|say|about|circa)\s+(\d{1,2})\b"))
_WEIGHT = re.compile(r"(?:perso|lost|calato di)\s*(?:circa\s*)?(\d{1,3})\s*(?:kg|chil|kilo)")
_MINIMIZER = re.compile(r"niente di che|un po['’]|a bit|a little|sometimes|a volte|not too bad|nothing serious"
                        r"|sopportabile|non è niente|it['’]s fine|va bene")
_IMAGING = re.compile(r"tac|\bct\b|scan|risonanza|immagin|imaging")
_RESULT = re.compile(r"mostrat|result|esito|risultat|show|uscit|cosa ha|what did|com['’]è andat|referto")
_DNA = re.compile(r"dna|genetic|genetica|gene\b|mutazion")


@dataclass
class Reading:
    symptoms: list[dict[str, str]] = field(default_factory=list)
    pain_score: int | None = None
    weight_loss_kg: int | None = None
    minimizer: bool = False
    asks_imaging: bool = False
    asks_genetics: bool = False
    verbal: float = 0.0
    language: str = "en"


def read_message(text: str) -> Reading:
    t = text.lower()
    r = Reading(language="it" if IT_HINT.search(text) else "en", minimizer=bool(_MINIMIZER.search(t)))
    if _SITE.search(t) and _PAIN.search(t):
        r.symptoms.append({"code": "flank_pain", "label": "flank pain"})
    r.symptoms += [{"code": code, "label": label} for code, label, rx in _SYMPTOMS if rx.search(t)]
    for rx in _SCORE:
        if m := rx.search(t):
            r.pain_score = min(10, int(m.group(1)))
            break
    if m := _WEIGHT.search(t):
        r.weight_loss_kg = int(m.group(1))
        r.symptoms.append({"code": "weight_loss", "label": f"weight loss {r.weight_loss_kg} kg"})
    r.asks_imaging = bool(_IMAGING.search(t) and _RESULT.search(t))
    r.asks_genetics = bool(_DNA.search(t) and ("?" in t or "cosa" in t or "what" in t))
    if r.pain_score is not None:
        r.verbal = r.pain_score / 10
    elif re.search(r"fortissim|insopportabil|terribil|severe|unbearable|atroce", t):
        r.verbal = 0.85
    elif _PAIN.search(t):
        r.verbal = 0.45
    if r.minimizer:
        r.verbal = max(0.0, r.verbal - 0.2)
    return r
