"""Patient communication adapter.

The in-memory mode is used by tests and the local golden path. When a bot token
is supplied, the same interface can send Telegram messages and poll text/photo/
voice/video update metadata. Media decoding (STT/OCR/face features) is a
separate adapter and is intentionally not fabricated here.
"""
from __future__ import annotations

import os
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

import asyncio
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any


InboundHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class PatientChannel:
    def __init__(self, token: str | None = None, chat_by_patient: dict[str, str] | None = None):
        self.token = token
        self.chat_by_patient = dict(chat_by_patient or {})
        self.outbox: list[dict[str, Any]] = []
        self.offset = 0

    @classmethod
    def from_env(cls, patient_id: str) -> "PatientChannel":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        mapping = {patient_id: chat_id} if chat_id else {}
        return cls(token=token, chat_by_patient=mapping)

    def bind_chat(self, patient_id: str, chat_id: str) -> None:
        self.chat_by_patient[patient_id] = str(chat_id)

    async def send(self, patient_id: str, text: str) -> dict[str, Any]:
        message = {"patient_id": patient_id, "text": text, "transport": "local"}
        self.outbox.append(message)
        chat_id = self.chat_by_patient.get(patient_id)
        if not self.token or not chat_id:
            return {"delivered": True, "transport": "local"}

        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
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

    async def poll_updates(self, on_inbound: InboundHandler, stop: asyncio.Event, *, patient_id: str) -> None:
        """Poll Telegram and pass supported update metadata to the application callback."""
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        while not stop.is_set():
            query = urllib.parse.urlencode({"timeout": 20, "offset": self.offset})
            url = f"https://api.telegram.org/bot{self.token}/getUpdates?{query}"

            def request() -> dict[str, Any]:
                with urllib.request.urlopen(url, timeout=25) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                result = await asyncio.to_thread(request)
                if not result.get("ok"):
                    raise RuntimeError("Telegram rejected getUpdates")
                for update in result.get("result", []):
                    self.offset = int(update["update_id"]) + 1
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    if chat.get("id") is not None:
                        self.bind_chat(patient_id, str(chat["id"]))
                    if isinstance(message.get("text"), str):
                        await on_inbound(patient_id, message["text"], {"transport": "telegram", "kind": "text"})
                    elif message.get("voice"):
                        await on_inbound(
                            patient_id, "", {"transport": "telegram", "kind": "voice",
                                             "file_id": message["voice"].get("file_id")}
                        )
                    elif message.get("photo"):
                        photo = message["photo"][-1]
                        await on_inbound(
                            patient_id, "", {"transport": "telegram", "kind": "photo",
                                             "file_id": photo.get("file_id")}
                        )
                    elif message.get("video"):
                        await on_inbound(
                            patient_id, "", {"transport": "telegram", "kind": "video",
                                             "file_id": message["video"].get("file_id")}
                        )
            except Exception:
                # Retry on the next poll; never log the bot token or message body.
                await asyncio.sleep(2)
