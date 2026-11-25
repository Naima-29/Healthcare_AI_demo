"""Run a target over a dataset and compute the task's metrics."""
from __future__ import annotations

import time

from evals import metrics
from evals.dataset import Dataset
from evals.targets import Predict


def run(ds: Dataset, predict: Predict) -> dict:
    field = ds.card.get("label_field", "urgency")
    rows = []
    for item in ds.items:
        t0 = time.perf_counter()
        try:
            pred, error = predict(item), None
        except Exception as e:  # a failing item is scored as a miss, never dropped
            miss = "none" if ds.task == "red_flag" else None
            pred, error = {field: miss, "score": 0.0, "evidence": []}, f"{type(e).__name__}: {e}"
        rows.append({
            "id": item["id"], "lang": item.get("lang"), "tags": item.get("tags", []),
            "label": item["label"][field], "pred": pred.get(field), "score": pred.get("score"),
            "evidence": pred.get("evidence", []), "error": error,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        })
    score = score_red_flag if ds.task == "red_flag" else score_classification
    return {"rows": rows, "metrics": score(rows, ds.card["labels"]), "checks": []}


def score_classification(rows: list[dict], labels: list[str]) -> dict:
    y_true = [r["label"] for r in rows]
    y_pred = [r["pred"] for r in rows]
    pc = metrics.per_class(y_true, y_pred, labels)
    return {
        "n": len(rows),
        "accuracy": sum(t == p for t, p in zip(y_true, y_pred, strict=True)) / len(rows) if rows else 0.0,
        "macro_f1": sum(c["f1"] for c in pc.values()) / len(pc) if pc else 0.0,
        "per_class": pc,
        "confusion": metrics.confusion(y_true, y_pred, labels),
        "errors": sum(1 for r in rows if r["error"]),
    }


def score_red_flag(rows: list[dict], labels: list[str]) -> dict:
    y_true = [r["label"] for r in rows]
    y_pred = [r["pred"] for r in rows]
    pc = metrics.per_class(y_true, y_pred, labels)
    high_true = [t == "high" for t in y_true]
    high_pred = [p == "high" for p in y_pred]
    any_true = [t != "none" for t in y_true]
    any_pred = [p != "none" for p in y_pred]
    crit = [r for r in rows if "critical" in r["tags"]]
    scores = [float(r["score"] or 0.0) for r in rows]
    return {
        "n": len(rows),
        "accuracy": sum(t == p for t, p in zip(y_true, y_pred, strict=True)) / len(rows) if rows else 0.0,
        "macro_f1": sum(c["f1"] for c in pc.values()) / len(pc) if pc else 0.0,
        "per_class": pc,
        "confusion": metrics.confusion(y_true, y_pred, labels),
        "high": metrics.binary(high_true, high_pred),
        "any_flag": metrics.binary(any_true, any_pred),
        "critical_recall": (sum(1 for r in crit if r["pred"] == "high") / len(crit)) if crit else None,
        "auroc_high": metrics.auroc(high_true, scores),
        "ece_high": metrics.ece(high_true, scores),
        "errors": sum(1 for r in rows if r["error"]),
    }


def check_thresholds(result: dict, thresholds: dict) -> list[dict]:
    """thresholds: {"high.recall": 0.98, ...} -> list of {metric, target, actual, ok}."""
    checks = []
    for key, target_value in thresholds.items():
        value: object = result["metrics"]
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        actual = float(value) if isinstance(value, (int, float)) else None
        checks.append({"metric": key, "target": target_value, "actual": actual,
                       "ok": actual is not None and actual >= target_value})
    result["checks"] = checks
    return checks
