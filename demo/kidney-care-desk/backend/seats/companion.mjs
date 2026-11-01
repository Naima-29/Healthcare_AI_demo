// COMPANION - the patient's only door. Runs on the patient's phone (edge).
// Interview (v1 four questions), Humanized AI (fuzzy distress), red flags on
// the device with a fixed safety script, data collection, plain-language
// explanation of SIGNED clinician decisions. Talks only to the Dispatcher.
import { Seat } from '../runtime/seat.mjs';
import { RefusalError } from '../runtime/guard.mjs';
import { extract, detectLanguage } from '../humanized/nlu.mjs';
import { fromFeatures } from '../humanized/fuzzy.mjs';
import { hub, pace, config } from '../core/hub.mjs';

const T = {
  it: {
    q2: 'Ha notato cambiamenti nelle urine, per esempio sangue, urine più scure, bruciore o difficoltà a urinare?',
    q3: 'Ha avuto anche febbre, brividi, nausea, perdita di peso o di appetito, o si sente più debole del solito?',
    q4: 'Da 0 a 10, quanto è forte il dolore adesso? Ha giramenti di testa o fa fatica nelle attività di tutti i giorni?',
    q1: 'Mi può dire dove sente il dolore: sul fianco, sotto le costole, o più in basso sulla schiena? Peggiora con i movimenti?',
    hello: 'Sono qui con lei. Mi racconti come si sente stasera.',
    noted: 'Grazie, ho riportato tutto al suo team di cura. Se qualcosa peggiora mi scriva subito.',
    safety: 'Quello che mi descrive va valutato da un medico oggi stesso. Se può, vada al Pronto Soccorso; se si sente svenire, vede molto sangue o il dolore peggiora, chiami subito il 112. Ho già avvisato il medico reperibile del suo team, che la contatterà a breve.',
    med_ack: 'Grazie, l’ho segnalato al suo team. Per favore non cambi da solo le sue terapie: sarà il medico a darle indicazioni.',
    lab_ask: 'Per completare la valutazione mi serve il suo ultimo esame del sangue. Può fotografare il referto più recente e inviarmelo qui?',
    lab_thanks: (v) => `Grazie, ricevuto (referto del ${v.date ?? 'n.d.'}). L’ho inviato al suo team.`,
    imaging_hold: 'Le sue immagini sono in revisione da parte del suo medico, che gliene parlerà di persona. Nel frattempo, se i sintomi peggiorano, segua le indicazioni che le ho dato.',
    imaging_noaccess: 'Non ho accesso ai risultati delle immagini: gliene parlerà direttamente il suo medico.',
    genetics_hold: 'Di questi risultati le parlerà lo specialista, in un colloquio dedicato.',
    decision_head: (who) => `Il suo medico (${who}) ha rivisto il suo caso e le chiede di:`,
    decision_tail: 'Se qualcosa peggiora prima, chiami il 112. Sono qui se ha domande.',
    rule_nsaid: (who) => `Il suo nefrologo (${who}) le ha indicato di evitare l’ibuprofene e gli antinfiammatori simili. Per il dolore segua il piano che le ha dato il medico.`,
    refused_generic: 'Questa è una domanda per il suo medico: gliela inoltro.',
  },
  en: {
    q2: 'Have you noticed any change in your urine, such as blood, darker urine, burning or difficulty passing urine?',
    q3: 'Have you also had fever, chills, nausea, weight loss or loss of appetite, or felt weaker than usual?',
    q4: 'From 0 to 10, how strong is the pain now? Any dizziness, or trouble with everyday activities?',
    q1: 'Where exactly is the pain: on the side under the ribs, or lower in the back? Does it get worse when you move?',
    hello: 'I am here with you. Tell me how you feel tonight.',
    noted: 'Thank you, I have passed everything to your care team. If anything gets worse, message me straight away.',
    safety: 'What you describe needs to be seen by a doctor today. If you can, go to the emergency department; if you feel faint, see a lot of blood or the pain gets worse, call 112 now. I have already alerted the on-call doctor of your team, who will contact you shortly.',
    med_ack: 'Thank you, I have flagged this to your team. Please do not change your medicines on your own: your doctor will advise you.',
    lab_ask: 'To complete the assessment I need your latest blood test. Could you take a photo of your most recent lab report and send it here?',
    lab_thanks: (v) => `Thank you, received (report dated ${v.date ?? 'n/a'}). I have sent it to your team.`,
    imaging_hold: 'Your images are being reviewed by your doctor, who will discuss them with you in person. Meanwhile, if symptoms get worse, follow the advice I gave you.',
    imaging_noaccess: 'I do not have access to imaging results: your doctor will discuss them with you directly.',
    genetics_hold: 'A specialist will discuss these results with you in a dedicated conversation.',
    decision_head: (who) => `Your doctor (${who}) has reviewed your case and asks you to:`,
    decision_tail: 'If anything gets worse before then, call 112. I am here if you have questions.',
    rule_nsaid: (who) => `Your nephrologist (${who}) asked you to avoid ibuprofen and similar anti-inflammatory painkillers. For pain, follow the plan your doctor gave you.`,
    refused_generic: 'That is a question for your doctor: I will pass it on.',
  },
};

