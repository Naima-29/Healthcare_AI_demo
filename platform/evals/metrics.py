"""Metrics for classification targets. Standard library only."""
from __future__ import annotations


def confusion(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict[str, dict[str, int]]:
    m = {t: {p: 0 for p in labels} for t in labels}
    for t, p in zip(y_true, y_pred, strict=True):
        m.setdefault(t, {q: 0 for q in labels})
        m[t][p] = m[t].get(p, 0) + 1
    return m


def per_class(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict[str, dict[str, float]]:
    out = {}
    for c in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[c] = {"precision": prec, "recall": rec, "f1": f1, "support": tp + fn}
    return out


def binary(y_true: list[bool], y_pred: list[bool]) -> dict[str, float]:
    tp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred, strict=True) if not t and p)
    fn = sum(1 for t, p in zip(y_true, y_pred, strict=True) if t and not p)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": prec, "recall": rec, "tp": tp, "fp": fp, "fn": fn}


def auroc(y_true: list[bool], scores: list[float]) -> float | None:
    """Probability that a random positive scores above a random negative (ties count half)."""
    pos = [s for t, s in zip(y_true, scores, strict=True) if t]
    neg = [s for t, s in zip(y_true, scores, strict=True) if not t]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def ece(y_true: list[bool], scores: list[float], bins: int = 10) -> float | None:
    """Expected calibration error with equal-width bins."""
    if not scores:
        return None
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if lo <= s < hi or (b == bins - 1 and s == 1.0)]
        if not idx:
            continue
        conf = sum(scores[i] for i in idx) / len(idx)
        acc = sum(1 for i in idx if y_true[i]) / len(idx)
        total += len(idx) / len(scores) * abs(acc - conf)
    return total
