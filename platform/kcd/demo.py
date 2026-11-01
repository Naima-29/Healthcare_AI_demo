"""Run the Evidence + Database slice end to end on kcd_core, with the synthetic patient Marco R.

    python -m kcd.demo                                   # stub CT model, in-memory database
    python -m kcd.demo --db sqlite:///cortex.db --blobs ./blobs
    python -m kcd.demo --ct-model-url http://localhost:9000/classify --calibration cal.json
    python -m kcd.demo --ct-model-callable chrislin_model.serve:predict

A whole desk runs in memory (kcd.desk): manifests, policy catalogue, signed messages, Dispatcher.
Deck case 3 (CT result), the genomic context, then the learning loop (signed decision -> trial rule ->
clinician confirms with a signed release -> reused) and finally erasure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from kcd_core.audit import content_hash
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import DataDelivered, ImagingSignal

from kcd.cortex import Cortex, FileBlobStore, HumanActor, MemoryBlobStore
from kcd.cortex.auth import release_verifier
from kcd.desk import CLINICIAN_ID, Desk
from kcd.genomics.source import FolderPanelSource
from kcd.imaging.calibration import Calibration
from kcd.imaging.model import CallableCtModel, CtModel, FixedCtModel, HttpCtModel
from kcd.imaging.pacs import FolderPacs
from kcd.seats.archivist import Archivist
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from kcd.seats.imaging_reader import ImagingReader
from kcd.tools import board_tools, genomics_tools, imaging_tools

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marco-r"
PATIENT = "pt_marco"
EPISODE = "ep_2026_10_12"
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
    panel = json.loads((FIXTURES / "dna-panel.json").read_text(encoding="utf-8"))
    panel["patient_id"] = PATIENT
    (panels / PATIENT / "panel.json").write_text(json.dumps(panel, indent=2), encoding="utf-8")
    return pacs, panels


def _print(title: str, obj: Any) -> None:
    print(f"\n== {title}")
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def run(model: CtModel, calibration: Calibration | None, db: str, blobs: str | None) -> int:
    desk = await Desk.create()
    cortex = Cortex(db, blobs=FileBlobStore(blobs) if blobs else MemoryBlobStore(),
                    human_verifier=release_verifier(desk.clinician_keys))
    board: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        pacs_root, panel_root = _seed(Path(tmp))
        reader = ImagingReader(cortex, calibration, audit=desk.audit, clock=desk.clock)
        reader_rt = await desk.add_seat(reader, imaging_tools(FolderPacs(pacs_root), model))
        await desk.add_seat(GenomicsInterpreter(cortex), genomics_tools(
            FolderPanelSource(panel_root), patients={PATIENT: {"conditions": ["Clear cell renal cell carcinoma"]}}))
        archivist = Archivist(cortex)
        archivist_rt = await desk.add_seat(archivist, board_tools(lambda artifact_id: board[artifact_id]))

        stub = " (STUB: not a real model)" if isinstance(model, FixedCtModel) else ""
        print(f"CT model: {type(model).__name__}{stub}")

        # 1. PACS event -> Imaging Reader -> imaging_signal to the Safety Verifier
        result = await reader.process_study(reader_rt.context(), patient_id=PATIENT, episode_id=EPISODE, force=True)
        await desk.bus.drain()
        signal = ImagingSignal.model_validate(desk.delivered_to("safety_verifier")[-1].payload)
        _print("1. Imaging Reader -> imaging_signal (to the Safety Verifier)", signal.model_dump(mode="json"))
        _print("   details kept in Cortex", {k: result.details[k] for k in (
            "probabilities", "calibration", "cam_metrics")})

        # 2. Case Briefer asks for the genomic context
        await desk.deliver(desk.envelope(MessageType.DATA_REQUEST, "case_briefer", "genomics_interpreter", {
            "items": [{"kind": "genomics", "code": "genomic_context"}], "reason": "case pack"},
            patient=PATIENT, episode=EPISODE))
        delivered = DataDelivered.model_validate(desk.delivered_to("safety_verifier")[-1].payload)
        summary, *findings = delivered.items
        _print("2. Genomics Interpreter -> data_delivered{genomic_context}", {
            "findings": [f"{f['gene']} {f['lab_classification']} -> {f['relevance']}" for f in findings],
            "not_reported": summary["not_reported"], "case_implications": summary["case_implications"]})

        # 3. The clinician signs a case pack on the Board; the Case Briefer sends the decision
        artifact = {"case_id": "case-2026-10-12",
                    "items": [{"code": "stop_nsaid", "patient_text": "Please stop ibuprofen; your doctor will call."}]}
        h = content_hash(artifact)
        board["cp_1"] = {"artifact": artifact, "release": desk.release("cp_1", h)}
        await desk.deliver(desk.envelope(MessageType.CLINICIAN_DECISION, "case_briefer", "archivist", {
            "artifact_id": "cp_1", "artifact_hash": h, "release_id": "rel_1", "decision": "approved",
            "clinician_role": "clinician:nephrology"}, patient=PATIENT, episode=EPISODE))
        pack = desk.gates.opened[-1][1]
        _print("3. Archivist -> trial rule + case_pack_ready (the Dispatcher opened the gate)",
               {"gate": pack.model_dump(mode="json"), "rule": cortex.get_knowledge(pack.artifact_id)})
        ctx = archivist_rt.context()
        _print("   active rules before confirmation", archivist.rules_for_next_episode(ctx, PATIENT))

        # 4. The clinician confirms the rule with a signed release for that rule
        release = desk.release(pack.artifact_id, pack.artifact_hash, release_id="rel_rule_1")
        cortex.confirm(pack.artifact_id, HumanActor(CLINICIAN_ID, "clinician:nephrology", release))
        _print("4. Clinician confirms -> rules for the next episode",
               [f"{k['key']} (confidence {k['confidence_effective']})"
                for k in archivist.rules_for_next_episode(ctx, PATIENT)])

        _print("5. Messages routed by the Dispatcher",
               [f"{env.type}: {env.from_} -> {env.to}" for subject, env in desk.bus.published
                if subject != "a2a.dispatcher"])
        _print("   dead letters", [d.error for d in desk.bus.dead_letters])
        _print("6. Evidence chain for the patient", cortex.verify_chain(3, PATIENT))
        _print("   Core audit chain intact", desk.audit.verify_chain())

        erase = desk.release(f"erase:{PATIENT}", "sha256:" + "0" * 64, role="clinician:nephrology",
                             release_id="rel_erase")
        receipt = cortex.erase_patient(PATIENT, HumanActor(CLINICIAN_ID, "clinician:nephrology", erase),
                                       "demo: right to erasure")
        _print("7. Erasure receipt (no patient id kept)", {k: receipt[k] for k in (
            "subject_hash", "evidence_rows", "knowledge_rows", "blobs_deleted", "complete")})
    await desk.close()
    return 0


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

    model: CtModel
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
    return asyncio.run(run(model, calibration, a.db, a.blobs))


if __name__ == "__main__":
    sys.exit(main())
