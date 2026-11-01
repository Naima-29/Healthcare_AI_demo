"""Tool bindings for the Evidence + Database seats.

kcd_core reaches every tool through its ToolRegistry, after the PEP has checked it. In production
each entry is an MCP client call; these bindings wrap the local adapters so the seats run today:

    pacs.list_studies    -> PacsSource.list_studies          (Imaging Reader)
    pacs.fetch_study     -> PacsSource.fetch_key_image       (Imaging Reader)
    ct.classify          -> CtModel.predict                  (Imaging Reader, model from Person 5)
    genomics.read_panel  -> PanelSource.get_panel            (Genomics Interpreter)
    km.lookup            -> variant rule table               (Genomics Interpreter, Knowledge Model of Person 4)
    fhir.read_patient    -> patient summary with conditions  (Genomics Interpreter)
    board.read_artifact  -> released artifact + release      (Archivist, Clinical Board of Person 5)

There is no write tool for PACS or genomics here at all (refusals no_image_alteration and
no_variant_reclassification also block them in the catalogue).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from kcd.genomics.rules import RULES_PATH
from kcd.genomics.source import PanelSource
from kcd.imaging.image_io import load_image
from kcd.imaging.model import CtModel
from kcd.imaging.pacs import PacsSource, StudyRef

Tool = Callable[..., Awaitable[Any]]


def imaging_tools(pacs: PacsSource, model: CtModel) -> dict[str, Tool]:
    async def list_studies(patient_id: str) -> list[StudyRef]:
        return pacs.list_studies(patient_id)

    async def fetch_study(study: StudyRef) -> dict[str, Any]:
        data, media_type = pacs.fetch_key_image(study)
        return {"data": data, "media_type": media_type}

    async def classify(image_bytes: bytes, media_type: str) -> Any:
        return model.predict(load_image(image_bytes), image_bytes, media_type)

    return {"pacs.list_studies": list_studies, "pacs.fetch_study": fetch_study, "ct.classify": classify}


def genomics_tools(panels: PanelSource, *, rules_path: Path = RULES_PATH,
                   patients: Mapping[str, dict[str, Any]] | None = None) -> dict[str, Tool]:
    """`patients` maps patient id -> summary ({"conditions": [...]}) until the FHIR server is connected."""
    summaries = dict(patients or {})

    async def read_panel(patient_id: str) -> dict[str, Any]:
        raw, panel = panels.get_panel(patient_id)
        return {"raw": raw, "panel": panel}

    async def km_lookup(topic: str) -> dict[str, Any]:
        if topic != "variant_rules":
            raise LookupError(f"km.lookup: no local binding for topic {topic!r}")
        text = await asyncio.to_thread(Path(rules_path).read_text, encoding="utf-8")
        return json.loads(text)

    async def read_patient(patient_id: str) -> dict[str, Any]:
        return dict(summaries.get(patient_id, {"conditions": []}))

    return {"genomics.read_panel": read_panel, "km.lookup": km_lookup, "fhir.read_patient": read_patient}


def board_tools(read_artifact: Callable[[str], dict[str, Any]]) -> dict[str, Tool]:
    """`read_artifact(artifact_id)` returns {"artifact": dict, "release": kcd_core Release}."""

    async def board_read_artifact(artifact_id: str) -> dict[str, Any]:
        return read_artifact(artifact_id)

    return {"board.read_artifact": board_read_artifact}
