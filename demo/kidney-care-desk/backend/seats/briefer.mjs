// CASE BRIEFER - the clinicians' agent on the Clinical Board (v1 Doctor Agent).
// Assembles one SBAR case pack per episode from every seat's (verified) output,
// with cited options and a DRAFT decision the clinician edits and signs.
// ARCHIVIST - turns signed resolutions into Cortex rules (shadow -> confirmed).
import { Seat } from '../runtime/seat.mjs';
import { gateway } from '../models/gateway.mjs';
import { CITATIONS } from '../knowledge/km.mjs';
import { hub, config } from '../core/hub.mjs';

export class CaseBriefer extends Seat {
  constructor(ctx) { super(ctx, 'case_briefer'); }

  async onMessage(msg) {
    const p = msg.patient;
    const cases = this.ctx.cases;
    const c = cases.open(p);
    switch (msg.type) {
      case 'red_flag_escalation': cases.patch(p, { redflag: msg.payload }); break;
      case 'symptom_report': cases.patch(p, { symptoms: msg.payload }); break;
      case 'therapy_conflict': cases.patch(p, { conflict: msg.payload }); break;
      case 'imaging_signal':
        cases.patch(p, { imaging: msg.payload });
        this.ctx.board.upsert({ kind: 'imaging', case_id: c.id }, { kind: 'imaging', gate: 'radiologist_review', patient: p, case_id: c.id, title: 'Imaging signal — radiologist review', data: msg.payload, by: 'imaging_reader' });
        break;
      case 'data_delivered':
        if (msg.payload.kind === 'genomic_context') {
          cases.patch(p, { genomics: msg.payload.data });
          this.ctx.board.upsert({ kind: 'genomics', case_id: c.id }, { kind: 'genomics', gate: 'genetic_disclosure', patient: p, case_id: c.id, title: 'Genomic context — for the clinical team', data: msg.payload.data, by: 'genomics_interpreter', status: 'info' });
        }
        break;
      default: return;
    }
    await this.rebuild(p);
  }

  async rebuild(patient) {
    const c = this.ctx.cases.get(patient);
    const [pt, conds, meds, egfr] = await Promise.all([
      this.tool('fhir.get_patient', { patient }),
      this.tool('fhir.get_conditions', { patient }),
      this.tool('fhir.get_medications', { patient }),
      this.tool('fhir.get_observations', { patient, code: 'eGFR' }),
    ]);
    const age = Math.floor((new Date('2026-10-12') - new Date(pt.birthDate)) / 31557600000);
    const S = [];
    if (c.redflag) S.push(`Red-flag pattern reported at home via the Companion: ${c.redflag.evidence.join('; ')}.`);
    else if (c.symptoms) S.push(`Symptoms reported via the Companion: ${c.symptoms.symptoms.map((s) => s.label).join(', ')}.`);
    if (c.symptoms?.distress) S.push(`Distress ${c.symptoms.distress.level} (fuzzy ${c.symptoms.distress.score})${c.symptoms.underreporting ? ' — under-reporting likely: non-verbal signs exceed the words' : ''}.`);
    if (c.escalation) { const it = this.ctx.board.get(c.escalation); S.push(`On-call uro-oncologist paged: ${it?.data?.paged?.name}.`); }

    const B = [
      `${pt.name}, ${age}, ${pt.gender}. ${conds.map((x) => x.display).join('; ')}.`,
      `Medications on record: ${meds.map((m) => m.name).join(', ')}.`,
      `eGFR trajectory: ${egfr.series.slice().reverse().map((x) => `${x.date.slice(0, 7)} ${x.value}`).join(' → ')}${c.conflict?.data_freshness ? ` → ${c.conflict.data_freshness.date} ${c.conflict.data_freshness.egfr} (patient photo)` : ''}.`,
    ];
    const A = [];
    if (c.redflag) A.push({ from: 'Companion', text: `Red-flag pattern: ${c.redflag.pattern}.`, cite: [CITATIONS.PLAYBOOK] });
    if (c.conflict) A.push({ from: 'Renal Guardian', text: `Drug–disease conflict (${c.conflict.severity}): ${c.conflict.drug} with ${c.conflict.conditions.join(', ')}. ${c.conflict.rationale}`, cite: c.conflict.citations });
    if (c.imaging) A.push({ from: 'Imaging Reader', text: `Preliminary CT signal "${c.imaging.label}", calibrated p=${c.imaging.p_calibrated} (raw ${c.imaging.p_raw}); quality ${c.imaging.quality.quality}; attention map attached. Not a diagnosis — radiologist review required.`, cite: ['[20] Grad-CAM (Selvaraju 2017)'] });
    if (c.genomics) for (const f of c.genomics.findings.filter((x) => x.relevance !== 'none')) A.push({ from: 'Genomics Interpreter', text: `${f.gene} ${f.hgvs} (${f.lab_classification}): ${f.summary}`, cite: f.citations });

    const R = [];
    if (c.redflag) R.push({ code: 'urgent_visit', text: 'Same-day assessment (uro-oncology / ED) given the red-flag pattern.', cite: [CITATIONS.EAU_RCC] });
    if (c.conflict) R.push({ code: 'stop_nsaid', text: 'Stop NSAID; alternative analgesia per clinician; recheck BP.', cite: c.conflict.citations });
    if (c.conflict || c.redflag) R.push({ code: 'repeat_labs', text: 'Repeat creatinine / eGFR, urinalysis and blood count.', cite: [CITATIONS.KDIGO2024] });
    if (c.imaging) R.push({ code: 'ct_review', text: 'Radiologist review of the new CT, compare with prior studies.', cite: [CITATIONS.EAU_RCC] });
    if (c.genomics) R.push({ code: 'vhl_followup', text: 'VHL carrier: consider genetics referral and surveillance plan; prefer kidney-sparing options.', cite: [CITATIONS.VHL2011] });

    const draft = draftDecision(R, config.lang);
    let narrative = null;
    // Optional: one-paragraph narrative from the edge SLM (de-identified input)
    const deid = this.ctx.runtime.seat('safety_verifier').deidentify({ S, A: A.map((a) => a.text) });
    narrative = await gateway.edge([
      { role: 'system', content: 'Write a 3-sentence clinical handover summary for a nephro-oncology MDT. Do not diagnose. Do not invent facts.' },
      { role: 'user', content: JSON.stringify(deid.data) },
    ]);

    const version = (c.packs || 0) + 1;
    this.ctx.cases.patch(patient, { packs: version });
    const sbar = { S, B, A, R, narrative };
    this.ctx.board.upsert({ kind: 'case_pack', case_id: c.id }, { kind: 'case_pack', gate: 'clinician_signoff', patient, case_id: c.id, title: 'Case pack (SBAR) — edit & sign', data: { sbar, draft, version }, by: this.id });
    if (version === 1) this.meter(1); // billed once per case; re-issues are free
    await this.send('case_pack_ready', 'dispatcher', patient, { case_id: c.id, sbar: { S, B, A: A.map((a) => a.text), R: R.map((r) => r.text) }, gates: ['clinician_signoff'], version });
  }
}

