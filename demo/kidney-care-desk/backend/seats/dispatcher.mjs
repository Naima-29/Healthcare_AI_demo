// DISPATCHER - the A2A endpoint. Verifies, acknowledges and routes every
// typed message; pages the on-call clinician on red flags and runs the
// escalation ladder. Never answers the patient. Never ranks people.
import { Seat } from '../runtime/seat.mjs';
import { validateMessage } from '../../contracts/a2aSchema.mjs';
import { hub, config } from '../core/hub.mjs';

export class Dispatcher extends Seat {
  constructor(ctx) {
    super(ctx, 'dispatcher');
    this.timers = new Map();
    this.opened = new Set();
  }
  reset() { for (const t of this.timers.values()) clearTimeout(t); this.timers.clear(); this.opened.clear(); }

  async onMessage(msg) {
    if (msg.type === 'acknowledgment') return;
    const verified = this.ctx.keyring.verify(msg);
    const valid = validateMessage(msg).ok;
    if (!verified || !valid) {
      hub.publish('a2a_rejected', { id: msg.id, from: msg.from, reason: !verified ? 'bad signature' : 'schema' });
      return this.send('acknowledgment', msg.from, msg.patient, { ack_of: msg.id, status: 'rejected', reason: !verified ? 'signature' : 'schema' }, msg.id);
    }
    // Ack the sender (silent for internal hops in the UI)
    await this.send('acknowledgment', msg.from, msg.patient, { ack_of: msg.id, status: 'routed' }, msg.id);

    const fwd = (to) => this.send(msg.type, to, msg.patient, msg.payload, msg.id);
    switch (msg.type) {
      case 'red_flag_escalation':
        await this.escalate(msg);
        await fwd('case_briefer');
        await fwd('archivist');
        break;
      case 'symptom_report':
        await fwd('case_briefer');
        if (!this.opened.has(msg.patient)) {
          this.opened.add(msg.patient);
          await this.send('data_request', 'genomics_interpreter', msg.patient, { what: 'genomic_context', why: 'case opened for a patient with a panel on file' }, msg.id);
        }
        break;
      case 'medication_report':
        await fwd('renal_guardian');
        break;
      case 'data_request':
        if (msg.payload.ask_patient) await fwd('companion');
        break;
      case 'data_delivered':
        if (msg.payload.kind === 'lab') { await fwd('renal_guardian'); await fwd('archivist'); }
        else await fwd('case_briefer');
        break;
      case 'imaging_signal':
      case 'therapy_conflict':
        // Everything leaves a seat through the Safety Verifier first
        await fwd(msg.payload.verification?.passed ? 'case_briefer' : 'safety_verifier');
        break;
      case 'case_pack_ready':
        hub.publish('note', { seat: this.id, text: `Case pack v${msg.payload.version ?? ''} posted to the Clinical Board` });
        break;
      case 'clinician_decision':
        await fwd('companion');
        await fwd('archivist');
        break;
      default:
        break;
    }
  }

  async escalate(msg) {
    const { ladder } = await this.tool('oncall.who_is_on_call', { role: 'uro_oncologist' });
    const first = ladder[0];
    await this.tool('oncall.page', { name: first.name, pager: first.pager, message: `RED FLAG ${msg.patient}: ${msg.payload.pattern}` });
    const c = this.ctx.cases.open(msg.patient);
    const item = this.gate({
      kind: 'escalation', gate: 'urgent_ack', patient: msg.patient, case_id: c.id,
      title: 'Red-flag escalation — acknowledge',
      data: { pattern: msg.payload.pattern, evidence: msg.payload.evidence, paged: first, ladder, deadline: new Date(Date.now() + config.escalationSeconds * 1000).toISOString(), level: 0 },
    });
    this.ctx.cases.patch(msg.patient, { escalation: item.id });
    this.armTimer(item.id, ladder, 0, msg.patient);
  }

  armTimer(itemId, ladder, level, patient) {
    clearTimeout(this.timers.get(itemId));
    const t = setTimeout(async () => {
      const item = this.ctx.board.get(itemId);
      if (!item || item.status !== 'open') return;
      const next = ladder[level + 1] || { name: 'ED triage desk', pager: '112' };
      await this.tool('oncall.page', { name: next.name, pager: next.pager, message: `ESCALATION (no ack) ${patient}` });
      this.ctx.board.update(itemId, { data: { ...item.data, paged: next, level: level + 1, deadline: new Date(Date.now() + config.escalationSeconds * 1000).toISOString() } }, `no acknowledgement — escalated to ${next.name}`);
      if (ladder[level + 1]) this.armTimer(itemId, ladder, level + 1, patient);
    }, config.escalationSeconds * 1000);
    t.unref?.();
    this.timers.set(itemId, t);
  }
  acknowledged(itemId) { clearTimeout(this.timers.get(itemId)); this.timers.delete(itemId); }
}
