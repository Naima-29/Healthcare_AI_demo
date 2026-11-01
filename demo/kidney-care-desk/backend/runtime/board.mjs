// Clinical Board - the clinicians' door. Every human gate lives here:
// urgent acknowledgements, imaging review, genetic disclosure, the signed
// decision on a case pack, rule confirmations. Human actions are signed with
// the clinician's own Ed25519 key (same KeyRing as the seats).
import { hub, uid } from '../core/hub.mjs';
import { GATES } from '../../contracts/catalog.mjs';

export class Board {
  constructor(keyring) {
    this.keyring = keyring;
    this.items = new Map();
    this.releases = new Map(); // decisionId -> release record
  }
  list() { return [...this.items.values()].sort((a, b) => a.created.localeCompare(b.created)); }
  get(id) { return this.items.get(id); }

  create({ kind, gate = 'none', title, patient, case_id, data = {}, by, status }) {
    const item = { id: uid('bi'), kind, gate, gate_label: GATES[gate]?.label, signer_role: GATES[gate]?.signer, title, patient, case_id, data, by, status: status || (gate === 'none' ? 'info' : 'open'), created: new Date().toISOString(), history: [] };
    this.items.set(item.id, item);
    hub.publish('board', { op: 'create', item });
    return item;
  }
  update(id, patch, note) {
    const item = this.items.get(id);
    if (!item) throw new Error('unknown board item');
    Object.assign(item, patch);
    if (note) item.history.push({ ts: new Date().toISOString(), note });
    hub.publish('board', { op: 'update', item });
    return item;
  }
  upsert(match, spec) {
    const found = this.list().find((i) => i.kind === match.kind && i.case_id === match.case_id);
    if (found) return this.update(found.id, { data: spec.data, title: spec.title ?? found.title, version: (found.version || 1) + 1 }, 'updated');
    return this.create({ ...spec, version: 1 });
  }

  // A human acts on a gate. Returns the signed act.
  humanAct(id, { action, clinician, role, payload = {} }) {
    const item = this.items.get(id);
    if (!item) throw new Error('unknown board item');
    if (!clinician) throw new Error('a named clinician is required');
    const kid = `human:${clinician}`;
    const act = { id: uid('act'), item: id, action, clinician, role, payload, ts: new Date().toISOString() };
    const signed = this.keyring.sign({ ...act, from: kid, type: 'human_act' });
    const status = { acknowledge: 'acknowledged', review: 'reviewed', sign: 'signed', confirm: 'confirmed', reject: 'rejected' }[action] || action;
    this.update(id, { status, signed_by: clinician, signed_role: role, signature: signed.sig.slice(0, 24) + '…' }, `${action} by ${clinician}`);
    hub.publish('gate', { item: id, kind: item.kind, gate: item.gate, action, clinician, role, verified: this.keyring.verify(signed) });
    return signed;
  }

  // A signed clinical decision releases patient-facing content classes.
  registerRelease(decisionId, signedAct, classes) {
    const verified = this.keyring.verify(signedAct);
    const rec = { decisionId, verified, classes, clinician: signedAct.clinician };
    this.releases.set(decisionId, rec);
    return rec;
  }
  release(decisionId) { return this.releases.get(decisionId) || null; }
  reset() { this.items.clear(); this.releases.clear(); }
}
