"""Mamdani fuzzy distress estimator ported from the v2 demo.

It consumes already-extracted voice prosody and facial action-unit features.
It does not perform speech recognition or face analysis itself.
"""
from __future__ import annotations

from typing import Any


def _tri(a: float, b: float, c: float, x: float) -> float:
    if x <= a or x >= c:
        return 1.0 if x == b else 0.0
    return (x - a) / (b - a) if x < b else (c - x) / (c - b)


def _shoulder_left(a: float, b: float, x: float) -> float:
    return 1.0 if x <= a else 0.0 if x >= b else (b - x) / (b - a)


def _shoulder_right(a: float, b: float, x: float) -> float:
    return 0.0 if x <= a else 1.0 if x >= b else (x - a) / (b - a)


def pspi(au: dict[str, Any] | None = None) -> float:
    au = au or {}
    value = lambda key: float(au.get(key) or 0)
    return value("AU4") + max(value("AU6"), value("AU7")) + max(value("AU9"), value("AU10")) + value("AU43")


def voice_strain(voice: dict[str, Any] | None = None) -> float:
    voice = voice or {}
    tremor = float(voice.get("tremor") or 0)
    pitch = float(voice.get("pitch_var") or 0)
    rate = abs(float(voice.get("rate_dev") or 0))
    return min(1.0, 0.5 * tremor + 0.3 * pitch + 0.2 * rate)


def distress(
    *, verbal: float = 0.0, face: float = 0.0, voice: float = 0.0, minimizer: bool = False
) -> dict[str, Any]:
    """Return score, level, under-reporting and fuzzy rule firing strengths."""
    verbal = max(0.0, min(1.0, float(verbal)))
    face = max(0.0, min(1.0, float(face)))
    voice = max(0.0, min(1.0, float(voice)))
    nonverbal = max(face, 0.7 * voice + 0.3 * face)

    def memberships(x: float) -> dict[str, float]:
        return {
            "low": _shoulder_left(0.2, 0.45, x),
            "med": _tri(0.25, 0.5, 0.75, x),
            "high": _shoulder_right(0.55, 0.8, x),
        }

    v, n = memberships(verbal), memberships(nonverbal)
    fire = {
        "high": max(v["high"], n["high"], min(v["med"], n["med"]) * 0.6),
        "med": max(min(v["med"], n["low"]), min(v["low"], n["med"]), min(v["med"], n["med"])),
        "low": min(v["low"], n["low"]),
    }

    numerator = denominator = 0.0
    for step in range(101):
        x = step / 100
        out_low = _shoulder_left(0.15, 0.4, x)
        out_med = _tri(0.25, 0.5, 0.75, x)
        out_high = _shoulder_right(0.6, 0.85, x)
        mu = max(min(fire["low"], out_low), min(fire["med"], out_med), min(fire["high"], out_high))
        numerator += x * mu
        denominator += mu
    score = numerator / denominator if denominator else 0.0
    level = "high" if score >= 0.62 else "moderate" if score >= 0.38 else "low"
    underreporting = nonverbal - verbal >= 0.3 and (minimizer or verbal < 0.5)
    return {
        "score": round(score, 3),
        "level": level,
        "verbal": round(verbal, 3),
        "nonverbal": round(nonverbal, 3),
        "underreporting": underreporting,
        "fired": fire,
        "face": round(face, 3),
        "voice": round(voice, 3),
    }


def from_features(
    *, verbal: float, minimizer: bool = False, features: dict[str, Any] | None = None
) -> dict[str, Any]:
    features = features or {}
    face = min(1.0, pspi(features.get("au") or {}) / 16.0)
    voice = voice_strain(features.get("voice") or {})
    return distress(verbal=verbal, face=face, voice=voice, minimizer=minimizer)
