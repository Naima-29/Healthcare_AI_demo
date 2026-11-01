// The golden path (deck slide 28) - seven steps, ~10 minutes on stage.
// Every step drives the system through one of the three human doors; agents
// do the rest by themselves. Steps 6-7 can be done live on the Clinical Board
// (autoplay signs/confirms with the default draft if nobody does).
import { hub, sleep, config } from '../core/hub.mjs';

export const PATIENT = 'marco-r';

// Humanized features captured by the phone (face action units + voice).
// High PSPI + tremor while the words minimise ("nothing serious" / "niente di che").
const FEAT_PAIN = { au: { AU4: 3, AU6: 3, AU7: 2, AU9: 2, AU10: 1, AU43: 1 }, voice: { tremor: 0.7, pitch_var: 0.6, rate_dev: -0.4 } };
const FEAT_MILD = { au: { AU4: 2, AU6: 2, AU9: 1, AU43: 0 }, voice: { tremor: 0.5, pitch_var: 0.4, rate_dev: -0.2 } };

// The script, in both languages. DEMO_LANG=en (default) or DEMO_LANG=it.
const SCRIPT = {
  en: {
    director: 'Companion: never disclose an imaging result to the patient before the doctor has reviewed it.',
    s2: 'Good evening… I have a pain in my right side, under the ribs. Nothing serious, but it won’t go away and it gets worse at night.',
    s3: ['My urine is pinkish, sometimes really dark red.', 'I’ve lost 23 kg in the last three months and sometimes I feel nauseous.', 'I’d say 8 out of 10. And I often feel dizzy.'],
    s4: 'For the pain I’m taking ibuprofen 600, twice a day.',
    s5: 'I had a CT scan yesterday: what did it show?',
    s7: 'Can I take another ibuprofen tonight? The pain is back.',
  },
  it: {
    director: 'Companion: non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.',
    s2: 'Buonasera… ho un dolore al fianco destro, sotto le costole. Niente di che, ma non passa e di notte peggiora.',
    s3: ['Le urine sono rosate, a volte proprio rosso scuro.', 'Ho perso 23 kg negli ultimi tre mesi e a volte ho nausea.', 'Direi 8 su 10. E mi gira spesso la testa.'],
    s4: 'Per il dolore sto prendendo ibuprofene 600, due volte al giorno.',
    s5: 'Ieri ho fatto la TAC: cosa ha mostrato?',
    s7: 'Posso prendere un altro ibuprofene stasera? Il dolore è tornato.',
  },
};

export function steps(lang = config.lang) {
  const L = SCRIPT[lang];
  return [
    { n: 1, short: 'Roster', title: 'Roster: the Director adds a refusal in plain language', door: 'director', lines: [L.director] },
    { n: 2, short: 'At home', title: 'At home, 21:40: Marco talks to his Companion', door: 'patient', lines: [{ text: L.s2, features: FEAT_PAIN }] },
    { n: 3, short: 'Red flag', title: 'Red flag caught on the phone', door: 'patient',
      lines: [{ text: L.s3[0], features: FEAT_MILD }, { text: L.s3[1], features: FEAT_MILD }, { text: L.s3[2], features: FEAT_PAIN }] },
    { n: 4, short: 'Conflict', title: 'Therapy conflict: ibuprofen × CKD', door: 'patient', lines: [{ text: L.s4 }, { attach: 'lab' }] },
    { n: 5, short: 'Imaging', title: 'Imaging: CT reaches PACS; Marco asks for the result', door: 'pacs', lines: [{ pacs: true }, { text: L.s5 }] },
    { n: 6, short: 'Sign-off', title: 'Sign-off on the Clinical Board', door: 'clinician', lines: [{ sign: true }] },
    { n: 7, short: 'Memory', title: 'Memory: rules confirmed, next episode smarter', door: 'clinician', lines: [{ confirmRules: true }, { text: L.s7 }] },
  ];
}
export const SCRIPT_LINES = SCRIPT;

async function waitFor(pred, timeoutMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const hit = hub.history.find(pred);
    if (hit) return hit;
    await sleep(50);
  }
  return null;
}

export async function runStep(ctx, n, { clinician = 'Dr. A. Rossi' } = {}) {
  const step = steps().find((s) => s.n === n);
  if (!step) throw new Error(`no step ${n}`);
  hub.publish('scenario', { op: 'step', n, title: step.title });
  for (const line of step.lines) {
    if (typeof line === 'string') { await ctx.doors.director(line); continue; }
    if (line.text) {
      const before = hub.history.length;
      await ctx.doors.patient.say(PATIENT, line.text, line.features);
      await waitFor((e) => e.seq > before && (e.kind === 'patient_out'), 8000);
      await sleep(Math.max(300, config.pace * 2));
    }
    if (line.attach === 'lab') {
      const asked = await waitFor((e) => e.kind === 'patient_out' && /referto|lab report/i.test(e.data.text), 15000);
      if (asked) { await sleep(Math.max(200, config.pace)); await ctx.doors.patient.attachLab(PATIENT); }
      await waitFor((e) => e.kind === 'a2a' && e.data.type === 'therapy_conflict' && e.data.payload.verification, 20000);
    }
    if (line.pacs) {
      await ctx.doors.pacs.newStudy(PATIENT);
      await waitFor((e) => e.kind === 'board' && e.data.item.kind === 'imaging', 20000);
    }
    if (line.sign) {
      const pack = ctx.board.list().find((i) => i.kind === 'case_pack' && i.status === 'open');
      const esc = ctx.board.list().find((i) => i.kind === 'escalation' && i.status === 'open');
      if (esc) ctx.doors.clinician.acknowledge(esc.id, 'Dr. L. Bianchi', 'oncall_clinician');
      const img = ctx.board.list().find((i) => i.kind === 'imaging' && i.status === 'open');
      if (img) ctx.doors.clinician.review(img.id, 'Dr. E. Conti', 'radiologist');
      if (pack) await ctx.doors.clinician.sign(pack.id, { clinician, note: '' });
      await waitFor((e) => e.kind === 'patient_out' && /ha rivisto il suo caso|reviewed your case/.test(e.data.text), 15000);
      await waitFor((e) => e.kind === 'board' && e.data.item.kind === 'rule', 15000);
    }
    if (line.confirmRules) {
      await waitFor((e) => e.kind === 'board' && e.data.item.kind === 'rule', 15000);
      await sleep(200);
      for (const r of ctx.board.list().filter((i) => i.kind === 'rule' && i.status === 'open')) ctx.doors.clinician.confirmRule(r.id, clinician, true);
    }
  }
  hub.publish('scenario', { op: 'done', n });
  return step;
}

export async function autoplay(ctx, { from = 1, to = 7, gap = 1500 } = {}) {
  for (let n = from; n <= to; n++) {
    await runStep(ctx, n);
    await sleep(config.pace > 0 ? gap : 100);
  }
}
