"""Every edge case for one synthetic patient, Sofia B., through the whole desk on kcd_core.

    python -m kcd.scenarios.edge_cases                       # ~5 s, deterministic (scripted CT model)
    python -m kcd.scenarios.edge_cases --with-real-ct-model  # + Chrislin's model on her 4 real CT slices

Sofia B. (synthetic, fixtures/sofia-b): 57, clear-cell RCC with a VHL variant, CKD stage 3, hypertension,
on sunitinib. Her data is built for edge cases: 11 CT studies (normal, cyst, stone, tumor, unsure model,
poor image, colour screenshot, corrupt file, model down, model updated without calibration, MR), a DNA
panel with every kind of variant class, an older VCF panel, a misfiled panel of another patient, a stale
kidney result, and two lab photos (one read correctly, one with an OCR error).

Each check prints the expected and the actual behaviour. Exit code 0 only when every check passes.
A markdown copy of the report goes to platform/reports/edge-cases.md.

Real seats: Dispatcher + escalation (Core), Renal Guardian, Safety Verifier, Knowledge Model, Imaging Reader,
Genomics Interpreter, Archivist, Cortex. Stand-ins (scripted): Companion, Case Briefer, the clinician.
The scripted CT model answers per study from ct-scripted-outputs.json, so the run is the same every time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from kcd_core.bus import DISPATCHER_INBOX, seat_inbox
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import DecisionItem
from kcd_core.pep import PolicyViolation
from sqlalchemy import update

from kcd.cortex import Cortex, HumanActor, HumanRequired, schema
from kcd.cortex.auth import release_verifier
from kcd.desk import CLINICIAN_ID, Desk
from kcd.fhir import FolderFhir
from kcd.genomics.source import FolderPanelSource
from kcd.imaging.calibration import Calibration
from kcd.imaging.image_io import LoadedImage
from kcd.imaging.model import CtModel, CtModelError, CtPrediction, prediction_from_dict
from kcd.imaging.pacs import FolderPacs
from kcd.knowledge import KnowledgeModel
from kcd.seats.archivist import Archivist
from kcd.seats.case_briefer import CaseBriefer
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from kcd.seats.imaging_reader import ImagingReader, NoStudy
from kcd.seats.renal_guardian import RenalGuardian
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.standins import StandInCompanion
from kcd.tools import board_tools, clinical_tools, genomics_tools, imaging_tools
from kcd.util import sha256_hex

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PATIENT = "sofia-b"
NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
SCRIPTED_VERSION = "stub-scripted-edge-v1"
REPORTS = Path(__file__).resolve().parents[2] / "reports"
REAL_IMAGES = Path(__file__).resolve().parents[3] / "models" / "kidney_project" / "Test Images"


@dataclass
class Check:
    area: str
    case: str
    expected: str
    actual: str
    ok: bool


class ScriptedCtModel:
    """Answers per key image from ct-scripted-outputs.json. A stub: its model_version says so."""

    def __init__(self, patient_dir: Path):
        table = json.loads((patient_dir / "ct-scripted-outputs.json").read_text(encoding="utf-8"))
        self.by_image = {sha256_hex((patient_dir / f"ct-{a['folder']}" / "key.png").read_bytes()): a
                         for a in table.values()}
        self.model_version = SCRIPTED_VERSION

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        answer = self.by_image.get(sha256_hex(image_bytes))
        if answer is None:
            raise CtModelError("the scripted model has no answer for this image")
        if "raise" in answer:
            raise CtModelError(answer["raise"])
        ys, xs = np.mgrid[0:32, 0:32] / 31.0
        heat = np.exp(-(((xs - 0.4) ** 2) + ((ys - 0.55) ** 2)) / 0.01)
        return prediction_from_dict({"model_version": answer.get("model_version", self.model_version),
                                     "labels": list(answer["probs"]), "probs": list(answer["probs"].values()),
                                     "heatmap": heat.tolist()})


class EdgeCases:
    def __init__(self, root: Path, model: CtModel | None = None, calibration: Calibration | None = None):
        self.root = root
        self.pdir = root / PATIENT
        self.checks: list[Check] = []
        self.model = model or ScriptedCtModel(self.pdir)
        self.calibration = calibration or Calibration(1.5, SCRIPTED_VERSION)
        self.lab = {n: json.loads((self.pdir / n).read_text(encoding="utf-8"))
                    for n in ("lab-photo-ok.json", "lab-photo-ocr-error.json")}

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        self.desk = await Desk.create(start=NOW)
        self.km = KnowledgeModel.load()
        self.cortex = Cortex(human_verifier=release_verifier(self.desk.clinician_keys), clock=self.desk.clock.now)
        fhir = FolderFhir(self.root)
        self.companion = StandInCompanion(self.km, lab_reports={PATIENT: self.lab["lab-photo-ok.json"]})
        self.briefer = CaseBriefer()
        self.reader = ImagingReader(self.cortex, self.calibration, audit=self.desk.audit, clock=self.desk.clock)
        self.verifier = SafetyVerifier(now=self.desk.clock.now)
        self.archivist = Archivist(self.cortex)
        d = self.desk
        self.rt = {
            "companion": await d.add_seat(self.companion),
            "case_briefer": await d.add_seat(self.briefer),
            "renal_guardian": await d.add_seat(RenalGuardian(), clinical_tools(fhir, self.km, d.clock.now,
                                                                               {PATIENT: {"sbp": 152, "dbp": 94}})),
            "safety_verifier": await d.add_seat(self.verifier, clinical_tools(fhir, self.km, d.clock.now)),
            "imaging_reader": await d.add_seat(self.reader, imaging_tools(FolderPacs(self.root), self.model),
                                               timeout_s=120),
            "genomics_interpreter": await d.add_seat(GenomicsInterpreter(self.cortex),
                                                     genomics_tools(FolderPanelSource(self.root), fhir)),
            "archivist": await d.add_seat(self.archivist, board_tools(d.gates.create_item)),
        }

    def ctx(self, seat: str):  # noqa: ANN201 - kcd_core.runtime.SeatContext
        return self.rt[seat].context()

    def check(self, area: str, case: str, expected: str, actual: Any, ok: bool) -> None:
        self.checks.append(Check(area, case, expected, str(actual), bool(ok)))

    def sent(self, sender: str, type_: MessageType | None = None) -> list:
        return [e for s, e in self.desk.bus.published if s == DISPATCHER_INBOX and e.from_ == sender
                and (type_ is None or e.type == type_)]

    def received(self, type_: str) -> list[dict]:
        case = self.briefer.cases.get(PATIENT)
        return [p for t, p in case.received if t == type_] if case else []

    def rejected_by_verifier(self, to: str) -> list:
        return [e for e in self.sent("safety_verifier", MessageType.ACKNOWLEDGMENT) if e.to == to]

    # ------------------------------------------------------------------ A. red flags
    async def red_flags(self) -> None:
        area = "A. Red flags"
        high = self.km.red_flags(symptoms=["visible_hematuria", "flank_pain"], pain_score=8, weight_loss_kg=6,
                                 history_rcc=True, distress_level="high", distress_score=0.82)
        await self.companion.escalate(self.ctx("companion"), patient_id=PATIENT, episode_id="ep-a1", red_flag=high)
        await self.desk.bus.drain()
        paged = [t for t, _ in self.desk.pager.pages]
        self.check(area, "A1 blood in urine + flank pain 8/10 + RCC history",
                   "urgency high, safety script sent, on-call doctor paged",
                   f"urgency {high['urgency']}, paged {paged}", high["urgency"] == "high" and paged == ["dr_rossi"])

        medium = self.km.red_flags(symptoms=["visible_hematuria"])
        await self.companion.report_symptoms(
            self.ctx("companion"), patient_id=PATIENT, episode_id="ep-a2", distress=0.35,
            transcript="Ho visto un po' di sangue nelle urine stamattina, ma non ho dolore.",
            symptoms=[{"code": "visible_hematuria", "label": "visible blood in urine", "severity": 3}])
        await self.desk.bus.drain()
        self.check(area, "A2 blood in urine only, no pain", "urgency medium: reported to the care team, nobody paged",
                   f"urgency {medium['urgency']}, symptom reports {len(self.received('symptom_report'))}, "
                   f"pages {len(self.desk.pager.pages)}",
                   medium["urgency"] == "medium" and len(self.received("symptom_report")) == 1
                   and len(self.desk.pager.pages) == 1)

        none = self.km.red_flags(symptoms=["fatigue"])
        self.check(area, "A3 'a bit tired today'", "no red flag, nothing escalated", f"red flag: {none}", none is None)

    # ------------------------------------------------------------------ B. medications
    async def medications(self) -> None:
        area = "B. Medications"
        await self.companion.report_medication(self.ctx("companion"), patient_id=PATIENT, episode_id="ep-b1",
                                               text="Prendo Brufen 600 tre volte al giorno per il mal di schiena")
        await self.desk.bus.drain()
        asked = self.sent("renal_guardian", MessageType.DATA_REQUEST)
        conflict = self.received("therapy_conflict")
        self.check(area, "B1 Italian brand 'Brufen', eGFR on record 165 days old",
                   "recognised as NSAID; asks the patient for a recent lab photo before concluding",
                   f"{len(asked)} data request: {asked[0].payload['why'] if asked else '-'}",
                   len(asked) == 1 and "165 days" in asked[0].payload["why"])
        self.check(area, "B1 ...photo: eGFR 49 (3 days old)",
                   "conflict high (CKD G3a, hypertension, TKI), passes the Safety Verifier, reaches the case pack",
                   f"{conflict[0]['severity']}: {conflict[0]['conditions']}" if conflict else "no conflict",
                   len(conflict) == 1 and conflict[0]["severity"] == "high" and len(conflict[0]["conditions"]) == 3)

        await self.companion.report_medication(self.ctx("companion"), patient_id=PATIENT, episode_id="ep-b2",
                                               text="Tachipirina 1000 per il mal di testa")
        await self.desk.bus.drain()
        self.check(area, "B2 paracetamol ('Tachipirina')", "no drug-kidney conflict, nothing sent to the doctors",
                   f"conflicts in case pack: {len(self.received('therapy_conflict'))}",
                   len(self.received("therapy_conflict")) == 1)

        self.companion.lab_reports[PATIENT] = self.lab["lab-photo-ocr-error.json"]
        await self.companion.report_medication(self.ctx("companion"), patient_id=PATIENT, episode_id="ep-b3",
                                               text="ibuprofen 400 for back pain")
        await self.desk.bus.drain()
        rejected = self.rejected_by_verifier("renal_guardian")
        self.check(area, "B3 lab photo misread by OCR (eGFR 400)",
                   "Safety Verifier blocks it (implausible eGFR) and tells the Renal Guardian; doctors never see it",
                   rejected[0].payload["reason"] if rejected else "not blocked",
                   len(rejected) == 1 and "eGFR plausible" in rejected[0].payload["reason"]
                   and len(self.received("therapy_conflict")) == 1)
        self.companion.lab_reports[PATIENT] = self.lab["lab-photo-ok.json"]

        plural = self.km.classify_drug("I take NSAIDs sometimes")
        self.check(area, "B4 'NSAIDs' (plural)", "KNOWN GAP: not recognised as a drug (raised with Diana)",
                   f"classified as {plural}", plural is None)

    # ------------------------------------------------------------------ C. imaging
    async def imaging(self) -> None:
        area = "C. CT imaging"
        studies = {json.loads(p.read_text(encoding="utf-8"))["study_uid"]: p.parent.name
                   for p in sorted(self.pdir.glob("ct-*/study.json"))}
        uid = {folder: u for u, folder in studies.items()}
        results: dict[str, Any] = {}
        for folder in ("ct-01-normal", "ct-02-cyst", "ct-03-stone", "ct-04-tumor", "ct-05-uncertain",
                       "ct-06-small-low-contrast", "ct-07-colour-screenshot", "ct-08-corrupt", "ct-09-model-down",
                       "ct-10-other-model-version"):
            results[folder] = await self.reader.process_study(self.ctx("imaging_reader"), patient_id=PATIENT,
                                                              episode_id=f"ep-{folder}", study_uid=uid[folder])
            await self.desk.bus.drain()
        verified = {s["provenance"]["study_uid"]: s for s in self.received("imaging_signal")}
        rejected = {e.payload["ack_of"]: e.payload["reason"] for e in self.rejected_by_verifier("imaging_reader")}

        def verified_as(folder: str) -> dict | None:
            return verified.get(uid[folder])

        for folder, label in (("ct-01-normal", "normal"), ("ct-02-cyst", "cyst"), ("ct-03-stone", "stone"),
                              ("ct-04-tumor", "tumor")):
            s = verified_as(folder)
            self.check(area, f"C{folder[3:5]} {label} study",
                       f"signal '{label}', calibrated, verified, in the case pack",
                       f"{s['label']} p_raw={s['p_raw']} p_cal={s['p_calibrated']}" if s else results[folder].status,
                       bool(s) and s["label"] == label and s["p_calibrated"] < s["p_raw"])
        r = results["ct-05-uncertain"]
        self.check(area, "C05 model unsure (30/26/22/22 %)",
                   "flagged out of distribution; Safety Verifier keeps it out of the case pack",
                   f"status {r.status}; verifier: {rejected.get(r.message_id, 'passed')}",
                   r.status == "out_of_distribution" and r.message_id in rejected
                   and "in distribution" in rejected[r.message_id])
        for folder, issue in (("ct-06-small-low-contrast", "min_side_px"), ("ct-07-colour-screenshot", "grayscale")):
            s = verified_as(folder)
            q = s["quality"] if s else {}
            self.check(area, f"C{folder[3:5]} {folder[6:].replace('-', ' ')}",
                       f"quality 'limited' with issue '{issue}', shown to the radiologist",
                       f"quality {q.get('quality')}, issues {q.get('issues')}" if s else results[folder].status,
                       bool(s) and q["quality"] == "limited" and issue in q["issues"])
        for folder, status, why in (("ct-08-corrupt", "unreadable", "cannot decode"),
                                    ("ct-09-model-down", "model_error", "unavailable")):
            r = results[folder]
            errors = [e for e in self.desk.audit.find("imaging.no_signal", "error")
                      if e.detail["study_uid"] == uid[folder]]
            self.check(area, f"C{folder[3:5]} {folder[6:].replace('-', ' ')}",
                       f"'{status}': nothing sent, recorded as evidence + audit error for the radiologist's queue",
                       f"status {r.status}, sent {r.sent}, audit errors {len(errors)}",
                       r.status == status and not r.sent and len(errors) == 1 and why in r.details["error"])
        r = results["ct-10-other-model-version"]
        self.check(area, "C10 model updated, calibration not refitted",
                   "calibration marked stale (raw confidence); Safety Verifier rejects the uncalibrated signal",
                   f"calibration {r.details['calibration']['status']}; verifier: "
                   f"{rejected.get(r.message_id, 'passed')}",
                   r.details["calibration"]["status"] == "stale" and r.message_id in rejected
                   and "calibrated" in rejected[r.message_id])
        try:
            await self.reader.process_study(self.ctx("imaging_reader"), patient_id=PATIENT, episode_id="ep-c11",
                                            study_uid=uid["ct-11-mr-not-ct"])
            actual, ok = "processed", False
        except NoStudy as e:
            actual, ok = f"refused: {e}", True
        self.check(area, "C11 MR study", "not read: the Imaging Reader reads CT only", actual, ok)
        again = await self.reader.process_study(self.ctx("imaging_reader"), patient_id=PATIENT, episode_id="ep-c12",
                                                study_uid=uid["ct-01-normal"])
        await self.desk.bus.drain()
        same_study = [s for s in self.received("imaging_signal") if s["provenance"]["study_uid"] == uid["ct-01-normal"]]
        self.check(area, "C12 same study arrives twice", "processed once, no second signal",
                   f"duplicate {again.duplicate}, signals for this study {len(same_study)}",
                   again.duplicate and len(same_study) == 1)
        key_ev_id = results["ct-04-tumor"].details["provenance"]["key_image_evidence_id"]
        key_ev = self.cortex.get_evidence(3, PATIENT, key_ev_id)
        original = (self.pdir / "ct-04-tumor" / "key.png").read_bytes()
        self.check(area, "C13 original image", "stored unaltered (same sha256), never written back to PACS",
                   f"stored sha256 matches: {key_ev['content']['sha256'] == sha256_hex(original)}",
                   self.cortex.read_blob(3, PATIENT, key_ev["blob_sha256"]) == original)

    # ------------------------------------------------------------------ D. genomics
    async def genomics(self) -> None:
        area = "D. Genomics"
        await self.briefer.request_genomics(self.ctx("case_briefer"), patient_id=PATIENT, episode_id="ep-d1")
        await self.desk.bus.drain()
        delivered = [p for p in self.received("data_delivered") if p["kind"] == "genomic_context"]
        data = delivered[0]["data"] if delivered else {}
        rel = {f["gene"]: f["relevance"] for f in data.get("findings", [])}
        self.check(area, "D1 two panels on file (2026 JSON, 2023 VCF)", "the most recent panel is used",
                   data.get("panel_id"), data.get("panel_id") == "GEN-SB-2026-0042")
        expected = {"VHL": "high", "FH": "high", "APOL1": "moderate", "TP53": "review", "MET": "unknown",
                    "PBRM1": "none"}
        for gene, what in (("VHL", "pathogenic -> high (hereditary RCC)"),
                           ("FH", "'Likely_pathogenic' (ClinVar spelling) -> high"),
                           ("APOL1", "homozygous risk allele = 2 alleles -> moderate"),
                           ("TP53", "pathogenic, no gene rule -> geneticist review"),
                           ("MET", "'Conflicting interpretations' -> unknown, never guessed"),
                           ("PBRM1", "VUS -> not actionable")):
            self.check(area, f"D2 {gene}", what, rel.get(gene), rel.get(gene) == expected[gene])
        not_reported = [n["gene"] for n in data.get("not_reported", [])]
        self.check(area, "D3 BAP1 benign", "left out of the report", f"not reported: {not_reported}",
                   not_reported == ["BAP1"])
        labs = {f["gene"]: f["lab_classification"] for f in data.get("findings", [])}
        self.check(area, "D4 laboratory classification", "copied verbatim, never reclassified",
                   f"FH '{labs.get('FH')}', MET '{labs.get('MET')}'",
                   labs.get("FH") == "Likely_pathogenic"
                   and labs.get("MET") == "Conflicting interpretations of pathogenicity")
        self.check(area, "D5 RCC history + VHL", "kidney-sparing note; geneticist review required",
                   f"{data.get('case_implications', [])[:1]}, review {data.get('needs_geneticist_review')}",
                   any("kidney-sparing" in t for t in data.get("case_implications", []))
                   and data.get("needs_geneticist_review") is True)

        misfiled = self.pdir / "panel-2026-09-misfiled.json"
        shutil.copy(self.pdir / "inbox" / "misfiled-panel.json", misfiled)
        before = len(self.cortex.list_evidence(3, PATIENT, "genomic_panel"))
        await self.briefer.request_genomics(self.ctx("case_briefer"), patient_id=PATIENT, episode_id="ep-d6")
        await self.desk.bus.drain()
        blocked = [e for e in self.desk.audit.find("seat.policy_violation", "blocked")
                   if "different patient" in e.detail.get("error", "")]
        self.check(area, "D6 newest panel on file belongs to another patient (misfiled)",
                   "blocked: never reaches the case pack, nothing stored",
                   f"blocked {len(blocked)}, genomic deliveries {len(delivered)} -> "
                   f"{len([p for p in self.received('data_delivered') if p['kind'] == 'genomic_context'])}",
                   len(blocked) == 1 and len(self.cortex.list_evidence(3, PATIENT, "genomic_panel")) == before)
        misfiled.unlink()

    # ------------------------------------------------------------------ E. doctor's decision
    async def decision(self) -> None:
        area = "E. Doctor's decision"
        pack1 = await self.briefer.publish_case_pack(self.ctx("case_briefer"), patient_id=PATIENT)
        await self.desk.bus.drain()
        types = [t for t, _ in self.briefer.cases[PATIENT].received]
        self.check(area, "E1 case pack",
                   "only verified items; gates for clinician, radiologist, nephrologist, geneticist",
                   f"{len(types)} items {sorted(set(types))}; gates {pack1.gates}",
                   set(pack1.gates) == {"clinician_signoff", "radiologist_review", "nephrologist_review",
                                        "genetic_disclosure"} and types.count("imaging_signal") == 6)
        old_release = self.desk.release(pack1.case_id, pack1.artifact_hash, release_id="dec-old")
        pack2 = await self.briefer.publish_case_pack(self.ctx("case_briefer"), patient_id=PATIENT)
        await self.desk.bus.drain()
        items = [DecisionItem(code="stop_nsaid", patient_text=(
                     "Per favore smetta il Brufen: il medico le indicherà un altro antidolorifico.")),
                 DecisionItem(code="urgent_visit", patient_text="Venga in ospedale oggi per un controllo.")]
        try:
            await self.briefer.publish_decision(self.ctx("case_briefer"), patient_id=PATIENT, release=old_release,
                                                items=items)
            actual, ok = "published", False
        except PolicyViolation as e:
            actual, ok = f"refused: {e.reason}", True
        self.check(area, "E2 doctor signed an older version of the case pack", "decision not published", actual, ok)

        release = self.desk.release(pack2.case_id, pack2.artifact_hash, release_id="dec-1")
        await self.briefer.publish_decision(self.ctx("case_briefer"), patient_id=PATIENT, release=release, items=items)
        await self.desk.bus.drain()
        told = [t["text"] for t in self.companion.told_patient]
        self.check(area, "E3 doctor signs the current case pack",
                   "the patient hears exactly the signed sentences; the Archivist proposes trial rules",
                   f"told patient {len(told)}; board items {len(self.desk.gates.items)}",
                   told == [i.patient_text for i in items] and len(self.desk.gates.items) == 2)

        forged = self.desk.envelope(MessageType.CLINICIAN_DECISION, "case_briefer", "archivist", {
            "case_id": pack2.case_id, "decision_id": "dec-forged", "artifact_hash": pack2.artifact_hash,
            "signed_by": "seat:case_briefer", "role": "treating_clinician",
            "items": [{"code": "stop_nsaid", "patient_text": "x"}]}, patient=PATIENT, episode="ep-e4")
        before = len(self.cortex.list_evidence(3, PATIENT, "clinician_decision"))
        await self.desk.deliver(forged)
        self.check(area, "E4 decision 'signed' by a seat, not a person", "Archivist refuses it, stores nothing",
                   f"decisions stored {before} -> {len(self.cortex.list_evidence(3, PATIENT, 'clinician_decision'))}",
                   len(self.cortex.list_evidence(3, PATIENT, "clinician_decision")) == before)

        env = next(e for s, e in self.desk.bus.published
                   if s == seat_inbox("archivist") and e.type == MessageType.CLINICIAN_DECISION
                   and e.payload["decision_id"] == "dec-1")
        await self.rt["archivist"]._on_message(env)
        self.check(area, "E5 the same decision delivered twice", "stored once, rules proposed once",
                   f"trial rules {len(self.cortex.list_knowledge(3, PATIENT))}, "
                   f"board items {len(self.desk.gates.items)}",
                   len(self.cortex.list_knowledge(3, PATIENT)) == 1 and len(self.desk.gates.items) == 2)

    # ------------------------------------------------------------------ F. memory
    async def memory(self) -> None:
        area = "F. Memory (Cortex)"
        ctx = self.ctx("archivist")
        self.check(area, "F1 new rules", "trial (shadow): not used until a doctor confirms",
                   f"active rules {len(self.archivist.rules_for_next_episode(ctx, PATIENT))}",
                   self.archivist.rules_for_next_episode(ctx, PATIENT) == [])
        try:
            ctx.act("cortex.confirm_rule")
            actual, ok = "allowed", False
        except PolicyViolation as e:
            actual, ok = f"refused ({e.refusal})", e.refusal == "auto_confirm_rule"
        self.check(area, "F2 the Archivist tries to confirm its own rule", "refused", actual, ok)
        items = {i["data"]["key"]: i for i in self.desk.gates.items}
        nsaid, routing = items["avoid_drug_class:NSAID"], items["routing:rcc_red_flag"]
        wrong = self.desk.release(routing["artifact_id"], routing["artifact_hash"], release_id="rel-wrong")
        try:
            self.cortex.confirm(nsaid["artifact_id"], HumanActor(CLINICIAN_ID, "clinician:nephrology", wrong))
            actual, ok = "confirmed", False
        except HumanRequired:
            actual, ok = "refused", True
        self.check(area, "F3 confirmation signed for a different rule", "refused", actual, ok)
        for item in (nsaid, routing):
            rel = self.desk.release(item["artifact_id"], item["artifact_hash"], release_id=f"rel-{item['id']}")
            self.cortex.confirm(item["artifact_id"], HumanActor(CLINICIAN_ID, "clinician:nephrology", rel))
        active = [r["key"] for r in self.archivist.rules_for_next_episode(ctx, PATIENT)]
        self.check(area, "F4 doctor confirms both rules with signed releases", "both active for the next episode",
                   active, sorted(active) == ["avoid_drug_class:NSAID", "routing:rcc_red_flag"])

        self.desk.clock.advance(timedelta(days=365))
        decayed = self.archivist.rules_for_next_episode(ctx, PATIENT)
        rel = self.desk.release(nsaid["artifact_id"], nsaid["artifact_hash"], release_id="rel-reverify")
        self.cortex.reverify(nsaid["artifact_id"], HumanActor(CLINICIAN_ID, "clinician:nephrology", rel))
        after = [r["key"] for r in self.archivist.rules_for_next_episode(ctx, PATIENT)]
        self.check(area, "F5 a year later, nobody re-checked the rules",
                   "confidence faded: rules inactive until a doctor re-verifies; re-verified rule active again",
                   f"active after a year {len(decayed)}; after re-verifying {after}",
                   decayed == [] and after == ["avoid_drug_class:NSAID"])

        ok_before = self.cortex.verify_chain(3, PATIENT)["ok"]
        with self.cortex.engine.begin() as conn:
            conn.execute(update(schema.evidence).where(schema.evidence.c.partition == PATIENT)
                         .where(schema.evidence.c.kind == "data_delivered").values(content='{"egfr": 90}'))
        tampered = self.cortex.verify_chain(3, PATIENT)
        self.check(area, "F6 someone edits a stored lab result in the database",
                   "the evidence chain check detects it", f"before ok={ok_before}; after: {tampered['reason']}",
                   ok_before and not tampered["ok"])

        erase = self.desk.release(f"erase:{PATIENT}", "sha256:" + "0" * 64, release_id="rel-erase")
        receipt = self.cortex.erase_patient(PATIENT, HumanActor(CLINICIAN_ID, "clinician:nephrology", erase),
                                            "patient request (right to erasure)")
        hospital = [k["key"] for k in self.cortex.list_knowledge(2, "hospital")]
        self.check(area, "F7 the patient asks to be erased",
                   "her evidence, rules and images deleted; anonymous receipt; hospital-wide rule kept",
                   f"deleted {receipt['evidence_rows']} evidence, {receipt['knowledge_rows']} rules, "
                   f"{receipt['blobs_deleted']} files; hospital rules {hospital}",
                   receipt["complete"] and self.cortex.list_evidence(3, PATIENT) == []
                   and PATIENT not in receipt["subject_hash"] and hospital == ["routing:rcc_red_flag"])

    # ------------------------------------------------------------------ G. policy guards
    async def policy(self) -> None:
        area = "G. Safety rules (enforced in code)"
        for seat, action, refusal, what in (
                ("imaging_reader", "patient.send", "report_to_patient", "Imaging Reader tries to message the patient"),
                ("genomics_interpreter", "genomics.reclassify", "reclassify_variants",
                 "Genomics Interpreter tries to reclassify a variant"),
                ("renal_guardian", "medication.change_dose", "change_dose", "Renal Guardian tries to change a dose"),
                ("safety_verifier", "verifier.override", "pass_failed_check",
                 "Safety Verifier tries to pass a failed check"),
                ("case_briefer", "case.decide", "decide", "Case Briefer tries to take the clinical decision")):
            try:
                self.ctx(seat).act(action)
                actual, ok = "allowed", False
            except PolicyViolation as e:
                actual, ok = f"refused ({e.refusal})", e.refusal == refusal
            self.check(area, f"G {what}", "refused", actual, ok)
        try:
            self.ctx("imaging_reader").check_memory(2, "read")
            actual, ok = "allowed", False
        except PolicyViolation:
            actual, ok = "refused", True
        self.check(area, "G Imaging Reader reads hospital-wide memory", "refused (not in its manifest)", actual, ok)
        dead = len(self.desk.bus.dead_letters)
        await self.desk.deliver(self.desk.envelope(MessageType.SYMPTOM_REPORT, "companion", "imaging_reader", {
            "symptoms": [{"code": "x", "label": "x"}], "distress": {"score": 0.1, "level": "low"}},
            patient=PATIENT, episode="ep-g"))
        letters = self.desk.bus.dead_letters[dead:]
        self.check(area, "G Companion messages the Imaging Reader directly", "Dispatcher rejects the route",
                   letters[0].error if letters else "delivered",
                   len(letters) == 1 and "may not address" in letters[0].error)
        self.check(area, "G every action audited", "tamper-evident audit chain intact",
                   f"{len(self.desk.audit.events)} audit events, chain ok {self.desk.audit.verify_chain()}",
                   self.desk.audit.verify_chain())

    async def run(self) -> list[Check]:
        await self.start()
        try:
            for step in (self.red_flags, self.medications, self.imaging, self.genomics, self.decision, self.memory,
                         self.policy):
                await step()
        finally:
            await self.desk.close()
        return self.checks


# ---------------------------------------------------------------------- Chrislin's real model (optional)
async def real_model_checks(work: Path) -> list[Check]:
    from kcd.imaging import chrislin_model
    from kcd.imaging.model import CallableCtModel

    area = "R. Chrislin's real CT model"
    images = sorted(REAL_IMAGES.glob("*.jpg")) if REAL_IMAGES.is_dir() else []
    if not chrislin_model.weights_present() or len(images) < 4:
        return [Check(area, "R0 real model", "weights and her 4 test slices present", "not found: skipped", True)]
    pdir = work / "real" / PATIENT
    for i, img in enumerate(images, 1):
        d = pdir / f"ct-r{i}"
        d.mkdir(parents=True)
        shutil.copy(img, d / "key.jpg")
        (d / "study.json").write_text(json.dumps({"study_uid": f"2.25.real.{i}", "modality": "CT",
                                                  "acquired": f"2026-0{i}-01T09:00:00Z", "key_image": "key.jpg",
                                                  "description": img.name, "source": "Chrislin test slice"}))
    d = pdir / "ct-r9"
    d.mkdir()
    shutil.copy(FIXTURES / "sofia-b" / "ct-01-normal" / "key.png", d / "key.png")
    (d / "study.json").write_text(json.dumps({"study_uid": "2.25.real.9", "modality": "CT", "key_image": "key.png",
                                              "acquired": "2026-09-01T09:00:00Z", "source": "synthetic drawing"}))
    desk = await Desk.create(start=NOW)
    cortex = Cortex(clock=desk.clock.now)
    cal = Calibration.load(Path(chrislin_model.__file__).with_name("chrislin_calibration.json"))
    reader = ImagingReader(cortex, cal, audit=desk.audit, clock=desk.clock)
    rt = await desk.add_seat(reader, imaging_tools(FolderPacs(pdir.parent), CallableCtModel(chrislin_model.predict)),
                             timeout_s=600)
    verifier_rt = await desk.add_seat(SafetyVerifier(now=desk.clock.now),
                                      clinical_tools(FolderFhir(FIXTURES), KnowledgeModel.load(), desk.clock.now))
    checks = []
    try:
        for i, img in enumerate(images, 1):
            t0 = time.perf_counter()
            r = await reader.process_study(rt.context(), patient_id=PATIENT, episode_id=f"ep-r{i}",
                                           study_uid=f"2.25.real.{i}")
            await desk.bus.drain()
            expected = img.name.split("-")[0].strip().lower()
            passed = [e for s, e in desk.bus.published if s == DISPATCHER_INBOX and e.from_ == "safety_verifier"
                      and e.type == MessageType.IMAGING_SIGNAL]
            checks.append(Check(area, f"R{i} {img.name}", f"'{expected}', calibrated, passes the Safety Verifier",
                                f"{r.details.get('label')} p_cal={r.details.get('p_calibrated')} "
                                f"({time.perf_counter() - t0:.0f} s)",
                                r.details.get("label") == expected and len(passed) == i))
        r = await reader.process_study(rt.context(), patient_id=PATIENT, episode_id="ep-r9", study_uid="2.25.real.9")
        checks.append(Check(area, "R9 a drawing with no real kidney", "not classified (no kidney found), nothing sent",
                            f"status {r.status}: {str(r.details.get('error'))[:70]}",
                            r.status == "model_error" and not r.sent))
        del verifier_rt
    finally:
        await desk.close()
    return checks


def report(checks: list[Check]) -> tuple[str, str]:
    lines, md = [], ["# Edge cases: Sofia B. (synthetic)", "", "| | Case | Expected | Actual |", "|---|---|---|---|"]
    area = None
    for c in checks:
        if c.area != area:
            area = c.area
            lines.append(f"\n{area}")
            md.append(f"| | **{area}** | | |")
        mark = "PASS" if c.ok else "FAIL"
        lines += [f"  [{mark}] {c.case}", f"         expected: {c.expected}", f"         actual:   {c.actual}"]
        md.append(f"| {'✅' if c.ok else '❌'} | {c.case} | {c.expected} | {c.actual.replace('|', '/')} |")
    failed = sum(not c.ok for c in checks)
    summary = f"\n{len(checks) - failed} of {len(checks)} checks passed" + (f", {failed} FAILED" if failed else "")
    return "\n".join(lines) + "\n" + summary, "\n".join(md) + "\n\n" + summary.strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="kcd.scenarios.edge_cases")
    ap.add_argument("--with-real-ct-model", action="store_true",
                    help="also run Chrislin's model on her 4 real CT slices (needs models/kidney_project; ~1 min)")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(FIXTURES / "sofia-b", work / PATIENT)  # a working copy: the scenario adds and removes files
        print("Sofia B. (synthetic): RCC with VHL, CKD stage 3, hypertension, on sunitinib. Scripted CT model.")
        checks = asyncio.run(EdgeCases(work).run())
        if a.with_real_ct_model:
            print("Running Chrislin's model on her 4 real slices (CPU, about 12 s each)...")
            checks += asyncio.run(real_model_checks(work))
    text, md = report(checks)
    print(text)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "edge-cases.md").write_text(md, encoding="utf-8")
    print(f"report: {REPORTS / 'edge-cases.md'}")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
