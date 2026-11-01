// Job Card Compiler - the Chief of Staff's core skill.
// Natural-language job description (Italian / English / French) -> agent
// manifest (validated) -> hot-reload. Two passes:
//   1. Rule pass (deterministic, always on): refusals, gates, tools, location.
//      Refusals found by rules are a SAFETY FLOOR: an SLM can add, never drop.
//   2. SLM pass (optional, EDGE_LLM_*): proposes extra skills/tools as JSON;
//      merged only if the result still validates.
import { validateManifest } from '../contracts/manifestSchema.mjs';
import { REFUSALS, SURFACES } from '../contracts/catalog.mjs';
import { gateway } from './models/gateway.mjs';

const NEG = '(?:non|mai|never|do not|don\'t|must not|jamais|ne\\s+\\w+\\s+pas)';
const R = (body) => new RegExp(body, 'i');

export const REFUSAL_PATTERNS = [
  [R(`${NEG}[^.]{0,60}(comunic|dire|rivel|riferi|disclos|tell|report|share|condivid|communiqu)[^.]{0,80}(imaging|\\btac\\b|\\bct\\b|immagin|radiolog|esame strumentale|scan|imagerie)`), 'disclose_imaging_before_review'],
  [R(`${NEG}[^.]{0,30}(diagnos)`), 'diagnose'],
  [R(`${NEG}[^.]{0,30}(prescriv|prescrib|prescri)`), 'prescribe'],
  [R(`${NEG}[^.]{0,40}(modific|cambi|change|stop|sospend|interromp|aument|ridu|alter)[^.]{0,20}(dos|terapi|farmac|medic|dose)`), 'change_dose'],
  [R(`${NEG}[^.]{0,60}(genetic|genetich|dna|varian)[^.]{0,40}(pazient|patient)|${NEG}[^.]{0,30}(disclos|comunic|rivel)[^.]{0,40}(genetic|genetich|dna)`), 'disclose_genetics_to_patient'],
  [R(`${NEG}[^.]{0,40}(contatt|contact|scriv)[^.]{0,30}(medic|clinic|doctor|staff)[^.]{0,20}(diretta|directly)`), 'contact_clinician_directly'],
  [R(`${NEG}[^.]{0,30}(rispond|answer|répond)[^.]{0,30}(pazient|patient)`), 'answer_patient_directly'],
  [R(`${NEG}[^.]{0,30}(classific|rank|ordin|valut)[^.]{0,30}(medic|clinic|person|people)`), 'rank_clinicians'],
  [R(`${NEG}[^.]{0,40}(call it|chiamar|definir|presentar)[^.]{0,20}(a )?(diagnos)`), 'label_as_diagnosis'],
  [R(`${NEG}[^.]{0,30}(report|riferi|invi|send|mand)[^.]{0,30}(anything )?(al |to the )?(pazient|patient)`), 'send_to_patient'],
  [R(`${NEG}[^.]{0,30}(alter|modific)[^.]{0,20}(image|immagin)`), 'alter_image'],
  [R(`${NEG}[^.]{0,30}(reclassif|riclassific)`), 'reclassify_variants'],
  [R(`${NEG}[^.]{0,30}(pass|lasci\\w* passare|approv)[^.]{0,30}(fail|fallit|non superat)`), 'pass_failed_check'],
  [R(`${NEG}[^.]{0,30}(alter|modific|scriv|write)[^.]{0,30}(record|cartella|fascicolo)`), 'alter_record'],
  [R(`${NEG}[^.]{0,20}(decid|take the decision|prend\\w* la decisione)`), 'decide'],
  [R(`${NEG}[^.]{0,30}(store|salv|memorizz)[^.]{0,30}(without evidence|senza evidenz|senza prov)`), 'store_without_evidence'],
  [R(`${NEG}[^.]{0,30}(auto-?confirm|conferm\\w* da sol|auto-?conferm)`), 'auto_confirm_rule'],
  [R(`${NEG}[^.]{0,60}(general model weights|pesi generali|pesi del modello)`), 'write_general_weights'],
  [R(`${NEG}[^.]{0,60}(identifiable|identificabil)[^.]{0,40}(outside|fuori|esterno|external)`), 'transmit_identifiable_external'],
];

