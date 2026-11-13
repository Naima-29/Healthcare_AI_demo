from __future__ import annotations

import unittest

from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import ClinicianDecision, DataRequest

from kcd.desk import Desk
from kcd.knowledge import KnowledgeModel
from kcd.patient.channel import PatientChannel
from kcd.patient.fuzzy import from_features
from kcd.patient.nlu import extract
from kcd.seats.companion import Companion, companion_tools, parse_lab_report
from kcd.cortex import Cortex, HumanActor
from kcd.cortex.auth import release_verifier


class CompanionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create()
        self.km = KnowledgeModel.load()
        self.channel = PatientChannel()

    async def asyncTearDown(self):
        await self.desk.close()

    async def start_companion(self, **kwargs):
        self.companion = Companion(self.km, **kwargs)
        self.runtime = await self.desk.add_seat(
            self.companion,
            companion_tools(
                self.km,
                self.channel,
                wearables={
                    "pt_marco": {"steps": 5000, "resting_hr": 68}
                },
                wearables_consent=lambda patient_id: (
                    self.companion.session(patient_id).wearables_consent
                ),
            ),
        )
        return self.companion, self.runtime

    async def test_confirmed_nsaid_rule_warns_patient(self):
        # Create Cortex using the real clinician signature verifier.
        cortex = Cortex(
            human_verifier=release_verifier(self.desk.clinician_keys)
        )

        # Add evidence and propose a patient-specific NSAID rule.
        ev = cortex.append_evidence(
            3,
            "pt_marco",
            kind="clinician_decision",
            source="dr_rossi",
            content={"instruction": "Avoid NSAIDs"},
        )

        rule = cortex.propose(
            3,
            "pt_marco",
            key="avoid_drug_class:NSAID",
            value={"instruction": "avoid"},
            source="dr_rossi",
            evidence_ids=[ev["id"]],
            proposed_by="seat:archivist",
        )

        # Sign and confirm the rule as a real clinician.
        confirmation_release = self.desk.release(
            rule["id"],
            "sha256:" + "0" * 64,
            release_id="rel_confirm",
        )

        cortex.confirm(
            rule["id"],
            HumanActor(
                "dr_rossi",
                "clinician:nephrology",
                confirmation_release,
            ),
        )

        # Create the separate patient-facing release.
        patient_release = self.desk.release(
            rule["id"],
            "sha256:" + "0" * 64,
            classes=["dose_instruction"],
            release_id="rel_patient",
        )

        companion, runtime = await self.start_companion(cortex=cortex)

        companion.register_confirmed_rule(
            patient_id="pt_marco",
            rule_key="avoid_drug_class:NSAID",
            artifact_hash=patient_release.artifact_hash,
            release=patient_release,
            clinician_name="Dr. Rossi",
        )

        await companion.on_patient_text(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            text="I take ibuprofen 600 mg three times a day.",
        )

        messages = [
            item["text"] for item in self.channel.outbox
        ]

        self.assertTrue(
            any("avoid ibuprofen" in message.lower() for message in messages),
            messages,
        )

    async def test_unconfirmed_nsaid_rule_does_not_warn_patient(self):
        cortex = Cortex(
            human_verifier=release_verifier(self.desk.clinician_keys)
        )

        ev = cortex.append_evidence(
            3,
            "pt_marco",
            kind="clinician_decision",
            source="dr_rossi",
            content={"instruction": "Avoid NSAIDs"},
        )

        rule = cortex.propose(
            3,
            "pt_marco",
            key="avoid_drug_class:NSAID",
            value={"instruction": "avoid"},
            source="dr_rossi",
            evidence_ids=[ev["id"]],
            proposed_by="seat:archivist",
        )

        self.assertEqual(rule["status"], "shadow")

        companion, runtime = await self.start_companion(cortex=cortex)

        await companion.on_patient_text(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            text="I take ibuprofen 600 mg three times a day.",
        )

        messages = [
            item["text"].lower() for item in self.channel.outbox
        ]

        self.assertFalse(
            any("avoid ibuprofen" in message for message in messages),
            messages,
        )

    async def test_red_flag_and_safety_script(self):
        companion, runtime = await self.start_companion(
            history_provider=lambda _patient: {"rcc": True}
        )
        await companion.on_patient_text(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            text="I have severe pain in my flank, blood in my urine, and pain is 8 out of 10.",
        )
        await self.desk.bus.drain()

        self.assertTrue(any("medical assessment today" in m["text"] for m in self.channel.outbox))
        routed = self.desk.delivered_to("case_briefer")
        self.assertTrue(any(env.type == MessageType.RED_FLAG_ESCALATION for env in routed))
        self.assertTrue(any(env.type == MessageType.SYMPTOM_REPORT for env in routed))

    async def test_single_symptom_does_not_page_or_use_up_the_escalation(self):
        companion, runtime = await self.start_companion(history_provider=lambda _patient: {"rcc": True})

        async def say(text):
            await companion.on_patient_text(runtime.context(), patient_id="pt_marco", episode_id="ep_1", text=text)
            await self.desk.bus.drain()

        await say("My urine is pink.")
        self.assertEqual(self.desk.pager.pages, [])
        self.assertFalse(any("medical assessment today" in m["text"] for m in self.channel.outbox))
        await say("I have pain in my flank, 8 out of 10.")
        self.assertEqual(len(self.desk.pager.pages), 1)
        self.assertTrue(any("medical assessment today" in m["text"] for m in self.channel.outbox))

    async def test_genetics_question_goes_to_the_specialist(self):
        companion, runtime = await self.start_companion()
        await companion.on_patient_text(runtime.context(), patient_id="pt_marco", episode_id="ep_1",
                                        text="What does my DNA say?")
        self.assertIn("specialist", self.channel.outbox[-1]["text"])

    
    async def test_imaging_question_is_referred_to_doctor(self):
        companion, runtime = await self.start_companion()

        await companion.on_patient_text(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            text="What did my CT scan show?",
        )

        self.assertIn(
            "cannot interpret or disclose imaging results",
            self.channel.outbox[-1]["text"],
        )


    async def test_medication_report_to_renal_guardian(self):
        companion, runtime = await self.start_companion()
        await companion.on_patient_text(
            runtime.context(), patient_id="pt_marco", episode_id="ep_1",
            text="I take ibuprofen 600 mg three times a day.",
        )
        await self.desk.bus.drain()

        reports = self.desk.delivered_to("renal_guardian")
        self.assertTrue(any(env.type == MessageType.MEDICATION_REPORT for env in reports))
        self.assertTrue(any("do not change your medicines" in m["text"] for m in self.channel.outbox))

    async def test_medication_and_symptoms_in_same_message(self):
        companion, runtime = await self.start_companion(
            history_provider=lambda _patient: {"rcc": True}
        )

        await companion.on_patient_text(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            text=(
                "I take ibuprofen 600 mg three times a day. "
                "I also have severe pain in my flank, blood in my urine, "
                "and pain is 8 out of 10."
            ),
        )
        await self.desk.bus.drain()

        # Medication must reach the Renal Guardian.
        medication_reports = self.desk.delivered_to("renal_guardian")
        self.assertTrue(
            any(env.type == MessageType.MEDICATION_REPORT
                for env in medication_reports)
        )

        # Symptoms must also reach the Case Briefer.
        case_reports = self.desk.delivered_to("case_briefer")
        self.assertTrue(
            any(env.type == MessageType.SYMPTOM_REPORT
                for env in case_reports)
        )

        # The emergency warning must not be skipped.
        self.assertTrue(
            any(env.type == MessageType.RED_FLAG_ESCALATION
                for env in case_reports)
        )

        self.assertTrue(
            any("medical assessment today" in m["text"]
                for m in self.channel.outbox)
        )

    async def test_data_request_and_demo_lab_upload(self):
        companion, runtime = await self.start_companion(
            demo_lab_reports={"pt_marco": {"egfr": 52, "creatinine": 1.41, "date": "2026-10-08"}}
        )
        env = self.desk.envelope(
            MessageType.DATA_REQUEST, "renal_guardian", "companion",
            {"what": "lab", "why": "recent eGFR required", "ask_patient": "Please send your latest lab report."},
        )
        await companion.handle(env, DataRequest.model_validate(env.payload), runtime.context(env))
        await self.desk.bus.drain()

        self.assertTrue(any("latest blood-test report" in m["text"] for m in self.channel.outbox))
        renal = self.desk.delivered_to("renal_guardian")
        self.assertTrue(any(env.type == MessageType.DATA_DELIVERED and env.typed_payload().kind == "lab"
                            for env in renal))

    async def test_forwards_provenanced_clinician_decision_wording(self):
        companion, runtime = await self.start_companion()
        decision = ClinicianDecision(
            case_id="case-1",
            decision_id="rel-1",
            artifact_hash="sha256:" + "a" * 64,
            signed_by="dr_rossi",
            role="nephrologist",
            items=[{"code": "follow_up", "patient_text": "Please attend your scheduled review."}],
            signature="verified-release-signature",
        )
        env = self.desk.envelope(
            MessageType.CLINICIAN_DECISION, "case_briefer", "companion",
            decision.model_dump(mode="json", by_alias=True),
        )
        await companion.handle(env, decision, runtime.context(env))

        self.assertIn("Please attend your scheduled review.", self.channel.outbox[-1]["text"])
        self.assertEqual(companion.told_patient[0]["decision_id"], "rel-1")

    async def test_wearables_require_consent_and_forward_summary(self):
        companion, runtime = await self.start_companion()
        with self.assertRaisesRegex(PermissionError, "consent"):
            await companion.sync_wearables(
                runtime.context(), patient_id="pt_marco", episode_id="ep_1", consented=False
            )
        companion.session("pt_marco").wearables_consent = True
        result = await companion.sync_wearables(
            runtime.context(), patient_id="pt_marco", episode_id="ep_1", consented=True
        )
        await self.desk.bus.drain()
        self.assertEqual(result["summary"]["steps"], 5000)
        self.assertTrue(any(env.type == MessageType.DATA_DELIVERED
                            for env in self.desk.delivered_to("renal_guardian")))

    
    async def test_patient_lab_report_ocr_is_forwarded(self):
        companion, runtime = await self.start_companion()

        result = await companion.attach_lab_report(
            runtime.context(),
            patient_id="pt_marco",
            episode_id="ep_1",
            ocr_text=(
                "Patient Name: Marco\n"
                "Date: 2026-11-12\n"
                "eGFR: 95 mL/min/1.73 m2\n"
                "Creatinine: 0.9 mg/dL"
            ),
        )
        await self.desk.bus.drain()

        self.assertEqual(result["egfr"], 95.0)
        self.assertEqual(result["creatinine"], 0.9)

        for receiver in ("renal_guardian", "archivist"):
            messages = self.desk.delivered_to(receiver)
            self.assertTrue(
                any(
                    env.type == MessageType.DATA_DELIVERED
                    and env.typed_payload().kind == "lab"
                    for env in messages
                ),
                f"Lab report was not delivered to {receiver}",
            )

        self.assertTrue(
            any("thank" in item["text"].lower()
                for item in self.channel.outbox)
        )



