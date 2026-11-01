// Kidney Care Desk v2 - front end (no build step, no framework).
// Three doors (Director, Patient, Clinicians) + observability views.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok && r.status !== 202) throw Object.assign(new Error(j.error || r.statusText), j);
  return j;
};
const toast = (t) => { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), 2600); };
const time = (ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

const S = {
  data: null, events: [], preset: 'pain', humanized: null,
  showAcks: false, showTools: false, agentSel: 'companion', agentVer: null,
  forms: {}, running: null, done: new Set(), view: 'stage',
};

const AVATAR = { companion: '/assets/patient_agent.jpg', case_briefer: '/assets/doctor_agent.jpg' };
const INITIALS = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();

const PRESETS = {
  none: null,
  neutral: { au: { AU4: 0, AU6: 0, AU9: 0, AU43: 0 }, voice: { tremor: 0.05, pitch_var: 0.1, rate_dev: 0 } },
  pain: { au: { AU4: 3, AU6: 3, AU7: 2, AU9: 2, AU10: 1, AU43: 1 }, voice: { tremor: 0.7, pitch_var: 0.6, rate_dev: -0.4 } },
  strong: { au: { AU4: 4, AU6: 4, AU7: 4, AU9: 3, AU10: 3, AU43: 1 }, voice: { tremor: 0.9, pitch_var: 0.8, rate_dev: -0.6 } },
};
// UI copy for the two demo languages (server decides: DEMO_LANG=en|it).
const UI = {
  en: {
    patient: [
      'Good evening… I have a pain in my right side, under the ribs. Nothing serious, but it won’t go away and it gets worse at night.',
      'My urine is pinkish, sometimes really dark red.',
      'I’ve lost 23 kg in the last three months and sometimes I feel nauseous.',
      'I’d say 8 out of 10. And I often feel dizzy.',
      'For the pain I’m taking ibuprofen 600, twice a day.',
      'I had a CT scan yesterday: what did it show?',
      'What does my DNA say?',
      'Can I take another ibuprofen tonight? The pain is back.',
      'Ho un dolore al fianco destro e le urine sono rosate.',
    ],
    director: [
      'Companion: never disclose an imaging result to the patient before the doctor has reviewed it.',
      'How is the Companion doing?',
      'List the agents',
      'How are all the agents doing?',
      'Create an agent for nephrology phone follow-up: it calls CKD patients on WhatsApp 7 days after their visit and collects home blood pressure from their wearables and their questions. It must never change a therapy or make a diagnosis; every piece of advice must be approved by the nephrologist.',
      'Renal Guardian: never write to the clinical record.',
      'help',
    ],
    phoneInput: 'Write a message…', phoneHello: 'Good evening Marco, I am your Companion. I am here whenever you need me.',
    dirInput: 'Write to the Chief of Staff, in plain language…', dirSend: 'Send',
    dirHello: 'Good evening, Director. Tell me what you would like to automate, or which rule to add to an agent. Type "help" for examples.',
    instr: 'e.g. Never disclose an imaging result to the patient before the doctor has reviewed it.',
    breakText: 'I had a CT scan yesterday: what did it show?',
  },
  it: {
    patient: [
      'Buonasera… ho un dolore al fianco destro, sotto le costole. Niente di che, ma non passa e di notte peggiora.',
      'Le urine sono rosate, a volte proprio rosso scuro.',
      'Ho perso 23 kg negli ultimi tre mesi e a volte ho nausea.',
      'Direi 8 su 10. E mi gira spesso la testa.',
      'Per il dolore sto prendendo ibuprofene 600, due volte al giorno.',
      'Ieri ho fatto la TAC: cosa ha mostrato?',
      'Cosa dice il mio DNA?',
      'Posso prendere un altro ibuprofene stasera? Il dolore è tornato.',
      'I have pain on my right side and my urine is pink.',
    ],
    director: [
      'Companion: non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.',
      'Come sta andando il Companion?',
      'Elenca gli agenti',
      'Come stanno andando tutti gli agenti?',
      'Crea un agente per il follow-up telefonico di nefrologia: chiama via WhatsApp i pazienti con CKD 7 giorni dopo la visita, raccoglie la pressione domiciliare dagli indossabili e le loro domande. Non deve mai modificare le terapie né fare diagnosi; ogni consiglio deve essere approvato dal nefrologo.',
      'Renal Guardian: non scrivere mai nella cartella clinica.',
      'aiuto',
    ],
    phoneInput: 'Scrivi un messaggio…', phoneHello: 'Buonasera Marco, sono il suo Companion. Sono qui quando ha bisogno.',
    dirInput: 'Scriva al Chief of Staff, in linguaggio naturale…', dirSend: 'Invia',
    dirHello: 'Buonasera Direttore. Mi dica cosa vuole automatizzare, o quale regola aggiungere a un agente. Scriva «aiuto» per gli esempi.',
    instr: 'es. Non comunicare mai al paziente l’esito di un esame di imaging prima che il medico lo abbia rivisto.',
    breakText: 'Ieri ho fatto la TAC: cosa ha mostrato?',
  },
};
const U = () => UI[S.data?.lang === 'it' ? 'it' : 'en'];

// ------------------------------------------------------------------ boot
async function boot() {
  S.data = await api('/api/state');
  S.events = S.data.history || [];
  for (const e of S.events) track(e);
  wireTabs();
  renderAll();
  const es = new EventSource('/events');
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (e.kind === 'reset') { S.events = []; S.done.clear(); S.running = null; S.humanized = null; S.forms = {}; refresh(); renderAll(); return; }
    S.events.push(e);
    if (S.events.length > 1500) S.events.shift();
    track(e);
    onEvent(e);
  };
  setInterval(tickTimers, 1000);
}

