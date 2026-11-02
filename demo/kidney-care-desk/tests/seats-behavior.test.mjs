// Behavior tests for Diana's seats: these run the REAL RenalGuardian and
// SafetyVerifier code from backend/seats/clinical.mjs.
// We replace the outside world (patient record, wearables, message bus) with
// small fakes, so we control exactly what goes in and can see exactly what comes out.
// The Knowledge Model is the REAL one (rules.json).
process.env.DEMO_PACE = '0';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { RenalGuardian, SafetyVerifier } from '../backend/seats/clinical.mjs';
import { config } from '../backend/core/hub.mjs';
import * as km from '../backend/knowledge/km.mjs';

const manifest = (id) => JSON.parse(fs.readFileSync(`seats/${id}.manifest.json`, 'utf8'));

// A date "n days before the demo's today"
const daysAgo = (n) => new Date(config.now().getTime() - n * 86400000).toISOString().slice(0, 10);

// ---- the fake world -------------------------------------------------------
// `patient` describes what the fake record contains. Defaults = Marco's situation.
function makeWorld(seatClass, id, patient = {}) {
  const p = {
    egfrAgeDays: 10, egfr: 56,
    conditions: ['Essential hypertension'], medications: ['Sunitinib 50 mg'], sbp: 150,
    ...patient,
  };
  const sent = [];       // messages the seat sends to the Dispatcher
  const gates = [];      // review tasks the seat opens for a human
  const ctx = {
    runtime: { manifest: () => manifest(id) },
    mcp: {
      serverFor: () => 'fake',
      call: async (name, args) => {
        switch (name) {
          case 'fhir.get_observations': {
            const date = daysAgo(p.egfrAgeDays);
            return { age_days: p.egfrAgeDays, latest: { date, value: p.egfr }, series: [{ date, value: p.egfr }] };
          }
          case 'fhir.get_conditions': return p.conditions.map((display) => ({ display }));
          case 'fhir.get_medications': return p.medications.map((name) => ({ name }));
          case 'wearables.read': return { summary_7d: { sbp: p.sbp } };
          case 'km.ckd_stage': return km.ckdStage(args.egfr);                       // real KM
          case 'km.check_conflicts': return { conflicts: km.checkConflicts(args.ctx) }; // real KM
          case 'km.query': return km.query(args.term);                                   // real KM
          default: throw new Error(`fake world has no tool ${name}`);
        }
      },
    },
    bus: { send: async (from, msg) => { sent.push({ from, ...msg }); } },
    board: { create: (spec) => { gates.push(spec); return spec; } },
    cases: new Map(),
  };
  return { seat: new seatClass(ctx), sent, gates };
}

const ibuprofenReport = { id: 'm1', type: 'medication_report', patient: 'marco-r', payload: { drug: 'ibuprofen', class: 'NSAID' } };

// ---------------------------------------------------------------- Renal Guardian

test('Renal Guardian: Marco takes ibuprofen with eGFR 56 -> a high-severity conflict with sources', async () => {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian');
  await seat.onMessage(ibuprofenReport);
  assert.equal(sent.length, 1);
  const m = sent[0];
  assert.equal(m.type, 'therapy_conflict');
  assert.equal(m.to, 'dispatcher');
  assert.equal(m.payload.severity, 'high');                       // eGFR + blood pressure + TKI
  assert.ok(m.payload.citations.length >= 1, 'every conflict must carry sources');
  assert.equal(m.payload.data_freshness.stage, 'G3a');            // eGFR 56
  assert.equal(m.payload.data_freshness.egfr, 56);
});

test('Renal Guardian: kidney result older than the limit -> asks for fresh data and concludes nothing', async () => {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian', { egfrAgeDays: config.staleDays + 32 });
  await seat.onMessage(ibuprofenReport);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'data_request');
  assert.ok(!sent.some((m) => m.type === 'therapy_conflict'), 'no conclusion on stale data');
});

