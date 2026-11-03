"""One running desk: every seat on kcd_core, two synthetic patients and the Clinical Board, for the web server.

Real: Dispatcher + escalation, Renal Guardian, Safety Verifier, Knowledge Model, Imaging Reader, Genomics
Interpreter, Archivist, Cortex, the Chief of Staff's compiler and, on request, Chrislin's CT model.
Stand-ins: Companion, Case Briefer (kcd/web/seats.py). Everything lives in memory and in a temporary folder:
restarting the server, or pressing Reset, starts again from the fixtures.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from kcd_core.audit import content_hash
from kcd_core.contracts import PolicyCatalog, Release
from kcd_core.contracts.messages import DecisionItem
from kcd_core.escalation import Escalation
from kcd_core.signing import Signer, sign_release
from PIL import Image

from kcd.cortex import DEFAULT_HOSPITAL, Cortex, HumanActor, MemoryBlobStore
from kcd.cortex.auth import release_verifier
from kcd.demo import DEMO_NOW, HOME_BP, LAB_PHOTO, V1_CACHED, _seed
from kcd.desk import CATALOG_PATH, CLINICIAN_ID, Desk
from kcd.fhir import FolderFhir
from kcd.genomics.source import FolderPanelSource
from kcd.imaging.calibration import Calibration
from kcd.imaging.image_io import ImageDecodeError, LoadedImage, load_image
from kcd.imaging.model import CallableCtModel, CtModelError, CtPrediction, FixedCtModel
from kcd.imaging.pacs import FolderPacs
from kcd.knowledge import KnowledgeModel
from kcd.scenarios.edge_cases import FIXTURES as ALL_FIXTURES
from kcd.scenarios.edge_cases import SCRIPTED_VERSION, ScriptedCtModel
from kcd.seats.archivist import Archivist
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from kcd.seats.imaging_reader import ImagingReader
from kcd.seats.renal_guardian import RenalGuardian
from kcd.tools import board_tools, clinical_tools, genomics_tools, imaging_tools
from kcd.util import sha256_hex
from kcd.web.board import Board, Metrics, ObservedAudit, ObservedBus, RunningClock
from kcd.web.hub import Hub
from kcd.web.seats import NSAID_RULE, WebCaseBriefer, WebCompanion, WebVerifier

MARCO, SOFIA = "marco-r", "sofia-b"
PATIENTS = (MARCO, SOFIA)
CHRISLIN_CALIBRATION = Path(__file__).resolve().parents[1] / "imaging" / "chrislin_calibration.json"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".dcm", ".dicom", ".bmp", ".tif", ".tiff"}
ROLES = {"nephrologist": "clinician:nephrology", "uro-oncologist": "clinician:uro_oncology",
         "radiologist": "clinician:radiology", "geneticist": "clinician:genetics"}


@dataclass(frozen=True)
class Clinician:
    id: str
    name: str
    role: str
    pager: str


CLINICIANS = (Clinician(CLINICIAN_ID, "Dr. A. Rossi", "nephrologist", "4471"),
              Clinician("dr_bianchi", "Dr. L. Bianchi", "uro-oncologist", "4472"),
              Clinician("dr_conti", "Dr. E. Conti", "radiologist", "4473"),
              Clinician("dr_greco", "Dr. P. Greco", "geneticist", "4474"),
              Clinician("dr_verdi", "Dr. M. Verdi", "uro-oncologist", "4475"))
BY_NAME = {c.name: c for c in CLINICIANS}
BY_ID = {c.id: c for c in CLINICIANS}


class DeskError(Exception):
    """A request the desk refuses, with the reason the person at the screen should read."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class _OnlyThisImage:
    """Marco's stub answers for his own CT only. A fixed answer for any upload would look like a real reading."""

    def __init__(self, image_bytes: bytes, model: FixedCtModel):
        self.sha, self.model = sha256_hex(image_bytes), model
        self.model_version = model.model_version

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        if sha256_hex(image_bytes) != self.sha:
            raise CtModelError("the stub has no answer for this image: switch to Chrislin's model to read it")
        return self.model.predict(image, image_bytes, media_type)


class _ModelSwitch:
    """The CT model the Imaging Reader's tool calls. Session changes `current` under its imaging lock."""

    def __init__(self) -> None:
        self.current: Any = None

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        return self.current.predict(image, image_bytes, media_type)


