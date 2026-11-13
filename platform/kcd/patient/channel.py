
"""Telegram communication adapter for the Patient Companion.

Only administrator-configured Telegram chats can be associated with demo
patients. Unknown chats are ignored; incoming chats are never auto-assigned.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any
try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv())
except ImportError:
    pass

InboundHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class PatientChannel:
    def __init__(
        self,
        token: str | None = None,
        chat_by_patient: dict[str, str] | None = None,
    ):
        self.token = token
        self.chat_by_patient = {
            str(patient): str(chat_id)
            for patient, chat_id in (chat_by_patient or {}).items()
        }
        self.patient_by_chat = {
            chat_id: patient
            for patient, chat_id in self.chat_by_patient.items()
        }
        if len(self.patient_by_chat) != len(self.chat_by_patient):
            raise ValueError("A Telegram chat ID cannot be mapped to multiple patients")

        self.outbox: list[dict[str, Any]] = []
        self.offset = 0

    @classmethod
    def from_env(cls) -> PatientChannel:
        """Load TELEGRAM_BOT_TOKEN and TELEGRAM_PATIENT_MAP from the environment.

        TELEGRAM_PATIENT_MAP must be a JSON object mapping patient IDs to
        Telegram chat IDs, for example:
        {"marco-r": "123456789", "sofia-b": "987654321"}
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        raw_mapping = os.getenv("TELEGRAM_PATIENT_MAP", "").strip()

        if not raw_mapping:
            mapping: dict[str, str] = {}
        else:
            try:
                parsed = json.loads(raw_mapping)
            except json.JSONDecodeError as exc:
                raise ValueError("TELEGRAM_PATIENT_MAP must contain valid JSON") from exc

            if not isinstance(parsed, dict):
                raise ValueError("TELEGRAM_PATIENT_MAP must be a JSON object")

            mapping = {}
            for patient, chat_id in parsed.items():
                if not isinstance(patient, str) or not isinstance(chat_id, (str, int)):
                    raise ValueError("Telegram patient mappings must contain string patient IDs and chat IDs")
                mapping[patient] = str(chat_id)

        return cls(token=token, chat_by_patient=mapping)

    def bind_chat(self, patient_id: str, chat_id: str) -> None:
        """Explicitly configure a patient/chat association."""
        patient_id = str(patient_id)
        chat_id = str(chat_id)

        existing_patient = self.patient_by_chat.get(chat_id)
        if existing_patient is not None and existing_patient != patient_id:
            raise ValueError("This Telegram chat is already mapped to another patient")

        old_chat = self.chat_by_patient.get(patient_id)
        if old_chat is not None:
            self.patient_by_chat.pop(old_chat, None)

        self.chat_by_patient[patient_id] = chat_id
        self.patient_by_chat[chat_id] = patient_id

    async def send(self, patient_id: str, text: str) -> dict[str, Any]:
        message = {"patient_id": patient_id, "text": text, "transport": "local"}
        self.outbox.append(message)

        chat_id = self.chat_by_patient.get(str(patient_id))
        if not self.token or not chat_id:
            return {"delivered": False, "transport": "local"}

        payload = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text}
        ).encode()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"

        def request() -> dict[str, Any]:
            req = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError("Telegram rejected sendMessage")
            return result

        await asyncio.to_thread(request)
        message["transport"] = "telegram"
        return {"delivered": True, "transport": "telegram"}

    

    async def download_file(
        self,
        file_id: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> bytes:
        """Download a Telegram file and return its bytes."""
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        if not file_id:
            raise ValueError("Telegram file_id is missing")

        query = urllib.parse.urlencode({"file_id": file_id})
        url = f"https://api.telegram.org/bot{self.token}/getFile?{query}"

        def get_file_info() -> dict[str, Any]:
            with urllib.request.urlopen(url, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))

        print("[Telegram download] Requesting file information...", flush=True)
        result = await asyncio.to_thread(get_file_info)
        print("[Telegram download] File information received.", flush=True)

        if not result.get("ok"):
            raise RuntimeError("Telegram rejected getFile")

        file_info = result.get("result") or {}
        file_path = file_info.get("file_path")

        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram did not return a valid file path")

        reported_size = file_info.get("file_size")
        if reported_size is not None and reported_size > max_bytes:
            raise ValueError("Telegram file exceeds the allowed download size")

        download_url = (
            f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        )

        def download() -> bytes:
            with urllib.request.urlopen(download_url, timeout=30) as response:
                data = response.read(max_bytes + 1)

            if len(data) > max_bytes:
                raise ValueError("Telegram file exceeds the allowed download size")

            if not data:
                raise ValueError("Telegram returned an empty file")

            return data

        print("[Telegram download] Starting file download...", flush=True)
        data = await asyncio.to_thread(download)
        print("[Telegram download] File downloaded:", len(data), "bytes", flush=True)
        return data


    async def download_photo(
        self,
        file_id: str,
        *,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> bytes:
        """Download a Telegram photo and return its image bytes."""
        return await self.download_file(file_id, max_bytes=max_bytes)


    async def download_voice(
        self,
        file_id: str,
        *,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> bytes:
        """Download a Telegram voice note and return its audio bytes."""
        return await self.download_file(file_id, max_bytes=max_bytes)


    async def poll_updates(
        self,
        on_inbound: InboundHandler,
        stop: asyncio.Event,
    ) -> None:
        """Poll Telegram and dispatch updates only for configured patient chats."""
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        while not stop.is_set():
            query = urllib.parse.urlencode(
                {"timeout": 20, "offset": self.offset}
            )
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?{query}"

            def request(req_url: str = url) -> dict[str, Any]:
                with urllib.request.urlopen(req_url, timeout=25) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                result = await asyncio.to_thread(request)
                if not result.get("ok"):
                    raise RuntimeError("Telegram rejected getUpdates")

                for update in result.get("result", []):
                    self.offset = int(update["update_id"]) + 1
                    message = update.get("message") or {}
                    print(
                        "[Telegram update received]",
                        "message_type:",
                        next(
                            (key for key in ("text", "voice", "audio", "photo", "video")
                            if message.get(key)),
                            "unknown",
                        ),
                    )
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")

                    if chat_id is None:
                        continue

                    # Never assign an unknown Telegram chat to a patient.
                    patient_id = self.patient_by_chat.get(str(chat_id))
                    if patient_id is None:
                        continue

                    if isinstance(message.get("text"), str):
                        await on_inbound(
                            patient_id,
                            message["text"],
                            {
                                "transport": "telegram",
                                "kind": "text",
                                "chat_id": str(chat_id),
                            },
                        )
                    elif message.get("voice"):
                        await on_inbound(
                            patient_id,
                            "",
                            {
                                "transport": "telegram",
                                "kind": "voice",
                                "file_id": message["voice"].get("file_id"),
                                "chat_id": str(chat_id),
                            },
                        )
                    elif message.get("photo"):
                        photo = message["photo"][-1]
                        await on_inbound(
                            patient_id,
                            "",
                            {
                                "transport": "telegram",
                                "kind": "photo",
                                "file_id": photo.get("file_id"),
                                "chat_id": str(chat_id),
                            },
                        )
                    elif message.get("video"):
                        await on_inbound(
                            patient_id,
                            "",
                            {
                                "transport": "telegram",
                                "kind": "video",
                                "file_id": message["video"].get("file_id"),
                                "chat_id": str(chat_id),
                            },
                        )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Telegram polling error] {type(exc).__name__}: {exc}")
                await asyncio.sleep(2)
