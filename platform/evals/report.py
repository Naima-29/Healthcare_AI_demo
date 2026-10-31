"""Markdown + JSON reports."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from evals.dataset import Dataset


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def markdown(ds: Dataset, target: str, result: dict) -> str:
    m = result["metrics"]
    labels = ds.card["labels"]
    lines = [
        f"# Eval: {ds.name} v{ds.card['version']} × {target}",
        "",
        f"- Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Items: {m['n']} · errors: {m['errors']}",
        f"- Review status: **{ds.card['review_status']}**"
        + ("" if ds.reviewed else " (draft labels, not clinical evidence)"),
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    if "high" in m:  # red-flag task
        lines += [
            f"| Must-escalate (high) recall | {_f(m['high']['recall'])} |",
            f"| Must-escalate (high) precision | {_f(m['high']['precision'])} |",
            f"| Critical recall | {_f(m['critical_recall'])} |",
            f"| Any red flag recall | {_f(m['any_flag']['recall'])} |",
        ]
    lines += [f"| Accuracy | {_f(m['accuracy'])} |", f"| Macro F1 | {_f(m['macro_f1'])} |"]
    if "auroc_high" in m:
        lines += [f"| AUROC (high) | {_f(m['auroc_high'])} |", f"| ECE (high) | {_f(m['ece_high'])} |"]
    lines.append("")
    if result["checks"]:
        lines += ["## Thresholds", "", "| Metric | Target | Actual | |", "|---|---|---|---|"]
        lines += [f"| {c['metric']} | {c['target']} | {_f(c['actual'])} | {'pass' if c['ok'] else '**FAIL**'} |"
                  for c in result["checks"]]
        lines.append("")
    lines += ["## Per class", "", "| Class | Precision | Recall | F1 | Support |", "|---|---|---|---|---|"]
    lines += [f"| {c} | {_f(v['precision'])} | {_f(v['recall'])} | {_f(v['f1'])} | {v['support']} |"
              for c, v in m["per_class"].items()]
    lines += ["", "## Confusion (rows = label, columns = prediction)", "",
              "| | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]
    lines += [f"| **{t}** | " + " | ".join(str(m["confusion"][t].get(p, 0)) for p in labels) + " |" for t in labels]
    misses = [r for r in result["rows"] if r["label"] != r["pred"]]
    lines += ["", f"## Misclassified ({len(misses)})", "", "| Id | Lang | Label | Pred | Tags | Error |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['lang']} | {r['label']} | {r['pred']} | {', '.join(r['tags'])} | {r['error'] or ''} |"
              for r in misses]
    return "\n".join(lines) + "\n"


def write(ds: Dataset, target: str, result: dict, out_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = out_dir / ds.name
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / f"{target}-{stamp}.md"
    md.write_text(markdown(ds, target, result), encoding="utf-8")
    payload = {"dataset": ds.name, "version": ds.card["version"], "target": target, **result}
    (folder / f"{target}-{stamp}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return md
