"""Confidence calibration by temperature scaling (Guo et al., 2017).

A single temperature T > 0 divides the logits before the softmax. T does not change the
predicted class, only how confident the signal claims to be. T is fitted on a held-out
validation set from Chrislin's training run (model version must match).
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from kcd.util import iso, utcnow

T_MIN, T_MAX = 0.05, 20.0
MIN_FIT_SAMPLES = 20


def softmax(logits: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    if not temperature > 0:
        raise ValueError("temperature must be positive")
    z = np.asarray(logits, dtype=np.float64) / temperature
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def logits_from_probs(probs: np.ndarray) -> np.ndarray:
    """Log-probabilities work as logits: softmax(log p) == p, and temperature scaling still applies."""
    return np.log(np.clip(np.asarray(probs, dtype=np.float64), 1e-12, 1.0))


def nll(logits: np.ndarray, labels: np.ndarray, temperature: float) -> float:
    p = softmax(logits, temperature)
    return float(-np.mean(np.log(np.clip(p[np.arange(len(labels)), labels], 1e-12, 1.0))))


def ece(probs: np.ndarray, labels: np.ndarray, bins: int = 15) -> float:
    """Expected calibration error of the top-label confidence."""
    probs = np.asarray(probs, dtype=np.float64)
    labels = np.asarray(labels)
    conf = probs.max(axis=1)
    correct = (probs.argmax(axis=1) == labels).astype(np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (conf > lo) & (conf <= hi) if i else (conf >= lo) & (conf <= hi)
        if mask.any():
            total += mask.mean() * abs(correct[mask].mean() - conf[mask].mean())
    return float(total)


def _validate(logits: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    logits = np.asarray(logits, dtype=np.float64)
    labels = np.asarray(labels)
    if logits.ndim != 2 or logits.shape[1] < 2:
        raise ValueError("logits must have shape (n_samples, n_classes >= 2)")
    if labels.shape != (logits.shape[0],):
        raise ValueError("labels must have shape (n_samples,)")
    if not np.issubdtype(labels.dtype, np.integer) or labels.min() < 0 or labels.max() >= logits.shape[1]:
        raise ValueError("labels must be class indices in range")
    if not np.all(np.isfinite(logits)):
        raise ValueError("logits must be finite")
    if logits.shape[0] < MIN_FIT_SAMPLES:
        raise ValueError(f"need at least {MIN_FIT_SAMPLES} validation samples to fit a temperature")
    return logits, labels.astype(np.int64)


def fit_temperature(logits: np.ndarray, labels: np.ndarray, iterations: int = 100) -> float:
    """Minimise validation NLL over log T with a golden-section search (NLL is unimodal in log T)."""
    logits, labels = _validate(logits, labels)
    a, b = math.log(T_MIN), math.log(T_MAX)
    ratio = (math.sqrt(5) - 1) / 2
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = nll(logits, labels, math.exp(c)), nll(logits, labels, math.exp(d))
    for _ in range(iterations):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = nll(logits, labels, math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = nll(logits, labels, math.exp(d))
    return float(math.exp((a + b) / 2))


@dataclass(frozen=True)
class Calibration:
    temperature: float
    model_version: str
    n_samples: int = 0
    ece_before: float | None = None
    ece_after: float | None = None
    fitted_at: str | None = None

    def __post_init__(self):
        if not (isinstance(self.temperature, (int, float)) and T_MIN <= self.temperature <= T_MAX):
            raise ValueError(f"temperature must be in [{T_MIN}, {T_MAX}]")
        if not self.model_version:
            raise ValueError("a calibration must name the model version it was fitted for")

    @classmethod
    def fit(cls, logits: np.ndarray, labels: np.ndarray, model_version: str) -> Calibration:
        logits, labels = _validate(logits, labels)
        t = fit_temperature(logits, labels)
        return cls(temperature=round(t, 4), model_version=model_version, n_samples=int(len(labels)),
                   ece_before=round(ece(softmax(logits), labels), 4),
                   ece_after=round(ece(softmax(logits, t), labels), 4), fitted_at=iso(utcnow()))

    def apply(self, logits: np.ndarray) -> np.ndarray:
        return softmax(logits, self.temperature)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Calibration:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))
