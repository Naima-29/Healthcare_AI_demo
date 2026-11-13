
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from kcd.patient.channel import PatientChannel
from urllib.parse import parse_qs


class PatientChannelTest(unittest.TestCase):
    def test_patient_chat_mapping_is_bidirectional(self):
        channel = PatientChannel(
            token="test-token",
            chat_by_patient={
                "marco-r": "12345",
                "sofia-b": "67890",
            },
        )

        self.assertEqual(channel.chat_by_patient["marco-r"], "12345")
        self.assertEqual(channel.chat_by_patient["sofia-b"], "67890")
        self.assertEqual(channel.patient_by_chat["12345"], "marco-r")
        self.assertEqual(channel.patient_by_chat["67890"], "sofia-b")

    def test_duplicate_chat_mapping_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "multiple patients"):
            PatientChannel(
                chat_by_patient={
                    "marco-r": "12345",
                    "sofia-b": "12345",
                }
            )

    def test_send_handles_telegram_api_failure(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={"marco-r": "12345"},
            )

            with patch(
                "urllib.request.urlopen",
                side_effect=OSError("Telegram connection failed"),
            ):
                with self.assertRaises(OSError):
                    await channel.send(
                        "marco-r",
                        "Your doctor has reviewed your case.",
                    )

            self.assertEqual(len(channel.outbox), 1)
            self.assertEqual(channel.outbox[0]["transport"], "local")

        asyncio.run(run_test())

    def test_chat_can_be_reassigned_to_same_patient(self):
        channel = PatientChannel(
            chat_by_patient={"marco-r": "12345"}
        )

        channel.bind_chat("marco-r", "99999")

        self.assertNotIn("12345", channel.patient_by_chat)
        self.assertEqual(channel.patient_by_chat["99999"], "marco-r")

    def test_chat_cannot_be_assigned_to_another_patient(self):
        channel = PatientChannel(
            chat_by_patient={"marco-r": "12345"}
        )

        with self.assertRaisesRegex(ValueError, "already mapped"):
            channel.bind_chat("sofia-b", "12345")

    def test_from_env_loads_patient_mapping(self):
        mapping = {
            "marco-r": "12345",
            "sofia-b": "67890",
        }

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "TELEGRAM_PATIENT_MAP": json.dumps(mapping),
            },
            clear=True,
        ):
            channel = PatientChannel.from_env()

        self.assertEqual(channel.token, "test-token")
        self.assertEqual(channel.chat_by_patient, mapping)

    
    def test_registered_chat_message_reaches_correct_patient(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={
                    "marco-r": "12345",
                    "sofia-b": "67890",
                },
            )
            stop = asyncio.Event()
            received = []

            update = {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 12345},
                            "text": "I have pain in my side.",
                        },
                    }
                ],
            }

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(update).encode("utf-8")

            def fake_urlopen(*args, **kwargs):
                stop.set()
                return FakeResponse()

            async def on_inbound(patient_id, text, metadata):
                received.append((patient_id, text, metadata))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                await channel.poll_updates(on_inbound, stop)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "marco-r")
            self.assertEqual(received[0][1], "I have pain in my side.")
            self.assertEqual(received[0][2]["kind"], "text")

        asyncio.run(run_test())

    def test_registered_chat_photo_and_voice_reach_correct_patient(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={"marco-r": "12345"},
            )

            stop = asyncio.Event()
            received = []

            telegram_updates = {
                "ok": True,
                "result": [
                    {
                        "update_id": 101,
                        "message": {
                            "chat": {"id": 12345},
                            "photo": [
                                {"file_id": "small-photo-id"},
                                {"file_id": "large-photo-id"},
                            ],
                        },
                    },
                    {
                        "update_id": 102,
                        "message": {
                            "chat": {"id": 12345},
                            "voice": {"file_id": "voice-file-id"},
                        },
                    },
                ],
            }

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(telegram_updates).encode("utf-8")

            async def on_inbound(patient_id, text, metadata):
                received.append((patient_id, text, metadata))

                if len(received) == 2:
                    stop.set()

            with patch(
                "urllib.request.urlopen",
                return_value=FakeResponse(),
            ):
                await channel.poll_updates(on_inbound, stop)

            self.assertEqual(len(received), 2)

            self.assertEqual(received[0][0], "marco-r")
            self.assertEqual(received[0][1], "")
            self.assertEqual(received[0][2]["kind"], "photo")
            self.assertEqual(
                received[0][2]["file_id"],
                "large-photo-id",
            )

            self.assertEqual(received[1][0], "marco-r")
            self.assertEqual(received[1][1], "")
            self.assertEqual(received[1][2]["kind"], "voice")
            self.assertEqual(
                received[1][2]["file_id"],
                "voice-file-id",
            )

        asyncio.run(run_test())


    def test_send_delivers_message_to_registered_telegram_chat(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={"marco-r": "12345"},
            )

            sent_requests = []

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps({
                        "ok": True,
                        "result": {"message_id": 1},
                    }).encode("utf-8")

            def fake_urlopen(request, *args, **kwargs):
                sent_requests.append(request)
                return FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = await channel.send(
                    "marco-r",
                    "Your doctor has reviewed your case.",
                )

            self.assertEqual(result["transport"], "telegram")
            self.assertEqual(len(sent_requests), 1)

            payload = parse_qs(sent_requests[0].data.decode("utf-8"))

            self.assertEqual(payload["chat_id"], ["12345"])
            self.assertEqual(
                payload["text"],
                ["Your doctor has reviewed your case."],
            )

        asyncio.run(run_test())


    def test_unknown_chat_is_ignored(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={"marco-r": "12345"},
            )
            stop = asyncio.Event()
            received = []

            update = {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 99999},
                            "text": "Hello",
                        },
                    }
                ],
            }

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(update).encode("utf-8")

            def fake_urlopen(*args, **kwargs):
                stop.set()
                return FakeResponse()

            async def on_inbound(patient_id, text, metadata):
                received.append((patient_id, text, metadata))

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                await channel.poll_updates(on_inbound, stop)

            self.assertEqual(received, [])

        asyncio.run(run_test())


    def test_download_photo_returns_image_bytes(self):
        async def run_test():
            channel = PatientChannel(
                token="test-token",
                chat_by_patient={"marco-r": "12345"},
            )

            image_bytes = b"fake-image-content"
            requests = []

            class FakeResponse:
                def __init__(self, content):
                    self.content = content

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, *args):
                    return self.content

            responses = [
                FakeResponse(json.dumps({
                    "ok": True,
                    "result": {
                        "file_path": "photos/report.jpg",
                        "file_size": len(image_bytes),
                    },
                }).encode("utf-8")),
                FakeResponse(image_bytes),
            ]

            def fake_urlopen(request, *args, **kwargs):
                requests.append(request)
                return responses.pop(0)

            with patch(
                "urllib.request.urlopen",
                side_effect=fake_urlopen,
            ):
                result = await channel.download_photo("photo-file-id")

            self.assertEqual(result, image_bytes)
            self.assertEqual(len(requests), 2)
            self.assertIn("getFile", requests[0])
            self.assertIn("photos/report.jpg", requests[1])

        asyncio.run(run_test())

    def test_download_photo_rejects_telegram_api_failure(self):
        async def run_test():
            channel = PatientChannel(token="test-token")

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, *args):
                    return json.dumps({
                        "ok": False,
                        "description": "File not found",
                    }).encode("utf-8")

            with patch(
                "urllib.request.urlopen",
                return_value=FakeResponse(),
            ):
                with self.assertRaisesRegex(RuntimeError, "getFile"):
                    await channel.download_photo("invalid-file-id")

        asyncio.run(run_test())

    def test_download_photo_rejects_oversized_file(self):
        async def run_test():
            channel = PatientChannel(token="test-token")

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, *args):
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "file_path": "photos/large.jpg",
                            "file_size": 1000,
                        },
                    }).encode("utf-8")

            with patch(
                "urllib.request.urlopen",
                return_value=FakeResponse(),
            ):
                with self.assertRaisesRegex(ValueError, "size"):
                    await channel.download_photo(
                        "large-file-id",
                        max_bytes=100,
                    )

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
