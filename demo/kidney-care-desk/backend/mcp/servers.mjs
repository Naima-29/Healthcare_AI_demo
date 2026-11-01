// The MCP servers the IT Expert wraps around hospital systems.
// Each one runs against the local synthetic dataset by default and switches
// to the real system when its environment variable is set:
//   FHIR_BASE_URL     HL7 FHIR R4 server (e.g. HAPI FHIR)        -> fhir
//   DICOMWEB_URL      DICOMweb QIDO/WADO (e.g. Orthanc)          -> pacs
//   CT_MODEL_URL      your v1 kidney CT classifier (HTTP)         -> imaging
//   TELEGRAM_BOT_TOKEN  real patient channel (Bot API)            -> channel
import fs from 'node:fs';
import path from 'node:path';
import { McpServer } from './protocol.mjs';
import * as km from '../knowledge/km.mjs';
import { config, hub } from '../core/hub.mjs';

const DATA = process.env.DATA_DIR || path.resolve('data');
const readJson = (p) => JSON.parse(fs.readFileSync(path.join(DATA, p), 'utf8'));
const pdir = (patient) => patient.replace(/[^a-z0-9_-]/gi, '');

async function getJson(url, headers = {}) {
  const r = await fetch(url, { headers: { accept: 'application/fhir+json, application/json', ...headers } });
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.json();
}

