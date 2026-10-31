"""A2A envelope and the payloads this slice sends or receives.

PROVISIONAL: Harith (Core) freezes contract C2 in week 1. Until then these
validators mirror the demo's contracts/a2aSchema.mjs field names, so swapping
in the frozen schemas should only touch this file. Deviations from the demo,
to raise with Core:
  * imaging_signal carries a `status`; when status != "ok" the label may be
    "indeterminate" and p_calibrated may be null (unreadable image, model down).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from kcd.util import iso, new_id, utcnow

A2A_TYPES = (
    "symptom_report", "red_flag_escalation", "medication_report", "data_request", "data_delivered",
    "imaging_signal", "therapy_conflict", "case_pack_ready", "clinician_decision", "acknowledgment",
)

CT_LABELS = ("normal", "cyst", "stone", "tumor")
IMAGING_STATUSES = ("ok", "out_of_distribution", "unreadable", "model_error")


class ContractError(ValueError):
    pass


@dataclass
class Envelope:
    type: str
    sender: str
    to: str
    patient: str
    payload: dict
    correlation: str | None = None
    route_hint: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("msg"))
    ts: str = field(default_factory=lambda: iso(utcnow()))

    def as_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "from": self.sender, "to": self.to, "patient": self.patient,
                "correlation": self.correlation, "route_hint": list(self.route_hint), "ts": self.ts,
                "payload": self.payload}


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _require(payload: dict, keys: tuple[str, ...], kind: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ContractError(f"{kind}: missing {missing}")


def validate_imaging_signal(p: dict) -> None:
    _require(p, ("status", "label", "p_calibrated", "quality", "provenance"), "imaging_signal")
    if p["status"] not in IMAGING_STATUSES:
        raise ContractError(f"imaging_signal: status {p['status']!r} not in {IMAGING_STATUSES}")
    if p["status"] in ("ok", "out_of_distribution"):
        if p["label"] not in CT_LABELS:
            raise ContractError(f"imaging_signal: label {p['label']!r} not in {CT_LABELS}")
        if not _is_num(p["p_calibrated"]) or not 0.0 <= p["p_calibrated"] <= 1.0:
            raise ContractError("imaging_signal: p_calibrated must be a number in [0, 1]")
    elif p["label"] != "indeterminate" or p["p_calibrated"] is not None:
        raise ContractError("imaging_signal: without a model result, label must be 'indeterminate' and p_calibrated null")
    if not isinstance(p["quality"], dict) or "ok" not in p["quality"]:
        raise ContractError("imaging_signal: quality must be an object with 'ok'")
    if not isinstance(p["provenance"], dict) or "study_uid" not in p["provenance"]:
        raise ContractError("imaging_signal: provenance must include study_uid")


def validate_data_delivered(p: dict) -> None:
    _require(p, ("kind", "data"), "data_delivered")
    if not isinstance(p["kind"], str) or not isinstance(p["data"], dict):
        raise ContractError("data_delivered: kind must be a string and data an object")


def validate_data_request(p: dict) -> None:
    _require(p, ("what", "why"), "data_request")


def validate_clinician_decision(p: dict) -> None:
    _require(p, ("case_id", "signed_by", "items"), "clinician_decision")
    if not isinstance(p["items"], list):
        raise ContractError("clinician_decision: items must be a list")
    for i, item in enumerate(p["items"]):
        if not isinstance(item, dict) or "code" not in item or "patient_text" not in item:
            raise ContractError(f"clinician_decision: item {i} needs 'code' and 'patient_text'")


VALIDATORS = {
    "imaging_signal": validate_imaging_signal,
    "data_delivered": validate_data_delivered,
    "data_request": validate_data_request,
    "clinician_decision": validate_clinician_decision,
}


def validate(msg_type: str, payload: dict) -> None:
    if msg_type not in A2A_TYPES:
        raise ContractError(f"unknown message type {msg_type!r}")
    if not isinstance(payload, dict):
        raise ContractError(f"{msg_type}: payload must be an object")
    check = VALIDATORS.get(msg_type)
    if check:
        check(payload)
