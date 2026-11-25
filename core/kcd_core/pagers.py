"""Pagers: how an escalation reaches a human. Selected by configuration, never by code.

- LogPager: development and tests. Logs ids only (no patient data) and reaches nobody.
- WebhookPager: POSTs a signed JSON body to your paging gateway (an SMS/push bridge, PagerDuty/Opsgenie adapter,
  hospital paging system). HMAC-SHA256 over the exact body in `X-KCD-Signature`, and an `Idempotency-Key` so the
  gateway can drop the duplicates that at-least-once delivery allows.

A pager that cannot deliver must raise: the escalation service then treats the level as unanswered and climbs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any, Protocol

import httpx

from .escalation import Escalation

log = logging.getLogger(__name__)


class PagerError(RuntimeError):
    pass


class MessagePager(Protocol):
    """Ad-hoc page with free text (the `oncall.page` tool). Escalation paging uses `Pager.page` instead."""

    async def page_message(self, target: str, message: str, *, unit: str, patient_id: str | None = None) -> str:
        """Returns a page id."""
        ...


class LogPager:
    async def page_message(self, target: str, message: str, *, unit: str, patient_id: str | None = None) -> str:
        page_id = str(uuid.uuid4())
        log.warning("PAGE (log only, nobody was reached) target=%s page=%s", target, page_id)
        return page_id

    async def page(self, target: str, escalation: Escalation) -> None:
        log.warning("PAGE (log only, nobody was reached) target=%s escalation=%s level=%d",
                    target, escalation.id, escalation.level)


class WebhookPager:
    def __init__(
        self,
        url: str,
        secret: str,
        *,
        timeout_s: float = 5.0,
        attempts: int = 3,
        backoff_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not secret:
            raise ValueError("the paging webhook needs a signing secret")
        self._url, self._secret = url, secret.encode()
        self._attempts, self._backoff = attempts, backoff_s
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def page(self, target: str, escalation: Escalation) -> None:
        await self._post({
            "escalation_id": escalation.id, "target": target, "unit": escalation.unit, "level": escalation.level,
            "patient_id": escalation.patient_id, "episode_id": escalation.episode_id,
            "deadline": escalation.deadline.isoformat(),
        }, f"{escalation.id}:{escalation.level}:{target}", target)

    async def page_message(self, target: str, message: str, *, unit: str, patient_id: str | None = None) -> str:
        page_id = str(uuid.uuid4())
        await self._post({"page_id": page_id, "target": target, "unit": unit, "patient_id": patient_id,
                          "message": message}, page_id, target)
        return page_id

    async def _post(self, doc: dict[str, Any], idempotency_key: str, target: str) -> None:
        body = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
        headers = {
            "Content-Type": "application/json",
            "X-KCD-Signature": "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest(),
            "Idempotency-Key": idempotency_key,
        }
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                resp = await self._client.post(self._url, content=body, headers=headers)
                if resp.status_code < 300:
                    return
                if resp.status_code < 500 and resp.status_code != 429:
                    raise PagerError(f"paging gateway refused the page: HTTP {resp.status_code}")  # retrying won't help
                last = PagerError(f"paging gateway HTTP {resp.status_code}")
            except httpx.TransportError as exc:
                last = exc
            if attempt < self._attempts:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
        raise PagerError(f"paging {target} failed after {self._attempts} attempts: {last!r}")

    async def aclose(self) -> None:
        await self._client.aclose()
