// Knowledge Model (L0 medical knowledge + L1 specialty playbook).
// The medical content now lives in rules.json (editable, reviewable data).
// This file only LOADS that data and applies it. Every rule carries the
// citation the Safety Verifier will require.
// NOTE: clinical rules are a demo knowledge base; they must be reviewed
// by the clinical team before any real use.

import { readFileSync } from 'node:fs';

// ---------- load + check the data file ----------
const DATA = JSON.parse(readFileSync(new URL('./rules.json', import.meta.url), 'utf-8'));

// Fail early, with a clear message, if the data file has a mistake.
function validateKnowledge(d) {
  const problems = [];
  const known = new Set(Object.keys(d.citations || {}));
  const classes = new Set((d.drug_classes || []).map((c) => c.class));
  if (!known.has(d.ckd_stage_citation)) problems.push(`ckd_stage_citation "${d.ckd_stage_citation}" is not in citations`);
  for (const r of d.conflict_rules || []) {
    if (!classes.has(r.drug_class)) problems.push(`rule ${r.id}: unknown drug_class "${r.drug_class}"`);
    for (const t of r.triggers || []) {
      if (!known.has(t.citation)) problems.push(`rule ${r.id}, trigger ${t.id}: citation "${t.citation}" is not in citations`);
    }
  }
  if (problems.length) throw new Error('rules.json problems:\n - ' + problems.join('\n - '));
}
validateKnowledge(DATA);

export const CITATIONS = DATA.citations;

// ---------- drugs ----------
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Build one word-matching pattern per drug class from its list of names.
export const DRUG_CLASSES = DATA.drug_classes.map((c) => ({
  re: new RegExp(`\\b(${[...c.names].sort((a, b) => b.length - a.length).map(escapeRe).join('|')})\\b`, 'i'),
  drug: c.label,
  class: c.class,
}));

export function classifyDrug(text) {
  for (const d of DRUG_CLASSES) {
    const m = text.match(d.re);
    if (m) return { drug: m[0].toLowerCase(), class: d.class };
  }
  return null;
}

// ---------- kidney stage ----------
export function ckdStage(egfr) {
  if (egfr == null) return null;
  // First stage whose minimum eGFR we reach; the last stage has no minimum (catch-all).
  const found = DATA.ckd_stages.find((s) => s.min_egfr == null || egfr >= s.min_egfr);
  return { stage: found.stage, egfr, citation: CITATIONS[DATA.ckd_stage_citation] };
}

// Red-flag patterns for a patient in RCC follow-up (L1 playbook).
export function redFlags(ctx) {
  const s = new Set((ctx.symptoms || []).map((x) => x.code));
  const evidence = [];
  if (s.has('visible_hematuria')) evidence.push('visible blood in urine');
  if ((ctx.painScore ?? 0) >= 7 && s.has('flank_pain')) evidence.push(`severe one-sided flank pain (${ctx.painScore}/10)`);
  if ((ctx.weightLossKg ?? 0) >= 5) evidence.push(`unintentional weight loss (${ctx.weightLossKg} kg)`);
  if (s.has('dizziness')) evidence.push('dizziness');
  if (s.has('fever')) evidence.push('fever / chills');
  if (ctx.history?.rcc) evidence.push('known RCC / post-nephrectomy follow-up');
  if (ctx.distress?.level === 'high') evidence.push(`high non-verbal distress (fuzzy ${ctx.distress.score.toFixed(2)})`);

  const core = s.has('visible_hematuria') && s.has('flank_pain') && (ctx.painScore ?? 0) >= 7;
  const systemic = (ctx.weightLossKg ?? 0) >= 5 || s.has('dizziness') || s.has('fever');
  if (core && (systemic || ctx.history?.rcc)) {
    return { urgency: 'high', pattern: 'RCC follow-up + visible haematuria + severe flank pain + systemic signs', evidence, citations: [CITATIONS.PLAYBOOK, CITATIONS.EAU_RCC] };
  }
  if (s.has('visible_hematuria') || (s.has('flank_pain') && (ctx.painScore ?? 0) >= 7)) {
    return { urgency: 'medium', pattern: 'single red-flag symptom', evidence, citations: [CITATIONS.PLAYBOOK] };
  }
  return null;
}

// ---------- drug-disease conflicts ----------
const fill = (template, values) => template.replace(/\{(\w+)\}/g, (_, k) => values[k]);

// Does this trigger apply to the patient? Returns the reason text, or null.
function applyTrigger(t, { egfr, sbp, conditions, medications }) {
  switch (t.type) {
    case 'egfr_below':
      if (egfr != null && egfr < t.value) return fill(t.reason, { stage: ckdStage(egfr).stage, egfr });
      return null;
    case 'hypertension':
      if (conditions.some((c) => /hypertension/i.test(c)) || (sbp ?? 0) >= t.sbp_at_least) {
        return sbp ? fill(t.reason_with_sbp, { sbp }) : t.reason;
      }
      return null;
    case 'medication_contains':
      if (medications.some((m) => t.names.some((n) => m.toLowerCase().includes(n.toLowerCase())))) return t.reason;
      return null;
    default:
      throw new Error(`Unknown trigger type "${t.type}"`);
  }
}

// Drug-disease conflicts (systemic medicine - slide 4 of the deck).
export function checkConflicts({ drugClass, egfr, sbp, conditions = [], medications = [] }) {
  const out = [];
  for (const rule of DATA.conflict_rules) {
    if (rule.drug_class !== drugClass) continue;
    const reasons = [];
    const cites = [];
    for (const t of rule.triggers) {
      const reason = applyTrigger(t, { egfr, sbp, conditions, medications });
      if (reason) { reasons.push(reason); cites.push(CITATIONS[t.citation]); }
    }
    if (reasons.length) {
      out.push({
        drug: rule.display_drug,
        conditions: reasons,
        severity: reasons.length >= 2 ? rule.severity.two_or_more_reasons : rule.severity.one_reason,
        rationale: rule.rationale,
        citations: cites,
      });
    }
  }
  return out;
}

export function interpretVariant(v) {
  const base = { gene: v.gene, hgvs: v.hgvs_notation, lab_classification: v.clinical_significance };
  switch (`${v.gene}:${v.clinical_significance}`) {
    case 'VHL:pathogenic':
      return { ...base, relevance: 'high', summary: 'Hereditary predisposition to clear-cell RCC (von Hippel-Lindau). New, multifocal or contralateral tumours are possible; favours surveillance and kidney-sparing strategies; family counselling to be considered by a geneticist.', citations: [CITATIONS.VHL2011, CITATIONS.ACMG2015] };
    case 'APOL1:risk_allele':
      return { ...base, relevance: 'low', summary: 'Single APOL1 risk allele (heterozygous). The high-risk genotype requires two risk alleles; on its own this is not expected to drive CKD progression.', citations: [CITATIONS.APOL1] };
    default:
      if (v.clinical_significance === 'VUS') return { ...base, relevance: 'none', summary: 'Variant of uncertain significance: not clinically actionable; do not use for decisions.', citations: [CITATIONS.ACMG2015] };
      if (v.clinical_significance === 'benign') return { ...base, relevance: 'none', summary: 'Benign: not reported.', citations: [CITATIONS.ACMG2015] };
      return { ...base, relevance: 'unknown', summary: 'No rule in the Knowledge Model.', citations: [] };
  }
}

export function query(term) {
  const t = String(term || '').toLowerCase();
  return Object.entries(CITATIONS).filter(([k, v]) => k.toLowerCase().includes(t) || v.toLowerCase().includes(t)).map(([k, v]) => ({ id: k, citation: v }));
}
