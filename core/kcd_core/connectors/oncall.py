"""`oncall` surface: who is on call, and page someone. Backed by the same rota the escalation ladder uses.

Call shapes follow what the seats already use (demo/backend/seats/dispatcher.mjs):
  oncall.who_is_on_call(role?)            -> {"ladder": [{"level", "role", "clinician_id"}...], "clinician_id"?}
  oncall.page(name, message, patient?)    -> {"paged", "page_id"}
`pager` (a device number) is accepted for compatibility and ignored: the paging gateway resolves the device from
the clinician id, so a seat can never page an arbitrary number.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..escalation import DEFAULT_LADDER, OnCallRota
from ..pagers import MessagePager
from .base import ConnectorError, guarded, new_server, run_connector, safe_id

ROLES = tuple(step.role for step in DEFAULT_LADDER)


def build_oncall_server(
    rota: OnCallRota, message_pager: MessagePager, *, default_unit: str = "uro_oncology"
) -> FastMCP:
    server = new_server("kcd-oncall", "On-call rota lookup and paging for the hospital unit.")

    @guarded(server, "oncall.who_is_on_call")
    async def who_is_on_call(
        role: str | None = None, unit: str | None = None, at: str | None = None
    ) -> dict[str, Any]:
        """The on-call ladder for `unit` (default: the desk's unit), now or at an ISO-8601 time. With `role`
        (primary|backup|head_of_unit) the answer also carries that role's `clinician_id`."""
        unit = safe_id(unit or default_unit, "unit")
        if role is not None and role not in ROLES:
            raise ConnectorError(f"role must be one of {ROLES}")
        moment = _parse(at) if at else datetime.now(UTC)
        ladder = [{"level": i, "role": step.role, "clinician_id": await rota.who(unit, step.role, moment)}
                  for i, step in enumerate(DEFAULT_LADDER)]
        out: dict[str, Any] = {"unit": unit, "at": moment.isoformat(), "ladder": ladder}
        if role is not None:
            out["clinician_id"] = next(r["clinician_id"] for r in ladder if r["role"] == role)
        return out

    @guarded(server, "oncall.page")
    async def page(name: str, message: str, patient: str | None = None, unit: str | None = None,
                   pager: str | None = None) -> dict[str, Any]:
        """Page a named clinician with a short message. Red-flag escalations are paged by the Dispatcher's ladder;
        use this for ad-hoc pages."""
        target = safe_id(name, "name")
        if not message.strip() or len(message) > 500:
            raise ConnectorError("message must be 1-500 characters")
        page_id = await message_pager.page_message(target, message.strip(), unit=safe_id(unit or default_unit, "unit"),
                                            patient_id=safe_id(patient, "patient") if patient else None)
        return {"paged": target, "page_id": page_id}  # a failed page raises: the caller must know

    return server


def _parse(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ConnectorError("'at' must be an ISO-8601 timestamp") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def main() -> None:  # python -m kcd_core.connectors.oncall
    from ..adapters._pg import make_pool
    from ..adapters.pg_stores import PostgresOnCallRota
    from ..service import build_pager
    from ..settings import Settings

    settings = Settings()
    pool = make_pool(settings.database_url, name="kcd-oncall-mcp", options=settings.pg_options)
    pager = build_pager(settings)
    if not hasattr(pager, "page_message"):
        raise SystemExit("the configured pager cannot send ad-hoc messages")
    run_connector(build_oncall_server(PostgresOnCallRota(pool), pager, default_unit=settings.default_unit))  # type: ignore[arg-type]


if __name__ == "__main__":
    os.environ.setdefault("KCD_MCP_TRANSPORT", "stdio")
    main()
