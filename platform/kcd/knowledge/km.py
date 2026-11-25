"""Knowledge Model (L0 medical knowledge + L1 specialty playbook), in Python on kcd_core.

Port of Diana's data-driven `km.mjs` (diana/agent-4): the medical content lives in knowledge/rules.json,
this module only loads it, validates it, and applies it. Same behaviour as the Node version:

- classify_drug: word-matching per drug class, longest names first
- ckd_stage: first stage whose minimum eGFR is reached (KDIGO), the last stage is the catch-all
- check_conflicts: per conflict rule, collect the triggers that apply, with their citations
- red_flags: RCC follow-up red-flag patterns (L1 playbook)
- query / has_citation: citation lookup; the Safety Verifier requires exact matches

The rules are a draft demo knowledge base; they must be reviewed by the clinical team before any real use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"
RULES_PATH = KNOWLEDGE_DIR / "rules.json"
TRIGGER_TYPES = ("egfr_below", "hypertension", "medication_contains")


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class DrugClass:
    drug_class: str
    label: str
    pattern: re.Pattern[str]


def validate(data: dict[str, Any]) -> None:
    """Fail early with a clear message if the data file has a mistake (same checks as km.mjs, plus a few)."""
    problems: list[str] = []
    citations = data.get("citations") or {}
    classes = {c.get("class") for c in data.get("drug_classes") or []}
    if data.get("ckd_stage_citation") not in citations:
        problems.append(f'ckd_stage_citation "{data.get("ckd_stage_citation")}" is not in citations')
    stages = data.get("ckd_stages") or []
    if not stages or stages[-1].get("min_egfr") is not None:
        problems.append("ckd_stages must end with a catch-all stage (min_egfr null)")
    mins = [s["min_egfr"] for s in stages if s.get("min_egfr") is not None]
    if mins != sorted(mins, reverse=True):
        problems.append("ckd_stages must be ordered from the highest min_egfr down")
    for rule in data.get("conflict_rules") or []:
        if rule.get("drug_class") not in classes:
            problems.append(f'rule {rule.get("id")}: unknown drug_class "{rule.get("drug_class")}"')
        for t in rule.get("triggers") or []:
            if t.get("citation") not in citations:
                problems.append(f'rule {rule.get("id")}, trigger {t.get("id")}: citation "{t.get("citation")}" '
                                "is not in citations")
            if t.get("type") not in TRIGGER_TYPES:
                problems.append(f'rule {rule.get("id")}, trigger {t.get("id")}: unknown type "{t.get("type")}"')
    if problems:
        raise KnowledgeError("rules.json problems:\n - " + "\n - ".join(problems))


def _fill(template: str, values: dict[str, Any]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]), template)


class KnowledgeModel:
    def __init__(self, data: dict[str, Any]):
        validate(data)
        self.data = data
        self.citations: dict[str, str] = dict(data["citations"])
        self.drug_classes = [
            DrugClass(c["class"], c["label"], re.compile(
                r"\b(" + "|".join(re.escape(n) for n in sorted(c["names"], key=len, reverse=True)) + r")\b",
                re.IGNORECASE))
            for c in data["drug_classes"]
        ]

    @classmethod
    def load(cls, path: str | Path = RULES_PATH) -> KnowledgeModel:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def version(self) -> str:
        return str(self.data.get("meta", {}).get("version", ""))

    # drugs ---------------------------------------------------------------------------------
    def classify_drug(self, text: str) -> dict[str, str] | None:
        for d in self.drug_classes:
            m = d.pattern.search(text or "")
            if m:
                return {"drug": m.group(0).lower(), "class": d.drug_class}
        return None

    # kidney stage ----------------------------------------------------------------------------
    def ckd_stage(self, egfr: float | None) -> dict[str, Any] | None:
        if egfr is None:
            return None
        found = next(s for s in self.data["ckd_stages"] if s.get("min_egfr") is None or egfr >= s["min_egfr"])
        return {"stage": found["stage"], "egfr": egfr, "citation": self.citations[self.data["ckd_stage_citation"]]}

    # drug-disease conflicts (deck slide 4) -----------------------------------------------------
    def check_conflicts(self, *, drug_class: str, egfr: float | None = None, sbp: float | None = None,
                        conditions: Iterable[str] = (), medications: Iterable[str] = ()) -> list[dict[str, Any]]:
        conditions, medications = list(conditions), list(medications)
        out = []
        for rule in self.data["conflict_rules"]:
            if rule["drug_class"] != drug_class:
                continue
            reasons, cites = [], []
            for t in rule["triggers"]:
                reason = self._apply_trigger(t, egfr, sbp, conditions, medications)
                if reason:
                    reasons.append(reason)
                    cites.append(self.citations[t["citation"]])
            if reasons:
                sev = rule["severity"]
                out.append({
                    "drug": rule["display_drug"], "conditions": reasons,
                    "severity": sev["two_or_more_reasons"] if len(reasons) >= 2 else sev["one_reason"],
                    "rationale": rule["rationale"], "citations": list(dict.fromkeys(cites)), "rule_id": rule["id"],
                })
        return out

    def _apply_trigger(self, t: dict[str, Any], egfr: float | None, sbp: float | None, conditions: list[str],
                       medications: list[str]) -> str | None:
        kind = t["type"]
        if kind == "egfr_below":
            if egfr is not None and egfr < t["value"]:
                stage = self.ckd_stage(egfr)
                return _fill(t["reason"], {"stage": stage["stage"] if stage else "?", "egfr": egfr})
            return None
        if kind == "hypertension":
            if any(re.search("hypertension", c, re.IGNORECASE) for c in conditions) or (sbp or 0) >= t["sbp_at_least"]:
                return _fill(t["reason_with_sbp"], {"sbp": sbp}) if sbp else t["reason"]
            return None
        if kind == "medication_contains":
            if any(n.lower() in m.lower() for m in medications for n in t["names"]):
                return str(t["reason"])
            return None
        raise KnowledgeError(f'Unknown trigger type "{kind}"')

    # red flags (L1 playbook) -----------------------------------------------------------------
    def red_flags(self, *, symptoms: Iterable[str], pain_score: float | None = None,
                  weight_loss_kg: float | None = None, history_rcc: bool = False,
                  distress_level: str | None = None, distress_score: float | None = None) -> dict[str, Any] | None:
        s = set(symptoms)
        pain, weight = pain_score or 0, weight_loss_kg or 0
        evidence = []
        if "visible_hematuria" in s:
            evidence.append("visible blood in urine")
        if pain >= 7 and "flank_pain" in s:
            evidence.append(f"severe one-sided flank pain ({pain_score:g}/10)")
        if weight >= 5:
            evidence.append(f"unintentional weight loss ({weight_loss_kg:g} kg)")
        if "dizziness" in s:
            evidence.append("dizziness")
        if "fever" in s:
            evidence.append("fever / chills")
        if history_rcc:
            evidence.append("known RCC / post-nephrectomy follow-up")
        if distress_level == "high":
            evidence.append(f"high non-verbal distress (fuzzy {distress_score or 0:.2f})")
        core = "visible_hematuria" in s and "flank_pain" in s and pain >= 7
        systemic = weight >= 5 or "dizziness" in s or "fever" in s
        if core and (systemic or history_rcc):
            return {"urgency": "high",
                    "pattern": "RCC follow-up + visible haematuria + severe flank pain + systemic signs",
                    "evidence": evidence, "citations": [self.citations["PLAYBOOK"], self.citations["EAU_RCC"]]}
        if "visible_hematuria" in s or ("flank_pain" in s and pain >= 7):
            return {"urgency": "medium", "pattern": "single red-flag symptom", "evidence": evidence,
                    "citations": [self.citations["PLAYBOOK"]]}
        return None

    # citations ---------------------------------------------------------------------------------
    def query(self, term: str) -> list[dict[str, str]]:
        t = str(term or "").lower()
        return [{"id": k, "citation": v} for k, v in self.citations.items() if t in k.lower() or t in v.lower()]

    def has_citation(self, text: str) -> bool:
        """Exact match only: a citation that merely contains a real source's text is not that source."""
        return text in self.citations.values()
