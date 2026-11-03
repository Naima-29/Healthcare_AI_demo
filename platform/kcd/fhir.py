"""Read-only FHIR R4 source over a local synthetic bundle (one folder per patient).

Same answers as the demo's fhir MCP server (demo/kidney-care-desk/backend/mcp/servers.mjs): conditions,
medication statements, and observations by code text, newest first, with the age of the latest one.
A FHIR server (HAPI) replaces it behind the same methods once Core's MCP template exists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class FhirError(LookupError):
    pass


class FolderFhir:
    """Layout: <root>/<patient_id>/fhir-bundle.json"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resources(self, patient_id: str, resource_type: str) -> list[dict[str, Any]]:
        d = (self.root / patient_id).resolve()
        if d.parent != self.root:
            raise FhirError(f"invalid patient id {patient_id!r}")
        path = d / "fhir-bundle.json"
        if not path.is_file():
            raise FhirError(f"no FHIR record for patient {patient_id}")
        bundle = json.loads(path.read_text(encoding="utf-8"))
        return [e["resource"] for e in bundle.get("entry", [])
                if e.get("resource", {}).get("resourceType") == resource_type]

    def conditions(self, patient_id: str) -> list[dict[str, Any]]:
        out = []
        for c in self._resources(patient_id, "Condition"):
            coding = (c.get("code", {}).get("coding") or [{}])[0]
            out.append({"code": coding.get("code"), "display": coding.get("display") or c.get("code", {}).get("text"),
                        "onset": c.get("onsetDateTime")})
        return out

    def medications(self, patient_id: str) -> list[dict[str, Any]]:
        return [{"name": m.get("medicationCodeableConcept", {}).get("text"), "status": m.get("status"),
                 "dosage": (m.get("dosage") or [{}])[0].get("text")}
                for m in self._resources(patient_id, "MedicationStatement")]

    def observations(self, patient_id: str, code: str, now: datetime) -> dict[str, Any]:
        series = sorted(
            ({"date": o["effectiveDateTime"], "value": o.get("valueQuantity", {}).get("value"),
              "unit": o.get("valueQuantity", {}).get("unit")}
             for o in self._resources(patient_id, "Observation")
             if (o.get("code", {}).get("text") or "").lower() == code.lower()),
            key=lambda x: x["date"], reverse=True)
        latest = series[0] if series else None
        age_days = None
        if latest:
            taken = datetime.fromisoformat(latest["date"])
            taken = taken if taken.tzinfo else taken.replace(tzinfo=UTC)
            age_days = round((now - taken).total_seconds() / 86400)
        trend = "flat" if len(series) < 2 else ("down" if series[0]["value"] < series[1]["value"] else "up")
        return {"code": code, "latest": latest, "age_days": age_days, "series": series, "trend": trend}
