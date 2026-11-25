"""CASE BRIEFER on kcd_core: one SBAR case pack per episode, and the clinician's signed decision (deck slides 20, 28).

Replaces kcd.standins.StandInCaseBriefer. Same public methods, so demo.py and the edge-case scenario can swap it in:
`cases`, `request_genomics`, `publish_case_pack`, `publish_decision`.

  inbound (all already verified by the Safety Verifier where the route says so):
    red_flag_escalation, symptom_report   from the Companion
    therapy_conflict, imaging_signal, data_delivered{genomic_context}   from the Safety Verifier
  -> collected per patient into one open case
  publish_case_pack:
    fhir.get_* (background, read-only) -> SBAR with a source and citations on every line
    -> board.create_item(kind="case_pack_detail")  (draft decision + line sources, for the Clinical Board)
    -> case_pack_ready to the Dispatcher, which opens the clinician's gate
  publish_decision (called by the Clinical Board after the clinician signs):
    verifies the Ed25519 release covers THIS case pack version (and, with strict_binding, exactly the edited text)
    -> clinician_decision to the Companion (what the patient hears) and the Archivist (what is learned)

Refusals: decide (the clinician decides, never this seat), send_to_patient (no channel tool; only the signed
decision travels, and only to the Companion).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kcd_core.audit import content_hash
from kcd_core.contracts import Envelope, MessageType, Release
from kcd_core.contracts.messages import (
    CasePackReady,
    ClinicianDecision,
    DataDelivered,
    DataRequest,
    DecisionItem,
    ImagingSignal,
    RedFlagEscalation,
    Sbar,
    SymptomReport,
    TherapyConflict,
)
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from kcd_core.signing import SignatureError, verify_release
from pydantic import BaseModel

GRADCAM = "[20] Grad-CAM (Selvaraju 2017)"
SOURCE = {
    "red_flag_escalation": "Companion",
    "symptom_report": "Companion",
    "therapy_conflict": "Renal Guardian",
    "imaging_signal": "Imaging Reader",
    "data_delivered": "Genomics Interpreter",
}

# Draft wording for the patient. The clinician edits it; the Companion says exactly what was signed.
# Deliberately free of imaging results, diagnoses and genetic terms: those need their own release.
DRAFT: dict[str, dict[str, str]] = {
    "urgent_visit": {
        "en": "Please come to the hospital today for a check. If things get worse before then, "
              "go to the emergency department.",
        "it": "Venga oggi in ospedale per una visita. Se peggiora prima, vada al Pronto Soccorso.",
    },
    "stop_nsaid": {
        "en": "Please stop taking ibuprofen and other anti-inflammatory painkillers. "
              "Your doctor will suggest another painkiller.",
        "it": "Smetta di prendere ibuprofene e altri antinfiammatori. Il medico le indicherà un altro antidolorifico.",
    },
    "review_medication": {
        "en": "Please do not change any medicine on your own; your doctor will review your treatment.",
        "it": "Non modifichi da solo nessuna terapia: il medico rivedrà il suo trattamento.",
    },
    "repeat_labs": {
        "en": "Please have a blood and urine test before your visit.",
        "it": "Faccia un prelievo di sangue e urine prima della visita.",
    },
    "ct_review": {
        "en": "Your doctor will go through your scan images with you in person during the visit.",
        "it": "Il medico rivedrà con lei le immagini dell'esame di persona durante la visita.",
    },
    "vhl_followup": {
        "en": "We will offer you a conversation with a specialist.",
        "it": "Le proporremo un colloquio con lo specialista.",
    },
    "genetics_followup": {
        "en": "We will offer you a conversation with a specialist.",
        "it": "Le proporremo un colloquio con lo specialista.",
    },
}
R_ORDER = ["urgent_visit", "stop_nsaid", "review_medication", "repeat_labs", "ct_review", "vhl_followup",
           "genetics_followup"]


def decision_release_id(items: list[DecisionItem], note: str, artifact_hash: str) -> str:
    """Id of the clinician's release, derived from the exact decision text. The release id is covered by the
    Ed25519 signature, so what the clinician signed is what the patient hears: change one word and it no longer
    matches (checked by CaseBriefer(strict_binding=True))."""
    body = {"items": [i.model_dump(mode="json") for i in items], "note": note or "", "artifact_hash": artifact_hash}
    return "dec_" + content_hash(body).removeprefix("sha256:")[:24]


@dataclass
class _Case:
    case_id: str
    episode_id: str
    received: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (message type, payload)
    meta: list[tuple[str, str]] = field(default_factory=list)  # (sender, message id), aligned with received
    version: int = 0
    artifact_hash: str | None = None
    sbar: Sbar | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    genomics_requested: bool = False


class CaseBriefer(Seat):
    seat_id = "case_briefer"

    def __init__(self, *, now: Callable[[], datetime] | None = None, draft_lang: str = "en",
                 strict_binding: bool = False, auto_publish: bool = False, auto_genomics: bool = False):
        self.now = now
        self.draft_lang = draft_lang
        self.strict_binding = strict_binding
        self.auto_publish = auto_publish  # re-issue the pack on every new assessment (the demo's behaviour)
        self.auto_genomics = auto_genomics  # ask for the genomic context when a case opens
        self.cases: dict[str, _Case] = {}
        self._numbers: dict[str, int] = {}
        self._seen: set[str] = set()

    # ------------------------------------------------------------------ inbound
    def case(self, patient_id: str, episode_id: str) -> _Case:
        c = self.cases.get(patient_id)
        if c is None or c.closed:
            n = self._numbers[patient_id] = self._numbers.get(patient_id, 0) + 1
            c = self.cases[patient_id] = _Case(f"case-{patient_id}-{n}", episode_id)
        return c

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if not isinstance(payload, ImagingSignal | TherapyConflict | DataDelivered | RedFlagEscalation
                          | SymptomReport):
            return  # acknowledgments
        if str(env.id) in self._seen:
            return  # the bus redelivered it
        self._seen.add(str(env.id))
        c = self.case(env.patient_id, env.episode_id)
        c.received.append((str(env.type), payload.model_dump(mode="json", by_alias=True)))
        c.meta.append((env.from_, str(env.id)))
        if self.auto_genomics and not c.genomics_requested and isinstance(payload, RedFlagEscalation | SymptomReport):
            c.genomics_requested = True
            await self.request_genomics(ctx, patient_id=env.patient_id, episode_id=env.episode_id)
        if self.auto_publish:
            await self.publish_case_pack(ctx, patient_id=env.patient_id)

    # ------------------------------------------------------------------ outbound
    async def request_genomics(self, ctx: SeatContext, *, patient_id: str, episode_id: str) -> None:
        await ctx.send(MessageType.DATA_REQUEST, "genomics_interpreter",
                       DataRequest(what="genomic_context", why="case pack: hereditary context"),
                       patient_id=patient_id, episode_id=episode_id)

    async def publish_case_pack(self, ctx: SeatContext, *, patient_id: str) -> CasePackReady:
        c = self.cases.get(patient_id)
        if c is None or c.closed:
            raise ValueError(f"no open case for patient {patient_id}")
        background = await self._background(ctx, patient_id)
        c.version += 1
        c.sbar, c.detail = assemble(c, background, self.draft_lang)
        c.detail.update(case_id=c.case_id, version=c.version, patient_id=patient_id)
        c.artifact_hash = content_hash({"case_id": c.case_id, "version": c.version,
                                        "sbar": c.sbar.model_dump(mode="json")})
        types = {t for t, _ in c.received}
        gates = ["clinician_signoff"]
        if "imaging_signal" in types:
            gates.append("radiologist_review")
        if "therapy_conflict" in types:
            gates.append("nephrologist_review")
        if any(t == "data_delivered" and p.get("kind") == "genomic_context" for t, p in c.received):
            gates.append("genetic_disclosure")
        pack = CasePackReady(case_id=c.case_id, sbar=c.sbar, artifact_hash=c.artifact_hash, version=c.version,
                             gates=gates)
        try:  # the Board shows the draft decision and where every line came from
            await ctx.tool("board.create_item", kind="case_pack_detail", gate="none", patient_id=patient_id,
                           case_id=c.case_id, version=c.version, title="Case pack detail", data=c.detail)
        except LookupError:
            pass  # no Board bound to this desk (tests): the plain SBAR still reaches the Dispatcher
        await ctx.send(MessageType.CASE_PACK_READY, "dispatcher", pack, patient_id=patient_id,
                       episode_id=c.episode_id)
        return pack

    async def publish_decision(self, ctx: SeatContext, *, patient_id: str, release: Release,
                               items: list[DecisionItem], note: str = "") -> ClinicianDecision:
        """The clinician signed on the Board. Publish only if the signed release covers exactly this case pack
        (and, in strict mode, exactly this wording)."""
        c = self.cases.get(patient_id)
        if c is None or c.artifact_hash is None:
            raise PolicyViolation(self.seat_id, "decision", patient_id, "no case pack to decide on")
        if c.closed:
            raise PolicyViolation(self.seat_id, "decision", c.case_id, "a decision was already published")
        try:
            verify_release(release, ctx.pep.clinician_keys)
        except SignatureError as exc:
            raise PolicyViolation(self.seat_id, "decision", c.case_id, f"invalid clinician release: {exc}") from exc
        if release.artifact_id != c.case_id or release.artifact_hash != c.artifact_hash:
            raise PolicyViolation(self.seat_id, "decision", c.case_id, "release does not cover this case pack")
        if self.strict_binding and release.id != decision_release_id(items, note, release.artifact_hash):
            raise PolicyViolation(self.seat_id, "decision", c.case_id,
                                  "release does not cover the edited decision text")
        decision = ClinicianDecision(case_id=c.case_id, decision_id=release.id, artifact_hash=release.artifact_hash,
                                     signed_by=release.clinician_id, role=_human_role(release.role), note=note,
                                     items=items, signature=release.sig)
        for to in ("archivist", "companion"):
            await ctx.send(MessageType.CLINICIAN_DECISION, to, decision, patient_id=patient_id,
                           episode_id=c.episode_id)
        c.closed = True
        return decision

    # ------------------------------------------------------------------ background (read-only FHIR)
    async def _background(self, ctx: SeatContext, patient_id: str) -> list[str]:
        try:
            pt = await ctx.tool("fhir.get_patient", patient_id=patient_id)
            conds = await ctx.tool("fhir.get_conditions", patient_id=patient_id)
            meds = await ctx.tool("fhir.get_medications", patient_id=patient_id)
            egfr = await ctx.tool("fhir.get_observations", patient_id=patient_id, code="eGFR")
        except LookupError:  # no record, or no FHIR tool bound
            return ["No patient record available: background not included."]
        age = None
        if self.now is not None and pt.get("birthDate"):
            born, today = datetime.fromisoformat(pt["birthDate"]), self.now()
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        who = ", ".join(str(x) for x in (pt.get("name"), age, pt.get("gender")) if x is not None)
        lines = [f"{who}."]
        shown = "; ".join(c["display"] for c in conds if c.get("display"))
        if shown:
            lines.append(f"Conditions: {shown}.")
        names = ", ".join(m["name"] for m in meds if m.get("name"))
        if names:
            lines.append(f"Medications on record: {names}.")
        if egfr.get("series"):
            trail = " → ".join(f"{x['date'][:7]} {x['value']}" for x in reversed(egfr["series"]))
            lines.append(f"eGFR trajectory: {trail} (latest {egfr.get('age_days')} days old).")
        return lines


# ---------------------------------------------------------------------- pure functions (tested directly)
def assemble(case: _Case, background: list[str], lang: str = "en") -> tuple[Sbar, dict[str, Any]]:
    s_lines: list[dict[str, Any]] = []
    a_lines: list[dict[str, Any]] = []
    r_lines: list[dict[str, Any]] = []
    detail: dict[str, Any] = {}

    def add_r(code: str, text: str, cite: list[str], ref: str) -> None:
        if not any(r["code"] == code for r in r_lines):
            r_lines.append({"code": code, "text": text, "cite": list(cite), "ref": ref})

    for (kind, p), (_sender, ref) in zip(case.received, case.meta, strict=True):
        src = SOURCE.get(kind, kind)
        if kind == "red_flag_escalation":
            s_lines.append({"text": f"Red flag at home ({p['urgency']}): {p['pattern']}. "
                                    f"Reported: {'; '.join(p['evidence'])}.", "from": src, "ref": ref})
            if p["urgency"] == "high":
                add_r("urgent_visit", "Same-day assessment (uro-oncology / ED) given the red-flag pattern.",
                      p.get("citations", []), ref)
            add_r("repeat_labs", "Repeat creatinine / eGFR, urinalysis and blood count.", [], ref)
        elif kind == "symptom_report":
            labels = ", ".join(s["label"] for s in p["symptoms"])
            extra = " Under-reporting likely: non-verbal signs exceed the words." if p.get("underreporting") else ""
            s_lines.append({"text": f"Reported at home: {labels} (distress {p['distress']['level']}).{extra}",
                            "from": src, "ref": ref})
        elif kind == "therapy_conflict":
            a_lines.append({"text": f"Drug–disease conflict ({p['severity']}): {p['drug']} with "
                                    f"{', '.join(p['conditions'])}. {p['rationale']}",
                            "from": src, "cite": p["citations"], "ref": ref})
            if "NSAID" in p["drug"].upper():
                add_r("stop_nsaid", "Stop NSAID; alternative analgesia per clinician; recheck BP.",
                      p["citations"], ref)
            else:
                add_r("review_medication", f"Review {p['drug']} against the patient's conditions; "
                                           "alternatives per clinician.", p["citations"], ref)
            add_r("repeat_labs", "Repeat creatinine / eGFR, urinalysis and blood count.",
                  [c for c in p["citations"] if "KDIGO" in c], ref)
            detail["egfr_series"] = p.get("egfr_series", [])
        elif kind == "imaging_signal":
            q = p["quality"]
            more = (f"; image issues: {', '.join(q['issues'])}" if q.get("issues") else "") + (
                "; model unsure (out of distribution)" if not q.get("in_distribution", True) else "")
            a_lines.append({"text": f"Preliminary CT signal \"{p['label']}\", calibrated p={p['p_calibrated']} "
                                    f"(raw {p.get('p_raw')}); quality {q['quality']}{more}. Attention map attached. "
                                    "Not a diagnosis: radiologist review required.",
                            "from": src, "cite": [GRADCAM], "ref": ref})
            add_r("ct_review", "Radiologist review of the new CT, compare with prior studies.", [GRADCAM], ref)
            detail["images"] = {"key_image": p.get("key_image"), "heatmap_ref": p.get("heatmap_ref"),
                                "study_uid": p.get("provenance", {}).get("study_uid")}
        elif kind == "data_delivered" and p.get("kind") == "genomic_context":
            data = p["data"]
            for f in data.get("findings", []):
                if f.get("relevance") in ("none", "filtered"):
                    continue
                a_lines.append({"text": f"{f['gene']} {f.get('hgvs', '')} ({f['lab_classification']}): "
                                        f"{f['summary']}".replace("  ", " "),
                                "from": src, "cite": f.get("citations", []), "ref": ref})
            if data.get("needs_geneticist_review"):
                a_lines.append({"text": "Some variants have no gene-specific rule: geneticist review needed.",
                                "from": src, "cite": [], "ref": ref})
            syndromes = data.get("hereditary_syndromes") or []
            if syndromes:
                code = "vhl_followup" if "von Hippel-Lindau disease" in syndromes else "genetics_followup"
                text = (f"Hereditary syndrome(s): {', '.join(syndromes)}. "
                        + " ".join(data.get("case_implications", []))).strip()
                add_r(code, text, p.get("citations", []), ref)

    r_lines.sort(key=lambda r: R_ORDER.index(r["code"]) if r["code"] in R_ORDER else len(R_ORDER))
    draft = []
    for r in r_lines:
        t = DRAFT.get(r["code"])
        draft.append({"code": r["code"], "selected": True, "clinician_text": r["text"],
                      "patient_text": t[lang] if t else r["text"], "patient_text_en": t["en"] if t else None})
    sbar = Sbar(S=[x["text"] for x in s_lines] or ["Follow-up review."], B=background or ["No background."],
                A=[x["text"] for x in a_lines] or ["No new findings."],
                R=[x["text"] for x in r_lines] or ["Routine follow-up."])
    detail.update(S=s_lines, B=[{"text": b} for b in background], A=a_lines, R=r_lines, draft=draft)
    return sbar, detail


def _human_role(role: str) -> str:
    """Board roles may be 'clinician:<unit>'; the decision names the clinical role."""
    return {"clinician:nephrology": "nephrologist", "clinician:radiology": "radiologist",
            "clinician:genetics": "geneticist"}.get(role, role)