export class Companion extends Seat {
  constructor(ctx) {
    super(ctx, 'companion');
    this.sessions = new Map();
  }
  session(patient) {
    if (!this.sessions.has(patient)) this.sessions.set(patient, { lang: config.lang, symptoms: [], painScore: null, weightLossKg: null, minimizer: false, asked: new Set(), distress: null, redflagSent: false, reported: false, pendingLab: null, transcript: [] });
    return this.sessions.get(patient);
  }
  reset() { this.sessions.clear(); }

  async say(patient, text, release = null) {
    this.content(text, release); // hard block on refused content classes
    await this.tool('channel.send', { patient, text });
  }

  // ---------------------------------------------------------- patient door
  async onPatientText(patient, text, features = null) {
    const s = this.session(patient);
    hub.publish('patient_in', { patient, text, features: !!features });
    if (!s.transcript.length) this.meter(1); // per patient-month: first contact of the session
    s.transcript.push(text);
    const n = extract(text);
    if (n.language) s.lang = n.language;
    const L = T[s.lang];

    // Humanized AI: fuse words, face (PSPI) and voice with the fuzzy engine
    if (features || n.verbal) {
      const d = fromFeatures({ verbal: n.verbal, minimizer: n.minimizer, features: features || {} });
      if (!s.distress || d.score >= s.distress.score || features) s.distress = d;
      hub.publish('humanized', { patient, ...d, minimizer: n.minimizer });
    }

    // Questions the Companion must not answer itself
    if (n.intents.includes('ask_imaging_result')) {
      try {
        this.act('patient.disclose_imaging');
        hub.publish('note', { seat: this.id, text: 'No imaging access: the Imaging Reader never reports to the patient (defence in depth).' });
        await pace(0.5);
        return this.say(patient, L.imaging_noaccess);
      } catch (e) {
        if (!(e instanceof RefusalError)) throw e;
        await pace(0.5);
        return this.say(patient, L.imaging_hold);
      }
    }
    if (n.intents.includes('ask_genetics')) {
      try { this.act('patient.disclose_genetics'); } catch (e) { if (!(e instanceof RefusalError)) throw e; }
      return this.say(patient, L.genetics_hold);
    }

    // Medication mentioned
    if (n.drug) {
      const rule = this.confirmedRule(patient, 'nsaid_avoid');
      if (rule && n.drug.class === 'NSAID') {
        await pace(0.5);
        return this.say(patient, L.rule_nsaid(rule.confirmer), { verified: true, classes: ['dose_instruction'], rule: rule.id });
      }
      await this.send('medication_report', 'dispatcher', patient, { drug: n.drug.drug, class: n.drug.class, dose_text: text, reason: 'self-reported for pain' });
      this.meter(0);
      await pace(0.5);
      await this.say(patient, L.med_ack);
      return;
    }

    // Interview + red flags
    for (const sy of n.symptoms) if (!s.symptoms.some((x) => x.code === sy.code)) s.symptoms.push(sy);
    if (n.painScore != null) s.painScore = n.painScore;
    if (n.weightLossKg != null) s.weightLossKg = n.weightLossKg;
    s.minimizer = s.minimizer || n.minimizer;
    if (!s.symptoms.length && n.painScore == null) { await pace(0.5); return this.say(patient, L.hello); }

    await this.evaluate(patient);
  }

