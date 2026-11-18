
import asyncio
from unittest.mock import AsyncMock, patch

from kcd.web.session import Session


def test_telegram_photo_is_downloaded_and_processed():
    async def run_test():
        session = Session.__new__(Session)
        session.telegram = AsyncMock()
        session.telegram.download_photo.return_value = b"fake-image-bytes"

        session.companion = AsyncMock()
        session.companion.episode = lambda patient_id: "ep-test"
        session.drain = AsyncMock()
        session._ctx = lambda seat: "fake-companion-context"

        events = []
        session.hub = type(
            "FakeHub",
            (),
            {"publish": lambda self, event, payload: events.append((event, payload))},
        )()

        with patch(
            "kcd.patient.ocr.extract_text_from_bytes",
            return_value="SERUM CREATININE 1.46 mg/dL",
        ) as mock_ocr:
            await session._on_telegram_message(
                "marco-r",
                "",
                {"kind": "photo", "file_id": "photo-123"},
            )

        session.telegram.download_photo.assert_awaited_once_with("photo-123")
        mock_ocr.assert_called_once_with(b"fake-image-bytes")
        session.companion.attach_lab_report.assert_awaited_once_with(
            "fake-companion-context",
            patient_id="marco-r",
            episode_id="ep-test",
            ocr_text="SERUM CREATININE 1.46 mg/dL",
        )
        session.drain.assert_awaited_once()

    asyncio.run(run_test())



def test_telegram_voice_is_transcribed_and_sent_to_patient_workflow():
    async def run_test():
        session = Session.__new__(Session)
        session.telegram = AsyncMock()
        session.telegram.download_voice.return_value = b"fake-audio-bytes"
        session.patient_message = AsyncMock()

        events = []
        session.hub = type(
            "FakeHub",
            (),
            {"publish": lambda self, event, payload: events.append((event, payload))},
        )()

        with patch(
            "kcd.patient.transcription.transcribe_audio",
            return_value="I have a headache",
        ) as mock_transcribe:
            await session._on_telegram_message(
                "marco-r",
                "",
                {"kind": "voice", "file_id": "voice-123"},
            )

        session.telegram.download_voice.assert_awaited_once_with("voice-123")
        mock_transcribe.assert_called_once_with(b"fake-audio-bytes")
        session.patient_message.assert_awaited_once_with(
            "I have a headache",
            None,
            "marco-r",
        )
        assert not events

    asyncio.run(run_test())


def test_telegram_voice_without_file_id_is_rejected():
    async def run_test():
        session = Session.__new__(Session)
        session.telegram = AsyncMock()
        session.patient_message = AsyncMock()

        events = []
        session.hub = type(
            "FakeHub",
            (),
            {"publish": lambda self, event, payload: events.append((event, payload))},
        )()

        await session._on_telegram_message(
            "marco-r",
            "",
            {"kind": "voice"},
        )

        session.telegram.download_voice.assert_not_awaited()
        session.patient_message.assert_not_awaited()
        assert events[0][0] == "note"
        assert "without a file ID" in events[0][1]["text"]

    asyncio.run(run_test())


def test_telegram_voice_transcription_error_is_handled():
    async def run_test():
        session = Session.__new__(Session)
        session.telegram = AsyncMock()
        session.telegram.download_voice.return_value = b"fake-audio-bytes"
        session.patient_message = AsyncMock()

        events = []
        session.hub = type(
            "FakeHub",
            (),
            {"publish": lambda self, event, payload: events.append((event, payload))},
        )()

        with patch(
            "kcd.patient.transcription.transcribe_audio",
            side_effect=RuntimeError("transcription failed"),
        ):
            await session._on_telegram_message(
                "marco-r",
                "",
                {"kind": "voice", "file_id": "voice-123"},
            )

        session.telegram.download_voice.assert_awaited_once_with("voice-123")
        session.patient_message.assert_not_awaited()
        assert events[0][0] == "note"
        assert "Could not transcribe" in events[0][1]["text"]

    asyncio.run(run_test())



def test_telegram_text_message_reaches_patient_message_workflow():
    async def run_test():
        session = Session.__new__(Session)
        session.patient_message = AsyncMock()

        await session._on_telegram_message(
            "marco-r",
            "I have a headache",
            {"kind": "text"},
        )

        session.patient_message.assert_awaited_once_with(
            "I have a headache",
            None,
            "marco-r",
        )

    asyncio.run(run_test())
