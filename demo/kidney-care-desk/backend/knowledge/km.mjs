// Knowledge Model (L0 medical knowledge + L1 specialty playbook).
// Every rule carries the citation the Safety Verifier will require.
// NOTE: clinical rules here are a demo knowledge base; they must be reviewed
// by the clinical team before any real use.

export const CITATIONS = {
  KDIGO2024: '[14] KDIGO 2024 CKD Guideline, Kidney Int 2024;105(4S):S117-S314',
  EAU_RCC: '[15] EAU Guidelines on Renal Cell Carcinoma (current edition)',
  ACMG2015: '[16] Richards S et al. ACMG/AMP variant interpretation, Genet Med 2015;17:405-424',
  VHL2011: '[17] Maher ER et al. von Hippel-Lindau disease, Eur J Hum Genet 2011;19:617-623',
  BEERS2023: '[3] AGS Beers Criteria 2023 update, J Am Geriatr Soc 2023;71:2052-2081',
  TKI_LABEL: 'Sunitinib SmPC - hypertension and proteinuria are labelled adverse effects',
  PLAYBOOK: 'L1 nephro-oncology playbook (Loop demo; to be validated by the clinical team)',
  APOL1: 'APOL1-mediated kidney disease: high-risk genotype = two risk alleles (G1/G2), recessive model',
};

export const DRUG_CLASSES = [
  { re: /\b(ibuprofen[eo]?|brufen|moment|nurofen|ketoprofen[eo]?|oki|diclofenac|voltaren|naprossene|naproxen|aspirin[ae]?|nsaid|fans)\b/i, drug: 'ibuprofen', class: 'NSAID' },
  { re: /\b(paracetamol[eo]?|tachipirina|acetaminophen)\b/i, drug: 'paracetamol', class: 'non_NSAID_analgesic' },
];

export function classifyDrug(text) {
  for (const d of DRUG_CLASSES) {
    const m = text.match(d.re);
    if (m) return { drug: m[0].toLowerCase(), class: d.class };
  }
  return null;
}

export function ckdStage(egfr) {
  if (egfr == null) return null;
  const g = egfr >= 90 ? 'G1' : egfr >= 60 ? 'G2' : egfr >= 45 ? 'G3a' : egfr >= 30 ? 'G3b' : egfr >= 15 ? 'G4' : 'G5';
  return { stage: g, egfr, citation: CITATIONS.KDIGO2024 };
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

// Drug-disease conflicts (systemic medicine - slide 4 of the deck).
export function checkConflicts({ drugClass, egfr, sbp, conditions = [], medications = [] }) {
  const out = [];
  if (drugClass === 'NSAID') {
    const reasons = [];
    const cites = [];
    if (egfr != null && egfr < 60) { reasons.push(`CKD ${ckdStage(egfr).stage} (eGFR ${egfr})`); cites.push(CITATIONS.KDIGO2024); }
    if (conditions.some((c) => /hypertension/i.test(c)) || (sbp ?? 0) >= 140) {
      reasons.push(`hypertension${sbp ? ` (home SBP ~${sbp} mmHg)` : ''}`); cites.push(CITATIONS.BEERS2023);
    }
    if (medications.some((m) => /sunitinib|tki/i.test(m))) { reasons.push('TKI therapy (hypertension, proteinuria)'); cites.push(CITATIONS.TKI_LABEL); }
    if (reasons.length) {
      out.push({
        drug: 'NSAID (ibuprofen)', conditions: reasons, severity: reasons.length >= 2 ? 'high' : 'moderate',
        rationale: 'NSAIDs reduce renal perfusion and raise blood pressure; prolonged use is to be avoided in CKD, particularly with eGFR < 60 and uncontrolled hypertension.',
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
