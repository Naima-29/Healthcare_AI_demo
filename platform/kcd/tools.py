"""Tool bindings for the seats in this package, named as in the policy catalogue (contracts/policy_catalog.json).

kcd_core reaches every tool through its ToolRegistry, after the PEP has checked it. In production each entry is
an MCP client call; these bindings wrap local adapters so the desk runs today:

    surface    tool                    adapter                                  used by
    pacs       pacs.list_studies       PacsSource.list_studies                  Imaging Reader
    pacs       pacs.fetch_study        PacsSource.fetch_key_image               Imaging Reader
    imaging    ct.quality_ood_check    kcd.imaging.quality.check_image          Imaging Reader
    imaging    ct.classify             CtModel.predict (model from Person 5)    Imaging Reader
    genomics   genomics.get_panel      PanelSource.get_panel                    Genomics Interpreter
    km         km.interpret_variant    knowledge/variant_rules.json             Genomics Interpreter
    fhir       fhir.get_conditions     FolderFhir                               Genomics Interpreter, Renal Guardian
    fhir       fhir.get_medications    FolderFhir                               Renal Guardian
    fhir       fhir.get_observations   FolderFhir (age relative to the clock)   Renal Guardian
    wearables  wearables.read          fixed 7-day summary                      Renal Guardian
    km         km.check_conflicts      knowledge/rules.json                     Renal Guardian
    km         km.ckd_stage            knowledge/rules.json                     Renal Guardian
    km         km.query                knowledge/rules.json (citations)         Safety Verifier
    (internal) board.create_item       Clinical Board review item               Archivist

There is no write tool for PACS, FHIR or genomics here at all (refusals alter_image, alter_record and
reclassify_variants also block those actions in the catalogue).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from kcd.fhir import FolderFhir
from kcd.genomics.panel import Panel
from kcd.genomics.rules import VariantRules, interpret
from kcd.genomics.source import PanelSource
from kcd.imaging.image_io import load_image
from kcd.imaging.model import CtModel
from kcd.imaging.pacs import PacsSource, StudyRef
from kcd.imaging.quality import DEFAULT_THRESHOLDS, QualityThresholds, check_image
from kcd.knowledge import KnowledgeModel

Tool = Callable[..., Awaitable[Any]]


def imaging_tools(pacs: PacsSource, model: CtModel,
                  thresholds: QualityThresholds = DEFAULT_THRESHOLDS) -> dict[str, Tool]:
    async def list_studies(patient_id: str) -> list[StudyRef]:
        return pacs.list_studies(patient_id)

    async def fetch_study(study: StudyRef) -> dict[str, Any]:
        data, media_type = pacs.fetch_key_image(study)
        return {"data": data, "media_type": media_type}

    async def quality_ood_check(image_bytes: bytes) -> dict[str, Any]:
        return check_image(load_image(image_bytes), thresholds)

    async def classify(image_bytes: bytes, media_type: str) -> Any:
        # In a worker thread: a real model takes seconds per slice and must not stall the bus.
        return await asyncio.to_thread(model.predict, load_image(image_bytes), image_bytes, media_type)

    return {"pacs.list_studies": list_studies, "pacs.fetch_study": fetch_study,
            "ct.quality_ood_check": quality_ood_check, "ct.classify": classify}


def genomics_tools(panels: PanelSource, fhir: FolderFhir, rules: VariantRules | None = None) -> dict[str, Tool]:
    variant_rules = rules or VariantRules.load()

    async def get_panel(patient_id: str) -> dict[str, Any]:
        raw, panel = panels.get_panel(patient_id)
        return {"raw": raw, "panel": panel}

    async def interpret_variant(panel: Panel, history: dict[str, bool]) -> dict[str, Any]:
        return interpret(panel, variant_rules, {"history": history})

    async def get_conditions(patient_id: str) -> list[dict[str, Any]]:
        return fhir.conditions(patient_id)

    return {"genomics.get_panel": get_panel, "km.interpret_variant": interpret_variant,
            "fhir.get_conditions": get_conditions}


def clinical_tools(
    fhir: FolderFhir,
    km: KnowledgeModel,
    now: Callable[[], datetime],
    wearables: Mapping[str, dict[str, Any]] | None = None,
    wearables_consent: Callable[[str], bool] | None = None,
) -> dict[str, Tool]:
    """Tools for the Renal Guardian and the Safety Verifier. `wearables` maps patient id -> 7-day summary."""
    summaries = dict(wearables or {})

    async def get_observations(patient_id: str, code: str) -> dict[str, Any]:
        return fhir.observations(patient_id, code, now())

    async def get_conditions(patient_id: str) -> list[dict[str, Any]]:
        return fhir.conditions(patient_id)

    async def get_medications(patient_id: str) -> list[dict[str, Any]]:
        return fhir.medications(patient_id)

    async def wearables_read(patient_id: str, days: int = 30) -> dict[str, Any]:
        if wearables_consent is None or not wearables_consent(patient_id):
            raise PermissionError(
                "explicit patient consent is required before reading wearables"
        )

        return {"summary_7d": dict(summaries.get(patient_id, {}))}

    async def check_conflicts(**kwargs: Any) -> list[dict[str, Any]]:
        return km.check_conflicts(**kwargs)

    async def ckd_stage(egfr: float | None) -> dict[str, Any] | None:
        return km.ckd_stage(egfr)

    async def query(term: str) -> list[dict[str, str]]:
        return km.query(term)

    return {"fhir.get_observations": get_observations, "fhir.get_conditions": get_conditions,
            "fhir.get_medications": get_medications, "wearables.read": wearables_read,
            "km.check_conflicts": check_conflicts, "km.ckd_stage": ckd_stage, "km.query": query}


def board_tools(create_item: Callable[[dict[str, Any]], str]) -> dict[str, Tool]:
    """`create_item(item)` puts a review item on the Clinical Board and returns its id."""

    async def board_create_item(**item: Any) -> str:
        return create_item(item)

    return {"board.create_item": board_create_item}
