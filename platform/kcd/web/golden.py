"""The golden path as five steps, for any patient, through the doors of Chrislin's frontend.

Each step only acts at a door: the patient's phone, a new CT in PACS, the Case Briefer asked for the genomics,
the case pack put on the Board. The clinician then reviews and signs on the Board. Patients, scripts and steps
are those of Chrislin's desk (kcd/web/app.py in PR #9).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from kcd.demo import DEMO_NOW
from kcd.web.session import DeskError, Session

PAIN = {"au": {"AU4": 3, "AU6": 3, "AU7": 2, "AU9": 2, "AU10": 1, "AU43": 1},
        "voice": {"tremor": 0.7, "pitch_var": 0.6, "rate_dev": -0.4}}
DULL = {"au": {"AU4": 1, "AU6": 1, "AU7": 1, "AU9": 0, "AU10": 0, "AU43": 1},
        "voice": {"tremor": 0.25, "pitch_var": 0.3, "rate_dev": -0.15}}

# Who each patient is, what they write, and the CT the desk reads in step 3.
PATIENTS: dict[str, dict[str, Any]] = {
    "marco-r": {
        "name": "Marco R.", "initials": "MR", "born": "1978-03-14", "sex": "male",
        "headline": "Kidney cancer follow-up, CKD stage 3",
        "story": "Red-flag case: blood in the urine, severe flank pain, weight loss, then ibuprofen.",
        "script": [
            "Good evening… I have a pain in my right side, under the ribs. Nothing serious, but it won’t go away and "
            "it gets worse at night.",
            "My urine is pinkish, sometimes really dark red.",
            "I’ve lost 23 kg in the last three months and sometimes I feel nauseous.",
            "I’d say 8 out of 10. And I often feel dizzy.",
        ],
        "ibuprofen": "For the pain I’m taking ibuprofen 600, twice a day.",
        "features": PAIN, "study": None,
    },
    "sofia-b": {
        "name": "Sofia B.", "initials": "SB", "born": "1969-07-02", "sex": "female",
        "headline": "Kidney cancer with a VHL variant, CKD stage 3",
        "story": "Quieter case: a dull ache and tiredness, no blood. Same conflict check, a VHL finding, no red flag.",
        "script": [
            "Hello, I have had a dull ache in my left side for about two weeks.",
            "I feel very tired lately, and my blood pressure at home has been high.",
            "No blood in the urine, thankfully. The pain is about 5 out of 10.",
        ],
        "ibuprofen": "For the ache I take ibuprofen 400 when it gets bad.",
        "features": DULL, "study": "2.25.sofiab.04",  # her follow-up CT (the other 10 are edge cases)
    },
}
STEPS = [
    {"n": 1, "short": "Symptoms", "title": "The patient writes to the Companion about their symptoms", "door": "patient"},
    {"n": 2, "short": "Ibuprofen", "title": "They mention ibuprofen: stale eGFR, lab photo, drug-disease conflict",
     "door": "patient"},
    {"n": 3, "short": "CT", "title": "A new CT reaches PACS: Imaging Reader, Safety Verifier", "door": "system"},
    {"n": 4, "short": "Genomics", "title": "The Case Briefer asks for the genomic context", "door": "clinician"},
    {"n": 5, "short": "Case pack", "title": "Case pack on the Board: review, edit, sign", "door": "clinician"},
]


def _age(born: str, today: datetime = DEMO_NOW) -> int:
    b = datetime.fromisoformat(born)
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def patients_view() -> list[dict[str, Any]]:
    return [{"id": pid, "age": _age(p["born"]), **{k: v for k, v in p.items() if k not in ("born", "study")}}
            for pid, p in PATIENTS.items()]


async def run_step(session: Session, n: int, patient: str | None = None, *, pace: float = 0.7) -> None:
    step = next((s for s in STEPS if s["n"] == n), None)
    if step is None:
        raise ValueError(f"no golden path step {n}")
    pid = session._patient(patient)  # noqa: SLF001 - the same validation as every door
    p = PATIENTS[pid]
    session.running = (n, pid)
    session.hub.publish("scenario", {"op": "step", "n": n, "patient": pid, "title": step["title"]})
    try:
        if n == 1:
            for text in p["script"]:
                await session.patient_message(text, p["features"], pid)
                await asyncio.sleep(pace)
        elif n == 2:
            await session.patient_message(p["ibuprofen"], None, pid)
            if pid in session.companion.awaiting_lab:  # the Renal Guardian asked for a recent lab report
                await asyncio.sleep(pace)
                await session.attach_lab(patient=pid)
        elif n == 3:
            await session.read_study(p["study"], pid)
        elif n == 4:
            await session.briefer.request_genomics(session._ctx("case_briefer"), patient_id=pid,  # noqa: SLF001
                                                   episode_id=session.companion.episode(pid))
            await session.drain()
        elif n == 5:
            try:
                await session.briefer.publish_case_pack(session._ctx("case_briefer"), patient_id=pid)  # noqa: SLF001
            except ValueError as e:
                raise DeskError(f"nothing to put in a case pack yet: {e}") from e
            await session.drain()
        session.done_steps.add((pid, n))
        session.hub.publish("scenario", {"op": "done", "n": n, "patient": pid})
    finally:
        session.running = None


async def autoplay(session: Session, patient: str | None = None, *, pace: float = 0.7, gap: float = 1.0) -> None:
    for step in STEPS:
        await run_step(session, step["n"], patient, pace=pace)
        await asyncio.sleep(gap)