// ---------------------------------------------------------------- FHIR
export function fhirServer() {
  const base = process.env.FHIR_BASE_URL;
  const bundle = (patient) => readJson(`${pdir(patient)}/fhir-bundle.json`);
  const local = (patient, type) => bundle(patient).entry.map((e) => e.resource).filter((r) => r.resourceType === type);
  const remote = async (type, q) => (await getJson(`${base}/${type}?${q}`)).entry?.map((e) => e.resource) || [];
  const res = (patient, type, q) => (base ? remote(type, q) : Promise.resolve(local(patient, type)));

  return new McpServer({ name: 'fhir', description: base ? `FHIR R4 @ ${base}` : 'FHIR R4 (local synthetic bundle)' })
    .tool('fhir.get_patient', { description: 'Patient demographics', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => {
        const p = base ? await getJson(`${base}/Patient/${patient}`) : local(patient, 'Patient')[0];
        return { id: p.id, name: `${p.name?.[0]?.given?.[0] ?? ''} ${p.name?.[0]?.family ?? ''}`.trim(), gender: p.gender, birthDate: p.birthDate, language: p.communication?.[0]?.language?.coding?.[0]?.code || 'it' };
      })
    .tool('fhir.get_conditions', { description: 'Active conditions', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => (await res(patient, 'Condition', `patient=${patient}`)).map((c) => ({ code: c.code?.coding?.[0]?.code, display: c.code?.coding?.[0]?.display || c.code?.text, onset: c.onsetDateTime, note: c.note?.[0]?.text })))
    .tool('fhir.get_medications', { description: 'Medication statements', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => (await res(patient, 'MedicationStatement', `patient=${patient}`)).map((m) => ({ name: m.medicationCodeableConcept?.text, status: m.status, dosage: m.dosage?.[0]?.text })))
    .tool('fhir.get_observations', { description: 'Observations by code text (e.g. eGFR), newest first', inputSchema: { type: 'object', properties: { patient: { type: 'string' }, code: { type: 'string' } }, required: ['patient', 'code'] } },
      async ({ patient, code }) => {
        const all = await res(patient, 'Observation', `patient=${patient}&code:text=${encodeURIComponent(code)}`);
        const list = all.filter((o) => (o.code?.text || '').toLowerCase() === code.toLowerCase())
          .map((o) => ({ date: o.effectiveDateTime, value: o.valueQuantity?.value, unit: o.valueQuantity?.unit }))
          .sort((a, b) => b.date.localeCompare(a.date));
        const latest = list[0];
        const ageDays = latest ? Math.round((config.now() - new Date(latest.date)) / 86400000) : null;
        return { code, latest, age_days: ageDays, series: list, trend: list.length > 1 ? (list[0].value < list[1].value ? 'down' : 'up') : 'flat' };
      });
}

// ---------------------------------------------------------------- PACS
export function pacsServer() {
  const base = process.env.DICOMWEB_URL;
  return new McpServer({ name: 'pacs', description: base ? `DICOMweb @ ${base}` : 'PACS (local demo study)' })
    .tool('pacs.list_studies', { description: 'Studies for a patient', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => {
        if (base) {
          const qido = await getJson(`${base}/studies?PatientID=${encodeURIComponent(patient)}`, { accept: 'application/dicom+json' });
          return qido.map((s) => ({ study_uid: s['0020000D']?.Value?.[0], date: s['00080020']?.Value?.[0], description: s['00081030']?.Value?.[0] }));
        }
        const s = readJson(`${pdir(patient)}/ct-study.json`);
        return [{ study_uid: s.study_uid, date: s.acquired, description: s.description, modality: s.modality }];
      })
    .tool('pacs.fetch_study', { description: 'Fetch study metadata + key image reference (read-only)', inputSchema: { type: 'object', properties: { patient: { type: 'string' }, study_uid: { type: 'string' } }, required: ['patient', 'study_uid'] } },
      async ({ patient, study_uid }) => {
        if (base) {
          return { study_uid, modality: 'CT', key_image: `${base}/studies/${study_uid}/rendered`, source: base, slice_thickness_mm: null };
        }
        const s = readJson(`${pdir(patient)}/ct-study.json`);
        if (s.study_uid !== study_uid) throw new Error('study not found');
        const { cached_inference, ...meta } = s;
        return meta;
      });
}

// ---------------------------------------------------------------- Imaging (wraps the v1 CT model)
export function imagingServer() {
  const url = process.env.CT_MODEL_URL;
  const cached = (patient) => readJson(`${pdir(patient)}/ct-study.json`).cached_inference;
  return new McpServer({ name: 'imaging', description: url ? `Kidney CT classifier @ ${url}` : 'Kidney CT classifier (cached v1 inference)' })
    .tool('ct.classify', { description: 'Classify a kidney CT key image (normal|cyst|stone|tumor). Expects the v1 model contract: POST {image_url} -> {label, confidence, cam_metrics, heatmap_url}', inputSchema: { type: 'object', properties: { patient: { type: 'string' }, image: { type: 'string' } }, required: ['patient', 'image'] } },
      async ({ patient, image }) => {
        if (url) {
          const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ image_url: image }) });
          if (!r.ok) throw new Error(`CT model HTTP ${r.status}`);
          const o = await r.json();
          return { model: o.model || 'kidney-ct-cls', label: o.label, p_raw: o.confidence, cam_metrics: o.cam_metrics || {}, heatmap: o.heatmap_url || null, live: true };
        }
        const c = cached(patient);
        return { model: c.model, label: c.label, p_raw: c.p_raw, cam_metrics: c.cam_metrics, heatmap: c.heatmap, live: false };
      })
    .tool('ct.gradcam_heatmap', { description: 'Attention heat map for the last classification', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => ({ heatmap: cached(patient).heatmap, method: 'Grad-CAM [20]' }))
    .tool('ct.quality_ood_check', { description: 'Image quality and out-of-distribution check on study metadata', inputSchema: { type: 'object', properties: { study: { type: 'object' } }, required: ['study'] } },
      async ({ study }) => {
        const issues = [];
        if (study.modality !== 'CT') issues.push('modality is not CT');
        if (study.slice_thickness_mm && study.slice_thickness_mm > 5) issues.push('slice thickness > 5 mm');
        const ood = issues.length ? 0.6 : 0.08;
        return { quality: issues.length ? 'limited' : 'adequate', issues, ood_score: ood, in_distribution: ood < 0.3 };
      });
}

// ---------------------------------------------------------------- Lab
export function labServer() {
  const parse = (text) => {
    const num = (re) => { const m = text.match(re); return m ? Number(m[1].replace(',', '.')) : null; };
    const iso = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
    const eu = text.match(/(\d{2})\/(\d{2})\/(\d{4})/); // dd/mm/yyyy (European reports)
    return {
      creatinine_mg_dl: num(/CREATININA[^\d]*(\d+[.,]\d+)/i) ?? num(/creatinine[^\d]*(\d+[.,]\d+)/i),
      egfr: num(/eGFR[^\d]*(?:\d{4}\)?\s*)?(\d{1,3})\s*mL/i),
      potassium: num(/(?:POTASSIO|POTASSIUM)[^\d]*(\d+[.,]\d+)/i),
      hemoglobin: num(/(?:EMOGLOBINA|HAEMOGLOBIN|HEMOGLOBIN)[^\d]*(\d+[.,]\d+)/i),
      urine_rbc: /(emazie|red blood cells|rbc)\s*:?\s*\+/i.test(text) ? 'positive' : null,
      date: iso ? `${iso[1]}-${iso[2]}-${iso[3]}` : eu ? `${eu[3]}-${eu[2]}-${eu[1]}` : null,
    };
  };
  return new McpServer({ name: 'lab', description: 'Laboratory results (report OCR + latest values)' })
    .tool('lab.parse_report', { description: 'Extract values from a photographed lab report (OCR text). On the phone, OCR runs on-device.', inputSchema: { type: 'object', properties: { ocr_text: { type: 'string' } }, required: ['ocr_text'] } },
      async ({ ocr_text }) => {
        const v = parse(ocr_text);
        const plausible = (v.egfr == null || (v.egfr > 1 && v.egfr < 150)) && (v.creatinine_mg_dl == null || (v.creatinine_mg_dl > 0.2 && v.creatinine_mg_dl < 15));
        return { ...v, plausible };
      })
    .tool('lab.latest', { description: 'Sample lab report text for the demo patient', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => {
        const f = config.lang === 'it' ? 'lab-report-photo.it.txt' : 'lab-report-photo.txt';
        return { ocr_text: fs.readFileSync(path.join(DATA, pdir(patient), f), 'utf8') };
      });
}

// ---------------------------------------------------------------- Genomics
export function genomicsServer() {
  return new McpServer({ name: 'genomics', description: 'Genomic panel (NGS, laboratory classification)' })
    .tool('genomics.get_panel', { description: 'Reported variants with the laboratory classification', inputSchema: { type: 'object', properties: { patient: { type: 'string' } }, required: ['patient'] } },
      async ({ patient }) => readJson(`${pdir(patient)}/dna-panel.json`));
}

// ---------------------------------------------------------------- Wearables (deterministic synthetic streams)
export function wearablesServer() {
  const rnd = (seed) => () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  const series = (patient, days = 90) => {
    const r = rnd(patient.length * 7919);
    const out = [];
    const end = config.now();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(end.getTime() - i * 86400000);
      const late = i < 14; // last two weeks deteriorate
      out.push({
        date: d.toISOString().slice(0, 10),
        sbp: Math.round((late ? 152 : 136) + r() * 10),
        dbp: Math.round((late ? 94 : 84) + r() * 6),
        hrv_ms: Math.round((late ? 24 : 38) + r() * 8),
        sleep_h: Number(((late ? 5.1 : 6.9) + r() * 1.2).toFixed(1)),
        steps: Math.round((late ? 2600 : 6400) + r() * 1500),
      });
    }
    return out;
  };
  return new McpServer({ name: 'wearables', description: 'HRV, sleep, home blood pressure (synthetic)' })
    .tool('wearables.read', { description: 'Daily wearable series and 7-day summary', inputSchema: { type: 'object', properties: { patient: { type: 'string' }, days: { type: 'number' } }, required: ['patient'] } },
      async ({ patient, days = 90 }) => {
        const s = series(patient, days);
        const last7 = s.slice(-7);
        const avg = (k) => Math.round(last7.reduce((a, x) => a + x[k], 0) / last7.length);
        return { series: s, summary_7d: { sbp: avg('sbp'), dbp: avg('dbp'), hrv_ms: avg('hrv_ms'), sleep_h: Number((last7.reduce((a, x) => a + x.sleep_h, 0) / 7).toFixed(1)) } };
      });
}

