process.env.DEMO_PACE = '0';
process.env.CORTEX_DIR = 'data/runtime/cortex-test-unit';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validateManifest } from '../contracts/manifestSchema.mjs';
import { KeyRing, newMessage, validateMessage } from '../contracts/a2aSchema.mjs';
import { amend, compileNew, ruleRefusals } from '../backend/compiler.mjs';
import { checkAction, checkContent, classify } from '../backend/runtime/guard.mjs';
import { fromFeatures, pspi } from '../backend/humanized/fuzzy.mjs';
import { extract } from '../backend/humanized/nlu.mjs';
import * as km from '../backend/knowledge/km.mjs';
import { Cortex } from '../backend/memory/cortex.mjs';
import { allServers } from '../backend/mcp/servers.mjs';
import { PatientChannel } from '../backend/channel.mjs';
import { gateway } from '../backend/models/gateway.mjs';
import { calibrate } from '../backend/seats/clinical.mjs';

const seed = (id) => JSON.parse(fs.readFileSync(`seats/${id}.manifest.json`, 'utf8'));

test('all eight seed manifests validate against CONTRACT 1', () => {
  const files = fs.readdirSync('seats').filter((f) => f.endsWith('.manifest.json'));
  assert.equal(files.length, 8);
  for (const f of files) {
    const r = validateManifest(JSON.parse(fs.readFileSync(`seats/${f}`, 'utf8')));
    assert.ok(r.ok, `${f}: ${r.errors.join('; ')}`);
  }
});

test('manifest validator rejects unknown refusals, tools without surface, L0 writes', () => {
  const m = seed('companion');
  assert.equal(validateManifest({ ...m, refuses: ['be_nice'] }).ok, false);
  assert.equal(validateManifest({ ...m, tools: [...m.tools, 'pacs.fetch_study'] }).ok, false);
  assert.equal(validateManifest({ ...m, memory: { read: [0], write: [0] } }).ok, false);
});

test('A2A: Ed25519 signature verifies, tampering is detected, schema is enforced', () => {
  const kr = new KeyRing();
  const msg = kr.sign(newMessage({ type: 'medication_report', from: 'companion', to: 'dispatcher', patient: 'p1', payload: { drug: 'ibuprofen', class: 'NSAID' } }));
  assert.ok(kr.verify(msg));
  assert.equal(kr.verify({ ...msg, payload: { ...msg.payload, drug: 'paracetamol' } }), false);
  assert.equal(kr.verify({ ...msg, from: 'dispatcher' }), false);
  assert.equal(validateMessage(newMessage({ type: 'imaging_signal', from: 'x', to: 'y', patient: 'p', payload: { label: 'tumor' } })).ok, false);
});

test('compiler: an English, Italian or French refusal becomes a hard block (the live demo sentence)', async () => {
  const r = await amend(seed('companion'), 'non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.');
  assert.ok(r.valid, r.errors.join(';'));
  assert.deepEqual(r.diff.refuses, ['disclose_imaging_before_review']);
  assert.equal(r.manifest.version, 2);
  assert.ok(ruleRefusals('Never call it a diagnosis.').includes('label_as_diagnosis'));
  const en = await amend(seed('companion'), 'Never disclose an imaging result to the patient before the doctor has reviewed it.');
  assert.deepEqual(en.diff.refuses, ['disclose_imaging_before_review']);
  assert.ok(ruleRefusals('Ne jamais communiquer le résultat de l’imagerie au patient.').includes('disclose_imaging_before_review'));
});

test('compiler: new seats inherit the clinical safety floor', async () => {
  const r = await compileNew('Crea un agente per il follow-up telefonico di nefrologia: chiama via WhatsApp i pazienti con CKD e raccoglie la pressione dagli indossabili. Ogni consiglio deve essere approvato dal nefrologo.');
  assert.ok(r.valid, r.errors.join(';'));
  for (const x of ['diagnose', 'prescribe', 'change_dose']) assert.ok(r.manifest.refuses.includes(x));
  assert.ok(r.manifest.gates.includes('nephrologist_review'));
  assert.ok(r.manifest.tools.includes('wearables.read'));
});

test('guard: actions and content classes are blocked; a verified release unlocks content', () => {
  const m = { ...seed('companion'), refuses: [...seed('companion').refuses, 'disclose_imaging_before_review'] };
  assert.throws(() => checkAction(m, 'patient.disclose_imaging'), /refused/);
  assert.throws(() => checkContent(m, 'La TAC mostra una massa al rene destro.'), /refused/);
  assert.throws(() => checkContent(m, 'Prenda 600 mg di ibuprofene.'), /refused/);
  assert.doesNotThrow(() => checkContent(m, 'Le sue immagini sono in revisione da parte del suo medico.'));
  assert.doesNotThrow(() => checkContent(m, 'La TAC mostra una massa.', { verified: true, classes: ['imaging_result'] }));
  assert.ok(classify('You have a kidney tumour').includes('diagnosis_claim'));
});

test('humanized: PSPI + voice reveal under-reporting behind "niente di che"', () => {
  assert.equal(pspi({ AU4: 3, AU6: 3, AU7: 2, AU9: 2, AU10: 1, AU43: 1 }), 9);
  const n = extract('Ho un dolore al fianco, niente di che.');
  assert.ok(n.minimizer);
  const d = fromFeatures({ verbal: n.verbal, minimizer: n.minimizer, features: { au: { AU4: 4, AU6: 4, AU9: 3, AU43: 1 }, voice: { tremor: 0.9, pitch_var: 0.8, rate_dev: -0.6 } } });
  assert.equal(d.level, 'high');
  assert.ok(d.underreporting);
  const calm = fromFeatures({ verbal: 0.1, features: {} });
  assert.equal(calm.level, 'low');
});

