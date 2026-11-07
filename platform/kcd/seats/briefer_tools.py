"""Tool bindings for the Case Briefer: read-only FHIR background plus the Clinical Board.

`fhir.get_patient` has no binding elsewhere in the repo (FolderFhir has no patient method), so it is added here as a
read-only lookup of the Patient resource. The other fhir.* tools and `board.create_item` reuse the existing bindings.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from kcd.fhir import FolderFhir
from kcd.knowledge import KnowledgeModel
from kcd.tools import Tool, board_tools, clinical_tools


def briefer_tools(fhir: FolderFhir, km: KnowledgeModel, now: Callable[[], datetime],
                  create_item: Callable[[dict[str, Any]], str]) -> dict[str, Tool]:
    base = clinical_tools(fhir, km, now)
    tools: dict[str, Tool] = {k: base[k] for k in ("fhir.get_conditions", "fhir.get_medications",
                                                  "fhir.get_observations")}

    async def get_patient(patient_id: str) -> dict[str, Any]:
        res = fhir._resources(patient_id, "Patient")  # noqa: SLF001 - same package, read-only
        if not res:
            raise LookupError(f"no Patient resource for {patient_id}")
        p = res[0]
        n = (p.get("name") or [{}])[0]
        return {"id": p.get("id"), "name": " ".join([*n.get("given", []), n.get("family", "")]).strip() or None,
                "gender": p.get("gender"), "birthDate": p.get("birthDate")}

    tools["fhir.get_patient"] = get_patient
    tools.update(board_tools(create_item))
    return tools
