// Episode/case state shared by the hospital seats (an L3 working view).
import { hub, uid } from '../core/hub.mjs';

export class Cases {
  constructor() { this.byPatient = new Map(); }
  open(patient) {
    let c = this.byPatient.get(patient);
    if (!c || c.status === 'closed') {
      c = { id: uid('case'), patient, opened: new Date().toISOString(), status: 'open', redflag: null, symptoms: null, medication: null, conflict: null, labs: null, imaging: null, genomics: null, packs: 0, decision: null, escalation: null };
      this.byPatient.set(patient, c);
      hub.publish('case', { op: 'open', case: c });
    }
    return c;
  }
  get(patient) { return this.byPatient.get(patient) || null; }
  patch(patient, patch) {
    const c = this.open(patient);
    Object.assign(c, patch);
    hub.publish('case', { op: 'update', case: c });
    return c;
  }
  reset() { this.byPatient.clear(); }
}
