"""Panel sources for the Genomics Interpreter (surface "genomics"). Read-only.

FolderPanelSource layout: <root>/<patient_id>/panel.json or <root>/<patient_id>/*.vcf.
A FHIR (MolecularSequence / Observation) source can replace it behind the same method.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from kcd.genomics.panel import Panel, PanelError, parse_panel_json, parse_vcf


class PanelSource(Protocol):
    def get_panel(self, patient_id: str) -> tuple[bytes, Panel]: ...


class NoPanel(LookupError):
    pass


class FolderPanelSource:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def get_panel(self, patient_id: str) -> tuple[bytes, Panel]:
        d = (self.root / patient_id).resolve()
        if d.parent != self.root:
            raise PanelError(f"invalid patient id {patient_id!r}")
        candidates = sorted(d.glob("*.json")) + sorted(d.glob("*.vcf")) if d.is_dir() else []
        if not candidates:
            raise NoPanel(f"no genetic panel on file for patient {patient_id}")
        panels = []
        for path in candidates:
            raw = path.read_bytes()
            if path.suffix == ".json":
                try:
                    panel = parse_panel_json(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise PanelError(f"{path.name}: not valid JSON") from e
            else:
                panel = parse_vcf(raw.decode("utf-8"), patient_id)
            panels.append((raw, panel))
        panels.sort(key=lambda rp: rp[1].date, reverse=True)  # most recent panel first
        return panels[0]