test('Renal Guardian: after the fresh lab arrives, it finishes the check and sends the conflict', async () => {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian', { egfrAgeDays: config.staleDays + 32 });
  await seat.onMessage(ibuprofenReport);                           // -> data_request
  await seat.onMessage({ id: 'd1', type: 'data_delivered', patient: 'marco-r',
    payload: { kind: 'lab', data: { egfr: 56, creatinine_mg_dl: 1.46, date: daysAgo(0), source: 'lab photo' } } });
  assert.deepEqual(sent.map((m) => m.type), ['data_request', 'therapy_conflict']);
});

test('Renal Guardian: healthy kidneys, normal blood pressure, no risky drugs -> no conflict sent', async () => {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian',
    { egfr: 95, conditions: [], medications: [], sbp: 120 });
  await seat.onMessage(ibuprofenReport);
  assert.equal(sent.length, 0);
});

test('Renal Guardian: a drug outside the NSAID group (paracetamol) -> no conflict sent', async () => {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian');
  await seat.onMessage({ ...ibuprofenReport, payload: { drug: 'paracetamol', class: 'non_NSAID_analgesic' } });
  assert.equal(sent.length, 0);
});

// ---------------------------------------------------------------- Safety Verifier

// A good conflict message, exactly like the Renal Guardian produces
async function goodConflict() {
  const { seat, sent } = makeWorld(RenalGuardian, 'renal_guardian');
  await seat.onMessage(ibuprofenReport);
  return sent[0];
}
const asIncoming = (m) => ({ id: 'x1', type: m.type, patient: m.patient, payload: m.payload });

test('Safety Verifier: passes a correct conflict and stamps it as verified', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  await seat.onMessage(asIncoming(await goodConflict()));
  assert.equal(gates.length, 0);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].payload.verification.passed, true);
});

test('Safety Verifier: blocks a conflict with no sources and opens a review instead', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  await seat.onMessage(asIncoming({ ...m, payload: { ...m.payload, citations: [] } }));
  assert.equal(sent.length, 0, 'a failed check must not be passed on');
  assert.equal(gates.length, 1);
});

test('Safety Verifier: blocks an implausible eGFR', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  const payload = { ...m.payload, data_freshness: { ...m.payload.data_freshness, egfr: 400 } };
  await seat.onMessage(asIncoming({ ...m, payload }));
  assert.equal(sent.length, 0);
  assert.equal(gates.length, 1);
});

test('Safety Verifier: blocks a conflict based on old kidney data', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  const payload = { ...m.payload, data_freshness: { ...m.payload.data_freshness, date: daysAgo(config.staleDays + 30) } };
  await seat.onMessage(asIncoming({ ...m, payload }));
  assert.equal(sent.length, 0);
  assert.equal(gates.length, 1);
});

test('Safety Verifier: ignores message types it does not check', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  await seat.onMessage({ id: 'x2', type: 'data_request', patient: 'marco-r', payload: { what: 'x', why: 'y' } });
  assert.equal(sent.length + gates.length, 0);
});

test('Safety Verifier: rejects a citation that is not in the Knowledge Model', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  await seat.onMessage(asIncoming({ ...m, payload: { ...m.payload, citations: ['Made-up source from nowhere'] } }));
  assert.equal(sent.length, 0, 'a made-up source must not pass');
  assert.equal(gates.length, 1);
});

test('Safety Verifier: rejects a citation that only partly matches a real source', async () => {
  const { seat, sent, gates } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  await seat.onMessage(asIncoming({ ...m, payload: { ...m.payload, citations: ['KDIGO'] } }));
  assert.equal(sent.length, 0);
  assert.equal(gates.length, 1);
});

test('Safety Verifier: one made-up citation among real ones is enough to block', async () => {
  const { seat, sent } = makeWorld(SafetyVerifier, 'safety_verifier');
  const m = await goodConflict();
  await seat.onMessage(asIncoming({ ...m, payload: { ...m.payload, citations: [...m.payload.citations, 'Invented guideline 2099'] } }));
  assert.equal(sent.length, 0);
});