// ---------------------------------------------------------------- On-call
export function oncallServer() {
  const rota = () => readJson('marco-r/oncall.json');
  return new McpServer({ name: 'oncall', description: 'On-call rota and paging' })
    .tool('oncall.who_is_on_call', { description: 'Escalation ladder for a role', inputSchema: { type: 'object', properties: { role: { type: 'string' } }, required: ['role'] } },
      async ({ role }) => ({ role, ladder: rota().roles[role] || [] }))
    .tool('oncall.page', { description: 'Page a clinician (demo: logged, not sent)', inputSchema: { type: 'object', properties: { name: { type: 'string' }, pager: { type: 'string' }, message: { type: 'string' } }, required: ['name', 'message'] } },
      async ({ name, pager, message }) => {
        hub.publish('page', { name, pager, message });
        return { paged: true, name, pager, at: new Date().toISOString() };
      });
}

// ---------------------------------------------------------------- Patient channel (UI phone + optional Telegram)
export function channelServer(channel) {
  return new McpServer({ name: 'channel', description: channel.describe() })
    .tool('channel.send', { description: 'Send a message to the patient', inputSchema: { type: 'object', properties: { patient: { type: 'string' }, text: { type: 'string' }, kind: { type: 'string' } }, required: ['patient', 'text'] } },
      async ({ patient, text, kind = 'text' }) => channel.send(patient, text, kind))
    .tool('channel.receive', { description: 'Pending inbound messages (polling transports)', inputSchema: { type: 'object', properties: {} } },
      async () => ({ pending: 0 }));
}

