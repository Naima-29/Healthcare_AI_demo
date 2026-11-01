process.env.DEMO_PACE = '0';
process.env.ESCALATION_SECONDS = '1';
process.env.CORTEX_DIR = 'data/runtime/cortex-test-e2e';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createDesk } from '../backend/desk.mjs';
import { autoplay, runStep, PATIENT } from '../backend/scenario/goldenPath.mjs';
import { hub, sleep } from '../backend/core/hub.mjs';
import { classify } from '../backend/runtime/guard.mjs';

const ev = (kind, pred = () => true) => hub.history.filter((e) => e.kind === kind && pred(e.data));

test('golden path end to end (English): three doors, signed A2A, refusals, gates, memory', async () => {
  process.env.DEMO_LANG = 'en';
  const ctx = createDesk();
  ctx.reset();
  await autoplay(ctx);
  await sleep(200);

  // 1. every A2A message is signed and verified
  const a2a = ev('a2a');
  assert.ok(a2a.length > 30);
  assert.ok(a2a.every((e) => e.data.verified), 'unsigned A2A message found');
  // all ten typed messages were exercised
  for (const t of ['symptom_report', 'red_flag_escalation', 'medication_report', 'data_request', 'data_delivered', 'imaging_signal', 'therapy_conflict', 'case_pack_ready', 'clinician_decision', 'acknowledgment']) {
    assert.ok(a2a.some((e) => e.data.type === t), `missing ${t}`);
  }
  // 2. hub-and-spoke: no seat other than the dispatcher addresses a seat directly
  assert.ok(a2a.every((e) => e.data.from === 'dispatcher' || e.data.to === 'dispatcher'));

  // 3. the Director's Italian refusal was compiled, hot-reloaded and enforced
  assert.equal(ctx.runtime.manifest('companion').version, 2);
  assert.ok(ev('refusal', (d) => d.seat === 'companion' && d.refusal === 'disclose_imaging_before_review').length >= 1);

  // 4. the patient never received an imaging result before the clinician signed
  const decisionAt = ev('gate', (d) => d.kind === 'case_pack')[0].seq;
  const before = hub.history.filter((e) => e.kind === 'patient_out' && e.seq < decisionAt);
  assert.ok(before.every((e) => !classify(e.data.text).includes('imaging_result')));
  assert.ok(before.every((e) => !classify(e.data.text).includes('diagnosis_claim')));

  // 5. red flag: safety script + page + escalation acknowledged
  assert.ok(ev('patient_out', (d) => /112/.test(d.text) && /today/.test(d.text)).length === 1);
  assert.ok(ev('page').length >= 1);
  assert.ok(ev('gate', (d) => d.gate === 'urgent_ack' && d.verified).length === 1);

  // 6. therapy conflict needed fresh data first, then passed the Safety Verifier
  const reqIdx = a2a.findIndex((e) => e.data.type === 'data_request' && e.data.from === 'renal_guardian');
  const conIdx = a2a.findIndex((e) => e.data.type === 'therapy_conflict' && e.data.from === 'renal_guardian');
  assert.ok(reqIdx > -1 && conIdx > reqIdx);
  assert.ok(ev('verification', (d) => d.type === 'therapy_conflict' && d.passed).length === 1);
  assert.ok(ev('verification', (d) => d.type === 'imaging_signal' && d.passed).length === 1);

  // 7. signed decision reached the patient; rules confirmed by a human; chain intact
  assert.ok(ev('patient_out', (d) => /reviewed your case/.test(d.text)).length === 1);
  const k = ctx.cortex.confirmed(`patient-${PATIENT}`);
  assert.ok(k.some((x) => x.key === 'nsaid_avoid' && x.confirmer === 'Dr. A. Rossi'));
  assert.ok(ctx.cortex.verifyChain(`patient-${PATIENT}`).ok);
  // and the next episode uses it
  assert.ok(ev('patient_out', (d) => /nephrologist .* asked you to avoid ibuprofen/.test(d.text)).length === 1);
  for (const id of ctx.runtime.ids()) assert.equal(ctx.runtime.seat(id).metrics.errors, 0, `${id} had errors`);
});

test('golden path in Italian (DEMO_LANG=it): same flow, Italian patient and Director', async () => {
  process.env.DEMO_LANG = 'it';
  const ctx = createDesk();
  ctx.reset();
  await autoplay(ctx);
  await sleep(200);
  assert.ok(ev('director_out', (d) => /Fatto\. Companion è ora alla v2/.test(d.text)).length === 1);
  assert.ok(ev('refusal', (d) => d.refusal === 'disclose_imaging_before_review').length >= 1);
  assert.ok(ev('patient_out', (d) => /oggi stesso/.test(d.text) && /112/.test(d.text)).length === 1);
  assert.ok(ev('patient_out', (d) => /ha rivisto il suo caso/.test(d.text)).length === 1);
  assert.ok(ev('patient_out', (d) => /nefrologo .* evitare l’ibuprofene/.test(d.text)).length === 1);
  for (const id of ctx.runtime.ids()) assert.equal(ctx.runtime.seat(id).metrics.errors, 0, `${id} had errors`);
  process.env.DEMO_LANG = 'en';
});

test('escalation ladder pages the next clinician when nobody acknowledges', async () => {
  const ctx = createDesk();
  ctx.reset();
  await runStep(ctx, 2);
  await runStep(ctx, 3);
  await sleep(1400);
  const pages = ev('page').map((e) => e.data.name);
  assert.deepEqual(pages.slice(0, 2), ['Dr. L. Bianchi', 'Dr. S. Marino']);
});

test('security: seats cannot bypass the Dispatcher or call tools outside their manifest', async () => {
  const ctx = createDesk();
  ctx.reset();
  const companion = ctx.runtime.seat('companion');
  await assert.rejects(() => ctx.bus.send('companion', { type: 'medication_report', to: 'renal_guardian', patient: PATIENT, payload: { drug: 'x', class: 'NSAID' } }), /only address the Dispatcher/);
  await assert.rejects(() => companion.tool('fhir.get_observations', { patient: PATIENT, code: 'eGFR' }), /tool_not_in_manifest/);
  const imaging = ctx.runtime.seat('imaging_reader');
  assert.throws(() => imaging.act('patient.send'), /report_to_patient/);
  // a forged message (bad signature) is rejected by the Dispatcher
  const forged = { id: 'msg-forged-1', type: 'red_flag_escalation', from: 'companion', to: 'dispatcher', ts: new Date().toISOString(), patient: PATIENT, payload: { urgency: 'high', pattern: 'x', evidence: [] }, alg: 'Ed25519', kid: 'companion', sig: 'AAAA' };
  await ctx.runtime.seat('dispatcher').receive(forged);
  assert.ok(ev('a2a_rejected', (d) => d.id === 'msg-forged-1').length === 1);
});
