import { createExtras } from '/extras.js';
// Kidney Care Desk (Python) - front end. 
// Doors: Patient -> Companion, Clinicians -> Clinical Board. Views behind them: stream, roster, agent page, memory.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), j);
  return j;
};
const toast = (t) => { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), 3200); };
const time = (ts) => new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const val = (v) => (typeof v === 'string' ? v : JSON.stringify(v));

const S = { data: null, events: [], preset: 'pain', humanized: {}, patient: null, runningPatient: null, boardFilter: 'all', pop: false, popQ: '', onlyPatient: false, tip: null, showAcks: false, showTools: false,
  agentSel: 'companion', agentVer: null, forms: {}, running: null, done: new Set(), view: 'stage' };
const P = (id) => (S.data?.patients || []).find((p) => p.id === id);
const pname = (id) => P(id)?.name || id || '';
const first = (id) => pname(id).split(' ')[0];
const cur = () => P(S.patient);
const key = (n, pid = S.patient) => `${pid}:${n}`;
const X = createExtras({ S, api, $, $$, esc, toast, time, pname, first, refresh, redraw: () => renderAll() });
const AVATAR = { companion: '/assets/patient_agent.jpg', case_briefer: '/assets/doctor_agent.jpg' };
const INITIALS = (n) => n.split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
const GATE_ROLE = { radiologist_review: 'clinician:radiology', nephrologist_review: 'clinician:nephrology', genetic_disclosure: 'clinician:genetics' };
const GATE_NAME = { radiologist_review: 'Imaging signal reviewed by the radiologist', nephrologist_review: 'Medication change reviewed by the nephrologist', genetic_disclosure: 'Genetic findings disclosed by the geneticist' };
const PRESETS = {
  none: null,
  neutral: { au: { AU4: 0, AU6: 0, AU9: 0, AU43: 0 }, voice: { tremor: 0.05, pitch_var: 0.1, rate_dev: 0 } },
  pain: { au: { AU4: 3, AU6: 3, AU7: 2, AU9: 2, AU10: 1, AU43: 1 }, voice: { tremor: 0.7, pitch_var: 0.6, rate_dev: -0.4 } },
  strong: { au: { AU4: 4, AU6: 4, AU7: 4, AU9: 3, AU10: 3, AU43: 1 }, voice: { tremor: 0.9, pitch_var: 0.8, rate_dev: -0.6 } },
};
const QUICK_EXTRA = [
  'I had a CT scan yesterday: what did it show?',
  'What does my DNA say?',
  'Ho un dolore al fianco destro e le urine sono rosate.',
];
const ICON = {
  stage: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  patient: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
  board: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 13l2 2 4-4"/>',
  director: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  roster: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14.5a6.5 6.5 0 0 1 3.5 5.5"/>',
  agent: '<path d="M7 3h8l4 4v14H7z"/><path d="M15 3v4h4M10 12h6M10 16h6"/>',
  stream: '<circle cx="12" cy="12" r="2.5"/><circle cx="4.5" cy="6" r="1.8"/><circle cx="19.5" cy="6" r="1.8"/><circle cx="4.5" cy="18" r="1.8"/><circle cx="19.5" cy="18" r="1.8"/><path d="M6 7l4.3 3.6M18 7l-4.3 3.6M6 17l4.3-3.6M18 17l-4.3-3.6"/>',
  memory: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  pack: '<path d="M7 3h8l4 4v14H7z"/><path d="M15 3v4h4M10 12h6M10 16h4"/>',
  rule: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  ct: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
  tools: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-2.6-.6-.6-2.6z"/>',
  hint: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
};
const svg = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[k]}</svg>`;
const TABS = [['stage', 'Stage'], ['patient', 'Patient'], ['board', 'Board'], ['director', 'Director'], ['ct', 'CT'], null,
  ['roster', 'Roster'], ['agent', 'Agent'], ['stream', 'Flow'], ['memory', 'Memory'], ['tools', 'Admin']];

// ------------------------------------------------------------------ boot
async function boot() {
  S.data = await api('/api/state');
  S.patient = S.data.patient;
  S.events = S.data.history || [];
  S.events.forEach(track);
  buildRail(); renderAll();
  const es = new EventSource('/events');
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (e.kind === 'reset') { S.events = []; S.done.clear(); S.running = null; S.humanized = {}; S.forms = {}; refresh(); renderAll(); return; }
    S.events.push(e);
    if (S.events.length > 1500) S.events.shift();
    track(e); onEvent(e);
  };
}
function track(e) {
  if (e.kind === 'humanized') S.humanized[e.data.patient] = e.data;
  if (e.kind === 'scenario') { if (e.data.op === 'step') { S.running = e.data.n; S.runningPatient = e.data.patient; } if (e.data.op === 'done') { S.done.add(key(e.data.n, e.data.patient)); S.running = S.runningPatient = null; } }
}
let refreshT;
function refresh() { clearTimeout(refreshT); refreshT = setTimeout(async () => { S.data = await api('/api/state'); renderData(); renderPatients(); }, 150); }
function onEvent(e) {
  if (['patient_in', 'patient_out', 'humanized'].includes(e.kind) && (!e.data.patient || e.data.patient === S.patient)) renderPhoneMsgs();
  if (['a2a', 'refusal', 'gate', 'page', 'tool', 'error', 'scenario'].includes(e.kind)) appendStream(e);
  if (e.kind === 'scenario') { renderGolden(); renderNext(); }
  mapEvent(e);
  if (['board', 'gate', 'a2a'].includes(e.kind)) refresh();
  X.onEvent(e);
}
function buildRail() {
  $('#tabs').innerHTML = `<img class="logo" src="/assets/loopnexus.png" alt="Loop Nexus">` + TABS.map((t) => t ? `<button data-view="${t[0]}" class="${t[0] === S.view ? 'active' : ''}" title="${t[1]}"><span class="badge">${svg(t[0])}<i hidden></i></span>${t[1]}</button>` : '<div class="sep"></div>').join('');
  $$('#tabs button').forEach((b) => b.onclick = () => {
    $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${b.dataset.view}`));
    S.view = b.dataset.view; renderData();
  });
  $('#autoplay').onclick = () => api('/api/scenario/autoplay', { patient: S.patient }).catch((e) => toast(e.message));
  $('#reset').onclick = () => api('/api/reset', {}).then(() => toast('Desk reset')).catch((e) => toast(e.message));
}
function renderAll() { renderPatients(); renderGolden(); renderStatus(); mountPhone(); mountMap(); mountStream(); renderData(); renderNext(); }
function renderData() {
  renderStatus(); renderBoard(); renderNext();
  const open = (S.data?.board || []).filter((i) => i.status === 'open').length;
  const badge = $('#tabs [data-view="board"] i'); if (badge) { badge.hidden = !open; badge.textContent = open; }
  if (S.view === 'roster') renderRoster();
  if (S.view === 'agent') renderAgent();
  if (S.view === 'memory') renderMemory();
  X.render();
}

