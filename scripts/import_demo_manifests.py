"""Import the demo's 8 seat manifests and job descriptions into seats/.

The demo's manifests are the clinically-sourced seeds. This script keeps them as they are and adds only what
Core needs:

- `a2a_peers`, `consumes`, `emits` from ROUTES below. In the demo every seat addresses only the Dispatcher and
  the Dispatcher routes with a hard-coded switch; in Core the sender names the receiver and the Dispatcher
  checks the route against both manifests.
- OVERRIDES: deliberate, reviewed differences from the demo seeds.

Run after the demo seeds change, then run the tests:  python scripts/import_demo_manifests.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_SEATS = ROOT / "demo" / "kidney-care-desk" / "seats"
OUT_MANIFESTS = ROOT / "seats" / "manifests"
OUT_JOBS = ROOT / "seats" / "jobs"

DEMO_SEAT_IDS = [
    "companion", "dispatcher", "imaging_reader", "genomics_interpreter",
    "renal_guardian", "safety_verifier", "case_briefer", "archivist",
]

# sender -> message type -> receivers. Taken from the demo Dispatcher's switch (backend/seats/dispatcher.mjs),
# with one deliberate change: genomic context also goes through the Safety Verifier, like every clinical output.
ROUTES: dict[str, dict[str, list[str]]] = {
    "companion": {
        "symptom_report": ["case_briefer"],
        "red_flag_escalation": ["case_briefer", "archivist"],
        "medication_report": ["renal_guardian"],
        "data_delivered": ["renal_guardian", "archivist"],  # lab report photo (OCR)
    },
    "renal_guardian": {
        "therapy_conflict": ["safety_verifier"],
        "data_request": ["companion"],  # stale eGFR: ask the patient for a recent lab report
    },
    "imaging_reader": {
        "imaging_signal": ["safety_verifier"],
    },
    "genomics_interpreter": {
        "data_delivered": ["safety_verifier"],
    },
    "safety_verifier": {
        "imaging_signal": ["case_briefer"],
        "therapy_conflict": ["case_briefer"],
        "data_delivered": ["case_briefer"],
        "acknowledgment": ["renal_guardian", "imaging_reader", "genomics_interpreter"],  # rejected output
    },
    "case_briefer": {
        "case_pack_ready": ["dispatcher"],  # the Dispatcher opens the clinician's gate
        "clinician_decision": ["companion", "archivist"],
        "data_request": ["genomics_interpreter"],  # genomic context when a case opens
    },
    "dispatcher": {
        "acknowledgment": [s for s in DEMO_SEAT_IDS if s != "dispatcher"],
    },
    "archivist": {},
}

OVERRIDES: dict[str, dict[str, list[str]]] = {
    # In the demo this refusal is added live by the Director (golden path step 1). In the real seed it is
    # there from day one: the Companion must never disclose an imaging result before review.
    "companion": {"refuses+": ["disclose_imaging_before_review"]},
}


def build(demo: dict[str, object]) -> dict[str, object]:
    seat = str(demo["seat"])
    routes = ROUTES[seat]
    consumes = sorted({t for sender, by_type in ROUTES.items() for t, tos in by_type.items() if seat in tos})
    out = dict(demo)
    out["a2a_peers"] = sorted({to for tos in routes.values() for to in tos})
    out["emits"] = sorted(routes)
    out["consumes"] = consumes
    for key, extra in OVERRIDES.get(seat, {}).items():
        field = key.rstrip("+")
        current = list(out.get(field) or [])  # type: ignore[call-overload]
        out[field] = current + [x for x in extra if x not in current]
    return out


def main() -> None:
    OUT_MANIFESTS.mkdir(parents=True, exist_ok=True)
    OUT_JOBS.mkdir(parents=True, exist_ok=True)
    for seat in DEMO_SEAT_IDS:
        demo = json.loads((DEMO_SEATS / f"{seat}.manifest.json").read_text(encoding="utf-8"))
        text = json.dumps(build(demo), indent=2, ensure_ascii=False) + "\n"
        (OUT_MANIFESTS / f"{seat}.manifest.json").write_text(text, encoding="utf-8", newline="\n")
        shutil.copyfile(DEMO_SEATS / f"{seat}.job.md", OUT_JOBS / f"{seat}.job.md")
        print(f"imported {seat}")


if __name__ == "__main__":
    main()
