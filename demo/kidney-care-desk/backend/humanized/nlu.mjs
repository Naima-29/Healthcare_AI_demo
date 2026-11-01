// Lightweight clinical NLU for patient messages (Italian + English).
// On the patient's phone this runs as a rule layer under the edge SLM; the SLM
// (if configured) can add findings but can never remove a red-flag finding.
import { classifyDrug } from '../knowledge/km.mjs';

// Words that only occur in Italian (no English homographs such as "urine" or "nausea").
const IT_HINT = /\b(ho|sono|mi|non|dolore|fianco|che|della|perso|testa|giorno|prendo|cosa|esame|niente|rosate|ieri|stasera|posso|buonasera)\b/i;

export function detectLanguage(text) {
  return IT_HINT.test(text) ? 'it' : 'en';
}

export function extract(text) {
  const t = text.toLowerCase();
  const symptoms = [];
  const add = (code, label) => { if (!symptoms.some((s) => s.code === code)) symptoms.push({ code, label }); };

  if (/(fianco|lombar|schiena|flank|lower back|\bside\b|sotto le costole|under the ribs)/.test(t) && /(dolor|male|pain|ache|fa male|hurts)/.test(t)) add('flank_pain', 'flank pain');
  if (/(sangue nelle urine|urine[^.]{0,20}(rosa|rosate|rosse|scure|sangue)|rosso scuro|ematuria|blood in (the )?urine|pink(ish)? urine|urine (is|are) (pink|red|dark)|dark red|hematuria|haematuria)/.test(t)) add('visible_hematuria', 'visible blood in urine');
  if (/(gira la testa|giramenti|vertigin|capogir|dizz)/.test(t)) add('dizziness', 'dizziness');
  if (/(nausea|nauseous|vomit)/.test(t)) add('nausea', 'nausea');
  if (/(febbre|fever|brividi|chills)/.test(t)) add('fever', 'fever / chills');
  if (/(stanc|fatigue|exhaust|spossat)/.test(t)) add('fatigue', 'fatigue');

  let painScore = null;
  const ps = t.match(/\b(\d{1,2})\s*(su|\/|out of|di)\s*10\b/) || t.match(/\b(?:direi|say|about|circa)\s+(\d{1,2})\b/);
  if (ps) painScore = Math.min(10, Number(ps[1]));

  let weightLossKg = null;
  const wl = t.match(/(?:perso|lost|calato di)\s*(?:circa\s*)?(\d{1,3})\s*(kg|chil|kilo)/);
  if (wl) { weightLossKg = Number(wl[1]); add('weight_loss', `weight loss ${weightLossKg} kg`); }

  const minimizer = /(niente di che|un po'|un po’|a bit|a little|sometimes|a volte|not too bad|nothing serious|sopportabile|non è niente|it's fine|va bene)/.test(t);
  const drug = classifyDrug(text);
  const intents = [];
  if (/(tac|\bct\b|scan|risonanza|immagin|imaging)/.test(t) && /(mostrat|result|esito|risultat|show|uscit|cosa ha|what did|com'è andat|referto)/.test(t)) intents.push('ask_imaging_result');
  if (/(dna|genetic|genetica|gene\b|mutazion)/.test(t) && /\?|cosa|what/.test(t)) intents.push('ask_genetics');
  if (drug) intents.push(/(posso|can i|devo|should i)/.test(t) ? 'ask_medication' : 'report_medication');

  // verbal distress 0..1 from words
  let verbal = 0;
  if (painScore != null) verbal = painScore / 10;
  else if (/(fortissim|insopportabil|terribil|severe|unbearable|atroce)/.test(t)) verbal = 0.85;
  else if (/(dolor|pain|male)/.test(t)) verbal = 0.45;
  if (minimizer) verbal = Math.max(0, verbal - 0.2);

  return { symptoms, painScore, weightLossKg, minimizer, drug, intents, verbal, language: detectLanguage(text) };
}
