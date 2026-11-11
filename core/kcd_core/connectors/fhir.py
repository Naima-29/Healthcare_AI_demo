"""`fhir` surface over any FHIR R4 server (HAPI in the dev stack). Returns compact, normalised records."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .base import ConnectorError, guarded, new_server, run_connector, safe_id

# Friendly names the seats use -> LOINC. Extend here, not in prompts.
LOINC: dict[str, list[str]] = {
    "egfr": ["98979-8", "62238-1", "33914-3", "48642-3", "48643-1"],
    "creatinine": ["2160-0", "38483-4"],
    "potassium": ["2823-3", "6298-4"],
    "hemoglobin": ["718-7"],
    "uacr": ["9318-7", "14959-1"],
    "blood_pressure": ["85354-9"],
}
_CODE = re.compile(r"^[0-9]{1,7}-[0-9]$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:.+Z-]+)?$")


class FhirClient:
    def __init__(self, base_url: str, *, token: str | None = None, timeout_s: float = 10.0,
                 client: httpx.AsyncClient | None = None) -> None:
        headers = {"Accept": "application/fhir+json"} | ({"Authorization": f"Bearer {token}"} if token else {})
        self._http = client or httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", timeout=timeout_s,
                                                 headers=headers)

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = await self._http.get(path, params=params)
        if resp.status_code == 404:
            raise ConnectorError("not found")
        resp.raise_for_status()  # anything else becomes the generic "upstream unavailable"
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected FHIR response")
        return body

    async def search(self, resource: str, params: dict[str, str]) -> list[dict[str, Any]]:
        bundle = await self.get(resource, params)
        return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]


def _coding(cc: dict[str, Any] | None) -> dict[str, str | None]:
    c = ((cc or {}).get("coding") or [{}])[0]
    return {"system": c.get("system"), "code": c.get("code"), "display": c.get("display") or (cc or {}).get("text")}


def _observation(r: dict[str, Any]) -> dict[str, Any]:
    q = r.get("valueQuantity") or {}
    value: Any = q.get("value", r.get("valueString"))
    return {"id": r.get("id"), "code": _coding(r.get("code")), "value": value, "unit": q.get("unit"),
            "effective": r.get("effectiveDateTime") or (r.get("effectivePeriod") or {}).get("start"),
            "status": r.get("status")}


def build_fhir_server(fhir: FhirClient) -> FastMCP:
    server = new_server("kcd-fhir", "Read-only access to the patient's FHIR record.")

    @guarded(server, "fhir.get_patient")
    async def get_patient(patient: str) -> dict[str, Any]:
        """Demographics only: pseudonymous id, birth year, sex. No name, address or contacts."""
        r = await fhir.get(f"Patient/{safe_id(patient, 'patient')}")
        return {"id": r.get("id"), "birth_year": (r.get("birthDate") or "")[:4] or None, "sex": r.get("gender")}

    @guarded(server, "fhir.get_observations")
    async def get_observations(patient: str, code: str = "egfr", since: str | None = None,
                               limit: int = 20) -> list[dict[str, Any]]:
        """Observations for a patient, newest first. `code` is a friendly name (egfr, creatinine, potassium,
        hemoglobin, uacr, blood_pressure) or a LOINC code."""
        pid = safe_id(patient, "patient")
        codes = LOINC.get(code.strip().lower()) or ([code.strip()] if _CODE.match(code.strip()) else None)
        if codes is None:
            raise ConnectorError(f"unknown code; use one of {sorted(LOINC)} or a LOINC code")
        params = {"patient": pid, "code": ",".join(f"http://loinc.org|{c}" for c in codes),
                  "_sort": "-date", "_count": str(max(1, min(int(limit), 100)))}
        if since:
            if not _DATE.match(since):
                raise ConnectorError("'since' must be an ISO-8601 date")
            params["date"] = f"ge{since}"
        return [_observation(r) for r in await fhir.search("Observation", params)]

    @guarded(server, "fhir.get_conditions")
    async def get_conditions(patient: str) -> list[dict[str, Any]]:
        """Active problem list."""
        pid = safe_id(patient, "patient")
        rows = await fhir.search("Condition", {"patient": pid, "clinical-status": "active", "_count": "100"})
        return [{"id": r.get("id"), "code": _coding(r.get("code")), "onset": r.get("onsetDateTime")} for r in rows]

    @guarded(server, "fhir.get_medications")
    async def get_medications(patient: str) -> list[dict[str, Any]]:
        """Active medication orders."""
        pid = safe_id(patient, "patient")
        rows = await fhir.search("MedicationRequest", {"patient": pid, "status": "active", "_count": "100"})
        return [{"id": r.get("id"), "medication": _coding(r.get("medicationCodeableConcept")),
                 "dosage": ((r.get("dosageInstruction") or [{}])[0]).get("text"), "authored": r.get("authoredOn")}
                for r in rows]

    return server


def main() -> None:  # python -m kcd_core.connectors.fhir
    base = os.environ.get("KCD_FHIR_URL", "http://localhost:8080/fhir")
    run_connector(build_fhir_server(FhirClient(base, token=os.environ.get("KCD_FHIR_TOKEN"))))


if __name__ == "__main__":
    main()
