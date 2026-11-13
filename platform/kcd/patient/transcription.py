
"""Local speech-to-text transcription for patient voice notes."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from faster_whisper import WhisperModel


_model: WhisperModel | None = None
_model_lock = threading.Lock()


def _get_model() -> WhisperModel:
    """Load the Whisper model once and reuse it."""
    global _model

    if _model is None:
        with _model_lock:
            if _model is None:
                model_name = os.getenv("WHISPER_MODEL", "base")
                _model = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type="int8",
                )

    return _model


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe audio bytes locally and return the recognized text."""
    if not audio_bytes:
        raise ValueError("Telegram voice message is empty")

    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".ogg",
            delete=False,
        ) as temp_file:
            temp_file.write(audio_bytes)
            temp_path = Path(temp_file.name)

        print("[Whisper] Loading model...")
        model = _get_model()
        print("[Whisper] Model loaded successfully.")

        print("[Whisper] Starting audio transcription...")
        segments, _info = model.transcribe(
            str(temp_path),
            beam_size=1,
            vad_filter=False,
            language="en",
        )
        print("[Whisper] Transcription started; reading segments...")

        transcript_parts = []

        for segment in segments:
            print("[Whisper] Segment received:", segment.text, flush=True)
            cleaned = segment.text.strip()
            if cleaned:
                transcript_parts.append(cleaned)

        transcript = " ".join(transcript_parts).strip()
        print("[Whisper] All segments processed.", flush=True)

        if not transcript:
            raise ValueError("No speech could be recognized in the voice message")

        return transcript

    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
