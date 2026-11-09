// Kidney Care Desk: the screens the frontend did not have yet.
//   Director (Chief of Staff chat), CT (studies, upload, model switch),
//   Admin (edge-case run, erase a patient), and the Board card for "verification_failed" items.
// app.js creates it once: X = createExtras({ S, api, $, $$, esc, toast, time, pname, first, refresh, redraw }).
export function createExtras(c) {
  const { S, api, $, $$, esc, toast, pname, first, refresh, redraw } = c;
  const MAX_UPLOAD = 25 * 1024 * 1024; // same limit as the server

  const b64 = (file) => new Promise((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result).split(',')[1] || '');
    r.onerror = () => no(new Error('could not read the file'));
    r.readAsDataURL(file);
  });
  const short = (u) => (String(u).length > 28 ? '…' + String(u).slice(-26) : String(u));

  // ------------------------------------------------------------------ DIRECTOR
  const QUICK = [
    'help',
    'List the agents',
    'How are all the agents doing?',
    'Case Briefer: never diagnose a patient.',
    'Create an agent for nephrology phone follow-up: it calls CKD patients on WhatsApp 7 days after their visit and collects home blood pressure from their wearables. It must never change a therapy or make a diagnosis; every piece of advice must be approved by the nephrologist.',
  ];
  let sending = false;

  const diffHtml = (d) => {
    if (!d) return '';
    const lines = [...(d.refuses || []).map((r) => `+ refuses: ${r}`), ...(d.tools || []).map((t) => `+ tool: ${t}`), ...(d.gates || []).map((g) => `+ gate: ${g}`)];
    return lines.length ? `<div class="diff">${lines.map(esc).join('<br>')}</div>` : '';
  };

  function directorMsgs() {
    const box = $('#director .con-msgs'); if (!box) return;
    const msgs = S.events.filter((e) => e.kind === 'director_in' || e.kind === 'director_out');
    box.innerHTML = msgs.map((e) => e.kind === 'director_in'
      ? `<div class="cmsg dir">${esc(e.data.text)}</div>`
      : `<div class="cmsg cos">${esc(e.data.text)}${diffHtml(e.data.diff)}</div>`).join('')
      || '<div class="cmsg cos">Good evening, Director. Tell me what you would like to automate, or which rule to add to an agent. Type "help" for examples.</div>';
    box.scrollTop = box.scrollHeight;
  }

  function director() {
    const el = $('#director'); if (!el) return;
    if (!$('.console', el)) {
      el.innerHTML = `<div class="pagehead"><h2>Chief of Staff</h2><p>The Director's only door. Describe a duty, refusal or gate in plain language (English, Italian or French). The change is compiled, validated against the policy catalogue and hot-reloaded. A new agent has a manifest but no code behind it yet, so it stays in shadow.</p></div>
        <div class="console">
          <div class="con-head"><span class="avatar" style="width:32px;height:32px;font-size:12px">CS</span><div><b>Chief of Staff</b><div class="s">builds, changes and reports on every seat</div></div></div>
          <div class="con-msgs"></div>
          <div class="con-quick">${QUICK.map((q) => `<button data-q="${esc(q)}" title="${esc(q)}">${esc(q)}</button>`).join('')}</div>
          <div class="con-input"><textarea placeholder="Write to the Chief of Staff, in plain language…" aria-label="Message to the Chief of Staff"></textarea><button class="btn primary">Send</button></div>
        </div>`;
      const ta = $('textarea', el), btn = $('.con-input .btn', el);
      const go = async (text) => {
        if (!text.trim() || sending) return;
        sending = true; btn.disabled = true; ta.value = '';
        try { await api('/api/director', { text }); } catch (e) { toast(e.message); } finally { sending = false; btn.disabled = false; }
      };
      btn.onclick = () => go(ta.value);
      ta.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(ta.value); } };
      $$('.con-quick button', el).forEach((b) => b.onclick = () => go(b.dataset.q));
    }
    directorMsgs();
  }

  // ------------------------------------------------------------------ CT
  async function ct() {
    const el = $('#ct'); if (!el || !S.data) return;
    // The server reads studies for ITS active patient: keep it the one chosen in the header.
    if (S.data.patient !== S.patient) {
      try { await api('/api/patient/select', { patient: S.patient }); S.data = await api('/api/state'); } catch (e) { toast(e.message); }
    }
    if (!$('#ct-studies', el)) {
      el.innerHTML = `<div class="pagehead"><h2>CT imaging</h2><p>Studies in PACS for <b id="ct-patient"></b>. A reading goes to the Safety Verifier first; the radiologist sees it on the Board, never the patient.</p></div>
        <div class="stats" id="ct-stats"></div>
        <div class="xgrid">
          <div class="panel"><div class="ph"><h3>Reading model</h3><span class="eyebrow">Imaging Reader</span></div><div id="ct-model"></div></div>
          <div class="panel"><div class="ph"><h3>Upload a CT image</h3><span class="eyebrow">PNG · JPEG · DICOM · 25 MB max</span></div>
            <label class="drop" id="ct-drop"><input type="file" id="ct-file" accept="image/*,.dcm,.dicom">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>
              <b id="ct-fname">Drop an image here, or click to browse</b><span class="hint">It is added to this patient's studies and read at once.</span></label>
            <div class="drop-act"><button class="btn primary" id="ct-upload">Upload and read</button><span class="hint" id="ct-upload-note"></span></div></div>
        </div>
        <div class="panel"><div class="ph"><h3>Studies</h3><span class="eyebrow">PACS</span></div><div id="ct-studies"></div></div>
        <div class="panel"><div class="ph"><h3>Imaging Reader log</h3><span class="eyebrow">Latest 8 events</span></div><div id="ct-log" class="xlog"></div></div>`;
      const fn = $('#ct-file', el), drop = $('#ct-drop', el), fname = $('#ct-fname', el), idle = fname.textContent;
      const shown = () => { const f = fn.files[0]; fname.textContent = f ? `${f.name} (${(f.size / 1048576).toFixed(1)} MB)` : idle; drop.classList.toggle('has', !!f); };
      fn.onchange = shown;
      ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
      ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
      drop.addEventListener('drop', (e) => { if (e.dataTransfer?.files?.length) { fn.files = e.dataTransfer.files; shown(); } });
      $('#ct-upload', el).onclick = async () => {
        const f = $('#ct-file', el).files[0];
        if (!f) return toast('Choose an image first');
        if (f.size > MAX_UPLOAD) return toast('That file is over 25 MB');
        const btn = $('#ct-upload', el); btn.disabled = true;
        try {
          await api('/api/pacs/upload', { name: f.name, data_base64: await b64(f) });
          toast('Uploaded: the Imaging Reader is reading it'); fn.value = ''; shown();
        } catch (e) { toast(e.message); } finally { btn.disabled = false; refresh(); }
      };
    }
    const d = S.data, m = d.ct_model || {}, studies = d.studies || [];
    $('#ct-patient', el).textContent = pname(S.patient);
    const nRead = studies.filter((s) => s.read).length, nTodo = studies.filter((s) => s.readable && !s.read).length;
    $('#ct-stats', el).innerHTML = [['Studies', studies.length, ''], ['Read', nRead, 'ok'], ['Waiting to be read', nTodo, nTodo ? 'warn' : ''], ['Model', m.mode === 'real' ? 'Real' : 'Stub', m.mode === 'real' ? 'ok' : '']]
      .map(([k, v, t]) => `<div class="stat ${t}"><span class="v">${esc(v)}</span><span class="k">${esc(k)}</span></div>`).join('');
    $('#ct-model', el).innerHTML = `<div class="modecards">
        <button data-mode="stub" class="mc ${m.mode === 'stub' ? 'on' : ''}"><span class="mc-t">Stub</span><span class="mc-s">Cached v1 result, scripted. Safe for the demo.</span></button>
        <button data-mode="real" class="mc ${m.mode === 'real' ? 'on' : ''}" ${m.real_available ? '' : 'disabled title="Needs the weights under models/kidney_project and the ct-model extras"'}><span class="mc-t">Chrislin's model</span><span class="mc-s">${m.real_available ? 'Six networks, reads any uploaded slice.' : 'Not available on this machine.'}</span></button></div>
      <p class="hint">${m.mode === 'real' ? 'The first reading loads six networks (about 20 s), then about 3 s per slice on a CPU.' : 'The stub answers only for the study it was written for. An uploaded image gets an error, not a made-up answer.'}</p>`;
    $$('#ct-model [data-mode]', el).forEach((b) => b.onclick = async () => {
      try { await api('/api/pacs/model', { mode: b.dataset.mode }); refresh(); } catch (e) { toast(e.message); }
    });
    $('#ct-upload-note', el).textContent = m.mode === 'stub' ? 'The stub cannot read uploads: switch to Chrislin\'s model first.' : '';
    $('#ct-studies', el).innerHTML = studies.length
      ? `<table class="t"><tr><th>Study</th><th>Acquired</th><th>Modality</th><th>Description</th><th>State</th><th></th></tr>${studies.map((s) => `<tr>
          <td class="mono" title="${esc(s.study_uid)}">${esc(short(s.study_uid))}</td><td>${esc(String(s.acquired || '').slice(0, 16).replace('T', ' '))}</td><td><span class="mod">${esc(s.modality)}</span></td><td>${esc(s.description)}</td>
          <td>${s.read ? '<span class="st confirmed">read</span>' : s.readable ? '<span class="st">not read yet</span>' : '<span class="st rejected">not CT</span>'}</td>
          <td><button class="btn small" data-read="${esc(s.study_uid)}" ${s.readable ? '' : 'disabled'}>${s.read ? 'Read again' : 'Read'}</button></td></tr>`).join('')}</table>`
      : '<div class="empty"><b>No studies yet</b>Upload an image above to add one for this patient.</div>';
    $$('#ct-studies [data-read]', el).forEach((b) => b.onclick = async () => {
      b.disabled = true;
      try { await api('/api/pacs/new-study', { study_uid: b.dataset.read }); toast('Reading started'); } catch (e) { toast(e.message); b.disabled = false; }
    });
    ctLog();
  }
  function ctLog() {
    const box = $('#ct-log'); if (!box) return;
    const rows = S.events.filter((e) => (e.kind === 'note' && e.data.seat === 'imaging_reader') || (e.kind === 'a2a' && e.data.type === 'imaging_signal' && e.data.patient === S.patient)).slice(-8).reverse();
    box.innerHTML = rows.map((e) => e.kind === 'note' ? `<div class="lg"><i></i><time>${esc(c.time(e.ts))}</time><span>${esc(e.data.text)}</span></div>`
      : `<div class="lg sig"><i></i><time>${esc(c.time(e.ts))}</time><span><b>imaging_signal</b> ${esc(e.data.from)} → ${esc(e.data.to)}: <em>${esc(e.data.payload?.label)}</em>, p_cal ${esc(e.data.payload?.p_calibrated)}</span></div>`).join('') || '<div class="hint">Nothing yet.</div>';
  }

  // ------------------------------------------------------------------ ADMIN (edge cases, erasure)
  const edgeRunning = () => [...S.events].reverse().find((e) => e.kind === 'edge_cases')?.data.op === 'start';

  function tools() {
    const el = $('#tools'); if (!el || !S.data) return;
    if (!$('#edge-report', el)) {
      el.innerHTML = `<div class="pagehead"><h2>Admin</h2><p>Checks and data-protection tools for the demo desk.</p></div>
        <div class="panel"><h3>Edge cases (Sofia B.)</h3>
          <p class="hint">Runs every edge case through a separate copy of the desk (it never touches the live patients) and shows expected against actual.</p>
          <label class="hint"><input type="checkbox" id="edge-real"> also run Chrislin's real model on her 4 test slices (about 1 minute)</label>
          <div style="margin:10px 0;display:flex;gap:10px;align-items:center"><button class="btn primary" id="edge-run">Run edge cases</button><span id="edge-status" class="hint"></span></div>
          <div id="edge-report"></div></div>
        <div class="panel" style="margin-top:16px"><h3>Right to erasure</h3>
          <p class="hint">Deletes the patient's evidence, rules, images, Board items, messages and phone history. Keeps an anonymous receipt. Hospital-wide rules stay.</p>
          <div id="erase-list"></div></div>`;
      $('#edge-run', el).onclick = async () => {
        try { await api('/api/edge-cases/run', { with_real_model: $('#edge-real', el).checked }); toast('Edge cases started'); }
        catch (e) { toast(e.message); }
        tools();
      };
    }
    const running = edgeRunning(), rep = S.data.edge_report;
    $('#edge-run', el).disabled = running;
    $('#edge-status', el).textContent = running ? 'Running…' : rep ? `${rep.passed} of ${rep.total} checks passed${rep.with_real_model ? ' (with the real model)' : ''}` : 'Not run yet.';
    $('#edge-report', el).innerHTML = rep ? (() => {
      let area = null;
      const rows = rep.checks.map((k) => {
        const head = k.area !== area ? `<tr class="area"><td colspan="4">${esc(area = k.area)}</td></tr>` : '';
        return `${head}<tr><td>${k.ok ? '✅' : '❌'}</td><td>${esc(k.case)}</td><td>${esc(k.expected)}</td><td class="${k.ok ? '' : 'bad'}">${esc(k.actual)}</td></tr>`;
      }).join('');
      return `<table class="t"><tr><th></th><th>Case</th><th>Expected</th><th>Actual</th></tr>${rows}</table>`;
    })() : '';
    $('#erase-list', el).innerHTML = S.data.patients.map((p) => `<div class="xrow"><span class="av">${esc(p.initials)}</span><div style="flex:1"><b>${esc(p.name)}</b><div class="hint">${esc(p.headline)}</div></div><button class="btn small ghost" data-erase="${esc(p.id)}">Erase patient</button></div>`).join('');
    $$('#erase-list [data-erase]', el).forEach((b) => b.onclick = () => erase(b.dataset.erase));
  }

  async function erase(pid) {
    if (!confirm(`Erase ${pname(pid)}? Their evidence, rules, images, Board items and chat are deleted. This cannot be undone.`)) return;
    try {
      const r = await api('/api/patient/erase', { patient: pid });
      S.events = S.events.filter((e) => e.data?.patient !== pid);
      [...S.done].forEach((k) => { if (String(k).startsWith(`${pid}:`)) S.done.delete(k); });
      delete S.humanized[pid];
      toast(`${pname(pid)} erased: ${r.evidence_rows} evidence, ${r.knowledge_rows} rules, ${r.blobs_deleted} files${r.complete ? '' : ' (some hooks failed)'}`);
      redraw(); refresh();
    } catch (e) { toast(e.message); }
  }

  // ------------------------------------------------------------------ BOARD: items with nothing to sign
  function infoCard(it, head, cls) {
    const d = it.data || {};
    const list = (arr) => (arr && arr.length ? `<ul style="margin:0;padding-left:18px">${arr.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '');
    const checks = (d.checks || []).length ? `<div class="hint">${d.checks.map((k) => `${k.ok ? '✓' : '✗'} ${esc(k.check)}`).join(' · ')}</div>` : '';
    return `<div class="${cls}" data-id="${it.id}">${head}<div class="card-b">
      ${d.meaning ? `<div>${esc(d.meaning)}</div>` : ''}
      ${d.from ? `<div class="kv"><span class="k">From</span><span>${esc(d.from)}</span></div>` : ''}
      ${list(d.failed)}${d.error ? `<div class="kv"><span class="k">Error</span><span>${esc(d.error)}</span></div>` : ''}
      ${d.reason ? `<div class="kv"><span class="k">Seat</span><span>${esc(d.seat)}</span><span class="k">Reason</span><span>${esc(d.reason)}</span></div>` : ''}
      ${d.study ? `<div class="kv"><span class="k">Study</span><span class="mono">${esc(d.study)}</span></div>` : ''}${checks}</div></div>`;
  }

  // ------------------------------------------------------------------ wiring
  function render() { if (S.view === 'director') director(); else if (S.view === 'ct') ct(); else if (S.view === 'tools') tools(); }

  function onEvent(e) {
    if (e.kind === 'director_in' || e.kind === 'director_out') { if (S.view === 'director') directorMsgs(); if (e.data?.seat) refresh(); }
    else if (e.kind === 'manifest' || ['memory', 'ct_model', 'erased', 'patient_selected'].includes(e.kind)) refresh();
    else if (e.kind === 'note' && e.data.seat === 'imaging_reader' && S.view === 'ct') ctLog();
    else if (e.kind === 'edge_cases') {
      if (e.data.op === 'done') { toast(`Edge cases: ${e.data.passed} of ${e.data.total} passed`); refresh(); }
      else if (e.data.op === 'failed') toast('Edge cases failed to run');
      if (S.view === 'tools') tools();
    }
  }

  return { render, onEvent, infoCard, first };
}