function track(e) {
  if (e.kind === 'humanized') S.humanized = e.data;
  if (e.kind === 'scenario') { if (e.data.op === 'step') S.running = e.data.n; if (e.data.op === 'done') { S.done.add(e.data.n); S.running = null; } }
}

let refreshT;
function refresh() { clearTimeout(refreshT); refreshT = setTimeout(async () => { S.data = await api('/api/state'); renderData(); }, 180); }

function onEvent(e) {
  if (['patient_in', 'patient_out', 'humanized'].includes(e.kind)) renderPhoneMsgs();
  if (['director_in', 'director_out'].includes(e.kind)) renderConsoleMsgs();
  if (['a2a', 'refusal', 'gate', 'page', 'verification', 'note', 'manifest', 'tool', 'error', 'a2a_rejected', 'scenario', 'tool_blocked'].includes(e.kind)) appendStream(e);
  if (e.kind === 'scenario') renderGolden();
  if (['board', 'manifest', 'memory', 'case', 'meter', 'gate'].includes(e.kind)) refresh();
}

function wireTabs() {
  $$('#tabs button').forEach((b) => b.onclick = () => {
    $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${b.dataset.view}`));
    S.view = b.dataset.view;
    renderData();
  });
}

function renderAll() {
  renderGolden(); renderStatus();
  mountPhone(); mountConsole(); mountStream();
  renderData();
}
function renderData() {
  renderStatus(); renderBoard();
  if (S.view === 'roster') renderRoster();
  if (S.view === 'agent') renderAgent();
  if (S.view === 'memory') renderMemory();
}

// ------------------------------------------------------------------ header
function renderGolden() {
  const el = $('#golden');
  const steps = S.data?.steps || [];
  el.innerHTML = `<span class="lbl">Golden path</span>` + steps.map((s) => `<button class="step ${s.n >= 6 ? 'gate' : ''} ${S.done.has(s.n) ? 'done' : ''} ${S.running === s.n ? 'running' : ''}" data-n="${s.n}" title="${esc(s.title)}"><b>${s.n}</b>${esc(s.short)}</button>`).join('')
    + `<button class="btn small" id="autoplay">▶ Autoplay</button><button class="btn small ghost" id="reset">Reset</button>`;
  $$('.step', el).forEach((b) => b.onclick = () => api('/api/scenario/step', { n: Number(b.dataset.n) }).catch((e) => toast(e.message)));
  $('#autoplay').onclick = () => api('/api/scenario/autoplay', {}).catch((e) => toast(e.message));
  $('#reset').onclick = () => api('/api/reset', {}).then(() => toast('Demo reset'));
}
function renderStatus() {
  const d = S.data; if (!d) return;
  const c = d.config;
  $('#status').innerHTML = [
    `<span class="pill ${/rules only/.test(d.models.edge) ? '' : 'ok'}" title="${esc(d.models.edge)}">edge SLM: ${/rules only/.test(d.models.edge) ? 'rules' : 'on'}</span>`,
    `<span class="pill">cloud: ${d.models.cloud === 'disabled' ? 'off' : 'de-identified only'}</span>`,
    `<span class="pill ${c.fhir ? 'ok' : ''}">FHIR: ${c.fhir ? 'live' : 'synthetic'}</span>`,
    `<span class="pill ${c.dicomweb ? 'ok' : ''}">PACS: ${c.dicomweb ? 'live' : 'demo'}</span>`,
    `<span class="pill ${c.telegram ? 'ok' : ''}">Telegram: ${c.telegram ? 'on' : 'off'}</span>`,
  ].join('');
}

// ------------------------------------------------------------------ PATIENT DOOR (phone)
function mountPhone() {
  $$('[data-mount="phone"]').forEach((m) => {
    m.innerHTML = `<div class="phone">
      <div class="ph-head"><img src="/assets/patient_agent.jpg" alt=""><div><div class="t">Loop Companion</div><div class="s">your care companion · on this phone</div></div></div>
      <div class="ph-msgs"></div>
      <div class="ph-sense">
        <div class="sense-row"><span class="k">Camera + mic (Humanized AI):</span>
          <span class="seg">${Object.keys(PRESETS).map((k) => `<button data-p="${k}">${k}</button>`).join('')}</span></div>
        <div class="sense-row"><span class="k">Distress</span><div class="gauge"><i></i></div><span class="hz"></span></div>
      </div>
      <div class="ph-quick">${U().patient.map((q) => `<button title="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div class="ph-input"><button class="att" title="Send photo of lab report">📷</button><input placeholder="${esc(U().phoneInput)}" /><button class="send">➤</button></div>
    </div>`;
    const input = $('input', m);
    const sendText = (t) => { if (!t.trim()) return; api('/api/patient/message', { text: t, features: PRESETS[S.preset] }).catch((e) => toast(e.message)); input.value = ''; };
    $('.send', m).onclick = () => sendText(input.value);
    input.onkeydown = (e) => { if (e.key === 'Enter') sendText(input.value); };
    $$('.ph-quick button', m).forEach((b) => b.onclick = () => sendText(b.title));
    $('.att', m).onclick = () => api('/api/patient/attach-lab', {}).catch((e) => toast(e.message));
    $$('.seg button', m).forEach((b) => b.onclick = () => { S.preset = b.dataset.p; renderPhoneMsgs(); });
  });
  renderPhoneMsgs();
}
function renderPhoneMsgs() {
  const msgs = S.events.filter((e) => e.kind === 'patient_in' || e.kind === 'patient_out');
  const html = msgs.map((e) => {
    const out = e.kind === 'patient_in';
    const t = e.data.text;
    const cls = !out && /112/.test(t) && /oggi stesso|today/.test(t) ? 'safety' : !out && /rivisto il suo caso|reviewed your case|le ha indicato|asked you to avoid/.test(t) ? 'decision' : '';
    return `<div class="bubble ${out ? 'out' : 'in'} ${cls}">${esc(t)}<span class="meta">${time(e.ts)}${out ? ' ✓✓' : ''}</span></div>`;
  }).join('') || `<div class="bubble in">${esc(U().phoneHello)}<span class="meta">—</span></div>`;
  $$('[data-mount="phone"]').forEach((m) => {
    const box = $('.ph-msgs', m); if (!box) return;
    box.innerHTML = html; box.scrollTop = box.scrollHeight;
    $$('.seg button', m).forEach((b) => b.classList.toggle('on', b.dataset.p === S.preset));
    const h = S.humanized;
    const g = $('.gauge', m);
    g.className = `gauge ${h?.level || ''}`;
    $('i', g).style.width = `${Math.round((h?.score || 0) * 100)}%`;
    $('.hz', m).innerHTML = h ? `${h.level} ${h.score.toFixed(2)} · face ${h.face} · voice ${h.voice}${h.underreporting ? ' · <span class="flag">under-reporting</span>' : ''}` : '—';
  });
}

// ------------------------------------------------------------------ DIRECTOR DOOR (Chief of Staff)
function mountConsole() {
  $$('[data-mount="director"]').forEach((m) => {
    m.innerHTML = `<div class="console">
      <div class="con-head"><span class="avatar" style="width:30px;height:30px;font-size:12px;background:#3C3949">CS</span><div><div>Chief of Staff</div><div class="s">the Director’s only agent · builds, changes and reports on every seat</div></div></div>
      <div class="con-msgs"></div>
      <div class="con-quick">${U().director.map((q) => `<button title="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div class="con-input"><textarea placeholder="${esc(U().dirInput)}"></textarea><button class="btn">${esc(U().dirSend)}</button></div>
    </div>`;
    const ta = $('textarea', m);
    const go = (t) => { if (!t.trim()) return; api('/api/director', { text: t }).catch((e) => toast(e.message)); ta.value = ''; };
    $('.btn', m).onclick = () => go(ta.value);
    ta.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(ta.value); } };
    $$('.con-quick button', m).forEach((b) => b.onclick = () => go(b.title));
  });
  renderConsoleMsgs();
}
function renderConsoleMsgs() {
  const msgs = S.events.filter((e) => e.kind === 'director_in' || e.kind === 'director_out');
  const html = msgs.map((e) => e.kind === 'director_in'
    ? `<div class="cmsg dir">${esc(e.data.text)}</div>`
    : `<div class="cmsg cos">${esc(e.data.text)}${e.data.diff ? `<div class="diff">${[...e.data.diff.refuses.map((r) => `+ refuses: ${r}`), ...e.data.diff.tools.map((t) => `+ tool: ${t}`), ...e.data.diff.gates.map((g) => `+ gate: ${g}`)].map(esc).join('<br>')}</div>` : ''}</div>`).join('')
    || `<div class="cmsg cos">${esc(U().dirHello)}</div>`;
  $$('[data-mount="director"] .con-msgs').forEach((box) => { box.innerHTML = html; box.scrollTop = box.scrollHeight; });
}

// ------------------------------------------------------------------ STREAM (behind the doors)
function mountStream() {
  $$('[data-mount="stream"]').forEach((m) => {
    m.innerHTML = `<div class="stream"><div class="st-tools"><b>A2A stream</b>
      <label><input type="checkbox" class="acks"> acknowledgements</label>
      <label><input type="checkbox" class="tools"> MCP tool calls</label>
      <span style="margin-left:auto;color:var(--muted)">Ed25519-signed · verified by the Dispatcher</span></div><div class="st-list"></div></div>`;
    $('.acks', m).checked = S.showAcks; $('.tools', m).checked = S.showTools;
    $('.acks', m).onchange = (e) => { S.showAcks = e.target.checked; redrawStream(); };
    $('.tools', m).onchange = (e) => { S.showTools = e.target.checked; redrawStream(); };
  });
  redrawStream();
}
function streamHtml(e) {
  const d = e.data;
  if (e.kind === 'a2a') {
    if (d.type === 'acknowledgment' && !S.showAcks) return '';
    const hot = ['red_flag_escalation', 'clinician_decision', 'therapy_conflict', 'imaging_signal'].includes(d.type);
    return `<div class="ev"><div class="row"><span class="who">${esc(d.from)}</span><span class="arrow">→</span><span class="who">${esc(d.to)}</span><span class="type ${hot ? 'hot' : ''}">${esc(d.type)}</span>${d.verified ? '<span class="sig">✓ signed</span>' : '<span class="sig" style="color:var(--bad)">✗ sig</span>'}${d.payload?.verification?.passed ? '<span class="sig">✓ verified</span>' : ''}<span class="t">${time(e.ts)}</span></div>
      <div class="sum">${esc(summary(d))}</div><details><summary class="sum">payload</summary><pre>${esc(JSON.stringify(d, null, 2))}</pre></details></div>`;
  }
  if (e.kind === 'tool' || e.kind === 'tool_blocked') {
    if (!S.showTools) return '';
    return `<div class="ev note"><div class="row"><span class="who">${esc(d.seat)}</span><span class="arrow">⇢ MCP</span><span class="type">${esc(d.tool)}</span>${e.kind === 'tool_blocked' ? '<b style="color:var(--bad)">blocked</b>' : ''}<span class="t">${time(e.ts)}</span></div></div>`;
  }
  const map = {
    refusal: () => `<div class="ev refusal"><div class="row"><b>⛔ Refusal enforced</b><span class="who">${esc(d.seat)} v${d.version}</span><span class="type">${esc(d.refusal)}</span><span class="t">${time(e.ts)}</span></div><div class="sum">${esc(d.label)} — blocked ${esc(d.action || d.content_class)}</div></div>`,
    gate: () => `<div class="ev gate"><div class="row"><b>✍ Human gate</b><span class="who">${esc(d.clinician)}</span><span class="type">${esc(d.gate)}</span><span>${esc(d.action)}</span>${d.verified ? '<span class="sig">✓ signed</span>' : ''}<span class="t">${time(e.ts)}</span></div></div>`,
    page: () => `<div class="ev page"><div class="row"><b>📟 Paged</b><span class="who">${esc(d.name)}</span><span class="type">${esc(d.pager || '')}</span><span class="t">${time(e.ts)}</span></div><div class="sum">${esc(d.message)}</div></div>`,
    verification: () => `<div class="ev verify"><div class="row"><b>✔ Safety Verifier</b><span class="type">${esc(d.type)}</span><span>${d.passed ? 'passed' : 'FAILED'}</span><span class="t">${time(e.ts)}</span></div><div class="sum">${d.checks.map((c) => `${c.ok ? '✓' : '✗'} ${esc(c.check)}`).join(' · ')}</div></div>`,
    note: () => `<div class="ev note"><div class="row"><span class="who">${esc(d.seat)}</span><span class="sum">${esc(d.text)}</span><span class="t">${time(e.ts)}</span></div></div>`,
    manifest: () => `<div class="ev manifest"><div class="row"><b>⟳ ${d.op === 'create' ? 'Seat created' : 'Hot reload'}</b><span class="who">${esc(d.seat)} v${d.version ?? d.manifest?.version}</span><span class="t">${time(e.ts)}</span></div></div>`,
    error: () => `<div class="ev refusal"><div class="row"><b>Error</b><span class="who">${esc(d.seat)}</span><span class="sum">${esc(d.message)}</span></div></div>`,
    a2a_rejected: () => `<div class="ev refusal"><div class="row"><b>A2A rejected</b><span class="sum">${esc(JSON.stringify(d))}</span></div></div>`,
    scenario: () => d.op === 'step' ? `<div class="ev note"><div class="row"><b>Step ${d.n}</b><span class="sum">${esc(d.title)}</span></div></div>` : '',
  };
  return map[e.kind]?.() || '';
}
function summary(d) {
  const p = d.payload || {};
  switch (d.type) {
    case 'symptom_report': return `${(p.symptoms || []).map((s) => s.label).join(', ')} · distress ${p.distress?.level} ${p.distress?.score}${p.underreporting ? ' · under-reporting' : ''}`;
    case 'red_flag_escalation': return `${p.urgency?.toUpperCase()}: ${p.pattern}`;
    case 'medication_report': return `${p.drug} (${p.class})`;
    case 'data_request': return `${p.what} — ${p.why}`;
    case 'data_delivered': return p.kind === 'lab' ? `lab: eGFR ${p.data?.egfr}, creatinine ${p.data?.creatinine_mg_dl} (${p.data?.date})` : `${p.kind}: ${(p.data?.findings || []).map((f) => f.gene).join(', ')}`;
    case 'imaging_signal': return `"${p.label}" p_cal ${p.p_calibrated} (raw ${p.p_raw}) · quality ${p.quality?.quality}`;
    case 'therapy_conflict': return `${p.drug} × ${(p.conditions || []).join(' · ')}`;
    case 'case_pack_ready': return `case pack v${p.version} → Clinical Board`;
    case 'clinician_decision': return `signed by ${p.signed_by}: ${(p.items || []).map((i) => i.code).join(', ')}`;
    case 'acknowledgment': return `${p.status} ${p.ack_of}`;
    default: return '';
  }
}
function redrawStream() {
  const html = S.events.map(streamHtml).join('');
  $$('[data-mount="stream"] .st-list').forEach((l) => { l.innerHTML = html; l.scrollTop = l.scrollHeight; });
}
function appendStream(e) {
  const h = streamHtml(e); if (!h) return;
  $$('[data-mount="stream"] .st-list').forEach((l) => { const near = l.scrollHeight - l.scrollTop - l.clientHeight < 80; l.insertAdjacentHTML('beforeend', h); if (near) l.scrollTop = l.scrollHeight; });
}

// ------------------------------------------------------------------ CLINICIAN DOOR (Clinical Board)
const CLINICIANS = ['Dr. A. Rossi', 'Dr. L. Bianchi', 'Dr. E. Conti', 'Dr. P. Greco'];
function renderBoard() {
  if ($$('[data-mount="board"]').some((m) => m.contains(document.activeElement) && /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName))) return;
  const items = (S.data?.board || []).slice().sort((a, b) => (a.status === 'open') === (b.status === 'open') ? b.created.localeCompare(a.created) : a.status === 'open' ? -1 : 1);
  const order = { escalation: 0, case_pack: 1, imaging: 2, rule: 3, genomics: 4, verification_failed: 5 };
  items.sort((a, b) => (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1) || (order[a.kind] ?? 9) - (order[b.kind] ?? 9));
  const html = items.length ? items.map(card).join('') : `<div class="empty">No open items. Escalations, case packs, imaging signals and rules to confirm will appear here — each one waits for a clinician’s signature.</div>`;
  $$('[data-mount="board"]').forEach((m) => { m.innerHTML = `<div class="board">${html}</div>`; wireBoard(m); });
}
function gateTag(it) {
  if (it.status === 'info') return `<span class="gate-tag info">info</span>`;
  const OPEN = { urgent_ack: 'needs on-call acknowledgement', clinician_signoff: 'needs clinician signature', radiologist_review: 'needs radiologist review', rule_confirmation: 'needs clinician confirmation', nephrologist_review: 'needs nephrologist', clinical_advice: 'needs clinician approval' };
  if (it.kind === 'genomics') return `<span class="gate-tag info">disclosure: geneticist only</span>`;
  if (it.status === 'open') return `<span class="gate-tag">${esc(OPEN[it.gate] || it.gate)}</span>`;
  return `<span class="gate-tag done">${esc(it.status)}</span>`;
}
function signedLine(it) { return it.signed_by ? `<span class="signed">✓ ${esc(it.status)} by ${esc(it.signed_by)} · sig ${esc(it.signature || '')}</span>` : ''; }
function who(sel, def) { return `<select class="who">${CLINICIANS.map((c) => `<option ${c === def ? 'selected' : ''}>${c}</option>`).join('')}</select>`; }

function card(it) {
  const d = it.data || {};
  const open = it.status === 'open';
  const head = `<div class="card-h"><h4>${esc(it.title)}</h4>${gateTag(it)}</div>`;
  if (it.kind === 'escalation') {
    return `<div class="card ${open ? 'open' : ''}" data-id="${it.id}">${head}<div class="card-b">
      <div><b>${esc(d.pattern)}</b></div><ul style="margin:0;padding-left:18px">${(d.evidence || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <div class="kv"><span class="k">Paged</span><span>${esc(d.paged?.name)} (pager ${esc(d.paged?.pager)})${d.level ? ` · escalated ×${d.level}` : ''}</span>
      ${open ? `<span class="k">Acknowledge within</span><span class="timer" data-deadline="${esc(d.deadline)}">—</span>` : ''}</div></div>
      <div class="card-f">${open ? `${who('who', 'Dr. L. Bianchi')}<button class="btn gate" data-act="acknowledge">Acknowledge</button>` : signedLine(it)}</div></div>`;
  }
  if (it.kind === 'case_pack') {
    const sb = d.sbar || {};
    const f = S.forms[it.id] ||= { items: (d.draft || []).map((x) => ({ ...x })), note: '', clinician: 'Dr. A. Rossi' };
    // merge new draft items (pack re-issued) without losing edits
    for (const x of d.draft || []) if (!f.items.some((y) => y.code === x.code)) f.items.push({ ...x });
    return `<div class="card ${open ? 'open' : ''}" data-id="${it.id}">${head}<div class="card-b sbar">
      <div style="font-size:11px;color:var(--muted)">Assembled by the Case Briefer · version ${d.version} · every claim cited</div>
      <h5>S · Situation</h5><ul>${(sb.S || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <h5>B · Background</h5><ul>${(sb.B || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <h5>A · Assessment</h5><ul>${(sb.A || []).map((a) => `<li><span class="src">${esc(a.from)}</span>${esc(a.text)}${(a.cite || []).map((c) => `<span class="cite">${esc(c)}</span>`).join('')}</li>`).join('')}</ul>
      <h5>R · Options for the clinician</h5><ul>${(sb.R || []).map((r) => `<li>${esc(r.text)}${(r.cite || []).map((c) => `<span class="cite">${esc(c)}</span>`).join('')}</li>`).join('')}</ul>
      ${sb.narrative ? `<h5>Edge SLM narrative</h5><div>${esc(sb.narrative)}</div>` : ''}
      ${open ? `<h5>Decision (draft — edit what the patient will read)</h5><div class="decision">${f.items.map((x, i) => `<label><input type="checkbox" data-i="${i}" ${x.selected !== false ? 'checked' : ''}><span><b>${esc(x.code)}</b><textarea data-i="${i}">${esc(x.patient_text)}</textarea></span></label>`).join('')}
        <textarea class="note" placeholder="Optional note to the patient">${esc(f.note)}</textarea></div>` : ''}
    </div><div class="card-f">${open ? `${who('who', f.clinician)}<button class="btn ok" data-act="sign">Sign &amp; release to patient</button><span style="font-size:11px;color:var(--muted)">Signed with your key; the Companion can only say what you sign.</span>` : signedLine(it)}</div></div>`;
  }
  if (it.kind === 'imaging') {
    return `<div class="card ${open ? 'open' : ''}" data-id="${it.id}">${head}<div class="card-b">
      <div class="imgs"><figure><img src="${esc(d.key_image)}" alt="CT key image"><figcaption>CT key image · ${esc(d.provenance?.study_uid)}</figcaption></figure>
      <figure><img src="${esc(d.heatmap_ref)}" alt="Attention heat map"><figcaption>Attention heat map (Grad-CAM) — regions flagged for review</figcaption></figure></div>
      <div class="bigp">${esc(d.label)} · p ${esc(d.p_calibrated)} <small>calibrated (raw ${esc(d.p_raw)}, ${esc(d.calibration)})</small></div>
      <div class="kv"><span class="k">Quality / OOD</span><span>${esc(d.quality?.quality)} · OOD ${esc(d.quality?.ood_score)}</span>
      <span class="k">CAM metrics</span><span class="mono">${esc(Object.entries(d.cam_metrics || {}).map(([k, v]) => `${k} ${v}`).join(' · '))}</span>
      <span class="k">Model</span><span>${esc(d.provenance?.model)} ${d.provenance?.live_model ? '(live)' : '(cached v1 inference)'}</span>
      <span class="k">Verification</span><span>${(d.verification?.checks || []).map((c) => `${c.ok ? '✓' : '✗'} ${esc(c.check)}`).join('<br>')}</span></div>
      <div style="font-size:12px;color:var(--muted)">${esc(d.note)}</div></div>
      <div class="card-f">${open ? `${who('who', 'Dr. E. Conti')}<button class="btn gate" data-act="review">Mark reviewed (radiologist)</button>` : signedLine(it)}</div></div>`;
  }
  if (it.kind === 'genomics') {
    return `<div class="card" data-id="${it.id}">${head}<div class="card-b"><div style="font-size:12px;color:var(--muted)">${esc(d.panel)} · ${esc(d.date)} · lab classification kept as reported (never reclassified)</div>
      <ul style="margin:0;padding-left:18px">${(d.findings || []).map((f) => `<li><b>${esc(f.gene)}</b> ${esc(f.hgvs)} — <i>${esc(f.lab_classification)}</i>: ${esc(f.summary)}${(f.citations || []).map((c) => `<span class="cite">${esc(c)}</span>`).join('')}</li>`).join('')}</ul>
      <div style="font-size:12px;color:#9a4a12">${esc(d.disclosure)}</div></div></div>`;
  }
  if (it.kind === 'rule') {
    return `<div class="card ${open ? 'open' : ''}" data-id="${it.id}">${head}<div class="card-b"><div><b>${esc(d.value)}</b></div>
      <div class="kv"><span class="k">Key / layer</span><span class="mono">${esc(d.key)} · L${esc(d.layer)} · ${esc(d.partition)}</span><span class="k">Status</span><span>${esc(d.status)} (shadow until confirmed)</span><span class="k">Evidence</span><span class="mono">${esc((d.evidence || []).join(', '))}</span></div></div>
      <div class="card-f">${open ? `${who('who', 'Dr. A. Rossi')}<button class="btn ok" data-act="confirm">Confirm rule</button><button class="btn ghost" data-act="reject">Reject</button>` : signedLine(it)}</div></div>`;
  }
  return `<div class="card" data-id="${it.id}">${head}<div class="card-b"><pre class="mono">${esc(JSON.stringify(d, null, 2))}</pre></div></div>`;
}
function wireBoard(m) {
  $$('.card', m).forEach((c) => {
    const id = c.dataset.id;
    const f = S.forms[id];
    $$('.decision input[type=checkbox]', c).forEach((cb) => cb.onchange = () => { f.items[cb.dataset.i].selected = cb.checked; });
    $$('.decision textarea[data-i]', c).forEach((ta) => ta.oninput = () => { f.items[ta.dataset.i].patient_text = ta.value; });
    const note = $('.decision .note', c); if (note) note.oninput = () => { f.note = note.value; };
    const sel = $('select.who', c); if (sel && f) sel.onchange = () => { f.clinician = sel.value; };
    $$('[data-act]', c).forEach((b) => b.onclick = async () => {
      const clinician = $('select.who', c)?.value;
      b.disabled = true;
      try {
        if (b.dataset.act === 'sign') await api('/api/board/act', { item: id, action: 'sign', clinician, items: f.items, note: f.note });
        else await api('/api/board/act', { item: id, action: b.dataset.act, clinician });
        toast(`${b.textContent} — signed by ${clinician}`);
      } catch (e) { toast(e.message); b.disabled = false; }
    });
  });
  tickTimers();
}
function tickTimers() {
  $$('.timer[data-deadline]').forEach((t) => {
    const s = Math.max(0, Math.round((new Date(t.dataset.deadline) - Date.now()) / 1000));
    t.textContent = s > 0 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : 'escalating…';
  });
}

// ------------------------------------------------------------------ ROSTER
function renderRoster() {
  const seats = S.data.seats;
  const chip = (cls, t) => `<span class="chipx ${cls}">${esc(t)}</span>`;
  const cos = `<div class="seat cos"><div class="h"><span class="avatar" style="background:#3C3949">CS</span><div><h3>Chief of Staff</h3><div class="role">The Hospital Director’s only agent</div></div></div>
    <div class="jd" style="color:#E1E1EA">Creates and changes every seat from plain-language job descriptions, validates and hot-reloads them, and reports how each one performs. Never talks to patients; never takes clinical decisions.</div></div>`;
  $('#roster').innerHTML = `<h2 style="margin-bottom:12px">Roster — eight clinical seats + the Chief of Staff</h2><div class="grid">${cos}${seats.map(({ id, manifest: m, metrics: x, shadow }) => `
    <div class="seat ${m.location === 'patient_edge' ? 'patient' : ''}">
      <div class="h">${AVATAR[id] ? `<img class="avatar" src="${AVATAR[id]}" alt="">` : `<span class="avatar">${INITIALS(m.name)}</span>`}<div><h3>${esc(m.name)} <span style="font:500 11px var(--body);color:var(--muted)">v${m.version}${shadow ? ' · shadow' : ''}</span></h3><div class="role">${esc(m.role || '')}</div></div></div>
      <div class="chips">${chip('', m.location === 'patient_edge' ? 'patient phone' : 'hospital edge')}${chip('', m.model.edge)}</div>
      <div class="jd">${esc(m.job_description)}</div>
      <div class="chips">${m.refuses.map((r) => chip('ref', r)).join('')}</div>
      <div class="chips">${m.gates.map((g) => chip('gate', g)).join('')}</div>
      <div class="metrics"><span><b>${x?.messages_in ?? 0}</b> in</span><span><b>${x?.messages_out ?? 0}</b> out</span><span><b>${x?.tool_calls ?? 0}</b> tools</span><span><b>${x?.refusals ?? 0}</b> refusals</span><span><b>${(x?.tc ?? 0).toFixed(1)}</b> TC</span></div>
      <button class="btn small ghost" data-open="${id}">Open agent page</button>
    </div>`).join('')}</div>`;
  $$('[data-open]').forEach((b) => b.onclick = () => { S.agentSel = b.dataset.open; S.agentVer = null; $('#tabs [data-view="agent"]').click(); });
}

// ------------------------------------------------------------------ AGENT PAGE
async function renderAgent() {
  const el = $('#agent');
  if (el.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA') return;
  const ids = S.data.seats.map((s) => s.id);
  if (!ids.includes(S.agentSel)) S.agentSel = ids[0];
  const { versions } = await api(`/api/seats/${S.agentSel}`);
  const ver = S.agentVer && versions.find((v) => v.version === S.agentVer) ? S.agentVer : versions[versions.length - 1].version;
  const cur = versions.find((v) => v.version === ver);
  const prev = versions.find((v) => v.version === ver - 1);
  const prevLines = new Set(prev ? JSON.stringify(prev, null, 2).split('\n') : []);
  const code = JSON.stringify(cur, null, 2).split('\n').map((l) => `<span class="${prev && !prevLines.has(l) ? 'add' : ''}">${esc(l)}</span>`).join('\n');
  el.innerHTML = `<div class="agent">
    <div class="panel">
      <div style="display:flex;gap:8px;align-items:center"><h3 style="flex:1">Agent page</h3>
        <select id="ag-seat">${ids.map((i) => `<option ${i === S.agentSel ? 'selected' : ''} value="${i}">${esc(S.data.seats.find((s) => s.id === i).manifest.name)}</option>`).join('')}</select>
        <select id="ag-ver">${versions.map((v) => `<option ${v.version === ver ? 'selected' : ''} value="${v.version}">v${v.version}</option>`).join('')}</select></div>
      <div class="field">Job description (the job description IS the configuration)<div style="font-size:13px;color:var(--text);background:var(--bg2);border-radius:8px;padding:10px">${esc(cur.job_description)}</div></div>
      <div class="field">Add a duty, refusal or gate — in English, Italian or French (compiled by the Chief of Staff)
        <textarea id="ag-instr" rows="3" placeholder="${esc(U().instr)}"></textarea></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="ag-compile">Compile → validate → hot-reload</button>
        ${S.agentSel === 'companion' ? `<button class="btn gate" id="ag-break">Try to break it: patient asks for the CT result</button>` : ''}
        <button class="btn ghost" id="ag-persist" title="Write this version to seats/${S.agentSel}.manifest.json">Save to repo</button></div>
      <div class="field">What compiles into what
        <div class="kv" style="font-size:12px"><span class="k">Mission / duties</span><span>capabilities &amp; allowed MCP tools</span><span class="k">Refuses</span><span>hard runtime blocks at the tool layer</span><span class="k">Surfaces</span><span>MCP servers the seat may touch</span><span class="k">Memory R/W</span><span>Cortex layers L0–L3</span><span class="k">Gate</span><span>the human hand-off, enforced</span><span class="k">Unit</span><span>metering (task credits)</span></div></div>
    </div>
    <div class="panel"><h3>Manifest v${cur.version} ${prev ? `<span style="font:400 12px var(--body);color:var(--ok)">· green = changed since v${prev.version}</span>` : ''}</h3><div class="code">${code}</div></div>
  </div>`;
  const adds = $$('.code .add', el).filter((x) => !/"version"|"compiled_at"|"compiled_by"/.test(x.textContent));
  if (adds.length) { const box = $('.code', el); box.scrollTop = Math.max(0, adds[0].offsetTop - box.clientHeight / 2); }
  $('#ag-seat').onchange = (e) => { S.agentSel = e.target.value; S.agentVer = null; renderAgent(); };
  $('#ag-ver').onchange = (e) => { S.agentVer = Number(e.target.value); renderAgent(); };
  $('#ag-compile').onclick = async () => {
    const t = $('#ag-instr').value.trim(); if (!t) return;
    const r = await api(`/api/seats/${S.agentSel}/compile`, { instruction: t }).catch((e) => ({ text: e.message }));
    toast(r.version ? `Compiled: ${S.data.seats.find((s) => s.id === S.agentSel).manifest.name} v${r.version}` : r.text.slice(0, 120));
    $('#ag-instr').value = ''; S.agentVer = null; document.activeElement.blur(); setTimeout(renderAgent, 300);
  };
  const br = $('#ag-break'); if (br) br.onclick = () => { api('/api/patient/message', { text: U().breakText }); toast('Sent from the patient phone — watch the A2A stream'); };
  $('#ag-persist').onclick = () => api(`/api/seats/${S.agentSel}/persist`, {}).then(() => toast('Saved to seats/'));
}

// ------------------------------------------------------------------ MEMORY
function renderMemory() {
  const mem = S.data.memory;
  const layers = [['L0', 'Medical knowledge & law', 'Guidelines (EAU, KDIGO), ontologies, drug–disease rules, AI Act. Same for everyone. Read-only.'],
    ['L1', 'Specialty playbook', 'Nephro-oncology pathways, red-flag patterns. Read-only.'],
    ['L2', 'The hospital', 'Local protocols, routing precedents, on-call rules. On-premise only.'],
    ['L3', 'The patient', 'DNA, EHR, imaging, wearables, conversations, confirmed rules. One partition each.']];
  $('#memory').innerHTML = `<h2 style="margin-bottom:12px">Loop Cortex Memory</h2><div class="memgrid"><div>${layers.map(([k, h, b]) => `<div class="layer ${k === 'L3' ? 'l3' : ''}"><b class="L">${k}</b><div><b>${h}</b><div style="font-size:12px">${b}</div></div></div>`).join('')}
    <p style="font-size:12px;color:var(--muted)">Evidence is append-only and hash-chained. Knowledge objects stay in <b>shadow</b> until a clinician confirms them on the Board; confidence decays if not re-verified. Erasing a patient = deleting the partition (and adapter). No patient data in general model weights.</p></div>
    <div>${mem.length ? mem.map((p) => `<div class="panel" style="margin-bottom:12px"><h3>${esc(p.partition)} <span style="font:400 12px var(--body);color:${p.chain.ok ? 'var(--ok)' : 'var(--bad)'}">· evidence chain ${p.chain.ok ? `verified (${p.chain.length})` : 'BROKEN'}</span></h3>
      <h4 style="font-size:13px">Knowledge store</h4><table class="t"><tr><th>Status</th><th>Key</th><th>Value</th><th>Conf</th><th>Source / confirmer</th><th>v</th></tr>${p.knowledge.map((k) => `<tr><td class="st-${k.status}">${k.status}</td><td class="mono">${esc(k.key)}</td><td>${esc(k.value)}</td><td>${k.conf_effective ?? k.conf}</td><td>${esc(k.source)} / ${esc(k.confirmer || '—')}</td><td>${k.version}</td></tr>`).join('') || '<tr><td colspan="6">—</td></tr>'}</table>
      <h4 style="font-size:13px;margin-top:8px">Evidence store (append-only)</h4><table class="t"><tr><th>Time</th><th>Kind</th><th>Source</th><th>Content hash</th><th>Chain hash</th></tr>${p.evidence.map((e) => `<tr><td>${time(e.ts)}</td><td>${esc(e.kind)}</td><td>${esc(e.source)}</td><td class="mono">${esc(e.content_hash.slice(0, 16))}…</td><td class="mono">${esc(e.chain_hash.slice(0, 16))}…</td></tr>`).join('')}</table></div>`).join('') : '<div class="empty">Memory is empty. Run the golden path: evidence and rules appear here as clinicians sign.</div>'}</div></div>`;
}

boot();
