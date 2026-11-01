// Hospital-side clinical seats: Imaging Reader, Genomics Interpreter,
// Renal Guardian, Safety Verifier. Each one only acts through its manifest.
import { Seat } from '../runtime/seat.mjs';
import { classify } from '../runtime/guard.mjs';
import { hub, config, pace } from '../core/hub.mjs';

const logit = (p) => Math.log(p / (1 - p));
const sigmoid = (x) => 1 / (1 + Math.exp(-x));
export const calibrate = (p, T = config.calibrationT) => {
  const q = Math.min(Math.max(p, 1e-6), 1 - 1e-6);
  return Number(sigmoid(logit(q) / T).toFixed(3));
};

// ------------------------------------------------------------ IMAGING READER
export class ImagingReader extends Seat {
  constructor(ctx) { super(ctx, 'imaging_reader'); }

  async onNewStudy(patient) {
    const studies = await this.tool('pacs.list_studies', { patient });
    const st = studies[0];
    const study = await this.tool('pacs.fetch_study', { patient, study_uid: st.study_uid });
    await pace(0.5);
    const q = await this.tool('ct.quality_ood_check', { study });
    const cls = await this.tool('ct.classify', { patient, image: study.key_image });
    const hm = await this.tool('ct.gradcam_heatmap', { patient });
    const p_cal = calibrate(cls.p_raw);
    const note = 'Preliminary model signal for radiologist review. It is not a diagnosis and is not shared with the patient.';
    this.content(note); // label_as_diagnosis: blocks diagnosis claims even in notes
    this.meter(1);
    await this.send('imaging_signal', 'dispatcher', patient, {
      label: cls.label, p_raw: cls.p_raw, p_calibrated: p_cal, cam_metrics: cls.cam_metrics,
      quality: q, provenance: { study_uid: study.study_uid, modality: study.modality, acquired: study.acquired, model: cls.model, live_model: cls.live, source: study.source },
      heatmap_ref: hm.heatmap, key_image: study.key_image, note, calibration: `temperature scaling T=${config.calibrationT}`,
    });
  }
}

// ------------------------------------------------------------ GENOMICS INTERPRETER
export class GenomicsInterpreter extends Seat {
  constructor(ctx) { super(ctx, 'genomics_interpreter'); }
  async onMessage(msg) {
    if (msg.type !== 'data_request' || msg.payload.what !== 'genomic_context') return;
    const panel = await this.tool('genomics.get_panel', { patient: msg.patient });
    const findings = [];
    for (const v of panel.variants) findings.push(await this.tool('km.interpret_variant', { variant: v }));
    const reportable = findings.filter((f) => f.relevance !== 'none' || f.lab_classification === 'VUS');
    this.meter(1);
    await this.send('data_delivered', 'dispatcher', msg.patient, {
      kind: 'genomic_context',
      data: { panel: panel.profiling_method, date: panel.profiling_date, findings: reportable, disclosure: 'Disclosure to the patient requires a geneticist (gate).' },
    }, msg.id);
  }
}

// ------------------------------------------------------------ RENAL GUARDIAN
export class RenalGuardian extends Seat {
  constructor(ctx) { super(ctx, 'renal_guardian'); this.pending = new Map(); }
  reset() { this.pending.clear(); }

  async onMessage(msg) {
    const p = msg.patient;
    if (msg.type === 'medication_report') {
      const egfr = await this.tool('fhir.get_observations', { patient: p, code: 'eGFR' });
      this.pending.set(p, { drug: msg.payload, egfrRecord: egfr });
      if (egfr.age_days == null || egfr.age_days > config.staleDays) {
        hub.publish('note', { seat: this.id, text: `Latest eGFR is ${egfr.age_days} days old (> ${config.staleDays}): requesting fresh data before concluding.` });
        await this.send('data_request', 'dispatcher', p, {
          what: 'recent creatinine / eGFR', why: `latest eGFR on record is ${egfr.age_days} days old (${egfr.latest?.date})`,
          ask_patient: 'photo of the most recent blood test report',
        }, msg.id);
        return;
      }
      return this.evaluate(p, { egfr: egfr.latest.value, date: egfr.latest.date, source: 'FHIR record' });
    }
    if (msg.type === 'data_delivered' && msg.payload.kind === 'lab') {
      const d = msg.payload.data;
      return this.evaluate(p, { egfr: d.egfr, creatinine: d.creatinine_mg_dl, date: d.date, source: d.source });
    }
  }

