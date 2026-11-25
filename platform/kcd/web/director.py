"""The Director's door: plain language to the real Chief of Staff compiler (kcd_core.chief_of_staff).

Routing and wording ported from the demo's seats/chiefOfStaff.mjs. kcd_core only drafts a manifest; activating it
is the Director's decision. Here the Director's own sentence is that approval, so the draft goes live at once.
A newly created seat has a manifest but no code behind it: it stays in shadow, and the Roster says so.
"""

from __future__ import annotations

import re
from typing import Any

from kcd_core.chief_of_staff import amend, compile_new, rule_refusals
from kcd_core.registry import ManifestError
from pydantic import ValidationError

from kcd.web.session import Session

_IT = re.compile(r"\b(non|mai|il|la|gli|agente|agenti|elenca|crea|aiuto|deve|della|paziente|medico|sta andando)\b")
ALIASES = {
    "companion": ("companion", "compagno", "patient agent", "agente del paziente"),
    "dispatcher": ("dispatcher", "smistatore"),
    "imaging_reader": ("imaging reader", "lettore di immagini", "imaging", "radiolog"),
    "genomics_interpreter": ("genomics", "genomica", "genetic", "genetica"),
    "renal_guardian": ("renal guardian", "guardiano renale", "renal", "nefro"),
    "safety_verifier": ("safety verifier", "verificatore", "verifier"),
    "case_briefer": ("case briefer", "briefer", "doctor agent", "agente medico"),
    "archivist": ("archivist", "archivista", "cortex"),
}
T = {
    "en": {
        "phone": "on the patient's phone", "edge": "on the hospital edge node",
        "help": 'I can: (1) list the agents; (2) tell you how an agent is doing; (3) add a refusal, tool or gate to an '
                'agent, written in plain language; (4) create a new agent from a job description. Examples: "Case '
                'Briefer: never diagnose a patient." · "How is the Renal Guardian doing?" · "Create an agent for '
                'nephrology phone follow-up: …"',
        "all": "All agents:\n",
        "metrics": "• {name} v{version}: {messages_in} msg in / {messages_out} out · {tool_calls} tool calls · "
                   "{refusals} refusals enforced",
        "cannot": "I cannot compile this agent: {e}",
        "exists": 'An agent called "{name}" already exists. Change it with "{name}: …".',
        "created": 'I have created the agent "{name}" (v1, {where}).\nTools: {tools}\nRefusals (runtime blocks): '
                   "{refuses}\nHuman gate: {gates}\nIt stays in shadow: it has a manifest, and no code runs it yet.",
        "already": "{name} v{version} already enforces this: {refuses}. Nothing to change.",
        "unclear": 'I could not find a duty, refusal or gate to add to {name} in that sentence. Could you rephrase it? '
                   '(e.g. "never …", "must be approved by the doctor")',
        "invalid": "The change does not pass validation: {e}",
        "done": "Done. {name} is now v{version} (hot-reloaded).\n{parts}\nThe block is enforced at the tool layer, not "
                "in a prompt.",
        "refusals": "new refusals", "tools": "new tools", "gates": "new gates",
        "which": 'I did not understand which agent you mean. Write for example "Companion: …" or "help".',
    },
    "it": {
        "phone": "sul telefono del paziente", "edge": "sull’edge ospedaliero",
        "help": "Posso: (1) elencare gli agenti; (2) dirle come sta andando un agente; (3) aggiungere un rifiuto, uno "
                "strumento o un gate a un agente, scritto in linguaggio naturale; (4) creare un nuovo agente da una job "
                "description. Esempi: «Case Briefer: non fare mai diagnosi.» · «Come sta andando il Renal Guardian?» · "
                "«Crea un agente per il follow-up telefonico di nefrologia: …»",
        "all": "Situazione di tutti gli agenti:\n",
        "metrics": "• {name} v{version}: {messages_in} msg in / {messages_out} out · {tool_calls} tool call · "
                   "{refusals} rifiuti applicati",
        "cannot": "Non riesco a compilare questo agente: {e}",
        "exists": "Esiste già un agente chiamato «{name}». Lo modifichi con «{name}: …».",
        "created": "Ho creato l’agente «{name}» (v1, {where}).\nStrumenti: {tools}\nRifiuti (blocchi a runtime): "
                   "{refuses}\nGate umano: {gates}\nResta in modalità ombra: ha un manifest, ma nessun codice lo esegue.",
        "already": "{name} v{version} applica già questa regola: {refuses}. Nulla da cambiare.",
        "unclear": "Non ho trovato nella frase un dovere, un rifiuto o un gate da aggiungere a {name}. Può riformularla? "
                   "(es. «non … mai …», «deve essere approvato dal medico»)",
        "invalid": "La modifica non supera la validazione: {e}",
        "done": "Fatto. {name} è ora alla v{version} (ricaricato a caldo).\n{parts}\nIl blocco è applicato a livello "
                "di tool, non di prompt.",
        "refusals": "nuovi rifiuti", "tools": "nuovi strumenti", "gates": "nuovi gate",
        "which": "Non ho capito a quale agente si riferisce. Scriva per esempio «Companion: …» oppure «aiuto».",
    },
}


