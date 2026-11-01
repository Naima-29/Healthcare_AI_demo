"""Targets: functions that turn a dataset item into a prediction.

Register with @target(name, task). A target that can't run in this environment
(for example, no model endpoint configured) raises TargetUnavailable.
"""
from __future__ import annotations

import functools
import json
import os
import urllib.request
from collections.abc import Callable

from evals import rules_baseline

Predict = Callable[[dict], dict]
REGISTRY: dict[str, tuple[str, Predict]] = {}


class TargetUnavailable(RuntimeError):
    pass


def target(name: str, task: str) -> Callable[[Predict], Predict]:
    def register(fn: Predict) -> Predict:
        REGISTRY[name] = (task, fn)
        return fn
    return register


def get(name: str) -> tuple[str, Predict]:
    if name not in REGISTRY:
        raise KeyError(f"unknown target '{name}'. Known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]


@target("rules_baseline", task="red_flag")
def _rules_baseline(item: dict) -> dict:
    return rules_baseline.predict(item)


@functools.lru_cache(maxsize=1)
def _variant_rules():
    from kcd.genomics.rules import VariantRules
    return VariantRules.load()


@target("genomics_rules", task="variant_relevance")
def _genomics_rules(item: dict) -> dict:
    """Genomics Interpreter rules (kcd/genomics): relevance of item['target_gene'] within the panel."""
    from kcd.genomics.panel import parse_panel_json
    from kcd.genomics.rules import interpret
    panel = parse_panel_json({"patient_id": "eval", "variants": item["variants"]})
    out = interpret(panel, _variant_rules(), item.get("context"))
    gene = item["target_gene"]
    for f in out["findings"]:
        if f["gene"] == gene:
            return {"relevance": f["relevance"]}
    if any(n["gene"] == gene for n in out["not_reported"]):
        return {"relevance": "filtered"}
    raise ValueError(f"target gene {gene} not in panel")


RED_FLAG_SYSTEM ="""You triage messages from a patient in kidney follow-up (history in the context).
Classify urgency:
- "high": needs a doctor today (e.g. visible blood in urine with severe one-sided pain and systemic signs
  or cancer history; fainting; no urine for 24 hours; chest pain; severe breathlessness).
- "medium": a single red-flag symptom that the care team should review soon.
- "none": no red flag.
Negated symptoms ("no blood") do not count. Patients often play symptoms down; judge the facts.
Answer only with JSON:
{"urgency": "none|medium|high", "score": <probability of high, 0..1>, "evidence": [<short strings>]}"""


@target("edge_llm", task="red_flag")
def _edge_llm(item: dict) -> dict:
    base = os.environ.get("EDGE_LLM_BASE_URL")
    model = os.environ.get("EDGE_LLM_MODEL")
    if not base or not model:
        raise TargetUnavailable("set EDGE_LLM_BASE_URL and EDGE_LLM_MODEL (OpenAI-compatible endpoint)")
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": RED_FLAG_SYSTEM},
            {"role": "user", "content": json.dumps({"context": item.get("context", {}), "message": item["text"]},
                                                   ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('EDGE_LLM_API_KEY', 'none')}"},
    )
    timeout = float(os.environ.get("EDGE_LLM_TIMEOUT", "60"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]
    out = json.loads(content)
    if out.get("urgency") not in ("none", "medium", "high"):
        raise ValueError(f"invalid urgency in model output: {content[:200]}")
    out["score"] = float(out.get("score", 1.0 if out["urgency"] == "high" else 0.0))
    return out
