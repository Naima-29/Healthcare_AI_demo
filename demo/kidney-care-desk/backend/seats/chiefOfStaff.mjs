// CHIEF OF STAFF - the Hospital Director's only door.
// The Director (or a delegated head of department) describes, in plain
// language, what to automate. The Chief of Staff compiles, validates and
// hot-reloads seats, and reports how each one is doing. It never touches
// patients and never takes clinical decisions.
import { amend, compileNew, describeRefusals } from '../compiler.mjs';
import { hub, config } from '../core/hub.mjs';

const IT_HINT = /\b(non|mai|il|la|gli|agente|agenti|elenca|crea|aiuto|deve|della|paziente|medico|sta andando)\b/;

const T = {
  en: {
    phone: "on the patient's phone", edge: 'on the hospital edge node',
    help: 'I can: (1) list the agents; (2) tell you how an agent is doing; (3) add a duty, refusal or gate to an agent, written in plain language; (4) create a new agent from a job description to automate a department. Examples: "Companion: never disclose an imaging result to the patient before the doctor has reviewed it." · "How is the Renal Guardian doing?" · "Create an agent for nephrology phone follow-up: …"',
    allSeats: 'All agents:\n',
    metrics: (m, x) => `• ${m.name} v${m.version}: ${x.messages_in ?? 0} msg in / ${x.messages_out ?? 0} out · ${x.tool_calls ?? 0} tool calls · ${x.refusals ?? 0} refusals enforced · ${x.gates_opened ?? 0} gates opened · ${Number(x.tc ?? 0).toFixed(1)} TC`,
    cannotCompile: (e) => `I cannot compile this agent: ${e}`,
    created: (m, where, refs) => `I have created the agent "${m.name}" (v1, ${where}).\nTools: ${m.tools.join(', ')}\nRefusals (runtime blocks): ${refs}\nHuman gate: ${m.gates.join(', ')}\nIt stays in shadow mode until your team switches it on.`,
    notUnderstood: (n) => `I could not find a duty, refusal or gate to add to ${n} in that sentence. Could you rephrase it? (e.g. "never …", "must be approved by the doctor")`,
    invalid: (e) => `The change does not pass validation: ${e}`,
    newRefusals: 'new refusals', newTools: 'new tools', newGates: 'new gates',
    done: (n, v, parts) => `Done. ${n} is now v${v} (hot-reloaded).\n${parts}\nThe block is enforced at the tool layer, not in a prompt.`,
    whichSeat: 'I did not understand which agent you mean. Write for example "Companion: …" or "help".',
  },
  it: {
    phone: 'sul telefono del paziente', edge: 'sull’edge ospedaliero',
    help: 'Posso: (1) elencare gli agenti; (2) dirle come sta andando un agente; (3) aggiungere un dovere, un rifiuto o un gate a un agente, scritto in linguaggio naturale; (4) creare un nuovo agente da una job description per automatizzare un reparto. Esempi: «Companion: non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.» · «Come sta andando il Renal Guardian?» · «Crea un agente per il follow-up telefonico di nefrologia: …»',
    allSeats: 'Situazione di tutti gli agenti:\n',
    metrics: (m, x) => `• ${m.name} v${m.version}: ${x.messages_in ?? 0} msg in / ${x.messages_out ?? 0} out · ${x.tool_calls ?? 0} tool call · ${x.refusals ?? 0} rifiuti applicati · ${x.gates_opened ?? 0} gate aperti · ${Number(x.tc ?? 0).toFixed(1)} TC`,
    cannotCompile: (e) => `Non riesco a compilare questo agente: ${e}`,
    created: (m, where, refs) => `Ho creato l’agente «${m.name}» (v1, ${where}).\nStrumenti: ${m.tools.join(', ')}\nRifiuti (blocchi a runtime): ${refs}\nGate umano: ${m.gates.join(', ')}\nÈ in modalità ombra finché il suo team non lo attiva.`,
    notUnderstood: (n) => `Non ho trovato nella frase un dovere, un rifiuto o un gate da aggiungere a ${n}. Può riformularla? (es. «non … mai …», «deve essere approvato dal medico»)`,
    invalid: (e) => `La modifica non supera la validazione: ${e}`,
    newRefusals: 'nuovi rifiuti', newTools: 'nuovi strumenti', newGates: 'nuovi gate',
    done: (n, v, parts) => `Fatto. ${n} è ora alla v${v} (ricaricato a caldo).\n${parts}\nIl blocco è applicato a livello di tool, non di prompt.`,
    whichSeat: 'Non ho capito a quale agente si riferisce. Scriva per esempio «Companion: …» oppure «aiuto».',
  },
};