// ------------------------------------------------------------------ header and guidance
function selectPatient(id) {
  if (id === S.patient) return;
  S.patient = id; S.tip = null;
  renderPatients(); renderGolden(); mountPhone(); redrawStream(); renderBoard(); renderNext();
  X.render();
}
function renderPatients() {
  const el = $('#patients'); if (!el || !S.data) return;
  const board = S.data.board || [], open = (id) => board.filter((i) => i.patient_id === id && i.status === 'open');
  const me = cur(), total = board.filter((i) => i.status === 'open').length, others = total - open(S.patient).length;
  el.innerHTML = `<button class="pt on" id="pt-btn" aria-haspopup="listbox" aria-expanded="${!!S.pop}"><span class="av">${esc(me.initials)}</span><span class="who"><b>${esc(me.name)}</b><small>${me.age}, ${esc(me.headline)}</small></span><span class="chev">▾</span>${others ? `<i class="n" title="${others} open item${others > 1 ? 's' : ''} for other patients">${others}</i>` : ''}</button>
    ${S.pop ? `<div class="pop" role="listbox"><input id="pt-q" placeholder="Find a patient (${S.data.patients.length})" autocomplete="off" value="${esc(S.popQ || '')}"><div class="pop-list">${patientRows(open)}</div></div>` : ''}`;
  $('#pt-btn').onclick = (e) => { e.stopPropagation(); S.pop = !S.pop; S.popQ = ''; renderPatients(); $('#pt-q')?.focus(); };
  const q = $('#pt-q');
  if (q) {
    q.oninput = () => { S.popQ = q.value; $('.pop-list').innerHTML = patientRows(open); wirePop(); };
    q.onkeydown = (e) => { if (e.key === 'Enter') $('.pop-list .row')?.click(); if (e.key === 'Escape') closePop(); };
    q.setSelectionRange(q.value.length, q.value.length);
  }
  wirePop();
}
function patientRows(open) {
  const board = S.data.board || [], term = (S.popQ || '').toLowerCase();
  const rows = S.data.patients.filter((p) => `${p.name} ${p.headline}`.toLowerCase().includes(term))
    .map((p) => ({ p, items: open(p.id), urgent: board.some((i) => i.patient_id === p.id && i.kind === 'escalation' && i.status === 'open') }))
    .sort((a, b) => b.urgent - a.urgent || b.items.length - a.items.length || a.p.name.localeCompare(b.p.name));
  return rows.map(({ p, items, urgent }) => `<button class="row ${p.id === S.patient ? 'on' : ''}" data-p="${p.id}" role="option"><span class="av">${esc(p.initials)}</span><span class="who"><b>${esc(p.name)}</b><small>${p.age}, ${esc(p.headline)}</small></span>${urgent ? '<span class="urg">Urgent</span>' : ''}${items.length ? `<i class="n">${items.length}</i>` : ''}</button>`).join('') || '<div class="hint" style="padding:10px 12px">No patient matches.</div>';
}
function wirePop() { $$('.pop-list .row').forEach((b) => b.onclick = () => { closePop(); selectPatient(b.dataset.p); }); }
function closePop() { if (S.pop) { S.pop = false; renderPatients(); } }
document.addEventListener('click', (e) => { if (S.pop && !e.target.closest('.pop, #pt-btn')) closePop(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePop(); });
function renderGolden() {
  const el = $('#golden');
  el.innerHTML = (S.data?.steps || []).map((s) => `<button class="step ${s.n >= 5 ? 'gate' : ''} ${S.done.has(key(s.n)) ? 'done' : ''} ${S.running === s.n && S.runningPatient === S.patient ? 'running' : ''}" data-n="${s.n}" title="${esc(s.title)}"><span class="dot">${S.done.has(key(s.n)) ? '✓' : s.n}</span>${esc(s.short)}</button>`).join('');
  $$('.step', el).forEach((b) => b.onclick = () => api('/api/scenario/step', { n: Number(b.dataset.n), patient: S.patient }).catch((e) => toast(e.message)));
}
function renderStatus() {
  const d = S.data; if (!d) return;
  $('#status').innerHTML = [
    `<span class="pill ${d.light ? 'warn' : 'ok'}">${d.light ? 'Light mode: no imaging, genomics or memory' : 'Full desk'}</span>`,
    `<span class="pill ${d.integrity.audit_chain_ok ? 'ok' : 'warn'}">Audit chain ${d.integrity.audit_chain_ok ? 'verified' : 'broken'}</span>`,
    `<span class="pill ${d.integrity.dead_letters ? 'warn' : 'ok'}">${d.integrity.dead_letters} dead letters</span>`,
  ].join('');
}
function nextHint() {
  const d = S.data; if (!d) return ['', false];
  const me = S.patient, who = first(me), b = d.board || [], mine = b.filter((i) => i.patient_id === me);
  if (S.running) return [`Running step ${S.running} for ${first(S.runningPatient)}: ${d.steps[S.running - 1]?.title || ''}`, false];
  const pack = mine.find((i) => i.kind === 'case_pack' && i.status === 'open');
  const esc_ = mine.find((i) => i.kind === 'escalation' && i.status === 'open');
  const rules = mine.filter((i) => i.kind === 'rule' && i.status === 'open');
  if (pack) {
    const todo = Object.entries(pack.reviews || {}).filter(([, r]) => !r).map(([g]) => g.replace('_review', '').replace('_disclosure', ''));
    return [todo.length ? `${who}'s case pack is ready. Have the ${todo.join(', ')} review it, then sign.` : `All of ${who}'s reviews are done. Edit the patient wording if needed, then sign.`, true];
  }
  if (esc_) return [`The on-call doctor has been paged for ${who}. Acknowledge the alert on the Board.`, true];
  if (rules.length) return [`${rules.length} new rule${rules.length > 1 ? 's' : ''} from ${who}'s case to confirm on the Board.`, true];
  const next = (d.steps || []).find((s) => !S.done.has(key(s.n)));
  if (!S.done.has(key(1))) return [`Press Autoplay, or start with step 1: ${who} writes to the Companion.`, false];
  if (next) return [`Next for ${who}: step ${next.n}, ${next.title.charAt(0).toLowerCase()}${next.title.slice(1)}.`, false];
  const other = b.find((i) => i.status === 'open');
  if (other) return [`${who}'s case is complete. ${pname(other.patient_id)} still has something waiting on the Board.`, true];
  return [`${who}'s case is complete. Open Memory to see what the desk learned, switch patient, or Reset.`, false];
}
function renderNext() { const [t, act] = nextHint(); const el = $('#next'); if (el) { el.className = `next ${act ? 'act' : ''}`; el.innerHTML = `${svg('hint')}<span>${esc(t)}</span>`; } }

// ------------------------------------------------------------------ DESK MAP (the Dispatcher routes everything)
const HUB = { x: 350, y: 215 };
const ang = (deg, rx = 215, ry = 150) => ({ x: Math.round(HUB.x + rx * Math.cos(deg * Math.PI / 180)), y: Math.round(HUB.y - ry * Math.sin(deg * Math.PI / 180)) });
const NODES = {
  companion: { ...ang(180), l: 'Companion', i: 'CO', cls: 'edge' },
  renal_guardian: { ...ang(135), l: 'Renal Guardian', i: 'RG' },
  imaging_reader: { ...ang(90), l: 'Imaging Reader', i: 'IR' },
  genomics_interpreter: { ...ang(45), l: 'Genomics', i: 'GI' },
  case_briefer: { ...ang(0), l: 'Case Briefer', i: 'CB' },
  archivist: { ...ang(-45), l: 'Archivist', i: 'AR' },
  safety_verifier: { ...ang(-90), l: 'Safety Verifier', i: 'SV' },
  board: { x: 625, y: 345, l: 'Clinical Board', i: '✍', cls: 'human', r: 27 },
};
const PULSE_KIND = { red_flag_escalation: 'red', therapy_conflict: 'red', clinician_decision: 'amber', case_pack_ready: 'amber' };
function mountMap() {
  $$('[data-mount="map"]').forEach((m) => {
    m.innerHTML = `<div class="mapcard"><svg class="map" viewBox="0 0 700 430" role="img" aria-label="Seats and the Dispatcher">
      ${Object.entries(NODES).map(([id, n]) => `<line class="spoke" x1="${HUB.x}" y1="${HUB.y}" x2="${n.x}" y2="${n.y}"/>`).join('')}
      <g class="hub"><circle class="b" cx="${HUB.x}" cy="${HUB.y}" r="36"/><text x="${HUB.x}" y="${HUB.y + 4}">Dispatcher</text></g>
      ${Object.entries(NODES).map(([id, n]) => `<g class="node ${n.cls || ''}" data-node="${id}"><circle class="b" cx="${n.x}" cy="${n.y}" r="${n.r || 24}"/><text class="i" x="${n.x}" y="${n.y}">${n.i}</text><text class="l" x="${n.x}" y="${n.y + (n.r || 24) + 16}">${n.l}</text></g>`).join('')}
      <g class="pulses"></g></svg>
      <div class="maplegend"><span><i style="background:var(--teal)"></i>patient side</span><span><i style="background:var(--blue)"></i>seat</span><span><i style="background:var(--amber)"></i>human gate</span><span><i style="background:var(--coral)"></i>red flag or conflict</span></div></div>`;
  });
}
let flowQueue = Promise.resolve(), pending = 0;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
function lightNode(id, ms = 900, bad = false) {
  $$(`[data-node="${id}"]`).forEach((n) => { n.classList.add(bad ? 'bad' : 'on'); setTimeout(() => n.classList.remove('on', 'bad'), ms); });
}
function flow(from, to, kind) {
  const a = from === 'board' ? NODES.board : NODES[from], b = to === 'board' ? NODES.board : NODES[to];
  if (!a || !b || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (pending > 10) return; // a long burst: skip the animation, never lag behind
  pending++;
  flowQueue = flowQueue.then(async () => {
    lightNode(from, 700, kind === 'red');
    $$('.pulses').forEach((g) => {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('r', 6); c.setAttribute('class', `pulse ${kind || ''}`); g.appendChild(c);
      const t = (p) => `translate(${p.x}px, ${p.y}px)`;
      const pts = a === HUB || b === HUB ? [a, b] : [a, HUB, b];
      c.animate(pts.map((p, i) => ({ transform: t(p), opacity: i === 0 || i === pts.length - 1 ? 0.2 : 1 })), { duration: 760, easing: 'ease-in-out' }).onfinish = () => c.remove();
    });
    setTimeout(() => lightNode(to, 800, kind === 'red'), 420);
    await wait(380);
    pending--;
  });
}
function mapEvent(e) {
  const d = e.data;
  if (e.kind === 'a2a') flow(d.from, d.to, PULSE_KIND[d.type]);
  else if (e.kind === 'patient_in') lightNode('companion', 800);
  else if (e.kind === 'board' && e.data.op === 'create' && ['case_pack', 'escalation'].includes(e.data.item.kind)) flow('case_briefer', 'board', e.data.item.kind === 'escalation' ? 'red' : 'amber');
  else if (e.kind === 'gate') flow('board', 'case_briefer', 'amber');
}

// ------------------------------------------------------------------ PATIENT DOOR
function mountPhone() {
  $$('[data-mount="phone"]').forEach((m) => {
    m.innerHTML = `<div class="phone">
      <div class="ph-head"><img src="/assets/patient_agent.jpg" alt=""><div><div class="t">Loop Companion</div><div class="s">${esc(pname(S.patient))}’s phone</div></div></div>
      <div class="ph-msgs"></div>
      <div class="ph-sense">
        <div class="sense-row"><span class="k">Camera + mic (Humanized AI):</span><span class="seg">${Object.keys(PRESETS).map((k) => `<button data-p="${k}">${k}</button>`).join('')}</span></div>
        <div class="sense-row"><span class="k">Distress</span><div class="gauge"><i></i></div><span class="hz"></span></div>
      </div>
      <div class="ph-quick">${[...(cur()?.script || []), cur()?.ibuprofen, ...QUICK_EXTRA].filter(Boolean).map((q) => `<button title="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div class="ph-input"><button class="att" title="Send photo of lab report">📷</button><input placeholder="Write a message…" /><button class="send">➤</button></div>
    </div>`;
    const input = $('input', m);
    const sendText = (t) => { if (!t.trim()) return; api('/api/patient/message', { text: t, features: PRESETS[S.preset], patient: S.patient }).catch((e) => toast(e.message)); input.value = ''; };
    $('.send', m).onclick = () => sendText(input.value);
    input.onkeydown = (e) => { if (e.key === 'Enter') sendText(input.value); };
    $$('.ph-quick button', m).forEach((b) => b.onclick = () => sendText(b.title));
    $('.att', m).onclick = () => api('/api/patient/attach-lab', { patient: S.patient }).catch((e) => toast(e.message));
    $$('.seg button', m).forEach((b) => b.onclick = () => { S.preset = b.dataset.p; renderPhoneMsgs(); });
  });
  renderPhoneMsgs();
}
function renderPhoneMsgs() {
  const msgs = S.events.filter((e) => (e.kind === 'patient_in' || e.kind === 'patient_out') && e.data.patient === S.patient);
  const html = msgs.map((e) => {
    const mine = e.kind === 'patient_in', t = e.data.text;
    const cls = !mine && /112|emergency|pronto soccorso/i.test(t) ? 'safety' : !mine && /(clinician|doctor|medico).*(reviewed|esaminato)/is.test(t) ? 'decision' : '';
    return `<div class="bubble ${mine ? 'out' : 'in'} ${cls}">${esc(t).replace(/\n/g, '<br>')}<span class="meta">${time(e.ts)}${mine ? ' ✓✓' : ''}</span></div>`;
  }).join('') || `<div class="bubble in">Good evening ${esc(first(S.patient))}, I am your Companion. I am here whenever you need me.<span class="meta">—</span></div>`;
  $$('[data-mount="phone"]').forEach((m) => {
    const box = $('.ph-msgs', m); if (!box) return;
    box.innerHTML = html; box.scrollTop = box.scrollHeight;
    $$('.seg button', m).forEach((b) => b.classList.toggle('on', b.dataset.p === S.preset));
    const h = S.humanized[S.patient], g = $('.gauge', m);
    g.className = `gauge ${h?.level || ''}`;
    $('i', g).style.width = `${Math.round((h?.score || 0) * 100)}%`;
    $('.hz', m).innerHTML = h ? `${esc(h.level)} ${Number(h.score).toFixed(2)}${h.face != null ? ` · face ${esc(h.face)}` : ''}${h.voice != null ? ` · voice ${esc(h.voice)}` : ''}${h.underreporting ? ' · <span class="flag">under-reporting</span>' : ''}` : '—';
  });
}

// ------------------------------------------------------------------ STREAM
function mountStream() {
  $$('[data-mount="stream"]').forEach((m) => {
    m.innerHTML = `<div class="stream"><div class="st-tools"><b>A2A stream</b>
      <label><input type="checkbox" class="acks"> acknowledgements</label><label><input type="checkbox" class="tools"> tool calls</label><label><input type="checkbox" class="only"> only this patient</label>
      <span style="margin-left:auto;color:var(--muted)">Ed25519-signed · verified by the Dispatcher</span></div><div class="st-list"></div></div>`;
    $('.acks', m).checked = S.showAcks; $('.tools', m).checked = S.showTools; $('.only', m).checked = S.onlyPatient; $('.only', m).onchange = (e) => { S.onlyPatient = e.target.checked; redrawStream(); };
    $('.acks', m).onchange = (e) => { S.showAcks = e.target.checked; redrawStream(); };
    $('.tools', m).onchange = (e) => { S.showTools = e.target.checked; redrawStream(); };
  });
  redrawStream();
}
function summary(d) {
  const p = d.payload || {};
  switch (d.type) {
    case 'symptom_report': return `${(p.symptoms || []).map((s) => s.label).join(', ')} · distress ${p.distress?.level} ${p.distress?.score}`;
    case 'red_flag_escalation': return `${String(p.urgency || '').toUpperCase()}: ${p.pattern}`;
    case 'medication_report': return `${p.drug} (${p.class})`;
    case 'data_request': return `${p.what || ''} ${p.why ? '— ' + p.why : ''}${p.ask_patient ? ' — asks patient: ' + p.ask_patient : ''}`;
    case 'data_delivered': return p.kind === 'lab' ? `lab: eGFR ${p.data?.egfr}, creatinine ${p.data?.creatinine} (${p.data?.date})` : `${p.kind}: ${(p.data?.findings || []).map((f) => f.gene).join(', ')}`;
    case 'imaging_signal': return `"${p.label}" p_cal ${p.p_calibrated} (raw ${p.p_raw}) · quality ${p.quality?.quality}`;
    case 'therapy_conflict': return `${p.drug} × ${(p.conditions || []).join(' · ')}`;
    case 'case_pack_ready': return `case pack v${p.version} → Clinical Board · gates: ${(p.gates || []).join(', ')}`;
    case 'clinician_decision': return `signed by ${p.signed_by}: ${(p.items || []).map((i) => i.code).join(', ')}`;
    case 'acknowledgment': return `${p.status} ${p.ack_of}`;
    default: return '';
  }
}
function streamHtml(e) {
  const d = e.data;
  if (S.onlyPatient && d.patient && d.patient !== S.patient) return '';
  const pt = d.patient ? `<span class="ptag">${esc(first(d.patient))}</span>` : '';
  if (e.kind === 'a2a') {
    if (d.type === 'acknowledgment' && !S.showAcks) return '';
    const hot = ['red_flag_escalation', 'clinician_decision', 'therapy_conflict', 'imaging_signal'].includes(d.type);
    return `<div class="ev"><div class="row"><span class="who">${esc(d.from)}</span><span class="arrow">→</span><span class="who">${esc(d.to)}</span><span class="type ${hot ? 'hot' : ''}">${esc(d.type)}</span><span class="sig">✓ signed</span>${pt}<span class="t">${time(e.ts)}</span></div>
      <div class="sum">${esc(summary(d))}</div><details><summary class="sum">payload</summary><pre>${esc(JSON.stringify(d.payload, null, 2))}</pre></details></div>`;
  }
  if (e.kind === 'tool') return S.showTools ? `<div class="ev note"><div class="row"><span class="who">${esc(d.seat)}</span><span class="arrow">⇢ tool</span><span class="type">${esc(d.tool)}</span><span class="t">${time(e.ts)}</span></div></div>` : '';
  const map = {
    refusal: () => `<div class="ev refusal"><div class="row"><b>⛔ Policy block</b><span class="who">${esc(d.seat)} ${esc(d.version || '')}</span><span class="type">${esc(d.refusal || '')}</span><span class="t">${time(e.ts)}</span></div><div class="sum">${esc(d.action)} — ${esc(d.label)}</div></div>`,
    gate: () => `<div class="ev gate"><div class="row"><b>✍ Human gate</b><span class="who">${esc(d.clinician)}</span><span class="type">${esc(d.gate)}</span><span>${esc(d.action)}</span><span class="sig">✓ signed</span><span class="t">${time(e.ts)}</span></div></div>`,
    page: () => `<div class="ev page"><div class="row"><b>📟 Paged</b><span class="who">${esc(d.name)}</span><span class="t">${time(e.ts)}</span></div><div class="sum">${esc(d.message)}</div></div>`,
    error: () => `<div class="ev refusal"><div class="row"><b>Error</b><span class="who">${esc(d.seat)}</span><span class="sum">${esc(d.message)}</span></div></div>`,
    scenario: () => d.op === 'step' ? `<div class="ev note"><div class="row"><b>${esc(first(d.patient))}, step ${d.n}</b><span class="sum">${esc(d.title)}</span></div></div>` : '',
  };
  return map[e.kind]?.() || '';
}
function redrawStream() { const h = S.events.map(streamHtml).join(''); $$('[data-mount="stream"] .st-list').forEach((l) => { l.innerHTML = h; l.scrollTop = l.scrollHeight; }); }
function appendStream(e) {
  const h = streamHtml(e); if (!h) return;
  $$('[data-mount="stream"] .st-list').forEach((l) => { const near = l.scrollHeight - l.scrollTop - l.clientHeight < 80; l.insertAdjacentHTML('beforeend', h); if (near) l.scrollTop = l.scrollHeight; });
}

// ------------------------------------------------------------------ CLINICIAN DOOR (Clinical Board)
const clin = () => S.data?.clinicians || [];
const who = (def, cls = 'who') => `<select class="${cls}" aria-label="Clinician">${clin().map((c) => `<option value="${esc(c.id)}" ${c.id === def ? 'selected' : ''}>${esc(c.name)} (${esc(c.role.replace('clinician:', '').replace('_', '-'))})</option>`).join('')}</select>`;
const byRole = (role) => clin().find((c) => c.role === role)?.id;
const nameOf = (id) => clin().find((c) => c.id === id)?.name || id;
const SRC = { Companion: 's-companion', 'Renal Guardian': 's-renal', 'Imaging Reader': 's-img', 'Genomics Interpreter': 's-gen' };

function renderBoard() {
  if ($$('[data-mount="board"]').some((m) => m.contains(document.activeElement) && /TEXTAREA|INPUT|SELECT/.test(document.activeElement.tagName))) return;
  const order = { escalation: 0, case_pack: 1, rule: 2 };
  const items = (S.data?.board || []).slice().sort((a, b) =>
    (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1) || (order[a.kind] ?? 9) - (order[b.kind] ?? 9) || b.created.localeCompare(a.created));
  const shown = items.filter((i) => S.boardFilter === 'all' || i.patient_id === S.patient);
  const count = (all) => items.filter((i) => i.status === 'open' && (all || i.patient_id === S.patient)).length;
  const filter = S.data.patients.length > 1 ? `<div class="bfilter">${[['all', 'All patients', true], ['current', first(S.patient), false]].map(([id, n, all]) => `<button class="${(S.boardFilter === 'all') === all ? 'on' : ''}" data-f="${id}">${esc(n)}${count(all) ? `<i>${count(all)}</i>` : ''}</button>`).join('')}</div>` : '';
  const html = shown.length ? shown.map(card).join('') : `<div class="empty"><b>Nothing waiting for a clinician</b>Urgent alerts, case packs and rules to confirm show up here. Press Autoplay to see the first one.</div>`;
  $$('[data-mount="board"]').forEach((m) => { m.innerHTML = `<div class="board">${filter}${html}</div>`; wireBoard(m); $$('.bfilter button', m).forEach((b) => b.onclick = () => { S.boardFilter = b.dataset.f === 'all' ? 'all' : 'current'; renderBoard(); }); });
}
const chipFor = (it) => {
  if (it.status === 'open') return `<span class="chip open">${esc({ urgent_ack: 'Needs acknowledgement', clinician_signoff: 'Needs your signature', rule_confirmation: 'Needs confirmation' }[it.gate] || 'Open')}</span>`;
  if (it.status === 'superseded') return '<span class="chip">Replaced by a newer version</span>';
  return `<span class="chip done">${esc(it.status[0].toUpperCase() + it.status.slice(1))}</span>`;
};
const signedLine = (it) => it.signed_by ? `<span class="signed">✓ ${esc(it.status[0].toUpperCase() + it.status.slice(1))} by ${esc(nameOf(it.signed_by))}</span>${it.decision_id ? `<span class="hint mono">release ${esc(it.decision_id)}</span>` : ''}` : `<span class="hint">${esc(it.status)}</span>`;
const cites = (c) => (c || []).map((x) => `<span class="cite">${esc(x)}</span>`).join('');

function card(it) {
  const d = it.data || {}, open = it.status === 'open';
  const icon = { escalation: 'alert', case_pack: 'pack', rule: 'rule' }[it.kind] || 'pack';
  const head = `<div class="card-h"><span class="ic">${svg(icon)}</span><h4>${esc(it.title)}</h4>${it.patient_id ? `<span class="ptchip" title="${esc(P(it.patient_id)?.headline || '')}">${esc(pname(it.patient_id))}</span>` : ''}${chipFor(it)}</div>`;
  const cls = `card ${open ? 'open' : it.status === 'superseded' ? 'superseded' : 'done'}`;
  if (it.kind === 'escalation') {
    return `<div class="${cls}" data-id="${it.id}">${head}<div class="card-b"><div><b>${esc(d.pattern)}</b></div>
      <ul style="margin:0;padding-left:18px">${(d.evidence || []).map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
      <div class="kv"><span class="k">Paged</span><span>${esc((d.paged || []).join(', '))}${d.level ? `, escalated to level ${d.level}` : ''}</span><span class="k">Unit</span><span>${esc(d.unit)}</span></div></div>
      <div class="card-f">${open ? `${who(byRole('clinician:uro_oncology'))}<button class="btn gate" data-act="acknowledge">Acknowledge</button><span class="hint">Stops the escalation ladder.</span>` : signedLine(it)}</div></div>`;
  }
  if (it.kind === 'case_pack') return casePack(it, d, head, cls, open);
  if (it.kind === 'rule') {
    return `<div class="${cls}" data-id="${it.id}">${head}<div class="card-b"><div><b>${esc(ruleText(d.value))}</b></div>
      <div class="kv"><span class="k">Rule</span><span class="mono">${esc(d.key)}</span><span class="k">Scope</span><span>Layer ${esc(d.layer)}, version ${esc(d.version)}</span><span class="k">Status</span><span>${open ? 'On trial (shadow) until a clinician confirms it' : esc(it.status)}</span></div></div>
      <div class="card-f">${open ? `${who('dr_rossi')}<input class="why" placeholder="Reason (needed to reject)"><button class="btn ok" data-act="confirm">Confirm rule</button><button class="btn ghost" data-act="reject">Reject</button>` : signedLine(it)}</div></div>`;
  }
  if (it.kind === 'verification_failed') return X.infoCard(it, head, cls);
  return `<div class="${cls}" data-id="${it.id}">${head}<div class="card-b"><pre class="mono">${esc(JSON.stringify(d, null, 2))}</pre></div></div>`;
}
function ruleText(v) { try { const o = typeof v === 'string' ? JSON.parse(v) : v; return o.instruction || val(v); } catch { return val(v); } }
function casePack(it, d, head, cls, open) {
  const f = S.forms[it.id] ||= { items: (d.draft || []).map((x) => ({ ...x })), note: '', clinician: 'dr_rossi' };
  for (const x of d.draft || []) if (!f.items.some((y) => y.code === x.code)) f.items.push({ ...x });
  const li = (arr, fallback, withCite) => (arr?.length ? arr : fallback.map((t) => ({ text: t }))).map((x) => `<li>${x.from ? `<span class="src ${SRC[x.from] || ''}">${esc(x.from)}</span>` : ''}${esc(x.text)}${x.ref ? `<span class="ref" title="message id">#${esc(x.ref.slice(0, 8))}</span>` : ''}${withCite ? cites(x.cite) : ''}</li>`).join('');
  const sec = (L, title, body) => `<div class="sbar-sec"><span class="L">${L}</span><div><h5>${title}</h5><ul>${body}</ul></div></div>`;
  const sb = it.sbar || {};
  const pending = Object.entries(it.reviews || {});
  const imgs = d.images && (d.images.key_image || d.images.heatmap_ref) ? `<div class="imgs">${[['key_image', 'CT key image'], ['heatmap_ref', 'Attention map: where the model looked']].filter(([k]) => d.images[k]).map(([k, c]) => `<figure><img src="/api/evidence/${encodeURIComponent(d.images[k])}" alt="${esc(c)}"><figcaption>${esc(c)}</figcaption></figure>`).join('')}</div>` : '';
  const reviews = pending.length ? `<div class="reviews"><h5>Reviews needed before you can sign</h5>${pending.map(([g, r]) => r
    ? `<div class="rv okline">✓ ${esc(GATE_NAME[g] || g)} (${esc(r.name)})</div>`
    : `<div class="rv"><span class="lbl">${esc(GATE_NAME[g] || g)}</span>${open ? `${who(byRole(GATE_ROLE[g]), 'rvwho')}<button class="btn small gate" data-review="${esc(g)}">Mark reviewed</button>` : '<span class="hint">not reviewed</span>'}</div>`).join('')}</div>` : '';
  const allReviewed = pending.every(([, r]) => r);
  return `<div class="${cls}" data-id="${it.id}">${head}<div class="card-b">
    <div class="hint">Assembled by the Case Briefer, version ${it.version}. Each line names the seat it came from.</div>
    ${sec('S', 'Situation', li(d.S, sb.S || [], false))}
    ${sec('B', 'Background from the record', li(d.B, sb.B || [], false))}
    ${sec('A', 'Assessment', li(d.A, sb.A || [], true))}${imgs}
    ${sec('R', 'Options for the clinician', li(d.R, sb.R || [], true))}
    ${reviews}
    ${open ? `<div class="decision"><h5>Your decision: edit what the patient will read</h5>${f.items.map((x, i) => `<label><input type="checkbox" data-i="${i}" ${x.selected !== false ? 'checked' : ''}><span><b>${esc(x.code.replace(/_/g, ' '))}</b><textarea data-i="${i}">${esc(x.patient_text)}</textarea></span></label>`).join('')}
      <textarea class="note" placeholder="Optional note to the patient">${esc(f.note)}</textarea></div>` : ''}
  </div><div class="card-f">${open ? `${who(f.clinician, 'who signer')}<button class="btn ok" data-act="sign" ${allReviewed ? '' : 'disabled title="Complete the reviews first"'}>Sign and release to patient</button><span class="hint">${allReviewed ? 'Signed with your key. The Companion can only say what you sign.' : 'Complete the reviews above first.'}</span>` : signedLine(it)}</div></div>`;
}
function wireBoard(m) {
  $$('.card', m).forEach((c) => {
    const id = c.dataset.id, f = S.forms[id];
    $$('.decision input[type=checkbox]', c).forEach((cb) => cb.onchange = () => { f.items[cb.dataset.i].selected = cb.checked; });
    $$('.decision textarea[data-i]', c).forEach((ta) => ta.oninput = () => { f.items[ta.dataset.i].patient_text = ta.value; });
    const note = $('.decision .note', c); if (note) note.oninput = () => { f.note = note.value; };
    const signer = $('select.signer', c); if (signer && f) signer.onchange = () => { f.clinician = signer.value; };
    const act = async (b, body, label) => {
      b.disabled = true;
      try { await api('/api/board/act', { item: id, ...body }); toast(`${label} by ${nameOf(body.clinician)}`); }
      catch (e) { toast(e.message); b.disabled = false; }
    };
    $$('[data-review]', c).forEach((b) => b.onclick = () => act(b, { action: 'review', gate: b.dataset.review, clinician: $('select', b.parentElement).value }, 'Reviewed'));
    $$('[data-act]', c).forEach((b) => b.onclick = () => {
      const clinician = $('select.who', c)?.value, a = b.dataset.act;
      if (a === 'sign') act(b, { action: 'sign', clinician, items: f.items.map((x) => ({ code: x.code, selected: x.selected !== false, patient_text: x.patient_text })), note: f.note }, 'Decision signed and released');
      else if (a === 'reject') act(b, { action: 'reject', clinician, reason: $('.why', c).value }, 'Rule rejected');
      else act(b, { action: a, clinician }, a === 'confirm' ? 'Rule confirmed' : 'Acknowledged');
    });
  });
}

// ------------------------------------------------------------------ ROSTER
const JOURNEY = ['companion', 'dispatcher', 'renal_guardian', 'safety_verifier', 'imaging_reader', 'genomics_interpreter', 'case_briefer', 'archivist', 'chief_of_staff'];
const ROSTER_ORDER = (seats) => seats.slice().sort((a, b) => { const r = (s) => { const i = JOURNEY.indexOf(s.id); return i < 0 ? 99 : i; }; return r(a) - r(b); });
function renderRoster() {
  const chip = (cls, t) => `<span class="chipx ${cls}">${esc(t)}</span>`;
  $('#roster').innerHTML = `<div class="pagehead"><h2>Roster</h2><p>The seats on this desk. Each one has a job description, a list of things it refuses to do, and the human gates it answers to. These come from the live manifests.</p></div>
    <div class="grid">${ROSTER_ORDER(S.data.seats).map(({ id, manifest: m, metrics: x, shadow }) => `
    <div class="seat ${m.location === 'patient_edge' ? 'edge' : ''}">
      <div class="h">${AVATAR[id] ? `<img class="avatar" src="${AVATAR[id]}" alt="">` : `<span class="avatar">${INITIALS(m.name)}</span>`}<div><h3>${esc(m.name)}</h3><div class="role">${esc(m.role || '')}${shadow ? ' · shadow: no code runs it yet' : ''}</div></div></div>
      <div class="jd">${esc(m.job_description)}</div>
      ${(m.refuses || []).length ? `<div class="chips">${m.refuses.map((r) => chip('no', `never: ${r.replace(/_/g, ' ')}`)).join('')}</div>` : ''}
      ${(m.gates || []).length ? `<div class="chips">${m.gates.map((g) => chip('gate', `gate: ${g.replace(/_/g, ' ')}`)).join('')}</div>` : ''}
      <div class="metrics"><span><b>${x.messages_in ?? 0}</b>in</span><span><b>${x.messages_out ?? 0}</b>out</span><span><b>${x.tool_calls ?? 0}</b>tool calls</span><span><b>${x.refusals ?? 0}</b>blocked</span></div>
      <button class="btn small ghost" data-open="${id}">Open agent page</button></div>`).join('')}</div>`;
  $$('[data-open]').forEach((b) => b.onclick = () => { S.agentSel = b.dataset.open; S.agentVer = null; $('#tabs [data-view="agent"]').click(); });
}

// ------------------------------------------------------------------ AGENT PAGE (read-only)
function renderAgent() {
  const el = $('#agent'), seats = S.data.seats;
  if (el.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA') return; // don't wipe what is being typed
  if (!seats.some((s) => s.id === S.agentSel)) S.agentSel = seats[0].id;
  const s = seats.find((x) => x.id === S.agentSel), m = s.manifest;
  const chips = (arr, cls = '') => (arr || []).length ? arr.map((v) => `<span class="chipx ${cls}">${esc(v)}</span>`).join('') : '<span class="hint">none</span>';
  const rows = [['Skills', m.skills, 'sk'], ['Tools', m.tools, 'tl'], ['Surfaces', m.surfaces, ''], ['Refuses', m.refuses, 'no'], ['Gates', m.gates, 'gate'], ['Talks to', m.a2a_peers, ''], ['Sends', m.emits, ''], ['Receives', m.consumes, '']];
  const mem = m.memory || {}, lay = (a) => (a || []).length ? a.map((n) => `<span class="chipx mem">L${esc(n)}</span>`).join('') : '<span class="hint">nothing</span>';
  const json = esc(JSON.stringify(m, null, 2)).replace(/(&quot;(?:(?!&quot;).)*&quot;)(:)?|\b(true|false|null)\b|(-?\b\d+(?:\.\d+)?\b)/g,
    (_, str, colon, kw, num) => str ? `<span class="${colon ? 'jk' : 'js'}">${str}</span>${colon || ''}` : kw ? `<span class="jb">${kw}</span>` : `<span class="jn">${num}</span>`);
  el.innerHTML = `<div class="pagehead"><h2>Agent page</h2><p>The job description is the configuration. This is what the seat is allowed to do, read straight from its manifest.</p></div>
    <div class="agent"><div class="panel">
    <div class="ag-head">${AVATAR[s.id] ? `<img class="avatar" src="${AVATAR[s.id]}" alt="">` : `<span class="avatar">${INITIALS(m.name)}</span>`}
      <div class="ag-t"><h3>${esc(m.name)}</h3><div class="ag-role">${esc(m.role || '')}</div>
        <div class="ag-pills"><span class="pill">v${esc(m.version)}</span><span class="pill">${esc(String(m.location || '').replace(/_/g, ' '))}</span>${s.shadow ? '<span class="pill warn">shadow: no code runs it yet</span>' : '<span class="pill ok">running</span>'}</div></div>
      <select id="ag-seat" aria-label="Seat">${seats.map((x) => `<option value="${x.id}" ${x.id === S.agentSel ? 'selected' : ''}>${esc(x.manifest.name)}</option>`).join('')}</select></div>
    <div class="ag-sec">Job description</div><div class="quote">${esc(m.job_description)}</div>
    <div class="ag-sec">What it can do</div>
    <div class="attrs">${rows.map(([k, v, c]) => `<div class="attr"><span class="k">${k}</span><span class="v">${chips(v, c)}</span></div>`).join('')}
      <div class="attr"><span class="k">Memory</span><span class="v"><span class="hint">reads</span>${lay(mem.read)}<span class="hint">writes</span>${lay(mem.write)}</span></div></div>
    <div class="compose"><div class="ag-sec" style="margin-top:0">Change this seat</div>
      <div class="field">Add a duty, refusal or gate, in plain language (compiled by the Chief of Staff)
      <textarea id="ag-instr" rows="3" placeholder="e.g. Never diagnose a patient."></textarea></div>
      <div><button class="btn primary" id="ag-compile">Compile, validate, hot-reload</button></div></div>
  </div><div class="panel"><div class="ph"><h3>Manifest</h3><span class="pill">version ${esc(m.version)}</span></div><div class="code dark">${json}</div></div></div>`;
  $('#ag-seat').onchange = (e) => { S.agentSel = e.target.value; renderAgent(); };
  $('#ag-compile').onclick = async () => {
    const t = $('#ag-instr').value.trim(); if (!t) return;
    try {
      const r = await api(`/api/seats/${S.agentSel}/compile`, { instruction: t });
      toast(r.version ? `${m.name} is now v${r.version}` : String(r.text).slice(0, 140));
      $('#ag-instr').value = ''; document.activeElement.blur(); refresh();
    } catch (e) { toast(e.message); }
  };
}

// ------------------------------------------------------------------ MEMORY
// Colour for an evidence kind, by family. Unknown kinds stay neutral.
const KIND_RULES = [[/^(ct|imaging|image|scan|radiolog)/, 'k-img'], [/^(genom|dna|gene|variant)/, 'k-gen'], [/(red_flag|escalat|alert|violation|block|refus)/, 'k-alert'],
  [/(decision|sign|rule|confirm|review|approv)/, 'k-dec'], [/^(data|lab|wearable|symptom|record|conversation|message|medication)/, 'k-data']];
const kindCls = (k) => (KIND_RULES.find(([re]) => re.test(String(k))) || [, ''])[1];
function renderMemory() {
  const mem = S.data.memory;
  const layers = [['L0', 'Medical knowledge and law', 'Guidelines (EAU, KDIGO), drug-disease rules. Same for everyone, read-only.'],
    ['L1', 'Specialty playbook', 'Nephro-oncology pathways and red-flag patterns. Read-only.'],
    ['L2', 'The hospital', 'Local protocols and routing precedents. Never leaves the hospital.'],
    ['L3', 'The patient', 'DNA, record, imaging, conversations and confirmed rules. One partition per patient.']];
  const count = (l) => mem.filter((p) => p.layer === l).reduce((n, p) => n + p.evidence.length + p.knowledge.length, 0);
  const empty = S.data.light ? 'Light mode has no Cortex. Start the desk without --light to see memory.' : 'Memory is empty. Evidence appears as the seats work, and rules appear when a clinician signs a decision.';
  const kv = (k) => { const c = k.confidence_effective ?? k.conf_effective ?? k.confidence; return c == null ? '' : Number(c).toFixed(2); };
  const stat = (k, v, t = '') => `<div class="stat ${t}"><span class="v">${esc(v)}</span><span class="k">${esc(k)}</span></div>`;
  const part = (p) => {
    const trial = p.knowledge.filter((k) => k.status === 'shadow').length, ok = p.chain.ok;
    return `<div class="panel mpart"><div class="ph"><h3>${p.layer === 3 ? `Patient: ${esc(pname(p.partition))}` : `Hospital: ${esc(p.partition)}`}</h3>
      <span class="chain ${ok ? '' : 'bad'}">${ok ? `Chain verified · ${p.chain.length} entries` : 'Chain broken'}</span></div>
      <div class="stats sm">${stat('Evidence', p.evidence.length)}${stat('Rules', p.knowledge.length)}${stat('On trial', trial, trial ? 'warn' : '')}${stat('Integrity', ok ? 'OK' : 'Broken', ok ? 'ok' : 'bad')}</div>
      <h4 class="tsec">Rules</h4><table class="t"><tr><th>Status</th><th>Rule</th><th>Confidence</th><th>Proposed by</th><th>Confirmed by</th></tr>${p.knowledge.map((k) => `<tr><td><span class="st ${esc(k.status)}">${esc(k.status === 'shadow' ? 'on trial' : k.status)}</span></td><td><b>${esc(ruleText(k.value))}</b><div class="mono">${esc(k.key)} v${esc(k.version)}</div></td><td>${esc(kv(k))}</td><td>${esc(k.proposed_by || k.source || '')}</td><td>${esc(k.decided_by || 'waiting')}</td></tr>`).join('') || '<tr><td colspan="5" class="hint">No rules yet.</td></tr>'}</table>
      <h4 class="tsec">Evidence <small>append-only, hash-chained</small></h4><table class="t"><tr><th>#</th><th>Time</th><th>Kind</th><th>Source</th><th>Content hash</th><th>Chain hash</th></tr>${p.evidence.map((e) => `<tr><td class="seq">${esc(e.seq)}</td><td class="mono">${esc(String(e.recorded_at).slice(11, 19))}</td><td><span class="kind ${kindCls(e.kind)}">${esc(e.kind.replace(/_/g, ' '))}</span></td><td>${esc(e.source)}</td><td><span class="hash">${esc(e.content_sha256.slice(0, 10))}…</span></td><td><span class="hash">${esc(e.chain_hash.slice(0, 10))}…</span></td></tr>`).join('')}</table></div>`;
  };
  $('#memory').innerHTML = `<div class="pagehead"><h2>Loop Cortex memory</h2><p>What the desk remembers, in four layers. Evidence is append-only and hash-chained. A new rule stays on trial until a clinician confirms it on the Board.</p></div>
    <div class="memgrid"><div>${layers.map(([k, h, b]) => `<div class="layer ${k === 'L3' ? 'l3' : ''}" data-l="${k}"><span class="L">${k}</span><div style="flex:1"><b>${h}</b><div style="font-size:12.5px;color:var(--muted)">${b}</div></div>${count(+k[1]) ? `<span class="cnt" title="entries stored in this layer">${count(+k[1])}</span>` : ''}</div>`).join('')}</div>
    <div>${mem.length ? mem.map(part).join('') : `<div class="empty"><b>Nothing remembered yet</b>${empty}</div>`}</div></div>`;
}

boot();