def _find_seat(session: Session, text: str) -> str | None:
    for seat, names in ALIASES.items():
        if any(n in text for n in names):
            return seat
    return next((s for s in session.desk.registry.seats()
                 if s != "chief_of_staff" and (s in text or s.replace("_", " ") in text)), None)


def _labels(session: Session, refusals: list[str]) -> str:
    return "; ".join(session.catalog.refusals[r].label for r in refusals) or "-"


def amend_seat(session: Session, seat: str, instruction: str, L: dict[str, str] | None = None) -> dict[str, Any]:
    L = L or T["en"]
    registry = session.desk.registry
    before = registry.get(seat)
    try:
        draft = amend(before, instruction, session.catalog, now=session.clock.now(), by="chief_of_staff (web)")
    except (ManifestError, ValidationError, ValueError) as e:
        return {"text": L["invalid"].format(e=e)}
    if not draft.understood:
        wanted = rule_refusals(instruction)
        if wanted:
            return {"text": L["already"].format(name=before.name, version=before.version,
                                                refuses=_labels(session, wanted))}
        return {"text": L["unclear"].format(name=before.name)}
    registry.activate(draft.manifest)
    parts = []
    if draft.diff.refuses:
        parts.append(f"{L['refusals']} → {_labels(session, draft.diff.refuses)}")
    if draft.diff.tools:
        parts.append(f"{L['tools']} → {', '.join(draft.diff.tools)}")
    if draft.diff.gates:
        parts.append(f"{L['gates']} → {', '.join(draft.diff.gates)}")
    return {"text": L["done"].format(name=before.name, version=draft.manifest.version, parts="\n".join(parts)),
            "diff": {"refuses": draft.diff.refuses, "tools": draft.diff.tools, "gates": draft.diff.gates},
            "seat": seat, "version": draft.manifest.version}


def route(session: Session, text: str) -> dict[str, Any]:
    t = text.lower().strip()
    lang = "it" if _IT.search(t) else (session.lang if len(t) < 12 else "en")
    L = T[lang]
    registry = session.desk.registry
    seats = [s for s in registry.seats() if s != "chief_of_staff"]
    where = lambda m: L["phone"] if m.location == "patient_edge" else L["edge"]  # noqa: E731

    if re.fullmatch(r"help|aiuto|\?", t) or re.search(r"cosa (puoi|sai) fare|what can you do", t):
        return {"text": L["help"]}
    if re.search(r"(elenca|lista|list|mostra|show).{0,20}(agent|seat)", t) or re.fullmatch(r"roster|agenti|agents", t):
        rows = [registry.get(s) for s in seats]
        return {"text": "\n".join(f"• {m.name} (v{m.version}) — {m.role or ''} — {where(m)}" for m in rows)}
    if re.search(r"come sta andando|come stanno andando|come va|how is|how are|how's|report on|performance|prestazion", t):
        seat = _find_seat(session, t)
        lines = [L["metrics"].format(name=registry.get(s).name, version=registry.get(s).version,
                                     **session.metrics.of(s)) for s in ([seat] if seat else seats)]
        return {"text": ("" if seat else L["all"]) + "\n".join(lines)}
    if re.search(r"\b(crea|create)\b|nuovo agente|new agent|automatizz|\bautomate\b", t):
        try:
            draft = compile_new(text, session.catalog, now=session.clock.now())
        except (ManifestError, ValidationError, ValueError) as e:
            return {"text": L["cannot"].format(e=e)}
        m = draft.manifest
        if m.seat in registry.seats():
            return {"text": L["exists"].format(name=registry.get(m.seat).name)}
        registry.activate(m)
        return {"text": L["created"].format(name=m.name, where=where(m), tools=", ".join(m.tools) or "-",
                                            refuses=_labels(session, m.refuses), gates=", ".join(m.gates)),
                "seat": m.seat, "version": m.version}
    seat = _find_seat(session, t)
    if seat:
        instruction = text.split(":", 1)[1] if ":" in text else text
        return amend_seat(session, seat, instruction, L)
    return {"text": L["which"]}


def handle(session: Session, text: str) -> dict[str, Any]:
    session.hub.publish("director_in", {"text": text})
    reply = route(session, text)
    session.hub.publish("director_out", reply)
    return reply