class CompanionPureFunctionsTest(unittest.TestCase):
    def test_nlu_and_fuzzy_features(self):
        parsed = extract("Ho dolore al fianco e sangue nelle urine, dolore 8 su 10.")
        self.assertEqual(parsed["language"], "it")
        self.assertGreaterEqual(
            {item["code"] for item in parsed["symptoms"]},
            {"flank_pain", "visible_hematuria"},
        )
        result = from_features(
            verbal=0.2,
            minimizer=True,
            features={"au": {"AU4": 3, "AU7": 2, "AU43": 1},
                      "voice": {"tremor": 0.8, "pitch_var": 0.7, "rate_dev": -0.5}},
        )
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 1)
        self.assertIn(result["level"], {"low", "moderate", "high"})
        self.assertTrue(result["underreporting"])

    def test_lab_ocr_parser(self):
        parsed = parse_lab_report('{"egfr": 52, "creatinine": 1.41, "date": "2026-10-08"}')
        self.assertEqual(parsed["egfr"], 52)
        with self.assertRaisesRegex(ValueError, "both eGFR and creatinine"):
            parse_lab_report("This is not a readable lab report.")

    def test_hematuria_negation(self):
        negative_messages = [
            "I have no blood in my urine.",
            "There is no blood in the urine.",
            "I don't have blood in my urine.",
            "My urine is not pink.",
            "I have no visible blood in my urine.",
            "Non ho sangue nelle urine.",
        ]

        for message in negative_messages:
            with self.subTest(message=message):
                parsed = extract(message)
                self.assertNotIn(
                    "visible_hematuria",
                    {item["code"] for item in parsed["symptoms"]},
                )

        positive_messages = [
            "I have blood in my urine.",
            "My urine is pink.",
            "I noticed blood in the urine.",
            "Ho sangue nelle urine.",
        ]

        for message in positive_messages:
            with self.subTest(message=message):
                parsed = extract(message)
                self.assertIn(
                    "visible_hematuria",
                    {item["code"] for item in parsed["symptoms"]},
                )
