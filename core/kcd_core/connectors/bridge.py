"""Bind a connected MCP client to a seat's ToolRegistry: `ctx.tool("fhir.get_observations", ...)` -> MCP call.

The PEP has already allowed the call by the time it reaches here. The bridge only translates: arguments out,
structured result back, MCP errors as ToolCallError (with the connector's own safe message).
"""

from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession

from ..runtime import ToolRegistry


class ToolCallError(RuntimeError):
    pass


def _unwrap(result: Any) -> Any:
    if result.isError:
        text = " ".join(getattr(c, "text", "") for c in result.content).strip()
        raise ToolCallError(text or "tool call failed")
    if getattr(result, "structuredContent", None) is not None:
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    texts = [c.text for c in result.content if getattr(c, "text", None) is not None]
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    return [json.loads(t) for t in texts]


async def bind_session(registry: ToolRegistry, session: ClientSession) -> list[str]:
    """Register every tool the server offers. Returns their names."""
    names: list[str] = []
    for tool in (await session.list_tools()).tools:
        name = tool.name

        async def call(_name: str = name, **kwargs: Any) -> Any:
            return _unwrap(await session.call_tool(_name, kwargs))

        registry.register(name, call)
        names.append(name)
    return names
