"""Variant rules (ACMG context) and the interpretation of a whole panel.

Rules give clinical *relevance* for this case. They never change the laboratory's
classification: every finding carries the lab class verbatim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kcd.genomics.panel import CLASSIFICATIONS, Panel, Variant

RULES_PATH = Path(__file__).parent / "variant_rules.json"
RELEVANCE_ORDER = ("high", "moderate", "review", "unknown", "low", "none", "filtered")
DISCLOSURE = "Disclosure to the patient requires a geneticist (gate genetic_disclosure)."


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class VariantRules:
    version: str
    citations: dict
    gene_rules: tuple[dict, ...]
    class_defaults: dict

    @classmethod
    def load(cls, path: str | Path = RULES_PATH) -> VariantRules:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, d: dict) -> VariantRules:
        missing_defaults = [c for c in CLASSIFICATIONS if c not in d.get("class_defaults", {})]
        if missing_defaults:
            raise RulesError(f"class_defaults missing {missing_defaults}")
        citations = d.get("citations", {})
        for r in list(d.get("gene_rules", [])) + list(d["class_defaults"].values()):
            if r.get("relevance") not in RELEVANCE_ORDER:
                raise RulesError(f"rule {r.get('id', '(default)')}: relevance {r.get('relevance')!r} not allowed")
            unknown = [c for c in r.get("citations", []) if c not in citations]
            if unknown:
                raise RulesError(f"rule {r.get('id', '(default)')}: unknown citations {unknown}")
        for r in d.get("gene_rules", []):
            if not r.get("id") or not r.get("genes") or not r.get("classifications"):
                raise RulesError(f"gene rule {r.get('id')!r} needs id, genes and classifications")
            bad = [c for c in r["classifications"] if c not in CLASSIFICATIONS]
            if bad:
                raise RulesError(f"rule {r['id']}: unknown classifications {bad}")
        return cls(version=str(d.get("version", "")), citations=citations,
                   gene_rules=tuple(d.get("gene_rules", [])), class_defaults=d["class_defaults"])

    def match(self, v: Variant, risk_alleles_in_gene: int) -> dict:
        for r in self.gene_rules:
            if v.gene not in r["genes"] or v.classification not in r["classifications"]:
                continue
            if "min_risk_alleles" in r and risk_alleles_in_gene < r["min_risk_alleles"]:
                continue
            if "max_risk_alleles" in r and risk_alleles_in_gene > r["max_risk_alleles"]:
                continue
            return r
        return {"id": f"default:{v.classification}", **self.class_defaults[v.classification]}


def interpret(panel: Panel, rules: VariantRules, context: dict | None = None) -> dict:
    history = (context or {}).get("history", {}) or {}
    risk_alleles: dict[str, int] = {}
    for v in panel.variants:
        if v.classification == "risk_allele":
            risk_alleles[v.gene] = risk_alleles.get(v.gene, 0) + v.allele_count

    findings, not_reported, syndromes, implications = [], [], [], []
    for v in panel.variants:
        rule = rules.match(v, risk_alleles.get(v.gene, 0))
        texts = [i["text"] for i in rule.get("case_implications", [])
                 if not i.get("when_history") or history.get(i["when_history"])]
        finding = {
            "gene": v.gene, "variant_id": v.variant_id, "hgvs": v.hgvs, "zygosity": v.zygosity,
            "lab_classification": v.classification_raw,          # verbatim, never reclassified
            "classification_normalized": v.classification,
            "relevance": rule["relevance"], "actionable": bool(rule.get("actionable", False)),
            "summary": rule["summary"], "implications": texts,
            "citations": [rules.citations[c] for c in rule.get("citations", [])],
            "citation_ids": list(rule.get("citations", [])), "rule_id": rule["id"],
        }
        if v.classification == "risk_allele":
            finding["risk_alleles_in_gene"] = risk_alleles.get(v.gene, 0)
        if rule["relevance"] == "filtered":
            not_reported.append({"gene": v.gene, "variant_id": v.variant_id,
                                 "lab_classification": v.classification_raw})
            continue
        findings.append(finding)
        if rule.get("hereditary_syndrome") and rule["hereditary_syndrome"] not in syndromes:
            syndromes.append(rule["hereditary_syndrome"])
        implications += [t for t in texts if t not in implications]

    findings.sort(key=lambda f: RELEVANCE_ORDER.index(f["relevance"]))
    return {
        "panel_id": panel.panel_id, "method": panel.method, "date": panel.date, "source_format": panel.source_format,
        "findings": findings, "not_reported": not_reported, "hereditary_syndromes": syndromes,
        "case_implications": implications,
        "needs_geneticist_review": any(f["relevance"] in ("review", "unknown") for f in findings),
        "disclosure": DISCLOSURE, "rules_version": rules.version,
    }
