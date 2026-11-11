"""Chief of Staff, model step: an LLM drafts the manifest, deterministic code decides what survives.

The rule pass (safety_floor.compile_new / amend) always runs first and is the fallback: if the model is absent,
slow, malformed or unreachable, the Director still gets a rule-based draft. The model's output is then
*constrained* (it may shape tools, surfaces, refusals, gates and wording, never routes, memory, cloud fallback,
billing, or the seat's identity) and merged through ``enforce_floor`` (it can add restrictions, never remove
them). A Director approves the result either way; this keeps the approval meaningful.

Only the Director's job description goes to the model. No patient data is ever in this prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from ..contracts import Manifest, PolicyCatalog
from ..registry import ManifestError
from .safety_floor import Diff, amend, compile_new, enforce_floor

log = logging.getLogger(__name__)


class DraftModel(Protocol):
    async def draft(self, *, instruction: str, current: Manifest | None, catalog: PolicyCatalog,
                    feedback: list[str]) -> dict[str, Any]:
        """Return a proposed manifest as a plain dict."""
        ...


@dataclass(frozen=True)
class CompiledDraft:
    manifest: Manifest
    rule_diff: Diff
    used_model: bool
    notes: list[str] = field(default_factory=list)


_SYSTEM = """You compile a hospital Director's plain-language instruction into a JSON seat manifest for a clinical \
AI desk. Reply with ONE JSON object and nothing else.
Rules: use only the tools, surfaces, gates and refusal ids listed below. When the instruction forbids something, \
add the matching refusal id; never remove an existing refusal or gate. Never grant yourself new routes. Keep \
job_description as the Director's own words.
Surfaces and their tools: {surfaces}
Internal tools: {internal}
Gate ids: {gates}
Refusal ids: {refusals}
Manifest JSON schema: {schema}"""


class OpenAICompatDraftModel:
    """Any OpenAI-compatible /chat/completions server (vLLM, Ollama, llama.cpp). Runs on the edge node."""

    def __init__(self, base_url: str, model: str, *, api_key: str | None = None, timeout_s: float = 60.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def draft(self, *, instruction: str, current: Manifest | None, catalog: PolicyCatalog,
                    feedback: list[str]) -> dict[str, Any]:
        system = _SYSTEM.format(
            surfaces=json.dumps(catalog.surfaces), internal=json.dumps(catalog.internal_tools),
            gates=sorted(catalog.gates), refusals=sorted(catalog.refusals),
            schema=json.dumps(Manifest.model_json_schema()),
        )
        user = {"instruction": instruction,
                "current_manifest": current.model_dump(mode="json") if current else None,
                "fix_these_errors_from_your_previous_answer": feedback or None}
        resp = await self._client.post(self._url, headers=self._headers, json={
            "model": self._model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
        })
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("model did not return a JSON object")
        return data


def constrain(proposed: Manifest, baseline: Manifest, current: Manifest | None) -> Manifest:
    """What the model is NOT allowed to decide. Everything not listed here is taken from the trusted reference."""
    ref = current or baseline
    surfaces = [s for s in proposed.surfaces if s != "control_plane" or s in ref.surfaces]
    tools = [t for t in proposed.tools if (t in ref.tools) or not t.startswith("control_plane.")]
    return proposed.model_copy(update={
        "seat": ref.seat, "version": ref.version, "location": ref.location, "desk": ref.desk,
        "model": ref.model, "memory": ref.memory, "a2a_peers": ref.a2a_peers, "consumes": ref.consumes,
        "emits": ref.emits, "unit": ref.unit, "artifact": ref.artifact, "surfaces": surfaces, "tools": tools,
    })


async def compile_draft(
    instruction: str, catalog: PolicyCatalog, *, now: datetime, model: DraftModel | None = None,
    current: Manifest | None = None, name: str | None = None, timeout_s: float = 60.0, attempts: int = 2,
) -> CompiledDraft:
    rule = (amend(current, instruction, catalog, now=now) if current
            else compile_new(instruction, catalog, now=now, name=name))
    baseline = CompiledDraft(rule.manifest, rule.diff, used_model=False)
    if model is None:
        return baseline
    feedback: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            raw = await asyncio.wait_for(
                model.draft(instruction=instruction, current=current, catalog=catalog, feedback=feedback), timeout_s)
            proposed = Manifest.model_validate({**raw, "seat": raw.get("seat", rule.manifest.seat),
                                                "version": raw.get("version", rule.manifest.version)})
            merged = enforce_floor(constrain(proposed, rule.manifest, current), instruction, catalog, previous=current)
            merged = merged.model_copy(update={"compiled_by": "chief_of_staff+model", "compiled_at": now.isoformat()})
            return CompiledDraft(Manifest.model_validate(merged.model_dump()), rule.diff, used_model=True,
                                 notes=[f"model draft accepted on attempt {attempt}"])
        except (ValidationError, ManifestError, ValueError, KeyError) as exc:
            feedback = [str(exc)[:600]]
            log.info("model draft rejected (attempt %d): %s", attempt, exc)
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            log.warning("draft model unavailable: %r", exc)
            return CompiledDraft(baseline.manifest, baseline.rule_diff, False,
                                 [f"model unavailable ({exc!r}); rule-based draft"])
    return CompiledDraft(baseline.manifest, baseline.rule_diff, False,
                         ["model output failed validation twice; rule-based draft"])
