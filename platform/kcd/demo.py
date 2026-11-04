"""The Kidney Care Desk golden path on kcd_core (deck slide 28), with the synthetic patient Marco R.

    python -m kcd.demo                                   # stub CT model, in-memory database
    python -m kcd.demo --db sqlite:///cortex.db --blobs ./blobs
    python -m kcd.demo --ct-model-url http://localhost:9000/classify --calibration cal.json
    python -m kcd.demo --ct-model-callable chrislin_model.serve:predict

Real seats: Dispatcher + escalation (Core), Companion (Meet), Renal Guardian + Safety Verifier + Knowledge Model
(Diana's rules), Imaging Reader + Genomics Interpreter + Archivist + Cortex (Naima). Case Briefer remains a scripted
stand-in for Chrislin. Every message is signed, routed and checked against the manifests and policy catalogue."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kcd_core.contracts.messages import DecisionItem

from kcd.cortex import Cortex, FileBlobStore, HumanActor, MemoryBlobStore
from kcd.cortex.auth import release_verifier
from kcd.desk import CLINICIAN_ID, Desk
from kcd.fhir import FolderFhir
from kcd.genomics.source import FolderPanelSource
from kcd.imaging.calibration import Calibration
from kcd.imaging.model import CallableCtModel, CtModel, FixedCtModel, HttpCtModel
from kcd.imaging.pacs import FolderPacs
from kcd.knowledge import KnowledgeModel
from kcd.patient.channel import PatientChannel
from kcd.seats.archivist import Archivist
from kcd.seats.companion import Companion, companion_tools
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from kcd.seats.imaging_reader import ImagingReader
from kcd.seats.renal_guardian import RenalGuardian
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.standins import StandInCaseBriefer
from kcd.tools import board_tools, clinical_tools, genomics_tools, imaging_tools

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marco-r"
PATIENT = "marco-r"
EPISODE = "ep-2026-10-12"
DEMO_NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)  # 21:40 in Italy, as on the deck's slide 28
# The v1 pipeline's cached result for Marco's CT, used only while Chrislin's model is not connected.
V1_CACHED = {"normal": 0.00004, "cyst": 0.00003, "stone": 0.00003, "tumor": 0.9999}
# What the patient's lab-report photo says after on-device OCR (fixtures/marco-r/lab-report-photo.txt).
LAB_PHOTO = {"egfr": 52, "creatinine": 1.41, "date": "2026-10-08", "source": "patient photo (on-device OCR)"}
HOME_BP = {"sbp": 148, "dbp": 91}


def _seed(root: Path) -> Path:
    d = root / PATIENT
    study = d / "ct-2026-10-11"
    study.mkdir(parents=True)
    shutil.copy(FIXTURES / "ct.png", study / "key.png")
    meta = json.loads((FIXTURES / "ct-study.json").read_text(encoding="utf-8"))
    (study / "study.json").write_text(json.dumps({
        "study_uid": meta["study_uid"], "modality": meta["modality"], "acquired": meta["acquired"],
        "description": meta["description"], "source": meta["source"], "key_image": "key.png"}), encoding="utf-8")
    shutil.copy(FIXTURES / "dna-panel.json", d / "panel.json")
    shutil.copy(FIXTURES / "fhir-bundle.json", d / "fhir-bundle.json")
    return root


def _say(step: str, obj: Any = None) -> None:
    print(f"\n== {step}")
    if obj is not None:
        print(obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def run(model: CtModel, calibration: Calibration | None, db: str, blobs: str | None) -> int:
    desk = await Desk.create(start=DEMO_NOW)
    km = KnowledgeModel.load()
    cortex = Cortex(db, blobs=FileBlobStore(blobs) if blobs else MemoryBlobStore(),
                    human_verifier=release_verifier(desk.clinician_keys), clock=desk.clock.now)
    with tempfile.TemporaryDirectory() as tmp:
        root = _seed(Path(tmp))
        fhir = FolderFhir(root)
        patient_channel = PatientChannel()
        companion = Companion(km, demo_lab_reports={PATIENT: LAB_PHOTO},
                              history_provider=lambda _patient: {"rcc": True})
        briefer = StandInCaseBriefer()
        reader = ImagingReader(cortex, calibration, audit=desk.audit, clock=desk.clock)
        verifier = SafetyVerifier(now=desk.clock.now)
        archivist = Archivist(cortex)
        rt = {
            "companion": await desk.add_seat(
                companion, companion_tools(km, patient_channel, wearables={PATIENT: HOME_BP})
            ),
            "case_briefer": await desk.add_seat(briefer),
            "renal_guardian": await desk.add_seat(RenalGuardian(), clinical_tools(fhir, km, desk.clock.now,
                                                                                  {PATIENT: HOME_BP})),
            "safety_verifier": await desk.add_seat(verifier, clinical_tools(fhir, km, desk.clock.now)),
            "imaging_reader": await desk.add_seat(reader, imaging_tools(FolderPacs(root), model)),
            "genomics_interpreter": await desk.add_seat(GenomicsInterpreter(cortex),
                                                        genomics_tools(FolderPanelSource(root), fhir)),
            "archivist": await desk.add_seat(archivist, board_tools(desk.gates.create_item)),
        }
        stub = " (STUB: v1 cached result, not a real model)" if isinstance(model, FixedCtModel) else ""
        print(f"Marco R. (synthetic), {DEMO_NOW:%Y-%m-%d %H:%M} UTC. CT model: {type(model).__name__}{stub}")

        # 1. Red flag at home: the Companion escalates, the Dispatcher pages the on-call uro-oncologist.
        flag = km.red_flags(symptoms=["visible_hematuria", "flank_pain"], pain_score=8, weight_loss_kg=23,
                            history_rcc=True, distress_level="high", distress_score=0.81)
        await companion.escalate(rt["companion"].context(), patient_id=PATIENT, episode_id=EPISODE, red_flag=flag)
        await desk.bus.drain()
        _say("1. Red flag -> escalation", {"urgency": flag["urgency"], "pattern": flag["pattern"],
                                           "paged": desk.pager.pages[0][0] if desk.pager.pages else None})

        # 2-3. Ibuprofen: stale eGFR -> ask for the lab photo -> conflict -> Safety Verifier.
        await companion.report_medication(rt["companion"].context(), patient_id=PATIENT, episode_id=EPISODE,
                                          text="I take ibuprofen 600 for the pain, three times a day")
        await desk.bus.drain()
        conflict = next(p for t, p in briefer.cases[PATIENT].received if t == "therapy_conflict")
        _say("2. Medication -> stale eGFR -> the Companion asks", companion.asked_patient)
        _say("3. Therapy conflict, verified by the Safety Verifier", {
            "drug": conflict["drug"], "severity": conflict["severity"], "conditions": conflict["conditions"],
            "checks": [f"{c['check']}: {'ok' if c['ok'] else 'FAIL'}" for c in verifier.results[-1]["checks"]]})

        # 4. CT reaches PACS -> Imaging Reader -> Safety Verifier.
        result = await reader.process_study(rt["imaging_reader"].context(), patient_id=PATIENT, episode_id=EPISODE)
        await desk.bus.drain()
        signal = next((p for t, p in briefer.cases[PATIENT].received if t == "imaging_signal"), None)
        _say("4. Imaging signal (to the clinicians only)", {
            "status": result.status, "verified": signal is not None,
            "label": signal and signal["label"], "p_raw": signal and signal["p_raw"],
            "p_calibrated": signal and signal["p_calibrated"], "quality": signal and signal["quality"]})

        # 5. The Case Briefer asks for the genomic context.
        await briefer.request_genomics(rt["case_briefer"].context(), patient_id=PATIENT, episode_id=EPISODE)
        await desk.bus.drain()
        genomic = next(p for t, p in briefer.cases[PATIENT].received
                       if t == "data_delivered" and p["kind"] == "genomic_context")
        _say("5. Genomic context, verified", {
            "findings": [f"{f['gene']} {f['lab_classification']} -> {f['relevance']}"
                         for f in genomic["data"]["findings"]],
            "implications": genomic["data"]["case_implications"]})

        # 6. Case pack -> Dispatcher opens the gate -> the clinician signs.
        pack = await briefer.publish_case_pack(rt["case_briefer"].context(), patient_id=PATIENT)
        await desk.bus.drain()
        _say("6. Case pack on the Board (gate opened by the Dispatcher)",
             {"case_id": pack.case_id, "gates": pack.gates, "sbar": pack.sbar.model_dump()})
        release = desk.release(pack.case_id, pack.artifact_hash, release_id="dec-1")
        decision = await briefer.publish_decision(rt["case_briefer"].context(), patient_id=PATIENT, release=release,
                                                  items=[DecisionItem(code="stop_nsaid", patient_text=(
                                                      "Please stop ibuprofen. Your doctor will suggest another "
                                                      "painkiller.")),
                                                         DecisionItem(code="urgent_visit", patient_text=(
                                                             "Please come to the hospital today for a check."))])
        await desk.bus.drain()
        _say("7. Signed decision -> what the Companion may tell Marco", companion.told_patient)

        # 8. Memory: trial rules -> clinician confirms -> next episode.
        items = [i for i in desk.gates.items if i["gate"] == "rule_confirmation"]
        for item in items:
            confirm = desk.release(item["artifact_id"], item["artifact_hash"], release_id=f"rel-{item['id']}")
            cortex.confirm(item["artifact_id"], HumanActor(CLINICIAN_ID, "clinician:nephrology", confirm))
        rules = archivist.rules_for_next_episode(rt["archivist"].context(), PATIENT)
        _say("8. Archivist: trial rules confirmed by the clinician -> next episode",
             [f"{r['key']} (layer {r['layer']}, confidence {r['confidence_effective']})" for r in rules])

        routed = [f"{env.type}: {env.from_} -> {env.to}" for subject, env in desk.bus.published
                  if subject != "a2a.dispatcher"]
        _say("Messages routed by the Dispatcher", routed)
        _say("Integrity", {"dead_letters": [d.error for d in desk.bus.dead_letters],
                           "policy_blocks": len(desk.audit.find("pep.tool", "blocked")),
                           "core_audit_chain_ok": desk.audit.verify_chain(),
                           "cortex_chain": cortex.verify_chain(3, PATIENT), "decision": decision.decision_id})
        erase = desk.release(f"erase:{PATIENT}", "sha256:" + "0" * 64, release_id="erase-1")
        receipt = cortex.erase_patient(PATIENT, HumanActor(CLINICIAN_ID, "clinician:nephrology", erase),
                                       "demo: right to erasure")
        _say("Erasure receipt (no patient id kept)", {k: receipt[k] for k in (
            "subject_hash", "evidence_rows", "knowledge_rows", "blobs_deleted", "complete")})
        ok = not desk.bus.dead_letters and desk.audit.verify_chain() and signal is not None and len(rules) == 2
    await desk.close()
    return 0 if ok else 1


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
