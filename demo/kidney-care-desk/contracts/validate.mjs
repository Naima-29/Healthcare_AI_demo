// Minimal JSON-Schema-like validator (zero dependencies).
// Supports: type, enum, const, required, properties, additionalProperties:false,
// items, minItems, minLength, pattern, minimum, maximum, oneOf (first match).

export function validate(schema, value, path = '$') {
  const errors = [];
  check(schema, value, path, errors);
  return { ok: errors.length === 0, errors };
}

export function assertValid(schema, value, label = 'value') {
  const r = validate(schema, value);
  if (!r.ok) {
    const e = new Error(`${label} failed validation: ${r.errors.slice(0, 5).join('; ')}`);
    e.code = 'SCHEMA_INVALID';
    e.errors = r.errors;
    throw e;
  }
  return value;
}

function typeOf(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  if (Number.isInteger(v)) return 'integer';
  return typeof v;
}

function check(s, v, p, errs) {
  if (!s) return;
  if (s.oneOf) {
    const ok = s.oneOf.some((sub) => validate(sub, v, p).ok);
    if (!ok) errs.push(`${p}: does not match any allowed shape`);
    return;
  }
  if (s.const !== undefined && v !== s.const) errs.push(`${p}: must equal ${JSON.stringify(s.const)}`);
  if (s.enum && !s.enum.includes(v)) errs.push(`${p}: must be one of ${s.enum.join(', ')}`);
  if (s.type) {
    const types = Array.isArray(s.type) ? s.type : [s.type];
    const t = typeOf(v);
    const ok = types.some((x) => x === t || (x === 'number' && t === 'integer'));
    if (!ok) { errs.push(`${p}: expected ${types.join('|')}, got ${t}`); return; }
  }
  if (typeof v === 'string') {
    if (s.minLength && v.length < s.minLength) errs.push(`${p}: shorter than ${s.minLength}`);
    if (s.pattern && !new RegExp(s.pattern).test(v)) errs.push(`${p}: does not match ${s.pattern}`);
  }
  if (typeof v === 'number') {
    if (s.minimum !== undefined && v < s.minimum) errs.push(`${p}: below ${s.minimum}`);
    if (s.maximum !== undefined && v > s.maximum) errs.push(`${p}: above ${s.maximum}`);
  }
  if (Array.isArray(v)) {
    if (s.minItems && v.length < s.minItems) errs.push(`${p}: needs at least ${s.minItems} items`);
    if (s.items) v.forEach((x, i) => check(s.items, x, `${p}[${i}]`, errs));
  }
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    for (const r of s.required || []) if (!(r in v)) errs.push(`${p}.${r}: required`);
    if (s.properties) {
      for (const [k, sub] of Object.entries(s.properties)) if (k in v) check(sub, v[k], `${p}.${k}`, errs);
      if (s.additionalProperties === false) {
        for (const k of Object.keys(v)) if (!(k in s.properties)) errs.push(`${p}.${k}: not allowed`);
      }
    }
  }
}