  async evaluate(patient, lab) {
    const pend = this.pending.get(patient);
    if (!pend) return;
    const [conds, meds, wear, stage] = await Promise.all([
      this.tool('fhir.get_conditions', { patient }),
      this.tool('fhir.get_medications', { patient }),
      this.tool('wearables.read', { patient, days: 30 }),
      this.tool('km.ckd_stage', { egfr: lab.egfr }),
    ]);
    const { conflicts } = await this.tool('km.check_conflicts', {
      ctx: { drugClass: pend.drug.class, egfr: lab.egfr, sbp: wear.summary_7d.sbp, conditions: conds.map((c) => c.display), medications: meds.map((m) => m.name) },
    });
    this.meter(1);
    if (!conflicts.length) { hub.publish('note', { seat: this.id, text: 'No drug–disease conflict found.' }); return; }
    const c = conflicts[0];
    const series = pend.egfrRecord.series.map((x) => ({ date: x.date, value: x.value }));
    if (lab.date && !series.some((x) => x.date === lab.date)) series.unshift({ date: lab.date, value: lab.egfr });
    await this.send('therapy_conflict', 'dispatcher', patient, {
      ...c, data_freshness: { egfr: lab.egfr, creatinine: lab.creatinine ?? null, date: lab.date, source: lab.source, stage: stage.stage, home_sbp_7d: wear.summary_7d.sbp },
      egfr_series: series,
    });
    this.pending.delete(patient);
  }
}

// ------------------------------------------------------------ SAFETY VERIFIER
export class SafetyVerifier extends Seat {
  constructor(ctx) { super(ctx, 'safety_verifier'); }

  async onMessage(msg) {
    if (!['imaging_signal', 'therapy_conflict'].includes(msg.type) || msg.payload.verification) return;
    const checks = msg.type === 'imaging_signal' ? this.checkImaging(msg.payload) : await this.checkConflict(msg.payload);
    const passed = checks.every((c) => c.ok);
    this.meter(checks.length / 100);
    hub.publish('verification', { type: msg.type, passed, checks });
    await pace(0.5);
    if (!passed) {
      try { this.act('verifier.override'); } catch { /* refusal: pass_failed_check */ }
      this.gate({ kind: 'verification_failed', gate: 'none', patient: msg.patient, case_id: this.ctx.cases.get(msg.patient)?.id, title: `Blocked ${msg.type}: failed checks`, data: { checks } });
      return;
    }
    await this.send(msg.type, 'dispatcher', msg.patient, { ...msg.payload, verification: { passed, by: 'safety_verifier', checks } }, msg.id);
  }

  checkImaging(p) {
    return [
      { check: 'provenance (study UID, model)', ok: !!(p.provenance?.study_uid && p.provenance?.model) },
      { check: 'confidence plausible 0..1', ok: p.p_calibrated >= 0 && p.p_calibrated <= 1 },
      { check: 'calibrated (not raw) confidence shown', ok: p.p_calibrated !== p.p_raw },
      { check: 'image quality / in distribution', ok: p.quality?.in_distribution !== false },
      { check: 'no diagnosis claim in the note', ok: !classify(p.note || '').includes('diagnosis_claim') },
    ];
  }
  
    async checkConflict(p) {
    const f = p.data_freshness || {};
    const age = f.date ? (config.now() - new Date(f.date)) / 86400000 : Infinity;
    const cited = Array.isArray(p.citations) ? p.citations : [];
    // Each citation must match a source in the Knowledge Model exactly.
    // (km.query matches partial text, so we compare the full citation ourselves.)
    const inKnowledgeModel = await Promise.all(cited.map(async (c) => {
      const hits = await this.tool('km.query', { term: c });
      return hits.some((h) => h.citation === c);
    }));
    return [
      { check: 'every claim cited to the Knowledge Model', ok: cited.length > 0 },
      { check: 'every citation exists in the Knowledge Model', ok: inKnowledgeModel.every(Boolean) },
      { check: 'eGFR plausible (1-150)', ok: f.egfr > 1 && f.egfr < 150 },
      { check: `kidney function data fresh (<= ${config.staleDays} days)`, ok: age <= config.staleDays },
    ];
  }

  // De-identification for anything that must leave the perimeter (cloud LLM)
  deidentify(obj, patientName = 'Marco') {
    const s = JSON.stringify(obj)
      .replace(new RegExp(patientName, 'gi'), '[PATIENT]')
      .replace(/marco-r/gi, '[PID]')
      .replace(/\b(19|20)\d{2}-\d{2}-\d{2}\b/g, (d) => `[DATE:${d.slice(0, 4)}]`)
      .replace(/\b\d{1,3}(\.\d{1,3}){4,}\b/g, '[UID]')
      .replace(/Dr\.\s+[A-Z]\.\s+\w+/g, '[CLINICIAN]');
    return { data: JSON.parse(s), stamp: { deidentified: true, by: 'safety_verifier', ts: new Date().toISOString() } };
  }
}
