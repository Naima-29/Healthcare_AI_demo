"""A2A bus port and the in-memory adapter.

Semantics every adapter must keep (NATS JetStream is the production adapter):
- at-least-once delivery; consumers dedupe on the envelope's idempotency_key
- a handler that raises is retried with backoff up to max_attempts, then dead-lettered
- a handler that raises PermanentReject is dead-lettered at once (bad signature, schema, routing)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .contracts import Envelope

log = logging.getLogger(__name__)

Handler = Callable[[Envelope], Awaitable[None]]

DISPATCHER_INBOX = "a2a.dispatcher"


def seat_inbox(seat: str) -> str:
    return f"a2a.seat.{seat}"


def dead_letter(subject: str) -> str:
    return f"dlq.{subject}"


class PermanentReject(Exception):
    """Retrying will not help; dead-letter the message now."""


@dataclass(frozen=True)
class DeadLetter:
    subject: str
    envelope: Envelope
    attempts: int
    error: str


class Bus(Protocol):
    async def publish(self, subject: str, env: Envelope) -> None: ...
    async def subscribe(self, subject: str, handler: Handler) -> None: ...


class InMemoryBus:
    def __init__(self, *, max_attempts: int = 5, base_backoff_s: float = 0.0) -> None:
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self._queues: dict[str, asyncio.Queue[tuple[Envelope, int]]] = defaultdict(asyncio.Queue)
        self._handlers: dict[str, Handler] = {}
        self._workers: list[asyncio.Task[None]] = []
        self.dead_letters: list[DeadLetter] = []
        self.published: list[tuple[str, Envelope]] = []

    async def publish(self, subject: str, env: Envelope) -> None:
        self.published.append((subject, env))
        await self._queues[subject].put((env, 1))

    async def subscribe(self, subject: str, handler: Handler) -> None:
        if subject in self._handlers:
            raise RuntimeError(f"'{subject}' already has a consumer")
        self._handlers[subject] = handler
        self._workers.append(asyncio.create_task(self._work(subject), name=f"bus:{subject}"))

    async def _work(self, subject: str) -> None:
        queue = self._queues[subject]
        handler = self._handlers[subject]
        while True:
            env, attempt = await queue.get()
            try:
                await handler(env)
            except PermanentReject as exc:
                self.dead_letters.append(DeadLetter(subject, env, attempt, f"rejected: {exc}"))
            except Exception as exc:  # noqa: BLE001 - any handler failure is a retry
                if attempt >= self.max_attempts:
                    log.warning("dead-lettering %s on %s after %d attempts", env.id, subject, attempt)
                    self.dead_letters.append(DeadLetter(subject, env, attempt, repr(exc)))
                else:
                    await asyncio.sleep(self.base_backoff_s * 2 ** (attempt - 1))
                    await queue.put((env, attempt + 1))
            finally:
                queue.task_done()

    async def drain(self) -> None:
        """Wait until every consumed subject is empty and idle (tests only).

        Subjects with no consumer keep their messages, as a durable stream would.
        """
        while True:
            consumed = [self._queues[s] for s in self._handlers]
            for q in consumed:
                await q.join()
            await asyncio.sleep(0)
            if all(q.empty() for q in (self._queues[s] for s in self._handlers)):
                return

    async def close(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
