"""Person 2: the patient-facing Companion seat running on kcd_core.

The seat owns patient intake, deterministic NLU/distress features, safety-floor
red flags, typed A2A reports, lab-report collection, wearables consent and
forwarding of clinician-signed patient wording. It never diagnoses or prescribes.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.pep import PolicyViolation
from kcd_core.contracts.messages import (
    ClinicianDecision,
    DataDelivered,
    DataRequest,
    Distress,
    MedicationReport,
    RedFlag,
    RedFlagEscalation,
    Symptom,
    SymptomReport,
)
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel
from kcd.cortex import CoreMemory
from kcd.knowledge import KnowledgeModel
from kcd.patient.fuzzy import from_features
from kcd.patient.nlu import extract

NSAID_RULE = "avoid_drug_class:NSAID"

RULE_NSAID = {
    "it": (
        "Il suo nefrologo ({who}) le ha indicato di evitare "
        "l’ibuprofene e gli antinfiammatori simili. "
        "Per il dolore segua il piano che le ha dato il medico."
    ),
    "en": (
        "Your nephrologist ({who}) asked you to avoid ibuprofen "
        "and similar anti-inflammatory painkillers. "
        "For pain, follow the plan your doctor gave you."
    ),
}

TEXT = {
    "en": {
        "hello": "I am here with you. Tell me how you feel today.",
        "q1": (
            "Where exactly is the pain: on the side under the ribs, "
            "or lower in the back? Does it get worse when you move?"
        ),
        "q2": (
            "Have you noticed any change in your urine, such as blood, "
            "darker urine, burning or difficulty passing urine?"
        ),
        "q3": (
            "Have you also had fever, chills, nausea, weight loss or loss "
            "of appetite, or felt weaker than usual?"
        ),
        "q4": (
            "From 0 to 10, how strong is the pain now? Any dizziness, "
            "or trouble with everyday activities?"
        ),
        "noted": "Thank you. I have shared this with your care team. If anything gets worse, message me.",
        "safety": (
            "What you describe needs medical assessment today. Please seek urgent medical care now. "
            "If you feel faint, have heavy bleeding or your pain becomes severe, call your local emergency number. "
            "I have alerted your care team."
        ),
        "med_ack": (
            "Thank you. I have reported this medicine to your care team. "
            "Please do not change your medicines yourself; your clinician will advise you."
        ),
        "lab_ask": (
            "Your care team needs your latest blood-test report. "
            "Please send a clear photo of the most recent report."
        ),
        "lab_thanks": "Thank you. I have sent the report to your care team.",
        "imaging_hold": "Your images must be reviewed by your doctor. I cannot interpret or disclose imaging results.",
        "genetics_hold": "A specialist will discuss these results with you in a dedicated conversation.",
        "decision_head": "Your clinician ({who}) has reviewed your case and provided the following instructions:",
        "decision_tail": "Please contact your care team if you have any questions or if your symptoms change.",
    },
    "it": {
        "hello": "Sono qui con lei. Mi racconti come si sente oggi.",
        "q1": (
            "Dove sente esattamente il dolore: sul fianco sotto le costole "
            "o più in basso sulla schiena? Peggiora quando si muove?"
        ),
        "q2": (
            "Ha notato cambiamenti nelle urine, per esempio sangue, "
            "urine più scure, bruciore o difficoltà a urinare?"
        ),
        "q3": "Ha avuto febbre, brividi, nausea, perdita di peso o appetito, o si sente più debole del solito?",
        "q4": (
            "Da 0 a 10, quanto è forte il dolore adesso? "
            "Ha giramenti di testa o difficoltà nelle attività quotidiane?"
        ),
        "noted": "Grazie. Ho trasmesso le informazioni al suo team di cura. Se qualcosa peggiora, mi scriva.",
        "safety": (
            "Quello che descrive richiede una valutazione medica oggi. Cerchi subito assistenza medica urgente. "
            "Se si sente svenire, ha molto sangue o il dolore diventa forte, chiami il numero di emergenza locale. "
            "Ho avvisato il suo team di cura."
        ),
        "med_ack": (
            "Grazie. Ho segnalato questo farmaco al suo team. "
            "Non modifichi da solo la terapia: sarà il medico a darle indicazioni."
        ),
        "lab_ask": (
            "Il suo team ha bisogno dell'ultimo esame del sangue. "
            "Invii una foto leggibile del referto più recente."
        ),
        "lab_thanks": "Grazie. Ho inviato il referto al suo team di cura.",
        "imaging_hold": (
            "Le immagini devono essere valutate dal medico. "
            "Non posso interpretare o comunicare i risultati delle immagini."
        ),
        "genetics_hold": "Di questi risultati le parlerà lo specialista, in un colloquio dedicato.",
        "decision_head": "Il suo medico ({who}) ha esaminato il suo caso e ha fornito le seguenti indicazioni:",
        "decision_tail": "Contatti il suo team di cura se ha domande o se i suoi sintomi cambiano.",
    },
}


@dataclass
class PatientSession:
    language: str = "en"
    symptoms: list[dict[str, Any]] = field(default_factory=list)
    pain_score: int | None = None
    weight_loss_kg: float | None = None
    minimizer: bool = False
    asked: set[str] = field(default_factory=set)
    distress: dict[str, Any] | None = None
    red_flag_sent: bool = False
    reported: bool = False
    transcript: list[str] = field(default_factory=list)
    wearables_consent: bool = False
    awaiting_wearables_consent: bool = False


def parse_lab_report(ocr_text: str) -> dict[str, Any]:
    """Parse a small, explicit subset of lab OCR; reject missing eGFR/creatinine."""
    text = (ocr_text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "egfr" in data:
            return {k: data[k] for k in ("egfr", "creatinine", "date") if k in data}
    except json.JSONDecodeError:
        pass

    egfr_match = re.search(r"\beGFR\b[^\d]{0,40}(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    creat_match = re.search(r"\b(?:serum\s+)?creatinine\b[^\d]{0,40}(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if not egfr_match or not creat_match:
        raise ValueError("lab OCR did not contain both eGFR and creatinine")
    return {
        "egfr": float(egfr_match.group(1)),
        "creatinine": float(creat_match.group(1)),
        "date": date_match.group(1) if date_match else None,
    }


def companion_tools(
    km: KnowledgeModel,
    channel: Any,
    *,
    wearables: dict[str, dict[str, Any]] | None = None,
    wearables_consent: Callable[[str], bool] | None = None,
) -> dict[str, Callable[..., Any]]:
    """Build local tool adapters; production deployments replace them with MCP clients."""
    summaries = dict(wearables or {})

    async def red_flags(**kwargs: Any) -> dict[str, Any] | None:
        return km.red_flags(**kwargs)

    async def parse_report(*, ocr_text: str) -> dict[str, Any]:
        return parse_lab_report(ocr_text)

    async def send_message(*, patient_id: str, text: str) -> dict[str, Any]:
        return await channel.send(patient_id, text)

    async def read_wearables(*, patient_id: str, days: int = 7) -> dict[str, Any]:
        if not 1 <= int(days) <= 90:
            raise ValueError("days must be between 1 and 90")

        if wearables_consent is None or not wearables_consent(patient_id):
            raise PermissionError(
                "explicit patient consent is required before reading wearables"
            )

        return {"summary": dict(summaries.get(patient_id, {})), "days": int(days)}

    return {
        "km.red_flags": red_flags,
        "lab.parse_report": parse_report,
        "channel.send": send_message,
        "wearables.read": read_wearables,
    }


class Companion(Seat):
    seat_id = "companion"

    def __init__(
        self,
        km: KnowledgeModel,
        *,
        demo_lab_reports: dict[str, dict[str, Any] | str] | None = None,
        history_provider: Callable[[str], dict[str, Any]] | None = None,
        cortex: Any | None = None,
    ) -> None:
        self.km = km
        self.demo_lab_reports = dict(demo_lab_reports or {})
        self.history_provider = history_provider
        self.cortex = cortex
        self.sessions: dict[str, PatientSession] = {}
        self.told_patient: list[dict[str, Any]] = []
        self.asked_patient: list[str] = []
        self.released_rules: dict[tuple[str, str], tuple[str, Any, str]] = {}

    def session(self, patient_id: str) -> PatientSession:
        if patient_id not in self.sessions:
            self.sessions[patient_id] = PatientSession()
        return self.sessions[patient_id]

    def register_confirmed_rule(
        self,
        patient_id: str,
        rule_key: str,
        artifact_hash: str,
        release: Any,
        clinician_name: str,
    ) -> None:
        """Store a rule release supplied after clinician confirmation."""
        self.released_rules[(patient_id, rule_key)] = (
            artifact_hash,
            release,
            clinician_name,
        )

    def _confirmed_rule_active(
        self,
        ctx: SeatContext,
        patient_id: str,
        rule_key: str,
    ) -> bool:
        if self.cortex is None or (patient_id, rule_key) not in self.released_rules:
            return False

        mem = CoreMemory(self.cortex, ctx, self.seat_id, patient_id)

        return any(
            rule["key"] == rule_key
            for rule in mem.active_patient_rules()
        )

    async def _say(self, ctx: SeatContext, patient_id: str, text: str) -> None:
        await ctx.tool("channel.send", patient_id=patient_id, text=text)

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, DataRequest):
            if payload.ask_patient:
                self.asked_patient.append(payload.ask_patient)

                if payload.what == "wearable_data_consent":
                    session = self.session(env.patient_id)
                    session.awaiting_wearables_consent = True
                    await self._say(
                        ctx,
                        env.patient_id,
                        "Your care team would like permission to access your wearable data "
                        "(such as heart rate and blood pressure) to support your care. "
                        "Do you agree? Please reply yes or no. You can withdraw consent at any time.",
                    )
                    return

                session = self.session(env.patient_id)
                prompt = TEXT[session.language]["lab_ask"]
                await self._say(ctx, env.patient_id, prompt)

                # The golden path may inject a synthetic upload to exercise the existing Renal Guardian flow.
                demo_report = self.demo_lab_reports.get(env.patient_id)
                if demo_report is not None:
                    ocr_text = json.dumps(demo_report) if isinstance(demo_report, dict) else demo_report
                    await self.attach_lab_report(
                        ctx, patient_id=env.patient_id, episode_id=env.episode_id, ocr_text=ocr_text
                    )
        elif isinstance(payload, ClinicianDecision):
            await self._deliver_signed_decision(ctx, env.patient_id, payload)

    async def run_telegram(
        self,
        channel: Any,
        runtime: Any,
        *,
        patient_id: str,
        episode_id: str,
        stop: Any,
    ) -> None:
        """Connect Telegram text updates to the Companion; unsupported media fails closed."""
        async def inbound(in_patient: str, text: str, metadata: dict[str, Any]) -> None:
            kind = metadata.get("kind", "text")
            if kind == "text" and text:
                await self.on_patient_text(
                    runtime.context(), patient_id=in_patient, episode_id=episode_id,
                    text=text, channel="telegram",
                )
            else:
                await self._say(
                    runtime.context(), in_patient,
                    "I received your attachment, but media transcription/OCR is not configured in this prototype. "
                    "Please send the information as text for now.",
                )

        await channel.poll_updates(inbound, stop, patient_id=patient_id)

    async def on_patient_text(
        self,
        ctx: SeatContext,
        *,
        patient_id: str,
        episode_id: str,
        text: str,
        features: dict[str, Any] | None = None,
        channel: str = "app",
    ) -> None:
        session = self.session(patient_id)
        parsed = extract(text, self.km.classify_drug)
        session.language = parsed["language"]
        session.transcript.append(text[:2000])
        lang = TEXT[session.language]

        if features is not None or parsed["verbal"] > 0:
            distress = from_features(
                verbal=parsed["verbal"], minimizer=parsed["minimizer"], features=features or {}
            )
            if session.distress is None or distress["score"] >= session.distress["score"] or features:
                session.distress = distress

        for intent, action in (
            ("ask_imaging_result", "patient.disclose_imaging"),
            ("ask_genetics", "patient.disclose_genetics"),
        ):
            if intent in parsed["intents"]:
                try:
                    ctx.act(action)
                except PolicyViolation:
                    pass

        if "ask_imaging_result" in parsed["intents"]:
            await self._say(ctx, patient_id, lang["imaging_hold"])
            return

        if "ask_genetics" in parsed["intents"]:
            await self._say(ctx, patient_id, lang["genetics_hold"])
            return

        medication_reported = False
        rule_reply_sent = False
        drug = parsed["drug"]

        if drug and drug.get("class") == "NSAID" and self._confirmed_rule_active(
            ctx, patient_id, NSAID_RULE
        ):
            artifact_hash, release, who = self.released_rules[
                (patient_id, NSAID_RULE)
            ]

            ctx.guard_content("dose_instruction", artifact_hash, release)

            await self._say(
                ctx,
                patient_id,
                RULE_NSAID[session.language].format(who=who),
            )
            rule_reply_sent = True

        if drug:
            await self.report_medication(
                ctx,
                patient_id=patient_id,
                episode_id=episode_id,
                text=text,
                drug=drug,
            )

            if not rule_reply_sent:
                await self._say(ctx, patient_id, lang["med_ack"])

            medication_reported = True

        # Continue processing symptoms even when medication was reported.
        for symptom in parsed["symptoms"]:
            if not any(
                existing["code"] == symptom["code"]
                for existing in session.symptoms
            ):
                session.symptoms.append(symptom)

        if parsed["pain_score"] is not None:
            session.pain_score = parsed["pain_score"]

        if parsed["weight_loss_kg"] is not None:
            session.weight_loss_kg = parsed["weight_loss_kg"]

        session.minimizer = session.minimizer or parsed["minimizer"]

        if not session.symptoms and session.pain_score is None:
            if not medication_reported and not rule_reply_sent:
                await self._say(ctx, patient_id, lang["hello"])
            return

        await self._evaluate(
            ctx,
            patient_id=patient_id,
            episode_id=episode_id,
            channel=channel,
        )

    async def _evaluate(
        self, ctx: SeatContext, *, patient_id: str, episode_id: str, channel: str
    ) -> None:
        session = self.session(patient_id)
        lang = TEXT[session.language]
        history = self.history_provider(patient_id) if self.history_provider else {}
        if self.history_provider:
            ctx.check_memory(3, "read")
        red_flag = await ctx.tool(
            "km.red_flags",
            symptoms=[s["code"] for s in session.symptoms],
            pain_score=session.pain_score,
            weight_loss_kg=session.weight_loss_kg,
            history_rcc=bool(history.get("rcc", False)),
            distress_level=(session.distress or {}).get("level"),
            distress_score=(session.distress or {}).get("score"),
        )
        # Only "high" gets the safety script and a page; a single symptom ("medium") goes on with the interview,
        # and must not use up the one escalation that a later "high" needs.
        if red_flag and red_flag["urgency"] == "high" and not session.red_flag_sent:
            session.red_flag_sent = True
            await self._say(ctx, patient_id, lang["safety"])
            await self.escalate(ctx, patient_id=patient_id, episode_id=episode_id, red_flag=red_flag,
                                send_script=False)
            await self.report_symptoms(ctx, patient_id=patient_id, episode_id=episode_id, channel=channel)
            return

        has = {s["code"] for s in session.symptoms}
        next_question = (
            "q1" if "flank_pain" not in has and "q1" not in session.asked else
            "q2" if "visible_hematuria" not in has and "q2" not in session.asked else
            "q3" if not ({"weight_loss", "nausea", "fever"} & has) and "q3" not in session.asked else
            "q4" if session.pain_score is None and "q4" not in session.asked else None
        )
        if next_question:
            session.asked.add(next_question)
            await self._say(ctx, patient_id, f"({len(session.asked)}/4) {lang[next_question]}")
            return
        if not session.reported:
            await self.report_symptoms(ctx, patient_id=patient_id, episode_id=episode_id, channel=channel)
            await self._say(ctx, patient_id, lang["noted"])

    async def report_symptoms(
        self, ctx: SeatContext, *, patient_id: str, episode_id: str, channel: str = "app"
    ) -> None:
        session = self.session(patient_id)
        session.reported = True
        distress = session.distress or {"score": 0.0, "level": "low", "underreporting": False}
        symptoms = [
            Symptom(
                code=s["code"], label=s["label"], severity=s.get("severity"),
                onset=s.get("onset"), source_text=s.get("source_text"),
            )
            for s in session.symptoms
        ]
        if session.pain_score is not None:
            symptoms.append(Symptom(
                code="pain_score", label=f"pain {session.pain_score}/10", severity=session.pain_score
            ))
        if not symptoms:
            return
        payload = SymptomReport(
            symptoms=symptoms,
            distress=Distress(
                score=distress["score"], level=distress["level"],
                face=distress.get("face"), voice=distress.get("voice"),
                verbal=distress.get("verbal"),
            ),
            underreporting=bool(distress.get("underreporting")),
            transcript=" / ".join(session.transcript)[:2000] or None,
            language=session.language,
            channel=channel if channel in {"telegram", "whatsapp", "app"} else "app",
        )
        await ctx.send(MessageType.SYMPTOM_REPORT, "case_briefer", payload,
                       patient_id=patient_id, episode_id=episode_id)

    async def report_medication(
        self,
        ctx: SeatContext,
        *,
        patient_id: str,
        episode_id: str,
        text: str,
        drug: dict[str, str] | None = None,
    ) -> None:
        classified = drug or self.km.classify_drug(text)
        if not classified:
            raise ValueError(f"no known drug in {text!r}")
        payload = MedicationReport(
            drug=classified["drug"], drug_class=classified["class"], dose_text=text,
            reason="self-reported by patient",
        )
        await ctx.send(MessageType.MEDICATION_REPORT, "renal_guardian", payload,
                       patient_id=patient_id, episode_id=episode_id)

    async def escalate(
        self,
        ctx: SeatContext,
        *,
        patient_id: str,
        episode_id: str,
        red_flag: dict[str, Any],
        send_script: bool = True,
        unit: str | None = None,
    ) -> None:
        if send_script:
            await self._say(ctx, patient_id, TEXT[self.session(patient_id).language]["safety"])
        payload = RedFlagEscalation(
            urgency=red_flag["urgency"],
            pattern=red_flag["pattern"],
            evidence=red_flag["evidence"],
            citations=red_flag.get("citations", []),
            safety_script_sent=True,
            unit=unit,
            flags=[RedFlag(code=red_flag["pattern"], detected_by="rules")],
        )
        for receiver in ("case_briefer", "archivist"):
            await ctx.send(MessageType.RED_FLAG_ESCALATION, receiver, payload,
                           patient_id=patient_id, episode_id=episode_id)

    async def attach_lab_report(
        self, ctx: SeatContext, *, patient_id: str, episode_id: str, ocr_text: str
    ) -> dict[str, Any]:
        parsed = await ctx.tool("lab.parse_report", ocr_text=ocr_text)
        data = {**parsed, "source": "patient-provided lab report"}
        payload = DataDelivered(kind="lab", data=data)
        for receiver in ("renal_guardian", "archivist"):
            await ctx.send(MessageType.DATA_DELIVERED, receiver, payload,
                           patient_id=patient_id, episode_id=episode_id)
        await self._say(ctx, patient_id, TEXT[self.session(patient_id).language]["lab_thanks"])
        return data

    async def sync_wearables(
        self,
        ctx: SeatContext,
        *,
        patient_id: str,
        episode_id: str,
        consented: bool,
        days: int = 7,
    ) -> dict[str, Any]:
        if not consented:
            raise PermissionError("explicit patient consent is required before reading wearables")
        summary = await ctx.tool("wearables.read", patient_id=patient_id, days=days)
        payload = DataDelivered(kind="wearable_summary", data=summary)
        for receiver in ("renal_guardian", "archivist"):
            await ctx.send(MessageType.DATA_DELIVERED, receiver, payload,
                           patient_id=patient_id, episode_id=episode_id)
        return summary

    async def _deliver_signed_decision(
        self, ctx: SeatContext, patient_id: str, decision: ClinicianDecision
    ) -> None:
        # Core's current ClinicianDecision carries the verified release id/signature but not
        # the full Release object. The Case Briefer is the trust boundary that verifies the
        # clinician's signature against the exact case-pack hash before publishing this message.
        if not decision.signature or not decision.signed_by or not decision.decision_id or not decision.artifact_hash:
            raise ValueError("clinician decision is missing its signed-release provenance")
        session = self.session(patient_id)
        lang = TEXT[session.language]
        lines = [lang["decision_head"].format(who=decision.signed_by)]
        lines.extend(f"• {item.patient_text}" for item in decision.items)
        if decision.note:
            lines.append(decision.note)
        lines.append(lang["decision_tail"])
        text = "\n".join(lines)
        await self._say(ctx, patient_id, text)
        self.told_patient.extend(
            [{"code": item.code, "text": item.patient_text, "decision_id": decision.decision_id}
             for item in decision.items]
        )
