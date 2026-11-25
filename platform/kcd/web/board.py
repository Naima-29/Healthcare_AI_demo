"""What shows the desk's work to the browser: the demo clock, and kcd_core's bus and audit, observed.

The Clinical Board itself is Chrislin's (kcd/board). ObservedBus and ObservedAudit are kcd_core's in-memory bus and
audit, publishing what passes through them.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from kcd_core.audit import InMemoryAudit
from kcd_core.bus import DISPATCHER_INBOX, InMemoryBus, seat_inbox
from kcd_core.contracts import Envelope, PolicyCatalog

from kcd.web.hub import Hub

DISPATCHER = "dispatcher"


class RunningClock:
    """Demo time: starts at the deck's evening (Marco's data is written for that date) and runs in real time,
    so the escalation deadlines really expire."""

    def __init__(self, start: datetime):
        self.start, self._t0 = start, monotonic()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=monotonic() - self._t0)


def for_browser(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Deadlines go out in wall-clock time, since the browser counts them down."""
    out = {**item, "data": dict(item.get("data") or {})}
    deadline = out["data"].get("deadline")
    if isinstance(deadline, datetime):
        out["data"]["deadline"] = (datetime.now(UTC) + (deadline - now)).isoformat()
    return out


class Metrics:
    def __init__(self) -> None:
        self.messages_in, self.messages_out = Counter[str](), Counter[str]()
        self.tool_calls, self.refusals = Counter[str](), Counter[str]()

    def of(self, seat: str) -> dict[str, int]:
        return {"messages_in": self.messages_in[seat], "messages_out": self.messages_out[seat],
                "tool_calls": self.tool_calls[seat], "refusals": self.refusals[seat]}


class ObservedBus(InMemoryBus):
    """A message appears in the A2A stream once the Dispatcher has checked it: when it reaches the seat's inbox,
    or the Dispatcher itself for what it consumes."""

    def __init__(self, hub: Hub, metrics: Metrics):
        super().__init__(max_attempts=3)
        self.hub, self.metrics = hub, metrics

    async def publish(self, subject: str, env: Envelope) -> None:
        sender = env.from_
        if subject == DISPATCHER_INBOX:
            self.metrics.messages_out[sender] += 1
        else:
            self.metrics.messages_in[env.to] += 1
        if subject == seat_inbox(env.to) or (subject == DISPATCHER_INBOX and env.to == DISPATCHER):
            self.hub.publish("a2a", {"from": sender, "to": env.to, "type": str(env.type), "patient": env.patient_id,
                                     "payload": env.payload, "verified": True, "id": str(env.id)})
        await super().publish(subject, env)


class ObservedAudit(InMemoryAudit):
    def __init__(self, hub: Hub, metrics: Metrics, catalog: PolicyCatalog,
                 on_violation: Callable[[str, str | None, str], None]):
        super().__init__()
        self.hub, self.metrics, self.catalog, self.on_violation = hub, metrics, catalog, on_violation

    def record(self, **kw: Any) -> Any:
        event = super().record(**kw)
        action, decision, actor = kw["action"], kw["decision"], kw["actor"]
        detail = kw.get("detail") or {}
        if action.startswith("tool."):
            self.metrics.tool_calls[actor] += 1
            self.hub.publish("tool", {"seat": actor, "tool": action[5:]})
        elif action.startswith("pep.") and decision == "blocked":
            self._blocked(actor, kw.get("manifest_version"), action, detail)
        elif action == "dispatch.reject":
            self.hub.publish("a2a_rejected", {"from": detail.get("from"), "to": detail.get("to"),
                                              "type": detail.get("type"), "error": detail.get("error")})
        elif action in ("seat.policy_violation", "seat.timeout"):
            message = str(detail.get("error") or f"timed out after {detail.get('timeout_s')} s")
            self.hub.publish("error", {"seat": actor, "message": message})
            self.on_violation(actor, kw.get("patient_id"), message)
        return event

    def _blocked(self, seat: str, ref: str | None, action: str, detail: dict[str, Any]) -> None:
        refusal = detail.get("refusal")
        if refusal is None:
            self.hub.publish("tool_blocked", {"seat": seat, "tool": detail.get("target")})
            return
        self.metrics.refusals[seat] += 1
        self.hub.publish("refusal", {"seat": seat, "version": (ref or "").rpartition("@v")[2], "refusal": refusal,
                                     "label": self.catalog.refusals[refusal].label, "action": detail.get("target")})