const TOOL_PATTERNS = [
  [/wearable|indossabil|smartwatch|hrv|pressione domiciliare/i, ['wearables.read'], 'wearables'],
  [/telegram|whatsapp|messagg|chat|telefon|phone|patient channel|canale/i, ['channel.send', 'channel.receive'], 'channel'],
  [/\bpacs\b|dicom|\btac\b|\bct\b|imaging|immagin/i, ['pacs.list_studies', 'pacs.fetch_study'], 'pacs'],
  [/classific|heat ?map|mappa di calore|grad-?cam/i, ['ct.classify', 'ct.gradcam_heatmap', 'ct.quality_ood_check'], 'imaging'],
  [/fhir|cartella|record|ehr|fascicolo|egfr|creatinin|farmac|medication/i, ['fhir.get_patient', 'fhir.get_observations', 'fhir.get_conditions', 'fhir.get_medications'], 'fhir'],
  [/laborator|lab report|referto|esami del sangue|blood test/i, ['lab.parse_report'], 'lab'],
  [/genetic|genom|\bdna\b|panel|pannello/i, ['genomics.get_panel'], 'genomics'],
  [/on-?call|reperibil|guardia|cercapersone|page/i, ['oncall.who_is_on_call', 'oncall.page'], 'oncall'],
  [/linee guida|guideline|knowledge|conoscenz|citazion|citation|red.?flag|conflitt|conflict/i, ['km.query', 'km.red_flags', 'km.check_conflicts'], 'km'],
  [/memori|cortex|regol|rule|evidenz|evidence/i, ['cortex.evidence_append', 'cortex.knowledge_propose', 'cortex.knowledge_read'], 'cortex'],
  [/board|bacheca|caso|case pack|sbar/i, ['board.create_item', 'board.update_item'], null],
];

const GATE_PATTERNS = [
  [/radiolog/i, 'radiologist_review'],
  [/genetist|geneticist/i, 'genetic_disclosure'],
  [/nefrolog|nephrolog/i, 'nephrologist_review'],
  [/reperibil|on-?call/i, 'urgent_ack'],
  [/(medico|clinician|doctor|curante)[^.]{0,40}(approv|firm|sign|rivist|review|conferm)|(approv|firm|sign|review|conferm)[^.]{0,40}(medico|clinician|doctor)/i, 'clinical_advice'],
];

const slug = (s) => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 40);

export function ruleRefusals(text) {
  const sentences = text.split(/(?<=[.;!?\n])\s+/);
  const found = new Set();
  for (const s of sentences) for (const [re, tok] of REFUSAL_PATTERNS) if (re.test(s)) found.add(tok);
  return [...found];
}

export function ruleTools(text) {
  const tools = new Set(['a2a.send']);
  const surfaces = new Set();
  for (const [re, ts, surf] of TOOL_PATTERNS) if (re.test(text)) { ts.forEach((t) => tools.add(t)); if (surf) surfaces.add(surf); }
  return { tools: [...tools], surfaces: [...surfaces] };
}

export function ruleGates(text) {
  const g = new Set();
  for (const [re, gate] of GATE_PATTERNS) if (re.test(text)) g.add(gate);
  return [...g];
}

