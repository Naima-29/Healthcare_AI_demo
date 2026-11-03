"""Neuro-fuzzy distress score for the Companion stand-in. Port of the demo's humanized/fuzzy.mjs.

Inputs in 0..1: verbal (words), face (PSPI / 16 from action units), voice (tremor, pitch, rate). Mamdani
inference with centroid defuzzification. In the demo the face and voice values come from presets on the phone
screen, not from a camera or a microphone. Person 2 (Meet) owns the real thing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Membership = Callable[[float], float]


def _tri(a: float, b: float, c: float) -> Membership:
    def f(x: float) -> float:
        if x <= a or x >= c:
            return 1.0 if x == b else 0.0
        return (x - a) / (b - a) if x < b else (c - x) / (c - b)
    return f


def _left(a: float, b: float) -> Membership:
    return lambda x: 1.0 if x <= a else 0.0 if x >= b else (b - x) / (b - a)


def _right(a: float, b: float) -> Membership:
    return lambda x: 0.0 if x <= a else 1.0 if x >= b else (x - a) / (b - a)


IN = {"low": _left(0.2, 0.45), "med": _tri(0.25, 0.5, 0.75), "high": _right(0.55, 0.8)}
OUT = {"low": _left(0.15, 0.4), "med": _tri(0.25, 0.5, 0.75), "high": _right(0.6, 0.85)}


def pspi(au: dict[str, float]) -> float:
    v = lambda k: float(au.get(k) or 0)  # noqa: E731
    return v("AU4") + max(v("AU6"), v("AU7")) + max(v("AU9"), v("AU10")) + v("AU43")


def voice_strain(voice: dict[str, float]) -> float:
    return min(1.0, 0.5 * float(voice.get("tremor") or 0) + 0.3 * float(voice.get("pitch_var") or 0)
               + 0.2 * abs(float(voice.get("rate_dev") or 0)))


def distress(*, verbal: float = 0.0, face: float = 0.0, voice: float = 0.0, minimizer: bool = False) -> dict[str, Any]:
    nonverbal = max(face, 0.7 * voice + 0.3 * face)
    v = {k: f(verbal) for k, f in IN.items()}
    n = {k: f(nonverbal) for k, f in IN.items()}
    fire = {"high": max(v["high"], n["high"], min(v["med"], n["med"]) * 0.6),
            "med": max(min(v["med"], n["low"]), min(v["low"], n["med"]), min(v["med"], n["med"])),
            "low": min(v["low"], n["low"])}
    num = den = 0.0
    for i in range(101):
        x = i / 100
        mu = max(min(fire[k], OUT[k](x)) for k in fire)
        num += x * mu
        den += mu
    score = num / den if den else 0.0
    level = "high" if score >= 0.62 else "medium" if score >= 0.38 else "low"
    return {"score": round(score, 3), "level": level, "verbal": verbal, "nonverbal": round(nonverbal, 3),
            "underreporting": nonverbal - verbal >= 0.3 and (minimizer or verbal < 0.5)}


def from_features(*, verbal: float, minimizer: bool, features: dict[str, Any] | None) -> dict[str, Any]:
    features = features or {}
    face = min(1.0, pspi(features.get("au") or {}) / 16)
    voice = voice_strain(features.get("voice") or {})
    return {**distress(verbal=verbal, face=face, voice=voice, minimizer=minimizer),
            "face": round(face, 3), "voice": round(voice, 3)}
