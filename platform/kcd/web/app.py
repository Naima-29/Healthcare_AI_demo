"""DeskApp: a whole Kidney Care Desk behind a browser.

Assembles the same desk as `kcd.demo` (all real seats), but with the REAL Case Briefer and the REAL Clinical Board,
and turns what happens on it into events for the UI. The browser is only a door: every action goes through the
same seats, the same PEP and the same signed messages as the tests.

    app = await DeskApp.create()           # full desk (needs sqlalchemy: Cortex)
    app = await DeskApp.create(light=True) # no memory seats (tests without sqlalchemy)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kcd_core.bus import DISPATCHER_INBOX
from kcd_core.contracts.messages import DecisionItem
from kcd_core.signing import Signer

from kcd.board import ClinicalBoard, Clinician
from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.knowledge import KnowledgeModel
from kcd.patient.channel import PatientChannel
from kcd.seats.briefer_tools import briefer_tools
from kcd.seats.case_briefer import CaseBriefer
from kcd.seats.companion import Companion, companion_tools
from kcd.seats.renal_guardian import RenalGuardian
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.tools import board_tools, clinical_tools
from kcd.web.hub import Hub

FIX_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURES = FIX_ROOT / "marco-r"
PATIENT = "marco-r"  # the default patient
EPISODE = "ep-2026-10-12"
DEMO_NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
V1_CACHED = {"normal": 0.00004, "cyst": 0.00003, "stone": 0.00003, "tumor": 0.9999}

# Every patient the desk knows. The seats are patient-agnostic (they work on the id in each message); this is the
# demo data per patient: who they are, what they write, their lab photo and home blood pressure.
PATIENTS: dict[str, dict[str, Any]] = {
    "marco-r": {
        "name": "Marco R.", "initials": "MR", "born": "1978-03-14", "sex": "male",
        "headline": "Kidney cancer follow-up, CKD stage 3",
        "lab": {"egfr": 52, "creatinine": 1.41, "date": "2026-10-08", "source": "patient photo (on-device OCR)"},
        "bp": {"sbp": 148, "dbp": 91},
        "story": "Red-flag case: blood in the urine, severe flank pain, weight loss, then ibuprofen.",
        "script": [
            "Good evening… I have a pain in my right side, under the ribs. Nothing serious, but it won’t go away and it gets worse at night.",
            "My urine is pinkish, sometimes really dark red.",
            "I’ve lost 23 kg in the last three months and sometimes I feel nauseous.",
            "I’d say 8 out of 10. And I often feel dizzy.",
        ],
        "ibuprofen": "For the pain I’m taking ibuprofen 600, twice a day.",
        "features": {"au": {"AU4": 3, "AU6": 3, "AU7": 2, "AU9": 2, "AU10": 1, "AU43": 1},
                     "voice": {"tremor": 0.7, "pitch_var": 0.6, "rate_dev": -0.4}},
    },
    "sofia-b": {
        "name": "Sofia B.", "initials": "SB", "born": "1969-07-02", "sex": "female",
        "headline": "Kidney cancer with a VHL variant, CKD stage 3",
        "lab": {"egfr": 49, "creatinine": 1.29, "date": "2026-10-09", "source": "patient photo (OCR)"},
        "bp": {"sbp": 152, "dbp": 94},
        "story": "Quieter case: a dull ache and tiredness, no blood. Same conflict check, a VHL finding, no red flag.",
        "script": [
            "Hello, I have had a dull ache in my left side for about two weeks.",
            "I feel very tired lately, and my blood pressure at home has been high.",
            "No blood in the urine, thankfully. The pain is about 5 out of 10.",
        ],
        "ibuprofen": "For the ache I take ibuprofen 400 when it gets bad.",
        "features": {"au": {"AU4": 1, "AU6": 1, "AU7": 1, "AU9": 0, "AU10": 0, "AU43": 1},
                     "voice": {"tremor": 0.25, "pitch_var": 0.3, "rate_dev": -0.15}},
    },
}


def _age(born: str) -> int:
    b = datetime.fromisoformat(born)
    return DEMO_NOW.year - b.year - ((DEMO_NOW.month, DEMO_NOW.day) < (b.month, b.day))


CLINICIANS = [  # (id, name, role): dr_rossi is the desk's own clinician key and the on-call primary
    ("dr_rossi", "Dr. A. Rossi", "clinician:nephrology"),
    ("dr_bianchi", "Dr. L. Bianchi", "clinician:uro_oncology"),
    ("dr_neri", "Dr. E. Neri", "clinician:radiology"),
    ("dr_gallo", "Dr. P. Gallo", "clinician:genetics"),
]
STEPS = [
    {"n": 1, "short": "Symptoms", "title": "The patient writes to the Companion about their symptoms", "door": "patient"},
    {"n": 2, "short": "Ibuprofen", "title": "They mention ibuprofen: stale eGFR, lab photo, drug-disease conflict", "door": "patient"},
    {"n": 3, "short": "CT", "title": "A new CT reaches PACS: Imaging Reader, Safety Verifier", "door": "system"},
    {"n": 4, "short": "Genomics", "title": "The Case Briefer asks for the genomic context", "door": "clinician"},
    {"n": 5, "short": "Case pack", "title": "Case pack on the Board: review, edit, sign", "door": "clinician"},
]
class ActionError(Exception):
    """A refused or invalid action, shown to the user."""


class _Rota:
    async def who(self, unit: str, role: str, at: datetime) -> str | None:
        return {"primary": "dr_rossi", "backup": "dr_bianchi", "head_of_unit": "dr_verdi"}.get(role)


def _seed(root: Path) -> Path:
    """One working copy of every patient's files under root/<patient id>/."""
    d = root / "marco-r"
    study = d / "ct-2026-10-11"
    study.mkdir(parents=True)
    shutil.copy(FIXTURES / "ct.png", study / "key.png")
    meta = json.loads((FIXTURES / "ct-study.json").read_text(encoding="utf-8"))
    (study / "study.json").write_text(json.dumps({
        "study_uid": meta["study_uid"], "modality": meta["modality"], "acquired": meta["acquired"],
        "description": meta["description"], "source": meta["source"], "key_image": "key.png"}), encoding="utf-8")
    shutil.copy(FIXTURES / "dna-panel.json", d / "panel.json")
    shutil.copy(FIXTURES / "fhir-bundle.json", d / "fhir-bundle.json")
    sofia, s_dir = FIX_ROOT / "sofia-b", root / "sofia-b"  # her follow-up CT (the other 10 studies are for the edge-case run)
    shutil.copytree(sofia / "ct-04-tumor", s_dir / "ct-2026-10-11")
    shutil.copy(sofia / "panel-2026.json", s_dir / "panel-2026.json")
    shutil.copy(sofia / "fhir-bundle.json", s_dir / "fhir-bundle.json")
    return root