// Draft wording for the patient, in the demo language. The clinician edits it;
// the Companion sends exactly what was signed.
export function draftDecision(R, lang = 'en') {
  const T = {
    urgent_visit: { patient_text: 'venire domani alle 9:00 in Urologia oncologica (Pad. B) per una visita; se peggiora prima, vada al Pronto Soccorso', patient_text_en: 'come tomorrow at 9:00 to the uro-oncology clinic (Building B); if things get worse before then, go to the emergency department' },
    stop_nsaid: { patient_text: 'non prendere più ibuprofene né altri antinfiammatori; per il dolore usi il paracetamolo come indicato nel piano che le invieremo', patient_text_en: 'stop taking ibuprofen or other anti-inflammatories; for pain use paracetamol as described in the plan we will send you' },
    repeat_labs: { patient_text: 'fare domattina un prelievo di sangue e urine prima della visita', patient_text_en: 'have blood and urine tests tomorrow morning before the visit' },
    ct_review: { patient_text: 'le immagini della TAC verranno discusse con lei durante la visita', patient_text_en: 'your CT images will be discussed with you during the visit' },
    vhl_followup: { patient_text: 'le proporremo un colloquio con il genetista', patient_text_en: 'we will offer you an appointment with the geneticist' },
  };
  return R.map((r) => {
    const t = T[r.code];
    const text = t ? (lang === 'it' ? t.patient_text : t.patient_text_en) : r.text;
    return { code: r.code, selected: true, clinician_text: r.text, patient_text: text };
  });
}

// ------------------------------------------------------------ ARCHIVIST
export class Archivist extends Seat {
  constructor(ctx) { super(ctx, 'archivist'); }
  async onMessage(msg) {
    const part = `patient-${msg.patient}`;
    if (msg.type === 'red_flag_escalation' || (msg.type === 'data_delivered' && msg.payload.kind === 'lab')) {
      await this.tool('cortex.evidence_append', { partition: part, kind: msg.type, source: msg.from, content: msg.payload });
      return;
    }
    if (msg.type !== 'clinician_decision') return;
    const ev = await this.tool('cortex.evidence_append', { partition: part, kind: 'clinician_decision', source: msg.payload.signed_by, content: msg.payload });
    const proposals = [];
    if (msg.payload.items.some((i) => i.code === 'stop_nsaid')) {
      proposals.push(await this.tool('cortex.knowledge_propose', { partition: part, layer: 3, key: 'nsaid_avoid', value: 'Avoid NSAIDs (ibuprofen and similar): CKD G3a + hypertension + TKI', conf: 0.95, source: msg.payload.signed_by, evidence: [ev.id] }));
    }
    if (msg.payload.items.some((i) => i.code === 'urgent_visit')) {
      const evh = await this.tool('cortex.evidence_append', { partition: 'hospital', kind: 'routing_precedent', source: 'dispatcher', content: { case: msg.payload.case_id, pattern: 'RCC follow-up + visible haematuria + flank pain >= 7' } });
      proposals.push(await this.tool('cortex.knowledge_propose', { partition: 'hospital', layer: 2, key: 'route_rcc_redflag', value: 'RCC follow-up + visible haematuria + flank pain ≥ 7/10 → page the uro-oncologist on call (not the GP)', conf: 0.9, source: msg.payload.signed_by, evidence: [evh.id] }));
    }
    for (const k of proposals) {
      this.gate({ kind: 'rule', gate: 'rule_confirmation', patient: msg.patient, case_id: msg.payload.case_id, title: `Confirm rule (${k.partition === 'hospital' ? 'L2 hospital' : 'L3 patient'})`, data: k });
      this.meter(1);
    }
    hub.publish('note', { seat: this.id, text: 'Rules stay in shadow mode until a clinician confirms them (auto_confirm_rule is a hard block).' });
  }
}
