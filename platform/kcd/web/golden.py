"""The golden path (deck slide 28) as seven steps through the three doors, with Marco R.

Each step only acts at a door: the Director's sentence, the patient's phone, a new CT in PACS, a clinician on the
Board. The seats do the rest. Script ported from the demo's scenario/goldenPath.mjs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kcd.web import director
from kcd.web.session import MARCO, Session

FEAT_PAIN = {"au": {"AU4": 3, "AU6": 3, "AU7": 2, "AU9": 2, "AU10": 1, "AU43": 1},
             "voice": {"tremor": 0.7, "pitch_var": 0.6, "rate_dev": -0.4}}
FEAT_MILD = {"au": {"AU4": 2, "AU6": 2, "AU9": 1, "AU43": 0}, "voice": {"tremor": 0.5, "pitch_var": 0.4, "rate_dev": -0.2}}

SCRIPT = {
    "en": {
        "d1": "Companion: never disclose an imaging result to the patient before the doctor has reviewed it.",
        "d2": "Case Briefer: never diagnose a patient.",
        "s2": "Good evening… I have a pain in my right side, under the ribs. Nothing serious, but it won’t go away and "
              "it gets worse at night.",
        "s3": ("My urine is pinkish, sometimes really dark red.",
               "I’ve lost 23 kg in the last three months and sometimes I feel nauseous.",
               "I’d say 8 out of 10. And I often feel dizzy."),
        "s4": "For the pain I’m taking ibuprofen 600, twice a day.",
        "s5": "I had a CT scan yesterday: what did it show?",
        "s7": "Can I take another ibuprofen tonight? The pain is back.",
    },
    "it": {
        "d1": "Companion: non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia "
              "rivisto.",
        "d2": "Case Briefer: non fare mai diagnosi.",
        "s2": "Buonasera… ho un dolore al fianco destro, sotto le costole. Niente di che, ma non passa e di notte "
              "peggiora.",
        "s3": ("Le urine sono rosate, a volte proprio rosso scuro.",
               "Ho perso 23 kg negli ultimi tre mesi e a volte ho nausea.",
               "Direi 8 su 10. E mi gira spesso la testa."),
        "s4": "Per il dolore sto prendendo ibuprofene 600, due volte al giorno.",
        "s5": "Ieri ho fatto la TAC: cosa ha mostrato?",
        "s7": "Posso prendere un altro ibuprofene stasera? Il dolore è tornato.",
    },
}
STEPS = [
    {"n": 1, "short": "Roster", "door": "director", "title": "Roster: the Director sets rules in plain language"},
    {"n": 2, "short": "At home", "door": "patient", "title": "At home, 21:40: Marco talks to his Companion"},
    {"n": 3, "short": "Red flag", "door": "patient", "title": "Red flag caught on the phone"},
    {"n": 4, "short": "Conflict", "door": "patient", "title": "Therapy conflict: ibuprofen x CKD"},
    {"n": 5, "short": "Imaging", "door": "pacs", "title": "Imaging: CT reaches PACS; Marco asks for the result"},
    {"n": 6, "short": "Sign-off", "door": "clinician", "title": "Sign-off on the Clinical Board"},
    {"n": 7, "short": "Memory", "door": "clinician", "title": "Memory: rules confirmed, next episode smarter"},
]


async def run_step(session: Session, n: int, *, pace: float = 0.7) -> None:
    step = next((s for s in STEPS if s["n"] == n), None)
    if step is None:
        raise ValueError(f"no golden path step {n}")
    if session.active != MARCO:
        session.select_patient(MARCO)
    L = SCRIPT[session.lang]
    session.hub.publish("scenario", {"op": "step", "n": n, "title": step["title"]})

    async def say(text: str, features: dict[str, Any] | None = None) -> None:
        await session.patient_message(text, features, MARCO)
        await asyncio.sleep(pace)

    if n == 1:
        director.handle(session, L["d1"])
        await asyncio.sleep(pace)
        director.handle(session, L["d2"])
    elif n == 2:
        await say(L["s2"], FEAT_PAIN)
    elif n == 3:
        await say(L["s3"][0], FEAT_MILD)
        await say(L["s3"][1], FEAT_MILD)
        await say(L["s3"][2], FEAT_PAIN)
    elif n == 4:
        await say(L["s4"])
        if MARCO in session.companion.awaiting_lab:
            await session.attach_lab(patient=MARCO)
    elif n == 5:
        await session.read_study(patient=MARCO)
        await asyncio.sleep(pace)
        await say(L["s5"])
    elif n == 6:
        await _sign_off(session, pace)
    elif n == 7:
        await _confirm_rules(session)
        await asyncio.sleep(pace)
        await say(L["s7"])
    session.done_steps.add(n)
    session.hub.publish("scenario", {"op": "done", "n": n})


def _open(session: Session, kind: str) -> list[dict[str, Any]]:
    return [i for i in session.board.items if i["patient"] == MARCO and i["kind"] == kind and i["status"] == "open"]


async def _sign_off(session: Session, pace: float) -> None:
    for item in _open(session, "escalation"):
        await session.board_act(item["id"], "acknowledge", item["data"]["paged"]["name"])
    for item in _open(session, "imaging"):
        await session.board_act(item["id"], "review", "Dr. E. Conti")
    await asyncio.sleep(pace)
    for item in _open(session, "case_pack"):
        await session.board_act(item["id"], "sign", "Dr. A. Rossi", items=item["data"]["draft"])


async def _confirm_rules(session: Session) -> None:
    for item in _open(session, "rule"):
        await session.board_act(item["id"], "confirm", "Dr. A. Rossi")


async def autoplay(session: Session, *, pace: float = 0.7, gap: float = 1.2) -> None:
    for step in STEPS:
        await run_step(session, step["n"], pace=pace)
        await asyncio.sleep(gap)