  async evaluate(patient) {
    const s = this.session(patient);
    const L = T[s.lang];
    const has = (c) => s.symptoms.some((x) => x.code === c);
    const history = { rcc: this.l3Mirror(patient).rcc };
    const ctx = { symptoms: s.symptoms, painScore: s.painScore, weightLossKg: s.weightLossKg, distress: s.distress, history };
    const { result: rf } = await this.tool('km.red_flags', { ctx });

    if (rf?.urgency === 'high' && !s.redflagSent) {
      s.redflagSent = true;
      await pace(0.5);
      await this.say(patient, L.safety); // fixed safety script: allowed by manifest.safety_script
      await this.send('red_flag_escalation', 'dispatcher', patient, { urgency: 'high', pattern: rf.pattern, evidence: rf.evidence, safety_script_sent: true });
      await this.reportSymptoms(patient);
      return;
    }
    // next interview question (v1 flow)
    const next = !has('flank_pain') && !s.asked.has('q1') ? 'q1'
      : !has('visible_hematuria') && !s.asked.has('q2') ? 'q2'
      : !(has('weight_loss') || has('nausea') || has('fever')) && !s.asked.has('q3') ? 'q3'
      : s.painScore == null && !s.asked.has('q4') ? 'q4' : null;
    if (next) {
      s.asked.add(next);
      await pace(0.5);
      return this.say(patient, `(${s.asked.size}/4) ${L[next]}`);
    }
    if (!s.reported) {
      await this.reportSymptoms(patient);
      await pace(0.5);
      await this.say(patient, L.noted);
    }
  }

  async reportSymptoms(patient) {
    const s = this.session(patient);
    s.reported = true;
    const d = s.distress || { score: 0, level: 'low', underreporting: false };
    await this.send('symptom_report', 'dispatcher', patient, {
      symptoms: s.symptoms.concat(s.painScore != null ? [{ code: 'pain_score', label: `pain ${s.painScore}/10` }] : []),
      distress: { score: d.score, level: d.level, face: d.face, voice: d.voice, verbal: d.verbal },
      underreporting: !!d.underreporting,
      transcript: s.transcript.join(' / ').slice(0, 600),
    });
    this.meter(0);
  }

  async attachLabPhoto(patient, ocrText) {
    const s = this.session(patient);
    hub.publish('patient_in', { patient, text: '📷 [photo of lab report]', attachment: 'lab_photo' });
    const v = await this.tool('lab.parse_report', { ocr_text: ocrText });
    await pace(0.5);
    await this.send('data_delivered', 'dispatcher', patient, { kind: 'lab', data: { ...v, source: 'patient photo (on-device OCR)' } }, s.pendingLab || undefined);
    s.pendingLab = null;
    await this.say(patient, T[s.lang].lab_thanks(v));
  }

  // ---------------------------------------------------------- from the hospital
  async onMessage(msg) {
    const { patient } = msg;
    const s = this.session(patient);
    const L = T[s.lang];
    if (msg.type === 'data_request') {
      s.pendingLab = msg.correlation || msg.id;
      await pace(0.5);
      await this.say(patient, L.lab_ask);
    }
    if (msg.type === 'clinician_decision') {
      const rel = this.ctx.board.release(msg.payload.decision_id);
      if (!rel?.verified) throw new Error('decision without a verified clinician release');
      const lines = msg.payload.items.map((i) => `• ${i.patient_text}`); // exactly what the clinician signed
      const text = [L.decision_head(msg.payload.signed_by), ...lines, msg.payload.note ? `\n${msg.payload.note}` : '', L.decision_tail].filter(Boolean).join('\n');
      await pace(0.5);
      await this.say(patient, text, rel);
    }
  }

  confirmedRule(patient, key) {
    return this.ctx.cortex.confirmed(`patient-${patient}`).find((k) => k.key === key) || null;
  }
  l3Mirror(patient) {
    // scoped mirror of L3: only what the Companion needs to recognise red flags
    return { rcc: true };
  }
}
