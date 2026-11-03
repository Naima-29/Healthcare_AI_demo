// CONTRACT 2 - The ten typed clinical A2A messages, and how they are signed.
// Envelope is signed with Ed25519 over a canonical JSON serialisation
// (sorted keys, no whitespace - JCS-like). Each seat holds its own key pair;
// the Dispatcher verifies every inbound message before routing it.
import crypto from 'node:crypto';
import { A2A_TYPES } from './catalog.mjs';
import { validate } from './validate.mjs';

const str = { type: 'string' };
const num = { type: 'number' };
const arr = (items = {}) => ({ type: 'array', items });

export const PayloadSchemas = {
  symptom_report: {
    type: 'object', required: ['symptoms', 'distress'],
    properties: {
      symptoms: arr({ type: 'object', required: ['code', 'label'] }),
      distress: { type: 'object', required: ['score', 'level'], properties: { score: num, level: str } },
      underreporting: { type: 'boolean' },
      transcript: str,
    },
  },
  red_flag_escalation: {
    type: 'object', required: ['urgency', 'pattern', 'evidence'],
    properties: { urgency: { enum: ['high', 'medium'] }, pattern: str, evidence: arr(str), safety_script_sent: { type: 'boolean' } },
  },
  medication_report: {
    type: 'object', required: ['drug', 'class'],
    properties: { drug: str, class: str, dose_text: str, reason: str },
  },
  data_request: {
    type: 'object', required: ['what', 'why'],
    properties: { what: str, why: str, ask_patient: str },
  },
  data_delivered: {
    type: 'object', required: ['kind', 'data'],
    properties: { kind: str, data: { type: 'object' }, evidence_id: str },
  },
  imaging_signal: {
    type: 'object', required: ['label', 'p_calibrated', 'quality', 'provenance'],
    properties: {
      label: str, p_raw: num, p_calibrated: num, cam_metrics: { type: 'object' },
      quality: { type: 'object' }, provenance: { type: 'object' }, heatmap_ref: str, note: str,
    },
  },
  therapy_conflict: {
    type: 'object', required: ['drug', 'conditions', 'severity', 'citations'],
    properties: { drug: str, conditions: arr(str), severity: str, rationale: str, citations: arr(str), data_freshness: { type: 'object' } },
  },
  case_pack_ready: {
    type: 'object', required: ['case_id', 'sbar'],
    properties: { case_id: str, sbar: { type: 'object', required: ['S', 'B', 'A', 'R'] }, gates: arr(str) },
  },
  clinician_decision: {
    type: 'object', required: ['case_id', 'signed_by', 'items'],
    properties: {
      case_id: str, signed_by: str, role: str, note: str,
      items: arr({ type: 'object', required: ['code', 'patient_text'] }),
    },
  },
  acknowledgment: {
    type: 'object', required: ['ack_of'],
    properties: { ack_of: str, status: { enum: ['received', 'routed', 'rejected'] }, reason: str },
  },
};

export const EnvelopeSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['id', 'type', 'from', 'to', 'ts', 'patient', 'payload'],
  properties: {
    id: { type: 'string', pattern: '^msg-' },
    type: { enum: A2A_TYPES },
    from: str,
    to: str,
    ts: str,
    patient: str,
    correlation: str,
    payload: { type: 'object' },
    alg: { const: 'Ed25519' },
    kid: str,
    sig: str,
  },
};

export function validateMessage(msg) {
  const r1 = validate(EnvelopeSchema, msg);
  if (!r1.ok) return r1;
  const r2 = validate(PayloadSchemas[msg.type], msg.payload, '$.payload');
  return r2;
}

// ---- canonical JSON + signing --------------------------------------------
export function canonical(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  return '{' + Object.keys(v).sort().filter((k) => v[k] !== undefined)
    .map((k) => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
}

export class KeyRing {
  constructor() { this.keys = new Map(); }
  ensure(kid) {
    if (!this.keys.has(kid)) {
      const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
      this.keys.set(kid, { publicKey, privateKey });
    }
    return this.keys.get(kid);
  }
  publicJwk(kid) { return this.ensure(kid).publicKey.export({ format: 'jwk' }); }
  sign(msg) {
    const { privateKey } = this.ensure(msg.from);
    const body = { ...msg, alg: 'Ed25519', kid: msg.from };
    delete body.sig;
    const sig = crypto.sign(null, Buffer.from(canonical(body)), privateKey).toString('base64url');
    return { ...body, sig };
  }
  verify(msg) {
    if (!msg.sig || !msg.kid) return false;
    const entry = this.keys.get(msg.kid);
    if (!entry || msg.kid !== msg.from) return false;
    const body = { ...msg };
    delete body.sig;
    try {
      return crypto.verify(null, Buffer.from(canonical(body)), entry.publicKey, Buffer.from(msg.sig, 'base64url'));
    } catch { return false; }
  }
}

let seq = 0;
export function newMessage({ type, from, to, patient, payload, correlation }) {
  seq += 1;
  return {
    id: `msg-${Date.now().toString(36)}-${seq}`,
    type, from, to, patient, payload,
    ts: new Date().toISOString(),
    ...(correlation ? { correlation } : {}),
  };
}
