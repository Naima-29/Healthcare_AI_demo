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
            self.companion, companion_tools(self.km, self.channel, wearables={
                "pt_marco": {"steps": 5000, "resting_hr": 68}
            })
        )
        return self.companion, self.runtime

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

        result = await companion.sync_wearables(
            runtime.context(), patient_id="pt_marco", episode_id="ep_1", consented=True
        )
        await self.desk.bus.drain()
        self.assertEqual(result["summary"]["steps"], 5000)
        self.assertTrue(any(env.type == MessageType.DATA_DELIVERED
                            for env in self.desk.delivered_to("renal_guardian")))


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
