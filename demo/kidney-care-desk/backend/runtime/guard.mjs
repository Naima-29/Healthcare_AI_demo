// Runtime guard: refusals are enforced HERE, at the tool/action layer, not in
// prompts. A refusal written in natural language by the Director becomes an
// entry in manifest.refuses, which blocks actions and content classes.
import { REFUSALS } from '../../contracts/catalog.mjs';
import { hub } from '../core/hub.mjs';

export class RefusalError extends Error {
  constructor(seat, refusal, what) {
    super(`${seat} refused (${refusal}): ${what}`);
    this.code = 'REFUSED'; this.seat = seat; this.refusal = refusal; this.what = what;
  }
}

// Content classifiers for anything addressed to a patient (IT + EN).
export const CONTENT_CLASSES = {
  diagnosis_claim: /\b(you have|hai (un|una)|lei ha|è (un|una)|si tratta di|diagnos\w*|confirmed|confermat\w*)\b[^.]{0,60}\b(cancer|tumou?r|carcinoma|tumore|cancro|recurrence|recidiva|metasta\w*)\b/i,
  imaging_result: /\b(ct|tac|scan|imaging|immagin\w*|esame)\b[^.]{0,80}\b(shows?|showed|mostra\w*|evidenzia\w*|tumou?r|tumore|mass[ae]?|lesion\w*|kidney_tumor|probabilit\w*|confidence|99[.,]9)\b/i,
  dose_instruction: /\b(take|prendi|prenda|assumi|assuma|stop|sospendi|sospenda|interrompi|interrompa|increase|aumenta|aumenti|reduce|riduci|riduca|double|raddoppia)\b[^.]{0,40}\b(\d+\s?mg|dose|dosi|dosaggio|compress\w*|pills?|tablets?|ibuprofen\w*|paracetamol\w*|sunitinib|amlodipin\w*)\b/i,
  prescription: /\b(i prescribe|ti prescrivo|le prescrivo|prescription for)\b/i,
  genetic_result: /\b(VHL|APOL1|PBRM1|BAP1|variant[ei]?|mutazion\w*|mutation)\b/i,
};

export function classify(text) {
  return Object.entries(CONTENT_CLASSES).filter(([, re]) => re.test(text)).map(([k]) => k);
}

export function checkAction(manifest, action) {
  for (const r of manifest.refuses) {
    if (REFUSALS[r]?.actions.includes(action)) {
      const e = new RefusalError(manifest.seat, r, `action ${action}`);
      hub.publish('refusal', { seat: manifest.seat, refusal: r, action, label: REFUSALS[r].label, version: manifest.version });
      throw e;
    }
  }
}

// `release` = a verified clinician release (gate passed) for this content.
export function checkContent(manifest, text, release = null) {
  const classes = classify(text);
  for (const r of manifest.refuses) {
    for (const c of REFUSALS[r]?.content || []) {
      if (classes.includes(c) && !(release && release.verified && release.classes?.includes(c))) {
        hub.publish('refusal', { seat: manifest.seat, refusal: r, content_class: c, label: REFUSALS[r].label, text: text.slice(0, 160), version: manifest.version });
        throw new RefusalError(manifest.seat, r, `content class ${c}`);
      }
    }
  }
  return classes;
}
