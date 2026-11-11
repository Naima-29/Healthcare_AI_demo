"""Connector template. To add a system (PACS, lab, EHR...):

1. Pick the surface and tool names from contracts/policy_catalog.json; the catalogue is the contract, so the PEP
   and manifests already know them. Tool names are `<surface>.<verb>`.
2. Write `build_<surface>_server(<backend ports>) -> FastMCP` using `new_server()` and `@guarded(server, name)`.
   Take the backend (an HTTP client, a DB pool) as a constructor argument so tests inject a fake.
3. Return compact, typed dicts, never raw upstream payloads (less PHI crosses the boundary, fewer tokens).
4. Validate every identifier that ends up in a URL or query (`safe_id`). Never build a path from raw input.
5. Raise `ConnectorError("short, safe message")` for expected failures. Anything else is logged (without the
   arguments, which may contain patient data) and surfaced as a generic error: upstream bodies never leak.
6. Add a `__main__` that reads config from env and calls `run_connector(server)`; add the service to compose.

The connector does NOT decide who may call it: the seat's PEP checks manifest, refusals and gates before any
call reaches here. Do not add authorisation logic here; add it to the catalogue/manifest instead.

stdio transport (default) uses stdout for protocol frames: log to stderr only.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

log = logging.getLogger("kcd.connector")

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$")  # pt_marco, ep_1, dr.rossi


class ConnectorError(ToolError):
    """An expected failure with a message that is safe to show the seat (and so the model)."""


def safe_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise ConnectorError(f"invalid {what}")
    return value


def new_server(name: str, instructions: str) -> FastMCP:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return FastMCP(name, instructions=instructions, host=os.environ.get("KCD_MCP_HOST", "127.0.0.1"),
                   port=int(os.environ.get("KCD_MCP_PORT", "8000")))


def guarded(server: FastMCP, name: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register a tool with uniform error handling and PHI-safe logging (tool name and timing only)."""

    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            except ConnectorError:
                raise
            except Exception:
                log.exception("tool %s failed", name)  # traceback without arguments
                raise ConnectorError("the upstream system is unavailable") from None
            finally:
                log.info("tool %s took %.0f ms", name, (time.monotonic() - started) * 1000)

        server.tool(name=name)(wrapper)
        return wrapper

    return decorate


def run_connector(server: FastMCP) -> None:
    """KCD_MCP_TRANSPORT = stdio (default, one process per seat) | streamable-http (a shared service)."""
    transport = os.environ.get("KCD_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "streamable-http"):
        raise SystemExit(f"unsupported KCD_MCP_TRANSPORT '{transport}'")
    server.run(transport)  # type: ignore[arg-type]
