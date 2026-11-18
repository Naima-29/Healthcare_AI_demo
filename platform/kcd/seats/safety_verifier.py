"""SAFETY VERIFIER on kcd_core: checks every clinical output before it reaches the case pack (deck slides 20, 29).

Port of Diana's Node seat (demo/kidney-care-desk/backend/seats/clinical.mjs, diana/agent-4), including her
"every citation exists in the Knowledge Model" check (exact match through km.query).

Difference from the demo, required by Core: the demo stamped `verification: {passed: true}` into the payload.
Core's payloads are strict and reject that field (it was a bypass: anyone could write it). Here the verified
payload goes on to the Case Briefer unchanged, and the proof is that the Safety Verifier signed the envelope
(the Dispatcher only accepts imaging_signal / therapy_conflict / data_delivered at the Case Briefer from it).

A failed check is never passed on: the verifier declares the refused action verifier.override (blocked by
pass_failed_check, so the attempt is audited) and sends a rejected acknowledgment, with the failed checks, back
to the seat that produced the output.

Patient-bound text (patient_content_classes / guard_patient_text): finds which of Core's release-gated content
classes a message to the patient contains (diagnosis, imaging result, dose instruction, prescription, genetic
result), in English and Italian, and asks the PEP for each one. The PEP blocks and audits any class that no
valid clinician release covers. The patterns are a draft and must be reviewed by the clinical team.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import Acknowledgment, DataDelivered, ImagingSignal, TherapyConflict
from kcd_core.contracts.release import Release
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.textguard import is_diagnosis_claim

PEER = "case_briefer"
STALE_DAYS = 90

# ---------------------------------------------------------------------- patient-bound text
_I = re.IGNORECASE
_MED_WORDS = (r"medicines?|medications?|tablets?|pills?|capsules?|doses?|dosage|painkillers?|anti-?inflammator\w*"
              r"|farmac\w*|medicin\w*|compress\w*|pastigli\w*|capsul\w*|dosaggio|antidolorific\w*"
              r"|antinfiammator\w*")
_DOSE_VERBS = (r"take|taking|stop|start|increase|reduce|lower|double|halve|skip|avoid"
               r"|prend\w*|assum\w*|smett\w*|sospend\w*|interromp\w*|aument\w*|ridu\w*|raddopp\w*|evit\w*")
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
    if any(p.search(text) for p in (instruction, _DOSE_AMOUNT, _DOSE_FREQUENCY, _TAKING_INSTRUCTION)):
        found.add("dose_instruction")
    return found


def guard_patient_text(ctx: SeatContext, text: str, *, artifact_hash: str | None = None,
                       release: Release | None = None, drug_names: Iterable[str] = ()) -> set[str]:
    """Call before any text reaches the patient. Raises PolicyViolation (audited by the PEP) when the text holds
    a content class that no valid clinician release for this artifact covers; returns the classes found."""
    found = patient_content_classes(text, drug_names)
    for cls in sorted(found):
        ctx.guard_content(cls, artifact_hash, release)
    return found


def _check(name: str, ok: bool) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok)}


def _parse_date(value: Any) -> datetime | None:
    """The lab date as a UTC datetime, or None when it is missing or unreadable (e.g. "08/10/2026")."""
    if not value:
        return None
    try:
        taken = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return taken if taken.tzinfo else taken.replace(tzinfo=UTC)


class SafetyVerifier(Seat):
    seat_id = "safety_verifier"

    def __init__(self, *, now: Callable[[], datetime], stale_days: int = STALE_DAYS):
        self.now = now  # the desk clock, so freshness is judged at the same time as everything else
        self.stale_days = stale_days
        self.results: list[dict[str, Any]] = []  # every verification, for the A2A stream / Board

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, ImagingSignal):
            checks = self.check_imaging(payload)
        elif isinstance(payload, TherapyConflict):
            checks = await self._check_citations(ctx, payload.citations) + self.check_conflict(payload)
        elif isinstance(payload, DataDelivered) and payload.kind == "genomic_context":
            checks = await self._check_citations(ctx, payload.citations, required=False)
            checks += self.check_genomic(payload)
        else:
            return  # acknowledgments, and data kinds this seat does not verify
        passed = all(c["ok"] for c in checks)
        self.results.append({"type": str(env.type), "from": env.from_, "message_id": str(env.id),
                             "passed": passed, "checks": checks})
        if passed:
            await ctx.send(env.type, PEER, payload)
            return
        try:
            ctx.act("verifier.override")  # refused (pass_failed_check): the attempt is audited, then blocked
        except PolicyViolation:
            pass
        failed = "; ".join(c["check"] for c in checks if not c["ok"])
        await ctx.send(MessageType.ACKNOWLEDGMENT, env.from_,
                       Acknowledgment(ack_of=str(env.id), status="rejected", reason=f"failed checks: {failed}"))

    # ------------------------------------------------------------------ checks
    @staticmethod
    def check_imaging(p: ImagingSignal) -> list[dict[str, Any]]:
        return [
            _check("provenance (study UID, model)", bool(p.provenance.study_uid and p.provenance.model)),
            _check("confidence plausible 0..1", 0.0 <= p.p_calibrated <= 1.0),
            _check("calibrated (not raw) confidence shown", p.p_calibrated != p.p_raw),
            _check("image quality / in distribution", p.quality.in_distribution),
            _check("no diagnosis claim in the note", not is_diagnosis_claim(p.note)),
        ]

    def check_conflict(self, p: TherapyConflict) -> list[dict[str, Any]]:
        f = p.data_freshness or {}
        egfr = f.get("egfr")
        taken = _parse_date(f.get("date"))
        age = (self.now() - taken).total_seconds() / 86400 if taken else None
        return [
            _check("eGFR plausible (1-150)", isinstance(egfr, int | float) and 1 < egfr < 150),
            _check("kidney function date readable", taken is not None),
            # one day of slack for time zones; a later date is a misread (e.g. OCR got the year wrong)
            _check("kidney function date not in the future", age is not None and age >= -1),
            _check(f"kidney function data fresh (<= {self.stale_days} days)",
                   age is not None and age <= self.stale_days),
        ]

    @staticmethod
    def check_genomic(p: DataDelivered) -> list[dict[str, Any]]:
        findings = p.data.get("findings") if isinstance(p.data, dict) else None
        ok_findings = isinstance(findings, list) and all(
            isinstance(f, dict) and f.get("lab_classification") for f in findings)
        return [
            _check("every finding keeps the laboratory's classification", ok_findings),
            _check("disclosure to the patient left to a geneticist", "geneticist" in str(p.data.get("disclosure", ""))),
        ]

    async def _check_citations(self, ctx: SeatContext, cited: list[str], *, required: bool = True) -> list[dict]:
        # km.query matches partial text, so the full citation is compared here: a made-up source that merely
        # contains a real one's words must not pass.
        exists = []
        for c in cited:
            hits = await ctx.tool("km.query", term=c)
            exists.append(any(h["citation"] == c for h in hits))
        checks = [_check("every citation exists in the Knowledge Model", all(exists))]
        if required:
            checks.insert(0, _check("every claim cited to the Knowledge Model", len(cited) > 0))
        return checks