test('NLU extracts the v1 red-flag facts in Italian and English', () => {
  const a = extract('Ho perso 23 kg negli ultimi tre mesi. Direi 8 su 10 e mi gira la testa. Le urine sono rosate.');
  assert.equal(a.weightLossKg, 23); assert.equal(a.painScore, 8);
  assert.ok(a.symptoms.some((s) => s.code === 'visible_hematuria'));
  assert.ok(a.symptoms.some((s) => s.code === 'dizziness'));
  const b = extract('I have pain on my right side and my urine is pink.');
  assert.ok(b.symptoms.some((s) => s.code === 'flank_pain'));
  assert.ok(extract('Ieri ho fatto la TAC: cosa ha mostrato?').intents.includes('ask_imaging_result'));
  assert.equal(extract('My urine is pinkish, sometimes really dark red.').language, 'en');
  assert.equal(extract('Le urine sono rosate.').language, 'it');
});

test('knowledge model: KDIGO staging, NSAID conflicts with citations, red flags, APOL1 single allele', () => {
  assert.equal(km.ckdStage(58).stage, 'G3a');
  assert.equal(km.ckdStage(29).stage, 'G4');
  const c = km.checkConflicts({ drugClass: 'NSAID', egfr: 56, sbp: 156, conditions: ['Essential (primary) hypertension'], medications: ['Sunitinib (TKI)'] });
  assert.equal(c[0].severity, 'high'); assert.ok(c[0].citations.length >= 2);
  assert.equal(km.checkConflicts({ drugClass: 'non_NSAID_analgesic', egfr: 56 }).length, 0);
  const rf = km.redFlags({ symptoms: [{ code: 'visible_hematuria' }, { code: 'flank_pain' }, { code: 'dizziness' }], painScore: 8, weightLossKg: 23, history: { rcc: true } });
  assert.equal(rf.urgency, 'high');
  assert.equal(km.interpretVariant({ gene: 'APOL1', clinical_significance: 'risk_allele' }).relevance, 'low');
});

test('cortex: hash chain detects tampering; rules need evidence and a human confirmer', () => {
  const c = new Cortex('data/runtime/cortex-test-unit');
  c.wipeAll();
  const e1 = c.appendEvidence('p', { kind: 'k', source: 's', content: { a: 1 } });
  c.appendEvidence('p', { kind: 'k', source: 's', content: { a: 2 } });
  assert.ok(c.verifyChain('p').ok);
  assert.throws(() => c.propose('p', { layer: 3, key: 'x', value: 'y', source: 's', evidence: [] }), /store_without_evidence/);
  const k = c.propose('p', { layer: 3, key: 'x', value: 'y', source: 's', evidence: [e1.id] });
  assert.equal(k.status, 'shadow');
  assert.throws(() => c.confirm('p', k.id, { confirmer: 'seat:archivist' }), /auto_confirm_rule/);
  assert.equal(c.confirm('p', k.id, { confirmer: 'Dr. A. Rossi' }).status, 'confirmed');
  const f = 'data/runtime/cortex-test-unit/p/evidence.jsonl';
  const lines = fs.readFileSync(f, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  lines[0].content_hash = 'deadbeef';
  fs.writeFileSync(f, lines.map((l) => JSON.stringify(l)).join('\n') + '\n');
  assert.equal(c.verifyChain('p').ok, false);
  c.erase('p');
  assert.equal(c.evidence('p').length, 0);
});

test('MCP servers speak JSON-RPC 2.0 (initialize, tools/list, tools/call)', async () => {
  const servers = allServers({ cortex: new Cortex('data/runtime/cortex-test-unit'), channel: new PatientChannel() });
  const fhir = servers.find((s) => s.name === 'fhir');
  const init = await fhir.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  assert.equal(init.result.serverInfo.name, 'fhir');
  const list = await fhir.handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  assert.ok(list.result.tools.some((t) => t.name === 'fhir.get_observations'));
  const call = await fhir.handle({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'fhir.get_observations', arguments: { patient: 'marco-r', code: 'eGFR' } } });
  assert.equal(call.result.structuredContent.latest.value, 58);
  assert.ok(call.result.structuredContent.age_days > 90);
  const lab = servers.find((s) => s.name === 'lab');
  const parsed = await lab.handle({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'lab.parse_report', arguments: { ocr_text: fs.readFileSync('data/marco-r/lab-report-photo.txt', 'utf8') } } });
  assert.equal(parsed.result.structuredContent.egfr, 56);
  assert.equal(parsed.result.structuredContent.creatinine_mg_dl, 1.46);
  assert.equal(parsed.result.structuredContent.date, '2026-10-05');
  const bad = await fhir.handle({ jsonrpc: '2.0', id: 5, method: 'nope' });
  assert.equal(bad.error.code, -32601);
});

test('model gateway: cloud calls without a de-identification stamp are refused', async () => {
  await assert.rejects(() => gateway.cloud([{ role: 'user', content: 'Marco R.' }], null), /transmit_identifiable_external/);
  assert.equal(await gateway.edge([{ role: 'user', content: 'hi' }]), null); // no edge model configured -> rules path
});

test('imaging calibration lowers over-confident raw scores', () => {
  assert.ok(calibrate(0.9999) < 0.99);
  assert.ok(calibrate(0.9999) > 0.9);
});
