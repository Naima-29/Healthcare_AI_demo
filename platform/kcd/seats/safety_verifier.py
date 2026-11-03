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
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import Acknowledgment, DataDelivered, ImagingSignal, TherapyConflict
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.textguard import is_diagnosis_claim

PEER = "case_briefer"
STALE_DAYS = 90


def _check(name: str, ok: bool) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok)}


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
        age = float("inf")
        if f.get("date"):
            taken = datetime.fromisoformat(str(f["date"]))
            taken = taken if taken.tzinfo else taken.replace(tzinfo=UTC)
            age = (self.now() - taken).total_seconds() / 86400
        return [
            _check("eGFR plausible (1-150)", isinstance(egfr, int | float) and 1 < egfr < 150),
            _check(f"kidney function data fresh (<= {self.stale_days} days)", age <= self.stale_days),
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
