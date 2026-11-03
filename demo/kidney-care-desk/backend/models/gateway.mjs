// Model gateway - any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio,
// Moonshot/Kimi, ...). Zero lock-in: swap the URL, keep the code.
//   EDGE_LLM_BASE_URL  e.g. http://localhost:11434/v1   (Ollama on the edge node)
//   EDGE_LLM_MODEL     e.g. qwen3:4b  or your Loop Q SLM tag
//   CLOUD_LLM_BASE_URL / CLOUD_LLM_MODEL / CLOUD_LLM_API_KEY  (teacher / fallback)
// When nothing is configured every call returns null and the seats use their
// deterministic rule paths, so the demo never depends on a model being up.
// Cloud calls REQUIRE a de-identification stamp from the Safety Verifier.
import { hub } from '../core/hub.mjs';

function cfg(prefix) {
  return {
    base: process.env[`${prefix}_LLM_BASE_URL`],
    model: process.env[`${prefix}_LLM_MODEL`],
    key: process.env[`${prefix}_LLM_API_KEY`] || 'none',
  };
}

async function chat(c, messages, { json = false, timeoutMs = 12000 } = {}) {
  if (!c.base || !c.model) return null;
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${c.base.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      signal: ctrl.signal,
      headers: { 'content-type': 'application/json', authorization: `Bearer ${c.key}` },
      body: JSON.stringify({ model: c.model, messages, temperature: 0.1, ...(json ? { response_format: { type: 'json_object' } } : {}) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.choices?.[0]?.message?.content ?? null;
  } catch (e) {
    hub.publish('model', { ok: false, base: c.base, error: String(e.message || e) });
    return null;
  } finally {
    clearTimeout(t);
  }
}

export const gateway = {
  status() {
    const e = cfg('EDGE'), c = cfg('CLOUD');
    return { edge: e.base ? `${e.model} @ ${e.base}` : 'rules only (no edge SLM configured)', cloud: c.base ? `${c.model} @ ${c.base}` : 'disabled' };
  },
  async edge(messages, opts) {
    const out = await chat(cfg('EDGE'), messages, opts);
    if (out != null) hub.publish('model', { ok: true, tier: 'edge', model: cfg('EDGE').model });
    return out;
  },
  async cloud(messages, stamp, opts) {
    if (!stamp || stamp.deidentified !== true || stamp.by !== 'safety_verifier') {
      const e = new Error('refused: transmit_identifiable_external (no de-identification stamp)');
      e.code = 'REFUSED'; e.refusal = 'transmit_identifiable_external';
      throw e;
    }
    return chat(cfg('CLOUD'), messages, opts);
  },
};