def real_model_available() -> bool:
    if not all(importlib.util.find_spec(m) for m in ("torch", "timm", "segmentation_models_pytorch", "scipy")):
        return False  # the optional ct-model extras: the desk runs without them, on the stubs
    from kcd.imaging import chrislin_model
    return chrislin_model.weights_present()


class Session:
    @classmethod
    async def create(cls, hub: Hub) -> Session:
        self = cls()
        await self._start(hub)
        return self

    async def _start(self, hub: Hub) -> None:
        self.hub = hub
        self.active = MARCO
        self.lang = "it" if os.environ.get("KCD_LANG") == "it" else "en"
        self.ct_mode = "stub"
        self.imaging_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.busy = False
        self.done_steps: set[int] = set()
        self.edge_report: dict[str, Any] | None = None
        self.edge_running = False
        self._checks: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._panels_shown: set[tuple[str, str]] = set()
        self.work = tempfile.TemporaryDirectory(prefix="kcd-web-", dir=os.environ.get("KCD_WEB_WORK"))
        root = _seed(Path(self.work.name))
        shutil.copytree(ALL_FIXTURES / SOFIA, root / SOFIA)
        self.root = root

        self.metrics = Metrics()
        catalog = PolicyCatalog.load(CATALOG_PATH)
        self.clock = RunningClock(DEMO_NOW)
        self.km = KnowledgeModel.load()
        self.fhir = FolderFhir(root)
        self.pacs = FolderPacs(root)
        self.switch = _ModelSwitch()
        self.lab_photos = self._lab_photos()
        self._red_flags: dict[str, dict[str, Any]] = {}

        audit = ObservedAudit(hub, self.metrics, catalog, self._on_violation)
        bus = ObservedBus(hub, self.metrics)
        self.board = Board(hub, self.clock, lambda pack: self.briefer.pack_view(pack))
        self.desk = desk = await Desk.create(clock=self.clock, audit=audit, bus=bus, gates=self.board, pager=self)
        self.catalog = desk.catalog
        self.signers = {CLINICIAN_ID: desk.clinician}
        for c in CLINICIANS:
            if c.id not in self.signers:
                self.signers[c.id] = Signer.generate(c.id)
                desk.clinician_keys.add(c.id, self.signers[c.id].public_key)
        self.cortex = Cortex(blobs=MemoryBlobStore(), human_verifier=release_verifier(desk.clinician_keys),
                             clock=self.clock.now)
        self.versions = {s: [desk.registry.get(s)] for s in desk.registry.seats()}
        desk.registry.on_reload(self._on_reload)

        self.companion = WebCompanion(self.km, hub, self.cortex, lang=self.lang, history_rcc=self._history_rcc,
                                      name_of=lambda cid: BY_ID[cid].name)
        self.briefer = WebCaseBriefer(language_of=lambda p: self.companion.talk(p).lang, on_finding=self._on_finding)
        self.reader = ImagingReader(self.cortex, None, audit=audit, clock=self.clock)
        self.archivist = Archivist(self.cortex)
        verifier = WebVerifier(now=self.clock.now, on_result=self._on_verification)
        wearables = {MARCO: HOME_BP, SOFIA: {"sbp": 152, "dbp": 94}}
        self.rt = {
            "companion": await desk.add_seat(self.companion, self._patient_door_tools()),
            "case_briefer": await desk.add_seat(self.briefer),
            "renal_guardian": await desk.add_seat(RenalGuardian(), clinical_tools(self.fhir, self.km, self.clock.now,
                                                                                  wearables)),
            "safety_verifier": await desk.add_seat(verifier, clinical_tools(self.fhir, self.km, self.clock.now)),
            "imaging_reader": await desk.add_seat(self.reader, imaging_tools(self.pacs, self.switch), timeout_s=300),
            "genomics_interpreter": await desk.add_seat(GenomicsInterpreter(self.cortex),
                                                        genomics_tools(FolderPanelSource(root), self.fhir)),
            "archivist": await desk.add_seat(self.archivist, board_tools(self.board.create_item)),
        }
        self.ticker = asyncio.create_task(self._tick_escalations(), name="escalation-ticker")

    async def close(self) -> None:
        self.ticker.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(self.ticker, *self.tasks, return_exceptions=True)
        await self.desk.close()
        self.work.cleanup()

    def spawn(self, coro: Any) -> asyncio.Task:
        """Run slow work (a scenario, a CT reading) after the request has returned; errors go to the stream."""
        async def guarded() -> None:
            try:
                await coro
            except DeskError as e:
                self.hub.publish("error", {"seat": "desk", "message": str(e)})
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - nothing else would report it
                self.hub.publish("error", {"seat": "desk", "message": f"{type(e).__name__}: {e}"})
        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _ctx(self, seat: str):  # noqa: ANN202 - kcd_core.runtime.SeatContext
        return self.rt[seat].context()

    async def drain(self) -> None:
        await self.desk.bus.drain()

    # ------------------------------------------------------------------ setup helpers
    def _lab_photos(self) -> dict[str, list[dict[str, Any]]]:
        read = lambda name: json.loads((self.root / SOFIA / name).read_text(encoding="utf-8"))  # noqa: E731
        return {MARCO: [{"id": "photo", "label": "Lab report photo", "data": LAB_PHOTO}],
                SOFIA: [{"id": "ok", "label": "Lab report photo", "data": read("lab-photo-ok.json")},
                        {"id": "ocr-error", "label": "Photo misread by OCR (eGFR 400)",
                         "data": read("lab-photo-ocr-error.json")}]}

    def _patient_door_tools(self) -> dict[str, Any]:
        async def channel_send(patient: str, text: str) -> None:
            self.hub.publish("patient_out", {"patient": patient, "text": text})

        async def red_flags(**kw: Any) -> dict[str, Any] | None:
            return self.km.red_flags(**kw)

        async def parse_report(photo: dict[str, Any]) -> dict[str, Any]:  # the photo's on-device OCR result
            return dict(photo)

        return {"channel.send": channel_send, "km.red_flags": red_flags, "lab.parse_report": parse_report}

    def _history_rcc(self, patient: str) -> bool:
        conditions = " ".join(str(c.get("display")) for c in self.fhir.conditions(patient)).lower()
        return "renal cell" in conditions or "rcc" in conditions

    def _on_reload(self, manifest: Any) -> None:
        self.versions.setdefault(manifest.seat, []).append(manifest)
        self.hub.publish("manifest", {"op": "reload", "seat": manifest.seat, "version": manifest.version})

    def _on_violation(self, seat: str, patient: str | None, message: str) -> None:
        if patient in PATIENTS and hasattr(self, "board"):
            self.board.add(kind="verification_failed", gate="review", status="info", patient=patient,
                           title=f"Blocked by policy: {seat}", data={"seat": seat, "reason": message})

    # ------------------------------------------------------------------ what the seats report
    async def page(self, target: str, esc: Escalation) -> None:
        """Core's Pager: the page goes out (here, to the stream) and the Board shows who must acknowledge."""
        who = BY_ID.get(target)
        name, pager = (who.name, who.pager) if who else (target, "-")
        flag = self._red_flag_of(esc)
        self.hub.publish("page", {"name": name, "pager": pager, "patient": esc.patient_id,
                                  "message": f"{esc.patient_id}: {flag.get('pattern', 'red flag')}"})
        item = next((i for i in self.board.items if i.get("escalation_id") == esc.id), None)
        data = {"pattern": flag.get("pattern"), "evidence": flag.get("evidence"), "level": len(esc.paged),
                "paged": {"name": name, "pager": pager}, "deadline": self._deadline(esc)}
        if item is None:
            self.board.add(kind="escalation", gate="urgent_ack", patient=esc.patient_id, data=data,
                           title=f"Red flag · {esc.patient_id}", escalation_id=esc.id)
        else:
            item["data"] = data
            self.board.changed(item)

    def _deadline(self, esc: Escalation) -> Any:
        window = self.desk.escalations.ladder[min(esc.level, len(self.desk.escalations.ladder) - 1)].ack_window
        return self.clock.now() + window

    def _red_flag_of(self, esc: Escalation) -> dict[str, Any]:
        env = next((e for _, e in self.desk.bus.published if str(e.id) == esc.message_id), None)
        return env.payload if env is not None else {}

    async def _tick_escalations(self) -> None:
        while True:
            await asyncio.sleep(2)
            moved = await self.desk.escalations.tick()
            for esc in moved:
                if esc.status == "exhausted":
                    item = next((i for i in self.board.items if i.get("escalation_id") == esc.id), None)
                    if item:
                        item["status"] = "exhausted: emergency desk paged"
                        self.board.changed(item)

    def _on_verification(self, patient: str, row: dict[str, Any]) -> None:
        self._checks[(patient, row["type"])] = row["checks"]
        self.hub.publish("verification", {**row, "patient": patient})
        if not row["passed"]:
            if row["type"] == "therapy_conflict":
                self.companion.new_episode(patient)  # the patient can report it again, with a better photo
            failed = [c["check"] for c in row["checks"] if not c["ok"]]
            self.board.add(kind="verification_failed", gate="review", status="info", patient=patient,
                           title=f"Stopped by the Safety Verifier: {row['type']}",
                           data={"from": row["from"], "failed": failed, "checks": row["checks"],
                                 "meaning": "Kept out of the case pack. The seat that produced it was told."})

    def _on_finding(self, patient: str, kind: str, p: dict[str, Any]) -> None:
        if kind == "imaging_signal":
            self.board.add(kind="imaging", gate="radiologist_review", patient=patient,
                           title=f"CT signal for review · {p['provenance']['study_uid']}",
                           data={**p, "key_image": self._image_url(patient, p.get("key_image")),
                                 "heatmap_ref": self._image_url(patient, p.get("heatmap_ref")),
                                 "verification": {"checks": self._checks.get((patient, kind), [])}},
                           artifact_id=p["provenance"]["study_uid"], artifact_hash=content_hash(p))
        elif kind == "data_delivered" and p.get("kind") == "genomic_context":
            d = p["data"]
            if (patient, d.get("panel_id")) not in self._panels_shown:
                self._panels_shown.add((patient, d.get("panel_id")))
                self.board.add(kind="genomics", gate="genetic_disclosure", status="info", patient=patient,
                               title=f"Genomic context · {d.get('panel_id')}",
                               data={**d, "panel": d.get("panel_id"), "citations": p.get("citations")})

    @staticmethod
    def _image_url(patient: str, ref: str | None) -> str | None:
        return f"/api/evidence/{patient}/{ref.rpartition('/')[2]}" if ref else None

    # ------------------------------------------------------------------ patient door
    def _patient(self, patient: str | None) -> str:
        chosen = patient or self.active
        if chosen not in PATIENTS:
            raise DeskError(f"unknown patient {chosen!r}", 404)
        return chosen

    def select_patient(self, patient: str) -> None:
        self.active = self._patient(patient)
        self.hub.publish("patient_selected", {"patient": self.active})

    async def patient_message(self, text: str, features: dict[str, Any] | None, patient: str | None = None) -> None:
        patient = self._patient(patient)
        await self.companion.on_text(self._ctx("companion"), patient, text, features)
        await self.drain()

    async def attach_lab(self, photo_id: str | None = None, patient: str | None = None) -> None:
        patient = self._patient(patient)
        photos = self.lab_photos[patient]
        photo = next((p for p in photos if p["id"] == photo_id), None) if photo_id else photos[0]
        if photo is None:
            raise DeskError(f"no lab photo {photo_id!r} for {patient}", 404)
        await self.companion.on_lab_photo(self._ctx("companion"), patient, photo["data"])
        await self.drain()

    # ------------------------------------------------------------------ CT
    def _use_model(self, patient: str) -> None:
        if self.ct_mode == "real":
            self.reader.calibration = Calibration.load(CHRISLIN_CALIBRATION)
            if not isinstance(getattr(self, "_real", None), CallableCtModel):
                self._real = CallableCtModel("kcd.imaging.chrislin_model:predict")
            self.switch.current = self._real
        elif patient == MARCO:
            image = next(self.root.glob(f"{MARCO}/*/key.png")).read_bytes()
            self.switch.current = _OnlyThisImage(image, FixedCtModel(V1_CACHED, model_version="stub-v1-cached"))
            self.reader.calibration = Calibration(2.5, "stub-v1-cached")
        else:
            self.switch.current = ScriptedCtModel(self.root / SOFIA)
            self.reader.calibration = Calibration(1.5, SCRIPTED_VERSION)

    def set_ct_model(self, mode: str) -> None:
        if mode not in ("stub", "real"):
            raise DeskError(f"unknown CT model {mode!r}", 422)
        if mode == "real" and not real_model_available():
            raise DeskError("Chrislin's model needs its weights under models/kidney_project and the ct-model extras")
        self.ct_mode = mode
        self.hub.publish("ct_model", {"mode": mode})

    def studies(self, patient: str | None = None) -> list[dict[str, Any]]:
        patient = self._patient(patient)
        return [{"study_uid": s.study_uid, "modality": s.modality, "acquired": s.acquired,
                 "description": s.description, "readable": s.modality.upper() == "CT",
                 "read": self.cortex.processed_result(f"study:{s.study_uid}") is not None}
                for s in self.pacs.list_studies(patient)]

    def check_study(self, study_uid: str | None, patient: str | None = None) -> None:
        """Before accepting a reading: the same refusal the Imaging Reader would give, straight to the screen."""
        patient = self._patient(patient)
        studies = self.studies(patient)
        chosen = [s for s in studies if s["study_uid"] == study_uid] if study_uid else studies
        if not chosen:
            raise DeskError(f"no study {study_uid} for {patient}", 404)
        if study_uid and not chosen[0]["readable"]:
            raise DeskError(f"Not read: this is a {chosen[0]['modality']} study, and the Imaging Reader reads CT only.",
                            422)

    async def read_study(self, study_uid: str | None = None, patient: str | None = None) -> Any:
        patient = self._patient(patient)
        async with self.imaging_lock:
            self._use_model(patient)
            model = "Chrislin's model (this can take a minute on a CPU)" if self.ct_mode == "real" else "the stub"
            self.hub.publish("note", {"seat": "imaging_reader", "text": f"Reading {study_uid or 'the latest CT'} for "
                                      f"{patient} with {model}."})
            result = await self.reader.process_study(self._ctx("imaging_reader"), patient_id=patient,
                                                     episode_id=self.companion.episode(patient), study_uid=study_uid)
        await self.drain()
        if result.duplicate:
            self.hub.publish("note", {"seat": "imaging_reader",
                                      "text": "This study was already read: no second signal."})
        elif not result.sent:
            self.board.add(kind="verification_failed", gate="radiologist_review", status="info", patient=patient,
                           title=f"CT not read: {result.status}",
                           data={"study": result.details["provenance"].get("study_uid"),
                                 "error": result.details.get("error"), "details_evidence": result.details_evidence_id,
                                 "meaning": "Nothing was sent. Recorded as evidence and audit for the radiologist."})
        self.hub.publish("memory", {"patient": patient})
        return result

    def add_upload(self, name: str, data_base64: str, patient: str | None = None) -> str:
        patient = self._patient(patient)
        try:
            data = base64.b64decode(data_base64, validate=True)
        except ValueError as e:
            raise DeskError(f"the upload is not valid base64: {e}", 422) from e
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise DeskError(f"the upload must be between 1 byte and {MAX_UPLOAD_BYTES // 2**20} MB", 422)
        suffix = Path(name or "").suffix.lower()
        study_uid = f"2.25.upload.{uuid.uuid4().hex[:12]}"
        folder = self.root / patient / f"upload-{uuid.uuid4().hex[:8]}"
        folder.mkdir()
        key = f"key{suffix if suffix in IMAGE_SUFFIXES else '.bin'}"
        (folder / key).write_bytes(data)
        (folder / "study.json").write_text(json.dumps({
            "study_uid": study_uid, "modality": "CT", "acquired": self.clock.now().isoformat(),
            "description": f"Uploaded from the browser: {Path(name or 'image').name}", "source": "browser upload",
            "key_image": key}), encoding="utf-8")
        return study_uid

    def evidence_image(self, patient: str, evidence_id: str) -> tuple[bytes, str]:
        row = self.cortex.get_evidence(3, self._patient(patient), evidence_id)
        if not row.get("blob_sha256"):
            raise DeskError("this evidence has no image", 404)
        blob = self.cortex.read_blob(3, patient, row["blob_sha256"])
        media = row["content"].get("media_type") or "application/octet-stream"
        if media.startswith("image/") and media != "image/tiff":
            return blob, media
        try:
            gray = load_image(blob).gray  # browsers cannot show DICOM: a preview, the stored bytes stay as they are
        except ImageDecodeError as e:
            raise DeskError(f"this file cannot be shown: {e}", 415) from e
        out = io.BytesIO()
        Image.fromarray((np.clip(gray, 0, 1) * 255).astype("uint8")).save(out, format="PNG")
        return out.getvalue(), "image/png"

    # ------------------------------------------------------------------ clinician door
    def _release(self, c: Clinician, artifact_id: str, artifact_hash: str, classes: list[str] | None = None,
                 release_id: str | None = None) -> Release:
        r = Release(id=release_id or f"rel-{uuid.uuid4().hex[:12]}", artifact_id=artifact_id,
                    artifact_hash=artifact_hash, clinician_id=c.id, role=ROLES[c.role],
                    content_classes=classes or [], signed_at=self.clock.now())
        return sign_release(r, self.signers[c.id])

    def _signed(self, item: dict[str, Any], status: str, c: Clinician, sig: str | None) -> None:
        item.update(status=status, signed_by=f"{c.name} ({c.role})", signature=(sig or "")[:16])
        self.board.changed(item)

    def _gate_event(self, c: Clinician, gate: str, action: str) -> None:
        self.hub.publish("gate", {"clinician": c.name, "gate": gate, "action": action, "verified": True})

    async def board_act(self, item_id: str, action: str, clinician: str, items: list[dict] | None = None,
                        note: str = "") -> None:
        item = self.board.get(item_id)
        c = BY_NAME.get(clinician)
        if item is None:
            raise DeskError(f"no board item {item_id}", 404)
        if c is None:
            raise DeskError(f"unknown clinician {clinician!r}", 422)
        if item["status"] != "open":
            raise DeskError(f"this item is already {item['status']}")
        expected = {"escalation": {"acknowledge"}, "imaging": {"review"}, "case_pack": {"sign"},
                    "rule": {"confirm", "reject"}}.get(item["kind"], set())
        if action not in expected:
            raise DeskError(f"'{action}' does not apply to a {item['kind']} item", 422)
        await getattr(self, f"_act_{action}")(item, c, items or [], note)
        await self.drain()
        self.hub.publish("memory", {"patient": item["patient"]})

    async def _act_acknowledge(self, item: dict, c: Clinician, _items: list, _note: str) -> None:
        await self.desk.escalations.acknowledge(item["escalation_id"], c.id)
        self._signed(item, "acknowledged", c, None)
        self._gate_event(c, "urgent_ack", "acknowledged")

    async def _act_review(self, item: dict, c: Clinician, _items: list, _note: str) -> None:
        if c.role != "radiologist":
            raise DeskError(f"A CT signal is reviewed by a radiologist; {c.name} is a {c.role}.", 403)
        release = self._release(c, item["artifact_id"], item["artifact_hash"], ["imaging_result"])
        self._signed(item, "reviewed", c, release.sig)
        self._gate_event(c, "radiologist_review", "reviewed")

    async def _act_sign(self, item: dict, c: Clinician, items: list[dict], note: str) -> None:
        patient = item["patient"]
        waiting = [i for i in self.board.items if i["patient"] == patient and i["kind"] == "imaging"
                   and i["status"] == "open"]
        if "radiologist_review" in item["gates"] and waiting:
            raise DeskError("The radiologist must review the CT signal before this case pack can be signed.")
        if c.role == "radiologist":
            raise DeskError("The case pack is signed by the treating clinician, not the radiologist.", 403)
        chosen = [DecisionItem(code=str(i["code"]), patient_text=str(i["patient_text"]).strip(),
                               patient_text_en=i.get("patient_text_en"))
                  for i in items if i.get("selected", True) and str(i.get("patient_text", "")).strip()]
        if not chosen:
            raise DeskError("Select at least one decision with text for the patient.", 422)
        if item["artifact_hash"] != self.briefer.cases[patient].artifact_hash:
            raise DeskError("A newer version of this case pack has arrived. Review it, then sign.")
        release = self._release(c, item["case_id"], item["artifact_hash"], ["dose_instruction"],
                                release_id=f"dec-{item['case_id']}-v{item['version']}")
        await self.briefer.publish_decision(self._ctx("case_briefer"), patient_id=patient, release=release,
                                            items=chosen, note=note)
        self._signed(item, "signed", c, release.sig)
        self._gate_event(c, "clinician_signoff", "signed")

    async def _act_confirm(self, item: dict, c: Clinician, _items: list, _note: str) -> None:
        release = self._release(c, item["artifact_id"], item["artifact_hash"], ["dose_instruction"])
        rule = self.cortex.confirm(item["artifact_id"], HumanActor(c.id, ROLES[c.role], release))
        if rule["key"] == NSAID_RULE:
            self.companion.released_rules[(item["patient"], rule["key"])] = (item["artifact_hash"], release, c.name)
        item["data"]["status"] = "confirmed"
        self._signed(item, "confirmed", c, release.sig)
        self._gate_event(c, "rule_confirmation", "confirmed")

    async def _act_reject(self, item: dict, c: Clinician, _items: list, _note: str) -> None:
        release = self._release(c, item["artifact_id"], item["artifact_hash"])
        self.cortex.reject(item["artifact_id"], HumanActor(c.id, ROLES[c.role], release), "rejected on the Board")
        item["data"]["status"] = "rejected"
        self._signed(item, "rejected", c, release.sig)
        self._gate_event(c, "rule_confirmation", "rejected")

    def erase_patient(self, patient: str | None, clinician: str = "Dr. A. Rossi") -> dict[str, Any]:
        patient = self._patient(patient)
        c = BY_NAME[clinician]
        release = self._release(c, f"erase:{patient}", "sha256:" + "0" * 64)
        receipt = self.cortex.erase_patient(patient, HumanActor(c.id, ROLES[c.role], release),
                                            "requested from the web desk")
        self.board.forget(patient)
        self.companion.forget(patient)
        self.briefer.cases.pop(patient, None)
        self.briefer.genomics_asked.discard(patient)
        self._panels_shown = {k for k in self._panels_shown if k[0] != patient}
        self._checks = {k: v for k, v in self._checks.items() if k[0] != patient}
        self.hub.forget(patient)
        self.hub.publish("erased", {"patient": patient})
        return {k: receipt[k] for k in ("subject_hash", "evidence_rows", "knowledge_rows", "blobs_deleted", "complete")}

    # ------------------------------------------------------------------ what the browser shows
    def seat_view(self, seat: str) -> dict[str, Any]:
        manifest = self.desk.registry.get(seat).model_dump(mode="json")
        versions = [m.model_dump(mode="json") for m in self.versions.get(seat, [])]
        return {"id": seat, "manifest": manifest, "versions": versions, "metrics": self.metrics.of(seat),
                "shadow": seat not in self.rt and seat != "dispatcher"}

    def memory_view(self, patient: str) -> list[dict[str, Any]]:
        out = []
        for layer, partition in ((3, patient), (2, DEFAULT_HOSPITAL)):
            evidence = [{"ts": e["recorded_at"], "kind": e["kind"], "source": e["source"],
                         "content_hash": e["content_sha256"], "chain_hash": e["chain_hash"]}
                        for e in self.cortex.list_evidence(layer, partition)]
            knowledge = [{"status": k["status"], "key": k["key"], "value": _rule_text(k["value"]),
                          "conf": k["confidence"], "conf_effective": k["confidence_effective"], "source": k["source"],
                          "confirmer": k["decided_by"], "version": k["version"]}
                         for k in self.cortex.list_knowledge(layer, partition)]
            out.append({"partition": f"L{layer} · {partition}", "chain": self.cortex.verify_chain(layer, partition),
                        "evidence": evidence, "knowledge": knowledge})
        return out

    def patient_view(self, patient: str) -> dict[str, Any]:
        return {**self.fhir.patient(patient), "conditions": [c.get("display") for c in self.fhir.conditions(patient)],
                "medications": [m.get("name") for m in self.fhir.medications(patient)],
                "lab_photos": [{"id": p["id"], "label": p["label"]} for p in self.lab_photos[patient]]}

    def state(self) -> dict[str, Any]:
        patient = self.active
        real = real_model_available()
        return {
            "patient": self.patient_view(patient), "active": patient,
            "patients": [{"id": p, "name": self.fhir.patient(p)["name"]} for p in PATIENTS],
            "seats": [self.seat_view(s) for s in self.desk.registry.seats() if s != "chief_of_staff"],
            "board": self.board.view(patient), "memory": self.memory_view(patient), "studies": self.studies(patient),
            "clinicians": [{"name": c.name, "role": c.role} for c in CLINICIANS],
            "lang": self.lang, "busy": self.busy, "done_steps": sorted(self.done_steps),
            "ct_model": {"mode": self.ct_mode, "real_available": real},
            "models": {"edge": "rules only", "cloud": "disabled"},
            "config": {"fhir": False, "dicomweb": False, "telegram": False},
            "edge_report": self.edge_report, "history": list(self.hub.history),
        }


def _rule_text(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("instruction") or f"{value.get('pattern')} -> {value.get('route_to')}"
    return str(value)