const ALIASES = {
  companion: ['companion', 'compagno', 'patient agent', 'agente del paziente', 'assistente del paziente'],
  dispatcher: ['dispatcher', 'smistatore', 'smistamento'],
  imaging_reader: ['imaging reader', 'lettore di immagini', 'imaging', 'radiolog'],
  genomics_interpreter: ['genomics', 'genomica', 'genetic', 'genetica', 'interprete genomico'],
  renal_guardian: ['renal guardian', 'guardiano renale', 'rene', 'renal', 'nefro'],
  safety_verifier: ['safety verifier', 'verificatore', 'verifier', 'sicurezza'],
  case_briefer: ['case briefer', 'briefer', 'doctor agent', 'agente medico', 'case pack'],
  archivist: ['archivist', 'archivista', 'memoria', 'cortex'],
};

export class ChiefOfStaff {
  constructor(ctx) { this.ctx = ctx; this.id = 'chief_of_staff'; this.log = []; }

  findSeat(text) {
    const t = text.toLowerCase();
    for (const [id, names] of Object.entries(ALIASES)) if (names.some((n) => t.includes(n))) return id;
    for (const id of this.ctx.runtime.ids()) if (t.includes(id.replace(/_/g, ' ')) || t.includes(id)) return id;
    return null;
  }

  async handle(text) {
    hub.publish('director_in', { text });
    const reply = await this.route(text.trim());
    hub.publish('director_out', reply);
    this.log.push({ text, reply });
    return reply;
  }

  async route(text) {
    const t = text.toLowerCase();
    const rt = this.ctx.runtime;
    // Answer in the language the Director writes in (English by default)
    const L = T[IT_HINT.test(t) ? 'it' : (t.length < 12 ? config.lang : 'en')];
    const where = (m) => (m.location === 'patient_edge' ? L.phone : L.edge);

    if (/^(help|aiuto|\?)$/.test(t) || /cosa (puoi|sai) fare|what can you do/.test(t)) return { text: L.help };
    if (/(elenca|lista|list|mostra|show).{0,20}(agent|seat|post)/.test(t) || /^(roster|agenti|agents)$/.test(t)) {
      return { text: rt.ids().map((id) => { const m = rt.manifest(id); return `• ${m.name} (v${m.version}) — ${m.role || ''} — ${where(m)}`; }).join('\n') };
    }
    if (/(come sta andando|come stanno andando|come va|how is|how are|how's|report|performance|prestazion)/.test(t)) {
      const id = this.findSeat(t);
      const ids = id ? [id] : rt.ids();
      const lines = ids.map((sid) => { const st = rt.seat(sid); const m = rt.manifest(sid); return L.metrics(m, st?.metrics || {}); });
      return { text: (id ? '' : L.allSeats) + lines.join('\n') };
    }
    if (/(crea|create|nuovo agente|new agent|automatizz|automate)/.test(t)) {
      const r = await compileNew(text);
      if (!r.valid) return { text: L.cannotCompile(r.errors.slice(0, 3).join('; ')), manifest: r.manifest, valid: false };
      rt.register(r.manifest);
      return { text: L.created(r.manifest, where(r.manifest), describeRefusals(r.manifest.refuses).join('; ')), manifest: r.manifest, valid: true, created: r.manifest.seat };
    }
    // Amend a seat: "<seat>: <instruction>" or a sentence that names a seat
    const id = this.findSeat(t);
    if (id) {
      const instruction = text.includes(':') ? text.slice(text.indexOf(':') + 1) : text;
      const before = rt.manifest(id);
      const r = await amend(before, instruction);
      if (!r.understood) return { text: L.notUnderstood(before.name) };
      if (!r.valid) return { text: L.invalid(r.errors.join('; ')) };
      rt.hotReload(r.manifest);
      const parts = [];
      if (r.diff.refuses.length) parts.push(`${L.newRefusals} → ${describeRefusals(r.diff.refuses).join('; ')}`);
      if (r.diff.tools.length) parts.push(`${L.newTools} → ${r.diff.tools.join(', ')}`);
      if (r.diff.gates.length) parts.push(`${L.newGates} → ${r.diff.gates.join(', ')}`);
      return { text: L.done(before.name, r.manifest.version, parts.join('\n')), diff: r.diff, seat: id, version: r.manifest.version };
    }
    return { text: L.whichSeat };
  }
}
