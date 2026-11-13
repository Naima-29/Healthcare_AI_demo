
from __future__ import annotations

import unittest
from datetime import datetime
from kcd.tools import clinical_tools
from kcd.desk import Desk
from kcd.knowledge import KnowledgeModel
from kcd.web.seats import WebChannel, WebCompanion


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event, payload):
        self.events.append((event, payload))


class WearableConsentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create()
        self.km = KnowledgeModel.load()
        self.hub = FakeHub()

        self.companion = WebCompanion(
            self.km,
            self.hub,
            cortex=None,
            history_rcc=lambda patient: False,
            name_of=lambda clinician: clinician,
        )

        self.runtime = await self.desk.add_seat(
            self.companion,
            __import__(
                "kcd.seats.companion",
                fromlist=["companion_tools"],
            ).companion_tools(
                self.km,
                WebChannel(self.hub),
                wearables={"marco-r": {"steps": 5000}},
                wearables_consent=self.companion.has_wearables_consent,
            ),
        )

    async def asyncTearDown(self):
        await self.desk.close()

    async def test_patient_can_grant_wearables_consent(self):
        session = self.companion.session("marco-r")
        session.awaiting_wearables_consent = True

        await self.companion.on_text(
            self.runtime.context(), "marco-r", "yes", None
        )

        self.assertTrue(self.companion.has_wearables_consent("marco-r"))
        self.assertFalse(session.awaiting_wearables_consent)
        self.assertIn(
            ("patient_in", {"patient": "marco-r", "text": "yes"}),
            self.hub.events,
        )

    async def test_patient_can_refuse_wearables_consent(self):
        session = self.companion.session("marco-r")
        session.awaiting_wearables_consent = True

        await self.companion.on_text(
            self.runtime.context(), "marco-r", "no", None
        )

        self.assertFalse(self.companion.has_wearables_consent("marco-r"))
        self.assertFalse(session.awaiting_wearables_consent)

    async def test_patient_can_withdraw_wearables_consent(self):
        session = self.companion.session("marco-r")
        session.wearables_consent = True

        await self.companion.on_text(
            self.runtime.context(), "marco-r", "withdraw consent", None
        )

        self.assertFalse(self.companion.has_wearables_consent("marco-r"))
        self.assertFalse(session.awaiting_wearables_consent)


    async def test_yes_without_pending_request_does_not_grant_consent(self):
        session = self.companion.session("marco-r")

        handled = await self.companion._handle_wearables_consent(
            self.runtime.context(), "marco-r", "yes"
        )

        self.assertFalse(handled)
        self.assertFalse(session.wearables_consent)

    async def test_clinical_wearables_require_and_revoke_consent(self):
        session = self.companion.session("marco-r")

        tools = clinical_tools(
            fhir=None,
            km=self.km,
            now=lambda: datetime.now(),
            wearables={"marco-r": {"steps": 5000}},
            wearables_consent=self.companion.has_wearables_consent,
        )

        with self.assertRaisesRegex(PermissionError, "consent"):
            await tools["wearables.read"](patient_id="marco-r")

        session.wearables_consent = True
        result = await tools["wearables.read"](patient_id="marco-r")
        self.assertEqual(result["summary_7d"]["steps"], 5000)

        session.wearables_consent = False
        with self.assertRaisesRegex(PermissionError, "consent"):
            await tools["wearables.read"](patient_id="marco-r")

    async def test_companion_wearables_require_and_revoke_consent(self):
        session = self.companion.session("marco-r")
        ctx = self.runtime.context()

        with self.assertRaisesRegex(PermissionError, "consent"):
            await ctx.tool("wearables.read", patient_id="marco-r", days=7)

        session.wearables_consent = True
        result = await ctx.tool("wearables.read", patient_id="marco-r", days=7)
        self.assertEqual(result["summary"]["steps"], 5000)

        session.wearables_consent = False
        with self.assertRaisesRegex(PermissionError, "consent"):
            await ctx.tool("wearables.read", patient_id="marco-r", days=7)
