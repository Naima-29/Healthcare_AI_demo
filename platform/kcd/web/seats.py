"""The Companion, Case Briefer and Safety Verifier, connected to the browser.

The Companion is Meet's real seat (kcd/seats/companion.py). WebCompanion adds only what the browser door needs: the
phone as its channel, the live events, the refusal declared at the policy layer when a patient asks for a result,
and a rule a clinician confirmed. The Case Briefer is still a stand-in until Person 5 delivers it (kcd/standins.py).
It re-issues its case pack whenever a verified finding arrives. The messages are real, signed and checked.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kcd_core.contracts import Envelope, Release
from kcd_core.contracts.messages import CasePackReady, ClinicianDecision, DataRequest, ImagingSignal
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import SeatContext
from pydantic import BaseModel

from kcd.patient.nlu import extract
from kcd.seats.companion import Companion
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.standins import StandInCaseBriefer
from kcd.web.hub import Hub

NSAID_RULE = "avoid_drug_class:NSAID"

RULE_NSAID = {
    "it": "Il suo nefrologo ({who}) le ha indicato di evitare l’ibuprofene e gli antinfiammatori simili. "
          "Per il dolore segua il piano che le ha dato il medico.",
    "en": "Your nephrologist ({who}) asked you to avoid ibuprofen and similar anti-inflammatory painkillers. "
          "For pain, follow the plan your doctor gave you.",
}

# Draft wording for the patient. The clinician edits it; the Companion sends exactly what was signed.
DRAFTS = {
    "urgent_visit": ("venire domani alle 9:00 in Urologia oncologica (Pad. B) per una visita; se peggiora prima, vada "
                     "al Pronto Soccorso",
                     "come tomorrow at 9:00 to the uro-oncology clinic (Building B); if things get worse before then, "
                     "go to the emergency department"),
    "stop_nsaid": ("non prendere più ibuprofene né altri antinfiammatori; per il dolore usi il paracetamolo come "
                   "indicato nel piano che le invieremo",
                   "stop taking ibuprofen or other anti-inflammatories; for pain use paracetamol as described in the "
                   "plan we will send you"),
    "repeat_labs": ("fare domattina un prelievo di sangue e urine prima della visita",
                    "have blood and urine tests tomorrow morning before the visit"),
    "ct_review": ("le immagini della TAC verranno discusse con lei durante la visita",
                  "your CT images will be discussed with you during the visit"),
    "genetics_visit": ("le proporremo un colloquio con il genetista",
                       "we will offer you an appointment with the geneticist"),
    "routine_followup": ("il suo team di cura la ricontatterà per il controllo",
                         "your care team will contact you again for your check-up"),
}


class WebChannel:
    """The patient's phone on screen, in place of Telegram (kcd/patient/channel.py)."""

    def __init__(self, hub: Hub):
        self.hub = hub

    async def send(self, patient_id: str, text: str) -> dict[str, Any]:
        self.hub.publish("patient_out", {"patient": patient_id, "text": text})
        return {"delivered": True, "transport": "web"}


class WebCompanion(Companion):
    def __init__(self, km: Any, hub: Hub, cortex: Any, *, history_rcc: Callable[[str], bool],
                 name_of: Callable[[str], str]):
        super().__init__(km, history_provider=lambda patient: {"rcc": history_rcc(patient)})
        self.hub, self.cortex, self.name_of = hub, cortex, name_of
        self.episodes: dict[str, int] = {}
        self.awaiting_lab: set[str] = set()
        self.released_rules: dict[tuple[str, str], tuple[str, Release, str]] = {}  # (patient, key) -> hash, release, who

    def new_episode(self, patient: str) -> None:
        """Core drops an identical message within an episode as a redelivery, so a real repeat needs a new episode."""
        self.episodes[patient] = self.episodes.get(patient, 1) + 1

    def forget(self, patient: str) -> None:
        self.new_episode(patient)
        self.sessions.pop(patient, None)
        self.awaiting_lab.discard(patient)
        self.released_rules = {k: v for k, v in self.released_rules.items() if k[0] != patient}

    def episode(self, patient: str) -> str:
        return f"ep-{patient}-{self.episodes.get(patient, 1)}"

    # ------------------------------------------------------------------ the patient door
    async def on_text(self, ctx: SeatContext, patient: str, text: str, features: dict[str, Any] | None) -> None:
        self.hub.publish("patient_in", {"patient": patient, "text": text})
        parsed = extract(text, self.km.classify_drug)
        for intent, action in (("ask_imaging_result", "patient.disclose_imaging"),
                               ("ask_genetics", "patient.disclose_genetics")):
            if intent in parsed["intents"]:
                try:
                    ctx.act(action)  # blocked by the manifest's refusal: audited and shown as such
                except PolicyViolation:
                    pass
        session = self.session(patient)
        drug = parsed["drug"]
        if drug and drug["class"] == "NSAID" and self._rule_active(ctx, patient):
            session.language = parsed["language"]
            artifact_hash, release, who = self.released_rules[(patient, NSAID_RULE)]
            ctx.guard_content("dose_instruction", artifact_hash, release)  # only with the clinician's release
            await self._say(ctx, patient, RULE_NSAID[session.language].format(who=who))
            return
        before = session.distress
        await self.on_patient_text(ctx, patient_id=patient, episode_id=self.episode(patient), text=text,
                                   features=features)
        if session.distress is not before:
            self.hub.publish("humanized", {"patient": patient, **session.distress})

    def _rule_active(self, ctx: SeatContext, patient: str) -> bool:
        if (patient, NSAID_RULE) not in self.released_rules:
            return False
        ctx.check_memory(3, "read")
        return any(k["key"] == NSAID_RULE for k in self.cortex.active_rules([(3, patient)]))

    async def on_lab_photo(self, ctx: SeatContext, patient: str, photo: dict[str, Any]) -> None:
        self.hub.publish("patient_in", {"patient": patient, "text": "📷 [photo of lab report]"})
        self.awaiting_lab.discard(patient)
        await self.attach_lab_report(ctx, patient_id=patient, episode_id=self.episode(patient),
                                     ocr_text=json.dumps(photo))

    # ------------------------------------------------------------------ from the hospital
    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, DataRequest) and payload.ask_patient:
            self.awaiting_lab.add(env.patient_id)
        await super().handle(env, payload, ctx)

    async def _deliver_signed_decision(self, ctx: SeatContext, patient_id: str, decision: ClinicianDecision) -> None:
        # The decision names the clinician by id; the patient reads the name.
        named = decision.model_copy(update={"signed_by": self.name_of(decision.signed_by)})
        await super()._deliver_signed_decision(ctx, patient_id, named)


