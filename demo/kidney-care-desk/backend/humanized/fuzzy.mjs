// Humanized AI - neuro-fuzzy distress engine (ref. [9] Haddiya & Pitrone).
// Inputs (all normalised 0..1):
//   verbal   - pain/distress expressed in words (NLU)
//   face     - facial pain intensity from action units, PSPI / 16
//              PSPI = AU4 + max(AU6, AU7) + max(AU9, AU10) + AU43 (Prkachin & Solomon)
//   voice    - paralinguistic strain: tremor, pitch variability, speech-rate deviation
// Output: distress score 0..1 (Mamdani inference, centroid defuzzification),
// level low|medium|high, and an under-reporting flag when the body says more
// than the words ("nothing serious" / "niente di che" with a high PSPI).

const tri = (a, b, c) => (x) => (x <= a || x >= c ? (x === b ? 1 : 0) : x < b ? (x - a) / (b - a) : (c - x) / (c - b));
const shoulderL = (a, b) => (x) => (x <= a ? 1 : x >= b ? 0 : (b - x) / (b - a));
const shoulderR = (a, b) => (x) => (x <= a ? 0 : x >= b ? 1 : (x - a) / (b - a));

const IN = { low: shoulderL(0.2, 0.45), med: tri(0.25, 0.5, 0.75), high: shoulderR(0.55, 0.8) };
const OUT = { low: shoulderL(0.15, 0.4), med: tri(0.25, 0.5, 0.75), high: shoulderR(0.6, 0.85) };

export function pspi(au = {}) {
  const v = (k) => Number(au[k] || 0); // AU intensities 0..5 (AU43 0/1)
  return v('AU4') + Math.max(v('AU6'), v('AU7')) + Math.max(v('AU9'), v('AU10')) + v('AU43');
}

export function voiceStrain(voice = {}) {
  const t = Number(voice.tremor || 0), p = Number(voice.pitch_var || 0), r = Math.abs(Number(voice.rate_dev || 0));
  return Math.min(1, 0.5 * t + 0.3 * p + 0.2 * r);
}

export function distress({ verbal = 0, face = 0, voice = 0, minimizer = false } = {}) {
  const nonverbal = Math.max(face, 0.7 * voice + 0.3 * face);
  const V = Object.fromEntries(Object.entries(IN).map(([k, f]) => [k, f(verbal)]));
  const N = Object.fromEntries(Object.entries(IN).map(([k, f]) => [k, f(nonverbal)]));
  const min = Math.min, max = Math.max;
  // Rule base (firing strengths per output set)
  const fire = {
    high: max(V.high, N.high, min(V.med, N.med) * 0.6),
    med: max(min(V.med, N.low), min(V.low, N.med), min(V.med, N.med)),
    low: min(V.low, N.low),
  };
  // Mamdani: clip output sets, aggregate with max, centroid
  let num = 0, den = 0;
  for (let x = 0; x <= 1.0001; x += 0.01) {
    const mu = max(min(fire.low, OUT.low(x)), min(fire.med, OUT.med(x)), min(fire.high, OUT.high(x)));
    num += x * mu; den += mu;
  }
  const score = den ? num / den : 0;
  const level = score >= 0.62 ? 'high' : score >= 0.38 ? 'medium' : 'low';
  const underreporting = nonverbal - verbal >= 0.3 && (minimizer || verbal < 0.5);
  return { score: Number(score.toFixed(3)), level, verbal, nonverbal: Number(nonverbal.toFixed(3)), underreporting, fired: fire };
}

export function fromFeatures({ verbal, minimizer, features = {} }) {
  const face = Math.min(1, pspi(features.au || {}) / 16);
  const voice = voiceStrain(features.voice || {});
  return { ...distress({ verbal, face, voice, minimizer }), face: Number(face.toFixed(3)), voice: Number(voice.toFixed(3)) };
}
