"""Event hub: one ordered stream of everything that happens on the desk, pushed to the browser as server-sent events."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from typing import Any


class Hub:
    def __init__(self, keep: int = 1500) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=keep)
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._n = 0

    def publish(self, kind: str, data: dict[str, Any] | None = None, ts: datetime | None = None) -> dict[str, Any]:
        self._n += 1
        ev = {"n": self._n, "kind": kind, "ts": (ts or datetime.now(UTC)).isoformat(), "data": data or {}}
        self.history.append(ev)
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    @staticmethod
    def sse(ev: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(ev, default=str, ensure_ascii=False)}\n\n".encode()
