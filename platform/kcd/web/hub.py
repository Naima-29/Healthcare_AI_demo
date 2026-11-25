"""Live events for the browser: an ordered history, plus one queue per open /events connection."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any


class Hub:
    def __init__(self, keep: int = 3000) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=keep)
        self.seq = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seq += 1
        event = {"seq": self.seq, "ts": datetime.now(UTC).isoformat(), "kind": kind, "data": data or {}}
        self.history.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def reset(self) -> None:
        self.history.clear()
        self.publish("reset")
        self.history.clear()

    def forget(self, patient: str) -> None:
        """Erasure: nothing about this patient stays on screen."""
        self.history = deque((e for e in self.history if e["data"].get("patient") != patient),
                             maxlen=self.history.maxlen)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