// ---------------------------------------------------------------- Knowledge Model
export function kmServer() {
  return new McpServer({ name: 'km', description: 'Knowledge Model: guidelines, ontologies, drug-disease rules, playbook' })
    .tool('km.query', { description: 'Look up citations', inputSchema: { type: 'object', properties: { term: { type: 'string' } } } }, async ({ term }) => km.query(term))
    .tool('km.ckd_stage', { description: 'KDIGO GFR category', inputSchema: { type: 'object', properties: { egfr: { type: 'number' } }, required: ['egfr'] } }, async ({ egfr }) => km.ckdStage(egfr))
    .tool('km.red_flags', { description: 'Red-flag patterns (L1 playbook)', inputSchema: { type: 'object', properties: { ctx: { type: 'object' } }, required: ['ctx'] } }, async ({ ctx }) => ({ result: km.redFlags(ctx) }))
    .tool('km.check_conflicts', { description: 'Drug-disease conflicts', inputSchema: { type: 'object', properties: { ctx: { type: 'object' } }, required: ['ctx'] } }, async ({ ctx }) => ({ conflicts: km.checkConflicts(ctx) }))
    .tool('km.interpret_variant', { description: 'Clinical context for a reported variant (never reclassifies)', inputSchema: { type: 'object', properties: { variant: { type: 'object' } }, required: ['variant'] } }, async ({ variant }) => km.interpretVariant(variant));
}

// ---------------------------------------------------------------- Cortex
export function cortexServer(cortex) {
  return new McpServer({ name: 'cortex', description: 'Loop Cortex Memory' })
    .tool('cortex.evidence_append', { description: 'Append evidence (immutable)', inputSchema: { type: 'object', properties: { partition: { type: 'string' }, kind: { type: 'string' }, source: { type: 'string' }, content: {} }, required: ['partition', 'kind', 'content'] } },
      async (a) => cortex.appendEvidence(a.partition, a))
    .tool('cortex.knowledge_propose', { description: 'Propose a rule (shadow mode)', inputSchema: { type: 'object', properties: { partition: { type: 'string' } }, required: ['partition'] } },
      async (a) => cortex.propose(a.partition, a))
    .tool('cortex.knowledge_read', { description: 'Read knowledge objects', inputSchema: { type: 'object', properties: { partition: { type: 'string' } }, required: ['partition'] } },
      async ({ partition }) => cortex.knowledge(partition));
}

export function allServers({ cortex, channel }) {
  return [fhirServer(), pacsServer(), imagingServer(), labServer(), genomicsServer(), wearablesServer(), oncallServer(), channelServer(channel), kmServer(), cortexServer(cortex)];
}
