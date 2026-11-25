"""Detects text that would turn a model signal into a diagnosis.

Seats run their own free text through this and, on a hit, ask the PEP for the release-gated
content class "diagnosis" (ctx.guard_content). Without a clinician release the PEP blocks it.
Negated mentions ("it is not a diagnosis") are removed before the claim patterns run.
"""

from __future__ import annotations

import re

NEGATED_DIAGNOSIS = re.compile(r"\b(not|never|no|without)\s+(a\s+|any\s+)?diagnos\w*", re.IGNORECASE)
DIAGNOSIS_CLAIM = re.compile(
    r"\bdiagnos(is|es)\s*(of|is|:)"
    r"|\bdiagnosed\s+(with|as)\b"
    r"|\bdiagnostic\s+(of|for)\b"
    r"|\bconfirm(s|ed|ing)?\s+(a\s+|an\s+|the\s+)?(tumou?r|cancer|carcinoma|malignan\w*)"
    r"|\b(is|has|shows)\s+(a\s+|an\s+)?(malignant|malignancy|cancer|carcinoma)\b"
    r"|\bconsistent\s+with\s+(a\s+)?(cancer|carcinoma|malignan\w*)"
    r"|\bdefinitive(ly)?\b",
    re.IGNORECASE,
)


def is_diagnosis_claim(text: str | None) -> bool:
    return bool(DIAGNOSIS_CLAIM.search(NEGATED_DIAGNOSIS.sub(" ", text or "")))
