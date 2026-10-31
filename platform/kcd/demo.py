"""Run the Evidence + Database slice end to end on the synthetic patient Marco R.

    python -m kcd.demo                                   # stub CT model, in-memory database
    python -m kcd.demo --db sqlite:///cortex.db --blobs ./blobs
    python -m kcd.demo --ct-model-url http://localhost:9000/classify --calibration cal.json
    python -m kcd.demo --ct-model-callable chrislin_model.serve:predict

Deck case 3 (CT result), then the learning loop (decision -> trial rule -> clinician confirms ->
reused) and finally erasure. Messages and gates are printed instead of going to Core's bus and
the Clinical Board.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from kcd.contracts import Envelope
from kcd.cortex import Cortex, FileBlobStore, HumanActor, MemoryBlobStore
from kcd.genomics.source import FolderPanelSource
from kcd.imaging.calibration import Calibration
from kcd.imaging.model import CallableCtModel, FixedCtModel, HttpCtModel
from kcd.imaging.pacs import FolderPacs
from kcd.seatkit import InMemoryGates, InMemoryOutbox
from kcd.seats.archivist import Archivist
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from kcd.seats.imaging_reader import ImagingReader

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marco-r"
PATIENT = "marco-r"
# The v1 pipeline's cached result for Marco's CT, used only while Chrislin's model is not connected.
V1_CACHED = {"normal": 0.00004, "cyst": 0.00003, "stone": 0.00003, "tumor": 0.9999}


def _seed(root: Path) -> tuple[Path, Path]:
    pacs, panels = root / "pacs", root / "panels"
    study = pacs / PATIENT / "ct-2026-10-11"
    study.mkdir(parents=True)
    shutil.copy(FIXTURES / "ct.png", study / "key.png")
    meta = json.loads((FIXTURES / "ct-study.json").read_text(encoding="utf-8"))
    (study / "study.json").write_text(json.dumps({
        "study_uid": meta["study_uid"], "modality": meta["modality"], "acquired": meta["acquired"],
        "description": meta["description"], "source": meta["source"], "key_image": "key.png"}), encoding="utf-8")
    (panels / PATIENT).mkdir(parents=True)
    shutil.copy(FIXTURES / "dna-panel.json", panels / PATIENT / "panel.json")
    return pacs, panels


def _print(title: str, obj) -> None:
    print(f"\n== {title}")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="kcd.demo")
    ap.add_argument("--db", default="sqlite://", help="SQLAlchemy URL for Cortex (default: in memory)")
    ap.add_argument("--blobs", help="folder for evidence blobs (default: in memory)")
    ap.add_argument("--ct-model-url", help="Chrislin's CT model service (contract C4)")
    ap.add_argument("--ct-model-callable", help="Chrislin's CT model as package.module:function")
    ap.add_argument("--calibration", help="calibration JSON fitted for that model version")
    a = ap.parse_args(argv)

    if a.ct_model_url:
        model = HttpCtModel(a.ct_model_url)
    elif a.ct_model_callable:
        model = CallableCtModel(a.ct_model_callable)
    else:
        model = FixedCtModel(V1_CACHED, model_version="stub-v1-cached")
    if a.calibration:
        calibration = Calibration.load(a.calibration)
    elif isinstance(model, FixedCtModel):
        calibration = Calibration(2.5, model.model_version)  # placeholder T from the demo, for the stub only
    else:
        calibration = None

    cortex = Cortex(a.db, blobs=FileBlobStore(a.blobs) if a.blobs else MemoryBlobStore())
    outbox, gates = InMemoryOutbox(), InMemoryGates()
    with tempfile.TemporaryDirectory() as tmp:
        pacs_root, panel_root = _seed(Path(tmp))
        reader = ImagingReader(cortex, outbox, gates, FolderPacs(pacs_root), model, calibration)
        genomics = GenomicsInterpreter(cortex, outbox, gates, FolderPanelSource(panel_root))
        archivist = Archivist(cortex, outbox, gates)

        print(f"CT model: {type(model).__name__}" + (" (STUB: not a real model)" if isinstance(model, FixedCtModel) else ""))
        img = reader.process_study(PATIENT, force=True)
        p = img["payload"]
        _print("1. Imaging Reader -> imaging_signal (to Dispatcher, for Safety Verifier + Case Briefer)", {
            k: p[k] for k in ("status", "label", "p_raw", "p_calibrated", "probabilities", "cam_metrics",
                              "calibration", "heatmap_ref", "note")})
        _print("   quality checks", [f"{c['name']}: {'ok' if c['ok'] else 'FAIL'} ({c['value']})"
                                     for c in p["quality"]["checks"]])

        gx = genomics.interpret(PATIENT, context={"history": {"rcc": True}})
        _print("2. Genomics Interpreter -> data_delivered{genomic_context}", {
            "findings": [f"{f['gene']} {f['lab_classification']} -> {f['relevance']}" for f in gx["data"]["findings"]],
            "not_reported": gx["data"]["not_reported"], "case_implications": gx["data"]["case_implications"]})

        decision = Envelope(type="clinician_decision", sender="case_briefer", to="archivist", patient=PATIENT, payload={
            "case_id": "case-2026-10-12", "signed_by": "dr-bianchi", "role": "nephrologist",
            "items": [{"code": "stop_nsaid", "patient_text": "Please stop ibuprofen; your doctor will call you."}]})
        arch = archivist.handle(decision)
        rule_id = arch["proposals"][0]
        _print("3. Archivist -> trial rule (shadow) + rule_confirmation gate", cortex.get_knowledge(rule_id))
        _print("   active rules before confirmation", archivist.rules_for_next_episode(PATIENT))
        cortex.confirm(rule_id, HumanActor("dr-bianchi", "nephrologist"))
        _print("4. Clinician confirms on the Board -> rules for the next episode",
               [f"{k['key']} (confidence {k['confidence_effective']})" for k in archivist.rules_for_next_episode(PATIENT)])

        _print("5. Messages sent (would go to Core's A2A bus)",
               [f"{e.type} -> {e.to} route={e.route_hint}" for e in outbox.sent])
        _print("   Gates opened (would go to the Clinical Board)", [f"{g['gate']}: {g['title']}" for g in gates.opened])
        _print("6. Evidence chain for the patient", cortex.verify_chain(3, PATIENT))

        receipt = cortex.erase_patient(PATIENT, HumanActor("dpo-1", "dpo"), "demo: right to erasure")
        _print("7. Erasure receipt (no patient id kept)", {k: receipt[k] for k in (
            "subject_hash", "evidence_rows", "knowledge_rows", "blobs_deleted", "complete")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
