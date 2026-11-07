"""The Companion, Case Briefer and Safety Verifier, connected to the browser.

The Companion is Meet's real seat (kcd/seats/companion.py). WebCompanion adds only what the browser door needs: the
phone as its channel, the live events, the refusal declared at the policy layer when a patient asks for a result,
and a rule a clinician confirmed. The Case Briefer is Chrislin's (kcd/seats/case_briefer.py), re-issuing its case
pack whenever a verified finding arrives. The messages are real, signed and checked.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kcd_core.contracts import Envelope, Release
from kcd_core.contracts.messages import ClinicianDecision, DataRequest
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import SeatContext
from pydantic import BaseModel

from kcd.patient.nlu import extract
from kcd.seats.case_briefer import CaseBriefer
from kcd.seats.companion import Companion
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.web.hub import Hub

NSAID_RULE = "avoid_drug_class:NSAID"

RULE_NSAID = {
    "it": "Il suo nefrologo ({who}) le ha indicato di evitare l’ibuprofene e gli antinfiammatori simili. "
          "Per il dolore segua il piano che le ha dato il medico.",
    "en": "Your nephrologist ({who}) asked you to avoid ibuprofen and similar anti-inflammatory painkillers. "
          "For pain, follow the plan your doctor gave you.",
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


class WebCaseBriefer(CaseBriefer):
    """Chrislin's Case Briefer. The browser also shows each verified CT signal and genomic context on its own card,
    so each new finding goes to `on_finding(patient, kind, payload)`."""

    def __init__(self, *, now: Callable[[], Any], language_of: Callable[[str], str],
                 on_finding: Callable[[str, str, dict[str, Any]], None]):
        super().__init__(now=now, strict_binding=True, auto_publish=True, auto_genomics=True)
        self.language_of, self.on_finding = language_of, on_finding

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        self.draft_lang = self.language_of(env.patient_id)  # the patient's wording, in the patient's language
        before = self.cases.get(env.patient_id)
        n = len(before.received) if before is not None and not before.closed else 0
        await super().handle(env, payload, ctx)
        case = self.cases.get(env.patient_id)
        if case is not None and len(case.received) > n:
            self.on_finding(env.patient_id, *case.received[n])


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
