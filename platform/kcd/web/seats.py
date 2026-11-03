"""The stand-in seats and the Safety Verifier, connected to the browser.

Companion and Case Briefer are still stand-ins until Person 2 and Person 5 deliver the real ones (kcd/standins.py).
The Companion here adds what the patient door needs: keyword reading, fuzzy distress, the v1 interview and the
fixed safety script (ported from the demo's seats/companion.mjs, in English and Italian). The Case Briefer
re-issues its case pack whenever a verified finding arrives. The messages are real, signed and checked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kcd_core.contracts import Envelope, MessageType, Release
from kcd_core.contracts.messages import (
    CasePackReady,
    ClinicianDecision,
    DataDelivered,
    DataRequest,
    Distress,
    ImagingSignal,
    Symptom,
    SymptomReport,
)
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import SeatContext
from pydantic import BaseModel

from kcd.seats.safety_verifier import SafetyVerifier
from kcd.standins import StandInCaseBriefer, StandInCompanion
from kcd.web.fuzzy import from_features
from kcd.web.hub import Hub
from kcd.web.nlu import read_message

NSAID_RULE = "avoid_drug_class:NSAID"

TEXT = {
    "it": {
        "q1": "Mi può dire dove sente il dolore: sul fianco, sotto le costole, o più in basso sulla schiena? "
              "Peggiora con i movimenti?",
        "q2": "Ha notato cambiamenti nelle urine, per esempio sangue, urine più scure, bruciore o difficoltà a urinare?",
        "q3": "Ha avuto anche febbre, brividi, nausea, perdita di peso o di appetito, o si sente più debole del solito?",
        "q4": "Da 0 a 10, quanto è forte il dolore adesso? Ha giramenti di testa o fa fatica nelle attività di tutti "
              "i giorni?",
        "hello": "Sono qui con lei. Mi racconti come si sente stasera.",
        "noted": "Grazie, ho riportato tutto al suo team di cura. Se qualcosa peggiora mi scriva subito.",
        "safety": "Quello che mi descrive va valutato da un medico oggi stesso. Se può, vada al Pronto Soccorso; se si "
                  "sente svenire, vede molto sangue o il dolore peggiora, chiami subito il 112. Ho già avvisato il "
                  "medico reperibile del suo team, che la contatterà a breve.",
        "med_ack": "Grazie, l’ho segnalato al suo team. Per favore non cambi da solo le sue terapie: sarà il medico a "
                   "darle indicazioni.",
        "lab_ask": "Per completare la valutazione mi serve il suo ultimo esame del sangue. Può fotografare il referto "
                   "più recente e inviarmelo qui?",
        "lab_thanks": "Grazie, ricevuto (referto del {date}). L’ho inviato al suo team.",
        "imaging_hold": "Le sue immagini sono in revisione da parte del suo medico, che gliene parlerà di persona. Nel "
                        "frattempo, se i sintomi peggiorano, segua le indicazioni che le ho dato.",
        "genetics_hold": "Di questi risultati le parlerà lo specialista, in un colloquio dedicato.",
        "decision_head": "Il suo medico ({who}) ha rivisto il suo caso e le chiede di:",
        "decision_tail": "Se qualcosa peggiora prima, chiami il 112. Sono qui se ha domande.",
        "rule_nsaid": "Il suo nefrologo ({who}) le ha indicato di evitare l’ibuprofene e gli antinfiammatori simili. "
                      "Per il dolore segua il piano che le ha dato il medico.",
    },
    "en": {
        "q1": "Where exactly is the pain: on the side under the ribs, or lower in the back? Does it get worse when you "
              "move?",
        "q2": "Have you noticed any change in your urine, such as blood, darker urine, burning or difficulty passing "
              "urine?",
        "q3": "Have you also had fever, chills, nausea, weight loss or loss of appetite, or felt weaker than usual?",
        "q4": "From 0 to 10, how strong is the pain now? Any dizziness, or trouble with everyday activities?",
        "hello": "I am here with you. Tell me how you feel tonight.",
        "noted": "Thank you, I have passed everything to your care team. If anything gets worse, message me straight "
                 "away.",
        "safety": "What you describe needs to be seen by a doctor today. If you can, go to the emergency department; if "
                  "you feel faint, see a lot of blood or the pain gets worse, call 112 now. I have already alerted the "
                  "on-call doctor of your team, who will contact you shortly.",
        "med_ack": "Thank you, I have flagged this to your team. Please do not change your medicines on your own: your "
                   "doctor will advise you.",
        "lab_ask": "To complete the assessment I need your latest blood test. Could you take a photo of your most "
                   "recent lab report and send it here?",
        "lab_thanks": "Thank you, received (report dated {date}). I have sent it to your team.",
        "imaging_hold": "Your images are being reviewed by your doctor, who will discuss them with you in person. "
                        "Meanwhile, if symptoms get worse, follow the advice I gave you.",
        "genetics_hold": "A specialist will discuss these results with you in a dedicated conversation.",
        "decision_head": "Your doctor ({who}) has reviewed your case and asks you to:",
        "decision_tail": "If anything gets worse before then, call 112. I am here if you have questions.",
        "rule_nsaid": "Your nephrologist ({who}) asked you to avoid ibuprofen and similar anti-inflammatory painkillers. "
                      "For pain, follow the plan your doctor gave you.",
    },
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


@dataclass
class _Talk:
    lang: str
    symptoms: list[dict[str, str]] = field(default_factory=list)
    pain_score: int | None = None
    weight_loss_kg: int | None = None
    minimizer: bool = False
    asked: set[str] = field(default_factory=set)
    distress: dict[str, Any] | None = None
    red_flag_sent: bool = False
    reported: bool = False
    awaiting_lab: bool = False
    transcript: list[str] = field(default_factory=list)


class WebCompanion(StandInCompanion):
    def __init__(self, km: Any, hub: Hub, cortex: Any, *, lang: str, history_rcc: Callable[[str], bool],
                 name_of: Callable[[str], str]):
        super().__init__(km)
        self.hub, self.cortex, self.lang, self.history_rcc, self.name_of = hub, cortex, lang, history_rcc, name_of
        self.talks: dict[str, _Talk] = {}
        self.episodes: dict[str, int] = {}
        self.released_rules: dict[tuple[str, str], tuple[str, Release, str]] = {}  # (patient, key) -> hash, release, who

    def talk(self, patient: str) -> _Talk:
        return self.talks.setdefault(patient, _Talk(self.lang))

    def new_episode(self, patient: str) -> None:
        """Core drops an identical message within an episode as a redelivery, so a real repeat needs a new episode."""
        self.episodes[patient] = self.episodes.get(patient, 1) + 1

    def forget(self, patient: str) -> None:
        self.new_episode(patient)
        self.talks.pop(patient, None)
        self.released_rules = {k: v for k, v in self.released_rules.items() if k[0] != patient}

    def episode(self, patient: str) -> str:
        return f"ep-{patient}-{self.episodes.get(patient, 1)}"

    async def _say(self, ctx: SeatContext, patient: str, text: str) -> None:
        await ctx.tool("channel.send", patient=patient, text=text)

    # ------------------------------------------------------------------ the patient door
    async def on_text(self, ctx: SeatContext, patient: str, text: str, features: dict[str, Any] | None) -> None:
        s = self.talk(patient)
        self.hub.publish("patient_in", {"patient": patient, "text": text})
        s.transcript.append(text)
        r = read_message(text)
        s.lang = r.language
        say = TEXT[s.lang]
        if features or r.verbal:
            d = from_features(verbal=r.verbal, minimizer=r.minimizer, features=features)
            if s.distress is None or d["score"] >= s.distress["score"] or features:
                s.distress = d
            self.hub.publish("humanized", {"patient": patient, **d})

        if r.asks_imaging:
            try:
                ctx.act("patient.disclose_imaging")  # blocked by the manifest's refusal: audited and shown as such
            except PolicyViolation:
                pass
            await self._say(ctx, patient, say["imaging_hold"])
            return
        if r.asks_genetics:
            try:
                ctx.act("patient.disclose_genetics")
            except PolicyViolation:
                pass
            await self._say(ctx, patient, say["genetics_hold"])
            return

        drug = self.km.classify_drug(text)
        if drug is not None:
            await self._on_drug(ctx, patient, text, drug)
            return

        s.symptoms += [x for x in r.symptoms if x["code"] not in {y["code"] for y in s.symptoms}]
        s.pain_score = r.pain_score if r.pain_score is not None else s.pain_score
        s.weight_loss_kg = r.weight_loss_kg if r.weight_loss_kg is not None else s.weight_loss_kg
        s.minimizer = s.minimizer or r.minimizer
        if not s.symptoms and s.pain_score is None:
            await self._say(ctx, patient, say["hello"])
            return
        await self._evaluate(ctx, patient)

    async def _on_drug(self, ctx: SeatContext, patient: str, text: str, drug: dict[str, str]) -> None:
        say = TEXT[self.talk(patient).lang]
        rule = self.released_rules.get((patient, NSAID_RULE))
        if rule is not None and drug["class"] == "NSAID" and self._rule_active(ctx, patient):
            artifact_hash, release, who = rule
            ctx.guard_content("dose_instruction", artifact_hash, release)  # only with the clinician's release
            await self._say(ctx, patient, say["rule_nsaid"].format(who=who))
            return
        await self.report_medication(ctx, patient_id=patient, episode_id=self.episode(patient), text=text)
        await self._say(ctx, patient, say["med_ack"])

    def _rule_active(self, ctx: SeatContext, patient: str) -> bool:
        ctx.check_memory(3, "read")
        return any(k["key"] == NSAID_RULE for k in self.cortex.active_rules([(3, patient)]))

    async def _evaluate(self, ctx: SeatContext, patient: str) -> None:
        s = self.talk(patient)
        say = TEXT[s.lang]
        codes = {x["code"] for x in s.symptoms}
        distress = s.distress or {}
        flag = await ctx.tool("km.red_flags", symptoms=sorted(codes), pain_score=s.pain_score,
                              weight_loss_kg=s.weight_loss_kg, history_rcc=self.history_rcc(patient),
                              distress_level=distress.get("level"), distress_score=distress.get("score"))
        if flag and flag["urgency"] == "high" and not s.red_flag_sent:
            s.red_flag_sent = True
            await self._say(ctx, patient, say["safety"])
            await self.escalate(ctx, patient_id=patient, episode_id=self.episode(patient), red_flag=flag)
            await self._report(ctx, patient)
            return
        order = (("q1", "flank_pain" in codes), ("q2", "visible_hematuria" in codes),
                 ("q3", bool(codes & {"weight_loss", "nausea", "fever"})), ("q4", s.pain_score is not None))
        question = next((q for q, known in order if not known and q not in s.asked), None)
        if question:
            s.asked.add(question)
            await self._say(ctx, patient, f"({len(s.asked)}/4) {say[question]}")
        elif not s.reported:
            await self._report(ctx, patient)
            await self._say(ctx, patient, say["noted"])

    async def _report(self, ctx: SeatContext, patient: str) -> None:
        s = self.talk(patient)
        s.reported = True
        d = s.distress or {"score": 0.0, "level": "low", "underreporting": False}
        symptoms = [Symptom(code=x["code"], label=x["label"]) for x in s.symptoms]
        if s.pain_score is not None:
            symptoms.append(Symptom(code="pain_score", label=f"pain {s.pain_score}/10", severity=s.pain_score))
        await ctx.send(MessageType.SYMPTOM_REPORT, "case_briefer", SymptomReport(
            symptoms=symptoms, underreporting=bool(d.get("underreporting")), language=s.lang, channel="app",
            transcript=" / ".join(s.transcript)[:600],
            distress=Distress(score=d["score"], level="moderate" if d["level"] == "medium" else d["level"],
                              face=d.get("face"), voice=d.get("voice"), verbal=d.get("verbal"))),
            patient_id=patient, episode_id=self.episode(patient))

    async def on_lab_photo(self, ctx: SeatContext, patient: str, photo: dict[str, Any]) -> None:
        s = self.talk(patient)
        self.hub.publish("patient_in", {"patient": patient, "text": "📷 [photo of lab report]"})
        lab = await ctx.tool("lab.parse_report", photo=photo)
        s.awaiting_lab = False
        for to in ("renal_guardian", "archivist"):
            await ctx.send(MessageType.DATA_DELIVERED, to, DataDelivered(kind="lab", data=lab),
                           patient_id=patient, episode_id=self.episode(patient))
        await self._say(ctx, patient, TEXT[s.lang]["lab_thanks"].format(date=lab.get("date") or "n/a"))

    # ------------------------------------------------------------------ from the hospital
    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        s = self.talk(env.patient_id)
        say = TEXT[s.lang]
        if isinstance(payload, DataRequest) and payload.ask_patient:
            self.asked_patient.append(payload.ask_patient)
            s.awaiting_lab = True
            await self._say(ctx, env.patient_id, say["lab_ask"])
        elif isinstance(payload, ClinicianDecision):
            self.told_patient += [{"code": i.code, "text": i.patient_text} for i in payload.items]
            lines = [say["decision_head"].format(who=self.name_of(payload.signed_by)), *(f"• {i.patient_text}" for i in payload.items)]
            if payload.note:
                lines.append(payload.note)
            await self._say(ctx, env.patient_id, "\n".join([*lines, say["decision_tail"]]))


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
