"""De-identification of payloads before they leave the hospital for a cloud model (Safety Verifier, Diana).

Plan, principle 7: identifiable data never leaves the hospital. The Safety Verifier de-identifies a payload, checks
the result again, and only then signs an EgressApproval for Core's egress gate (core/kcd_core/egress.py). This module
holds the first two steps; the signing is SafetyVerifier.approve_egress.

What is removed follows the HIPAA "Safe Harbor" list as a recognised baseline:
  - the patient's own identifiers (id, full name, given name, record numbers) -> [PATIENT] / [NAME]
  - identifying fields (name, address, telephone, email, tax code, ...) are dropped; a birth date becomes an
    age band ("40-49", "90+")
  - in free text: full dates -> their year only; emails, telephone numbers, Italian tax codes (codice fiscale),
    street addresses, long id numbers, web links and clinicians' names -> placeholders
Clinical content (values, units, conditions, drugs) is kept.

residual_findings re-checks a payload with the same strictness as the egress gate's tripwire: if anything
identifying is still there, the verifier rejects with a reason from REASONS instead of approving.

Rule-based and a draft: it recognises typical formats, not every possible one (for example postal codes and
relatives' names are not detected). It must be reviewed, and measured on a PHI test set, before any real use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import rfc8785

# Fixed rejection reasons (proposed answer to the egress contract's question 4).
REASONS = ("patient_identifier", "name", "email", "url", "fiscal_code", "date", "phone", "address", "clinician",
           "id_number")

_I = re.IGNORECASE
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
           r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto"
           r"|settembre|ottobre|novembre|dicembre")
# (reason, pattern, replacement): applied in this order. A date keeps only its year (group "year").
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[EMAIL]"),
    ("url", re.compile(r"\bhttps?://\S+|\bwww\.\S+", _I), "[URL]"),
    ("fiscal_code", re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b", _I), "[FISCAL_CODE]"),
    ("date", re.compile(r"\b(?P<year>(?:19|20)\d{2})-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                        r"(?:Z|[+-]\d{2}:?\d{2})?)?\b"), "{year}"),
    ("date", re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-](?P<year>(?:19|20)\d{2})\b"), "{year}"),
    ("date", re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\.?\s+(?P<year>(?:19|20)\d{{2}})\b", _I), "{year}"),
    ("date", re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+(?P<year>(?:19|20)\d{{2}})\b", _I), "{year}"),
    ("phone", re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\d{2,4}(?:[\s.-]?\d{2,4}){2,3}(?![\w.])"), "[PHONE]"),
    ("phone", re.compile(r"(?<![\w.])(?:0\d{1,3}|3\d{2})[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\w.])"), "[PHONE]"),
    ("address", re.compile(r"\b(?:via|viale|piazza|piazzale|corso|largo|vicolo|street|st\.|road|rd\.|avenue|ave\.)"
                           r"\s+[A-Za-zÀ-ÿ'. ]{2,40}?,?\s*\d{1,4}[a-z]?\b", _I), "[ADDRESS]"),
    ("clinician", re.compile(r"\b(?:Dott\.ssa|Dott|Dr|Prof)\.?\s+(?:[A-Z]\.\s*)*[A-Z][\wÀ-ÿ'-]+"), "[CLINICIAN]"),
    ("id_number", re.compile(r"(?<![\w.])\d{7,}(?![\w.])"), "[ID]"),
]
_WHOLE_FIRST = {"email", "url"}
# Fields that identify the patient whatever their value: dropped (a birth date becomes an age band).
_DROP_KEYS = {"name", "patient_name", "given", "family", "address", "telecom", "phone", "email", "mrn",
              "identifier", "fiscal_code", "codice_fiscale", "ssn", "photo"}
_BIRTH_KEYS = {"birthdate", "birth_date", "dob", "date_of_birth"}


@dataclass(frozen=True)
class Identifiers:
    """What identifies this patient. `values` must never leave the hospital (the gate's known_identifiers);
    `names` are name parts, replaced as words."""

    values: tuple[str, ...]
    names: tuple[str, ...] = ()

    @classmethod
    def from_patient(cls, patient: dict[str, Any], extra: Iterable[str] = ()) -> Identifiers:
        """From the FHIR patient summary (FolderFhir.patient: id, name, ...), plus record numbers in `extra`.
        Proposed answer to the egress contract's question 6: the identifiers come from the patient's record."""
        full = str(patient.get("name") or "").strip()
        values = [str(v) for v in (patient.get("id"), full, *extra) if v]
        names = [part for part in full.split() if len(part.strip(".")) >= 3]
        return cls(tuple(dict.fromkeys(values)), tuple(dict.fromkeys(names)))

    def all(self) -> tuple[str, ...]:
        return self.values + self.names


def age_band(birth: Any, today: date) -> str | None:
    try:
        born = date.fromisoformat(str(birth)[:10])
    except ValueError:
        return None
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if age >= 90:
        return "90+"  # Safe Harbor: ages over 89 are grouped
    low = age // 10 * 10
    return f"{low}-{low + 9}"


def _word(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(value)}(?!\w)", _I)


def _clean_text(text: str, ids: Identifiers, found: set[str]) -> str:
    # Emails and links first, as a whole (they often contain the name), then the patient's own identifiers
    # (longest first: "Marco R." before "Marco"), then the other patterns.
    def apply(patterns: list[tuple[str, re.Pattern[str], str]], text: str) -> str:
        for reason, pattern, replacement in patterns:
            def sub(m: re.Match[str], replacement: str = replacement, reason: str = reason) -> str:
                found.add(reason)
                return replacement.format(year=m.group("year")) if "{year}" in replacement else replacement
            text = pattern.sub(sub, text)
        return text

    text = apply([p for p in _PATTERNS if p[0] in _WHOLE_FIRST], text)
    for value in sorted(ids.values, key=len, reverse=True):
        text, n = _word(value).subn("[PATIENT]", text)
        found |= {"patient_identifier"} if n else set()
    for value in sorted(ids.names, key=len, reverse=True):
        text, n = _word(value).subn("[NAME]", text)
        found |= {"name"} if n else set()
    return apply([p for p in _PATTERNS if p[0] not in _WHOLE_FIRST], text)


def _clean(value: Any, ids: Identifiers, today: date, found: set[str]) -> Any:
    if isinstance(value, str):
        return _clean_text(value, ids, found)
    if isinstance(value, list):
        return [_clean(v, ids, today, found) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, v in value.items():
            k = str(key).lower()
            if k in _BIRTH_KEYS:
                band = age_band(v, today)
                if band:
                    out["age_band"] = band
                found.add("date")
            elif k in _DROP_KEYS:
                found.add("name" if k in {"name", "patient_name", "given", "family"} else "patient_identifier")
            else:
                out[key] = _clean(v, ids, today, found)
        return out
    return value  # numbers, booleans, None: kept


def deidentify(payload: dict[str, Any], ids: Identifiers, *, today: date | datetime) -> tuple[dict[str, Any], set[str]]:
    """A de-identified copy of `payload` (the original is not changed) and what was removed (REASONS, no values)."""
    found: set[str] = set()
    day = today.date() if isinstance(today, datetime) else today
    return _clean(payload, ids, day, found), found


def residual_findings(payload: dict[str, Any], ids: Identifiers) -> set[str]:
    """What still looks identifying in `payload`, field names included (empty set: nothing found).

    Known identifiers are matched as plain text, exactly like the egress gate's tripwire, so anything the gate
    would block is rejected here first, with a reason."""
    text = rfc8785.dumps(payload).decode()
    found = {reason for reason, pattern, _ in _PATTERNS if pattern.search(text)}
    lowered = text.lower()
    if any(len(v) >= 3 and v.lower() in lowered for v in ids.values):
        found.add("patient_identifier")
    if any(v.lower() in lowered for v in ids.names):
        found.add("name")
    return found