class WebCaseBriefer(StandInCaseBriefer):
    """On each verified finding: a new case pack version for the Board. The first CT also asks for the genomics."""

    def __init__(self, *, language_of: Callable[[str], str], on_finding: Callable[[str, str, dict[str, Any]], None]):
        super().__init__()
        self.language_of, self.on_finding = language_of, on_finding
        self.genomics_asked: set[str] = set()

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        await super().handle(env, payload, ctx)
        case = self.cases.get(env.patient_id)
        if case is None or not case.received or isinstance(payload, ClinicianDecision):
            return
        kind, data = case.received[-1]
        self.on_finding(env.patient_id, kind, data)
        if isinstance(payload, ImagingSignal) and env.patient_id not in self.genomics_asked:
            self.genomics_asked.add(env.patient_id)
            await self.request_genomics(ctx, patient_id=env.patient_id, episode_id=env.episode_id)
        await self.publish_case_pack(ctx, patient_id=env.patient_id)

    def pack_view(self, pack: CasePackReady) -> dict[str, Any]:
        """What the Board shows for a case pack. The signed artifact is the plain SBAR in `pack`."""
        patient = next(p for p, c in self.cases.items() if c.case_id == pack.case_id)
        received = self.cases[patient].received
        assessment, options, codes = [], [], []
        for line_a, line_r in zip(*_by_finding(received), strict=True):
            assessment += line_a
            options += line_r
        kinds = {k for k, _ in received}
        if "red_flag_escalation" in kinds:
            codes.append("urgent_visit")
        if "therapy_conflict" in kinds:
            codes += ["stop_nsaid", "repeat_labs"]
        if "imaging_signal" in kinds:
            codes.append("ct_review")
        if any(k == "data_delivered" and p.get("kind") == "genomic_context" for k, p in received):
            codes.append("genetics_visit")
        lang = self.language_of(patient)
        draft = [{"code": c, "selected": True, "patient_text": DRAFTS[c][0 if lang == "it" else 1],
                  "patient_text_en": DRAFTS[c][1]} for c in codes or ["routine_followup"]]
        sbar = pack.sbar.model_dump()
        return {"version": pack.version, "draft": draft,
                "sbar": {**sbar, "A": assessment or sbar["A"], "R": options or sbar["R"]}}


def _by_finding(received: list[tuple[str, dict[str, Any]]]) -> tuple[list[list[dict]], list[list[dict]]]:
    """Per finding: the assessment and option lines, each with the seat it came from and its citations."""
    assessment, options = [], []
    for kind, p in received:
        cite = p.get("citations") or []
        if kind == "therapy_conflict":
            assessment.append([{"from": "Renal Guardian", "cite": cite, "text":
                                f"Drug-disease conflict ({p['severity']}): {p['drug']} with "
                                f"{', '.join(p['conditions'])}. {p.get('rationale') or ''}"}])
            options.append([{"text": "Review the analgesic; consider stopping the NSAID.", "cite": cite}])
        elif kind == "imaging_signal":
            assessment.append([{"from": "Imaging Reader", "cite": [], "text":
                                f"CT model signal: {p['label']} (calibrated p={p['p_calibrated']}), for radiologist "
                                "review. Not a diagnosis."}])
            options.append([{"text": "Radiologist to review the CT and the heat map.", "cite": []}])
        elif kind == "data_delivered" and p.get("kind") == "genomic_context":
            findings = ", ".join(f"{f['gene']} ({f['relevance']})" for f in p["data"].get("findings", []))
            assessment.append([{"from": "Genomics Interpreter", "cite": cite,
                                "text": f"Genomic context: {findings or 'nothing to report'}."}])
            options.append([{"text": t, "cite": cite} for t in p["data"].get("case_implications", [])])
    return assessment, options


class _Hooked(list):
    def __init__(self, hook: Callable[[dict[str, Any]], None]):
        super().__init__()
        self.hook = hook

    def append(self, row: dict[str, Any]) -> None:
        super().append(row)
        self.hook(row)


class WebVerifier(SafetyVerifier):
    """The real Safety Verifier. Each verification also goes to `on_result(patient, result)`."""

    def __init__(self, *, now: Callable[..., Any], on_result: Callable[[str, dict[str, Any]], None]):
        super().__init__(now=now)
        self._patient = ""
        self.results = _Hooked(lambda row: on_result(self._patient, row))

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        self._patient = env.patient_id  # one bus worker per seat: messages are handled one at a time
        await super().handle(env, payload, ctx)