class DeskApp:
    def __init__(self, *, light: bool, model: Any = None, calibration: Any = None) -> None:
        self.light, self.model, self.calibration = light, model, calibration
        self.hub = Hub()
        self.lock = asyncio.Lock()
        self.running: int | None = None
        self.running_patient: str | None = None
        self._pump_task: asyncio.Task[None] | None = None

    @classmethod
    async def create(cls, *, light: bool = False, model: Any = None, calibration: Any = None) -> DeskApp:
        app = cls(light=light, model=model, calibration=calibration)
        await app._build()
        app._pump_task = asyncio.create_task(app._pump())
        return app

    # ------------------------------------------------------------------ assembly
    async def _build(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = _seed(Path(self._tmp.name))
        self.desk = desk = await Desk.create(start=DEMO_NOW)
        km, fhir = KnowledgeModel.load(), FolderFhir(root)
        self.cortex = None
        self.people: dict[str, Clinician] = {}
        for cid, name, role in CLINICIANS:
            signer = desk.clinician if cid == "dr_rossi" else Signer.generate(cid)
            desk.clinician_keys.add(cid, signer.public_key)
            self.people[cid] = Clinician(cid, name, role, signer)

        self.board = ClinicalBoard(list(self.people.values()), desk.clock.now,
                                   acknowledge=lambda esc, cid: desk.dispatcher.escalations.acknowledge(esc, cid))
        desk.gates = desk.dispatcher.gates = self.board
        self.channel = PatientChannel()
        self.companion = Companion(km, demo_lab_reports={pid: p["lab"] for pid, p in PATIENTS.items()},
                                   history_provider=lambda _p: {"rcc": True})
        self.briefer = CaseBriefer(now=desk.clock.now)
        self.verifier = SafetyVerifier(now=desk.clock.now)
        self.rt = {
            "companion": await desk.add_seat(self.companion, companion_tools(km, self.channel,
                                                                              wearables={pid: p["bp"] for pid, p in PATIENTS.items()})),
            "case_briefer": await desk.add_seat(self.briefer, briefer_tools(fhir, km, desk.clock.now,
                                                                            self.board.create_item)),
            "renal_guardian": await desk.add_seat(RenalGuardian(), clinical_tools(fhir, km, desk.clock.now,
                                                                                  {pid: p["bp"] for pid, p in PATIENTS.items()})),
            "safety_verifier": await desk.add_seat(self.verifier, clinical_tools(fhir, km, desk.clock.now)),
        }
        self.reader = None
        if not self.light:
            self._build_memory_seats(desk, km, fhir, root)
            await self._add_memory_seats()
        self.board.decide = lambda pid, rel, items, note: self.briefer.publish_decision(
            self.rt["case_briefer"].context(), patient_id=pid, release=rel, items=items, note=note)
        self._cursor = {"bus": 0, "audit": 0, "out": 0, "pages": 0}
        self._esc_items: dict[str, str] = {}
        self.metrics: dict[str, dict[str, int]] = {}
        self.board.subscribe(lambda e: self.hub.publish("board", e, desk.clock.now()))

    def _build_memory_seats(self, desk: Desk, km: KnowledgeModel, fhir: FolderFhir, root: Path) -> None:
        from kcd.cortex import Cortex, HumanActor, MemoryBlobStore
        from kcd.cortex.auth import release_verifier
        from kcd.genomics.source import FolderPanelSource
        from kcd.imaging.calibration import Calibration
        from kcd.imaging.model import FixedCtModel
        from kcd.imaging.pacs import FolderPacs
        from kcd.seats.archivist import Archivist
        from kcd.seats.genomics_interpreter import GenomicsInterpreter
        from kcd.seats.imaging_reader import ImagingReader
        from kcd.tools import genomics_tools, imaging_tools

        self.cortex = cortex = Cortex("sqlite://", blobs=MemoryBlobStore(),
                                      human_verifier=release_verifier(desk.clinician_keys), clock=desk.clock.now)
        model = self.model or FixedCtModel(V1_CACHED, model_version="stub-v1-cached")
        calibration = self.calibration or (Calibration(2.5, model.model_version)
                                           if isinstance(model, FixedCtModel) else None)
        self.reader = ImagingReader(cortex, calibration, audit=desk.audit, clock=desk.clock)
        self.archivist = Archivist(cortex)
        self.board.confirm_rule_fn = lambda rid, cid, role, rel, _r: cortex.confirm(rid, HumanActor(cid, role, rel))
        self.board.reject_rule_fn = lambda rid, cid, role, rel, why: cortex.reject(rid, HumanActor(cid, role, rel), why)
        add = desk.add_seat
        self._pending = [
            (self.reader, imaging_tools(FolderPacs(root), model), "imaging_reader"),
            (GenomicsInterpreter(cortex), genomics_tools(FolderPanelSource(root), fhir), "genomics_interpreter"),
            (self.archivist, board_tools(self.board.create_item), "archivist"),
        ]
        self._add = add

    async def _add_memory_seats(self) -> None:
        for seat, tools, name in getattr(self, "_pending", []):
            self.rt[name] = await self._add(seat, tools)

    async def close(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        await self.desk.close()
        self._tmp.cleanup()

    async def reset(self) -> None:
        async with self.lock:
            await self.desk.close()
            self._tmp.cleanup()
            await self._build()
            self.running = None
            self.hub.history.clear()
            self.hub.publish("reset")

    # ------------------------------------------------------------------ event pump
    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            with contextlib.suppress(Exception):
                self.sync()

    async def settle(self) -> None:
        await self.desk.bus.drain()
        self.sync()

    def _count(self, seat: str, key: str) -> None:
        self.metrics.setdefault(seat, {}).setdefault(key, 0)
        self.metrics[seat][key] += 1

    def sync(self) -> None:
        d, c, ts = self.desk, self._cursor, self.desk.clock.now()
        for subject, env in d.bus.published[c["bus"]:]:
            if subject == DISPATCHER_INBOX:
                continue
            self._count(env.from_, "messages_out")
            self._count(env.to, "messages_in")
            self.hub.publish("a2a", {"id": str(env.id), "type": str(env.type), "from": env.from_, "to": env.to,
                                     "payload": env.payload, "verified": True, "patient": env.patient_id}, ts)
        c["bus"] = len(d.bus.published)
        for e in d.audit.events[c["audit"]:]:
            if e.action.startswith("tool."):
                self._count(e.actor, "tool_calls")
                self.hub.publish("tool", {"seat": e.actor, "tool": e.action[5:]}, e.ts)
            elif e.action.startswith("pep.") and e.decision == "blocked":
                self._count(e.actor, "refusals")
                self.hub.publish("refusal", {"seat": e.actor, "version": e.manifest_version,
                                             "refusal": e.detail.get("refusal"), "label": e.detail.get("reason"),
                                             "action": e.detail.get("target")}, e.ts)
        c["audit"] = len(d.audit.events)
        for m in self.channel.outbox[c["out"]:]:
            self.hub.publish("patient_out", {"text": m["text"], "patient": m.get("patient_id")}, ts)
        c["out"] = len(self.channel.outbox)
        for target, esc_id in d.pager.pages[c["pages"]:]:
            self._on_page(target, esc_id, ts)
        c["pages"] = len(d.pager.pages)

    def _on_page(self, target: str, esc_id: str, ts: datetime) -> None:
        esc = self.desk.dispatcher.escalations.store.get(esc_id)
        name = self.people[target].name if target in self.people else target
        self.hub.publish("page", {"name": name, "pager": target, "message": "Red flag: acknowledge on the Board"}, ts)
        if esc is None:
            return
        if esc_id in self._esc_items:
            self.board.update_item(self._esc_items[esc_id], {"data": self._esc_data(esc)})
            return
        self._esc_items[esc_id] = self.board.create_item({
            "kind": "escalation", "gate": "urgent_ack", "patient_id": esc.patient_id,
            "title": f"Urgent: red flag for {PATIENTS.get(esc.patient_id, {}).get('name', esc.patient_id)}", "artifact_id": esc_id, "data": self._esc_data(esc)})

    def _esc_data(self, esc: Any) -> dict[str, Any]:
        env = next((e for _, e in self.desk.bus.published if str(e.id) == esc.message_id), None)
        p = env.payload if env else {}
        return {"pattern": p.get("pattern"), "evidence": p.get("evidence", []), "level": esc.level,
                "paged": [self.people[t].name if t in self.people else t for t in esc.paged],
                "unit": esc.unit}

    # ------------------------------------------------------------------ doors
    def _patient(self, patient: str | None) -> str:
        pid = patient or PATIENT
        if pid not in PATIENTS:
            raise ActionError(f"unknown patient {pid!r}")
        return pid

    async def patient_say(self, text: str, features: dict[str, Any] | None = None, patient: str | None = None) -> None:
        pid, text = self._patient(patient), (text or "").strip()
        if not text:
            raise ActionError("empty message")
        async with self.lock:
            self.hub.publish("patient_in", {"text": text, "patient": pid}, self.desk.clock.now())
            await self.companion.on_patient_text(self.rt["companion"].context(), patient_id=pid,
                                                 episode_id=EPISODE, text=text, features=features, channel="app")
            self._humanized(pid)
            await self.settle()

    def _humanized(self, pid: str) -> None:
        dis = self.companion.session(pid).distress
        if dis:
            self.hub.publish("humanized", {**dis, "underreporting": self.companion.session(pid).minimizer,
                                           "patient": pid})

    async def attach_lab(self, patient: str | None = None) -> None:
        pid = self._patient(patient)
        async with self.lock:
            await self.companion.attach_lab_report(self.rt["companion"].context(), patient_id=pid,
                                                   episode_id=EPISODE, ocr_text=json.dumps(PATIENTS[pid]["lab"]))
            await self.settle()

    async def new_study(self, patient: str | None = None) -> None:
        pid = self._patient(patient)
        if self.reader is None:
            raise ActionError("imaging needs the full desk (Cortex); this app runs in light mode")
        async with self.lock:
            await self.reader.process_study(self.rt["imaging_reader"].context(), patient_id=pid,
                                            episode_id=EPISODE)
            await self.settle()

    async def run_step(self, n: int, patient: str | None = None) -> None:
        pid = self._patient(patient)
        if self.running is not None:
            raise ActionError("a step is already running")
        if not 1 <= n <= len(STEPS):
            raise ActionError(f"no step {n}")
        p = PATIENTS[pid]
        self.running, self.running_patient = n, pid
        self.hub.publish("scenario", {"op": "step", "n": n, "patient": pid, "title": STEPS[n - 1]["title"]})
        try:
            if n == 1:
                for t in p["script"]:
                    await self.patient_say(t, p["features"], pid)
            elif n == 2:
                await self.patient_say(p["ibuprofen"], None, pid)
            elif n == 3:
                await self.new_study(pid)
            elif n == 4:
                async with self.lock:
                    await self.briefer.request_genomics(self.rt["case_briefer"].context(), patient_id=pid,
                                                        episode_id=EPISODE)
                    await self.settle()
            elif n == 5:
                async with self.lock:
                    try:
                        await self.briefer.publish_case_pack(self.rt["case_briefer"].context(), patient_id=pid)
                    except ValueError as exc:
                        raise ActionError(f"nothing to put in a case pack yet: {exc}") from exc
                    await self.settle()
            self.hub.publish("scenario", {"op": "done", "n": n, "patient": pid})
        finally:
            self.running = self.running_patient = None

    async def autoplay(self, patient: str | None = None) -> None:
        pid = self._patient(patient)
        for s in STEPS:
            if self.light and s["n"] in (3, 4):
                continue
            await self.run_step(s["n"], pid)

    # ------------------------------------------------------------------ clinicians (Clinical Board)
    async def board_act(self, item: str, action: str, clinician: str, **kw: Any) -> dict[str, Any]:
        from kcd.board import BoardError
        from kcd_core.pep import PolicyViolation

        async with self.lock:
            try:
                if action == "acknowledge":
                    out = await self.board.acknowledge(item, clinician)
                elif action == "review":
                    out = self.board.review(item, kw["gate"], clinician)
                elif action == "sign":
                    dec = await self.board.sign(item, clinician, kw.get("items"), kw.get("note", ""))
                    out = {"decision_id": dec.decision_id}
                elif action == "confirm":
                    out = self.board.confirm_rule(item, clinician)
                elif action == "reject":
                    out = self.board.reject_rule(item, clinician, kw.get("reason", ""))
                else:
                    raise ActionError(f"unknown action {action}")
            except (BoardError, PolicyViolation, KeyError) as exc:
                raise ActionError(str(exc)) from exc
            self.hub.publish("gate", {"item": item, "action": action, "clinician": clinician,
                                      "gate": self.board.get(item).get("gate"), "verified": True})
            await self.settle()
            return out

    # ------------------------------------------------------------------ state for the UI
    def evidence_blob(self, ref: str) -> tuple[bytes, str]:
        if self.cortex is None:
            raise ActionError("no memory in light mode")
        ev_id = ref.removeprefix("cortex:evidence/")
        for pid in PATIENTS:  # evidence ids are global; find the partition that holds it
            try:
                ev = self.cortex.get_evidence(3, pid, ev_id)
            except Exception:  # noqa: BLE001 - not in this partition
                continue
            if not ev.get("blob_sha256"):
                raise ActionError("evidence has no image")
            return (self.cortex.read_blob(3, pid, ev["blob_sha256"]), ev["content"].get("media_type", "image/png"))
        raise ActionError(f"no such evidence {ev_id}")

    def state(self) -> dict[str, Any]:
        reg = self.desk.registry
        seats = []
        for sid in reg.seats():
            m = reg.get(sid)
            seats.append({"id": sid, "manifest": m.model_dump(mode="json"), "metrics": self.metrics.get(sid, {}),
                          "versions": [v.version for v in reg._history.get(sid, [m])]})
        patients = [{"id": pid, "name": p["name"], "initials": p["initials"], "age": _age(p["born"]), "sex": p["sex"],
                     "headline": p["headline"], "story": p["story"], "script": p["script"], "ibuprofen": p["ibuprofen"],
                     "features": p["features"]} for pid, p in PATIENTS.items()]
        return {"patient": PATIENT, "patients": patients, "running_patient": self.running_patient, "light": self.light, "seats": seats, "board": self.board.items,
                "memory": self._memory(), "steps": STEPS, "running": self.running,
                "clinicians": [{"id": c.id, "name": c.name, "role": c.role} for c in self.people.values()],
                "history": list(self.hub.history)[-600:],
                "integrity": {"audit_chain_ok": self.desk.audit.verify_chain(),
                              "dead_letters": len(self.desk.bus.dead_letters)}}

    def _memory(self) -> list[dict[str, Any]]:
        if self.cortex is None:
            return []
        from kcd.cortex import DEFAULT_HOSPITAL

        out = []
        for layer, part in [*((3, pid) for pid in PATIENTS), (2, DEFAULT_HOSPITAL)]:
            ev = [{k: v for k, v in e.items() if k not in ("content", "meta")}
                  for e in self.cortex.list_evidence(layer, part)]
            kn = self.cortex.list_knowledge(layer, part)
            if ev or kn:
                out.append({"layer": layer, "partition": part, "chain": self.cortex.verify_chain(layer, part),
                            "evidence": ev, "knowledge": kn})
        return out
