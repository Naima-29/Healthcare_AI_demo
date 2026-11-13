
import asyncio
import unittest

from kcd.web.seats import WebChannel


class FakeHub:
    def __init__(self):
        self.events = []

    def publish(self, event, payload):
        self.events.append((event, payload))


class FakeTelegram:
    async def send(self, patient_id, text):
        return {"delivered": True, "transport": "telegram"}

class FailingTelegram:
    async def send(self, patient_id, text):
        raise RuntimeError("Telegram API unavailable")


class WebChannelTest(unittest.TestCase):

    def test_message_reaches_browser_and_telegram(self):
        async def run_test():
            hub = FakeHub()
            telegram = FakeTelegram()
            channel = WebChannel(hub, telegram=telegram)

            result = await channel.send(
                "marco-r",
                "Your doctor has reviewed your case.",
            )

            self.assertEqual(
                result,
                {"delivered": True, "transport": "web+telegram"},
            )

            self.assertEqual(len(hub.events), 1)
            self.assertEqual(
                hub.events[0],
                (
                    "patient_out",
                    {
                        "patient": "marco-r",
                        "text": "Your doctor has reviewed your case.",
                    },
                ),
            )

        asyncio.run(run_test())

    def test_telegram_failure_does_not_stop_browser_delivery(self):
        async def run_test():
            hub = FakeHub()
            telegram = FailingTelegram()
            channel = WebChannel(hub, telegram=telegram)

            result = await channel.send(
                "marco-r",
                "Your doctor has reviewed your case.",
            )

            self.assertEqual(
                result,
                {"delivered": True, "transport": "web"},
            )

            self.assertEqual(
                hub.events[0],
                (
                    "patient_out",
                    {
                        "patient": "marco-r",
                        "text": "Your doctor has reviewed your case.",
                    },
                ),
            )

            self.assertEqual(
                hub.events[1],
                (
                    "error",
                    {
                        "seat": "companion",
                        "message": "Telegram delivery failed; browser delivery is still available.",
                    },
                ),
            )

        asyncio.run(run_test())

    def test_browser_delivery_works_without_telegram(self):
        async def run_test():
            hub = FakeHub()
            channel = WebChannel(hub, telegram=None)

            result = await channel.send(
                "marco-r",
                "Your doctor has reviewed your case.",
            )

            self.assertEqual(
                result,
                {"delivered": True, "transport": "web"},
            )

            self.assertEqual(
                hub.events,
                [
                    (
                        "patient_out",
                        {
                            "patient": "marco-r",
                            "text": "Your doctor has reviewed your case.",
                        },
                    )
                ],
            )

        asyncio.run(run_test())
