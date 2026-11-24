"""Background loops. Each takes a stop event so the service can shut down cleanly on SIGTERM."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .escalation import EscalationService

log = logging.getLogger(__name__)


async def run_periodically(name: str, step: Callable[[], Awaitable[object]], interval_s: float,
                           stop: asyncio.Event) -> None:
    """Run `step` every `interval_s` until stopped. An exception is logged and the loop keeps going: a failing
    iteration must never silently end the loop that pages doctors."""
    while not stop.is_set():
        try:
            await step()
        except Exception:
            log.exception("%s iteration failed", name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass


async def escalation_worker(service: EscalationService, stop: asyncio.Event, interval_s: float = 2.0) -> None:
    await run_periodically("escalation tick", service.tick, interval_s, stop)