// Amend an existing seat with a new instruction (the live demo moment).
export async function amend(manifest, instruction, { by = 'chief_of_staff' } = {}) {
  const refusals = ruleRefusals(instruction);
  const { tools, surfaces } = ruleTools(instruction);
  const gates = ruleGates(instruction);
  const next = structuredClone(manifest);
  const addedRefusals = refusals.filter((r) => !next.refuses.includes(r));
  next.refuses = [...next.refuses, ...addedRefusals];
  // Only add tools for surfaces explicitly granted by the sentence when it is not a refusal
  const addedTools = [];
  if (!refusals.length) {
    for (const t of tools) if (!next.tools.includes(t) && t !== 'a2a.send') { next.tools.push(t); addedTools.push(t); }
    for (const s of surfaces) if (!next.surfaces.includes(s)) next.surfaces.push(s);
  }
  const addedGates = gates.filter((g) => !next.gates.includes(g) && g !== 'none');
  next.gates = [...next.gates.filter((g) => g !== 'none' || !addedGates.length), ...addedGates];
  next.version = manifest.version + 1;
  next.job_description = `${manifest.job_description.trim()} ${instruction.trim()}`;
  next.compiled_by = by;
  next.compiled_at = new Date().toISOString();
  const v = validateManifest(next);
  return { manifest: next, valid: v.ok, errors: v.errors, diff: { refuses: addedRefusals, tools: addedTools, gates: addedGates }, understood: addedRefusals.length + addedTools.length + addedGates.length > 0 };
}

// Compile a brand-new seat from a job description.
export async function compileNew(text, { name, location } = {}) {
  const nm = name || (text.match(/(?:agente|agent|seat|posto)\s+(?:per|for|chiamato|called|named)?\s*(?:il |la |lo |l'|the )?([^.:,\n]{3,50})/i)?.[1] ?? 'New Seat').trim();
  const loc = location || (/(telefono del paziente|patient'?s phone|a casa|at home|presso il paziente)/i.test(text) ? 'patient_edge' : 'hospital_edge');
  const { tools, surfaces } = ruleTools(text);
  const refuses = ruleRefusals(text);
  // Every new seat inherits the clinical safety floor
  for (const r of ['diagnose', 'prescribe', 'change_dose']) if (!refuses.includes(r)) refuses.push(r);
  if (loc === 'hospital_edge' && !refuses.includes('send_to_patient') && !surfaces.includes('channel')) refuses.push('send_to_patient');
  let gates = ruleGates(text);
  if (!gates.length) gates = ['clinical_advice'];
  const manifest = {
    seat: slug(nm) || 'new_seat', name: nm.replace(/^\w/, (c) => c.toUpperCase()), role: 'Compiled by the Chief of Staff', version: 1,
    location: loc, desk: 'new', model: { edge: loc === 'patient_edge' ? 'loopq-clin-2b' : 'loopq-clin-4b', fallback: null },
    skills: [], tools, surfaces, memory: { read: [0, 1, 3], write: [] }, refuses, gates,
    a2a_peers: ['dispatcher'], artifact: 'task_result', unit: { kind: 'per_task', tc: 2 },
    job_description: text.trim(), compiled_by: 'chief_of_staff', compiled_at: new Date().toISOString(),
  };
  // Optional SLM pass: propose skills only (never refusals removal)
  const out = await gateway.edge([
    { role: 'system', content: 'You turn a clinical job description into a JSON list of short snake_case skills. Reply only {"skills":[...]} with at most 6 items.' },
    { role: 'user', content: text },
  ], { json: true });
  if (out) { try { const j = JSON.parse(out); if (Array.isArray(j.skills)) manifest.skills = j.skills.slice(0, 6).map(slug); } catch {} }
  if (!manifest.skills.length) manifest.skills = guessSkills(text);
  // surfaces must cover tools
  for (const t of manifest.tools) {
    const s = Object.entries(SURFACES).find(([, ts]) => ts.includes(t))?.[0];
    if (s && !manifest.surfaces.includes(s)) manifest.surfaces.push(s);
  }
  const v = validateManifest(manifest);
  return { manifest, valid: v.ok, errors: v.errors };
}

function guessSkills(t) {
  const s = [];
  if (/follow|richiam|chiama|call/i.test(t)) s.push('follow_up_contact');
  if (/promemori|remind/i.test(t)) s.push('reminders');
  if (/questionari|survey|prom/i.test(t)) s.push('patient_reported_outcomes');
  if (/appuntament|agenda|schedul|prenot/i.test(t)) s.push('scheduling');
  if (/aderenz|adherence/i.test(t)) s.push('adherence_support');
  return s.length ? s : ['task_execution'];
}

export function describeRefusals(list) { return list.map((r) => `${r} — ${REFUSALS[r]?.label}`); }
