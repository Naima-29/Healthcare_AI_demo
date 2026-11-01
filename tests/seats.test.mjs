// Tests for Diana's two seats: Renal Guardian and Safety Verifier.
// Each test proves a promise in the seat's job description: what it must NOT do
// is blocked by the runtime, and what it MAY do still works.
process.env.DEMO_PACE = '0';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validateManifest } from '../contracts/manifestSchema.mjs';
import { checkAction, checkContent } from '../backend/runtime/guard.mjs';
import { Seat } from '../backend/runtime/seat.mjs';

// Read a seat's manifest straight from the seats/ folder.
const manifest = (id) => JSON.parse(fs.readFileSync(`seats/${id}.manifest.json`, 'utf8'));

// A tiny fake "world" so we can test a Seat without starting the whole demo.
// It records which tools were really called.
function fakeSeat(id) {
  const calls = [];
  const ctx = {
    runtime: { manifest: () => manifest(id) },
    mcp: { call: async (name) => { calls.push(name); return 'ok'; }, serverFor: () => 'fake' },
  };
  return { seat: new Seat(ctx, id), calls };
}

// ---------------------------------------------------------------- Renal Guardian

test('Renal Guardian: manifest is valid', () => {
  const r = validateManifest(manifest('renal_guardian'));
  assert.ok(r.ok, r.errors?.join('; '));
});

test('Renal Guardian refuses to change a dose, prescribe, message the patient or alter the record', () => {
  const m = manifest('renal_guardian');
  for (const action of ['medication.change_dose', 'medication.prescribe', 'patient.send', 'fhir.write']) {
    assert.throws(() => checkAction(m, action), /refused/, `${action} should be blocked`);
  }
});

test('Renal Guardian cannot put a dose instruction or a prescription in a message', () => {
  const m = manifest('renal_guardian');
  assert.throws(() => checkContent(m, 'Take 600 mg of ibuprofen twice a day.'), /refused/);
  assert.throws(() => checkContent(m, 'I prescribe a new medication for you.'), /refused/);
  // Plain explanatory text is fine.
  assert.doesNotThrow(() => checkContent(m, 'Ibuprofen can be a concern when kidney function is reduced.'));
});

test('Renal Guardian can only use the tools in its manifest', async () => {
  const { seat, calls } = fakeSeat('renal_guardian');
  // Allowed: reading kidney data and asking the Knowledge Model.
  assert.equal(await seat.tool('fhir.get_observations', {}), 'ok');
  assert.equal(await seat.tool('km.ckd_stage', { egfr: 56 }), 'ok');
  // Not allowed: images, genetics, on-call paging.
  for (const tool of ['pacs.fetch_study', 'genomics.get_panel', 'oncall.page', 'channel.send']) {
    await assert.rejects(() => seat.tool(tool, {}), /refused/, `${tool} should be blocked`);
  }
  assert.deepEqual(calls, ['fhir.get_observations', 'km.ckd_stage'], 'blocked tools must never reach the connector');
});

test('Renal Guardian talks only to the Dispatcher and needs a nephrologist to review', () => {
  const m = manifest('renal_guardian');
  assert.deepEqual(m.a2a_peers, ['dispatcher']);
  assert.deepEqual(m.gates, ['nephrologist_review']);
  assert.equal(m.artifact, 'therapy_conflict');
});

// ---------------------------------------------------------------- Safety Verifier

test('Safety Verifier: manifest is valid', () => {
  const r = validateManifest(manifest('safety_verifier'));
  assert.ok(r.ok, r.errors?.join('; '));
});

test('Safety Verifier refuses to pass a failed check, alter a record or send identifiable data out', () => {
  const m = manifest('safety_verifier');
  for (const action of ['verifier.override', 'fhir.write', 'pacs.write', 'llm.external_identifiable']) {
    assert.throws(() => checkAction(m, action), /refused/, `${action} should be blocked`);
  }
});

test('Safety Verifier can only use the Knowledge Model, nothing else', async () => {
  const { seat, calls } = fakeSeat('safety_verifier');
  assert.equal(await seat.tool('km.query', { term: 'kdigo' }), 'ok');
  for (const tool of ['fhir.get_observations', 'fhir.get_medications', 'pacs.fetch_study', 'channel.send']) {
    await assert.rejects(() => seat.tool(tool, {}), /refused/, `${tool} should be blocked`);
  }
  assert.deepEqual(calls, ['km.query']);
});

test('Safety Verifier talks only to the Dispatcher', () => {
  assert.deepEqual(manifest('safety_verifier').a2a_peers, ['dispatcher']);
});

// ---------------------------------------------------------------- Counting refusals

test('every blocked action is counted by the seat', () => {
  const { seat } = fakeSeat('renal_guardian');
  assert.throws(() => seat.act('medication.prescribe'), /refused/);
  assert.throws(() => seat.act('patient.send'), /refused/);
  assert.equal(seat.metrics.refusals, 2);
});
