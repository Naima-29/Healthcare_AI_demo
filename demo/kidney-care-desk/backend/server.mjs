// HTTP server: front end (static), REST API for the three human doors,
// Server-Sent Events for live observability, MCP over HTTP.
//   npm start  ->  http://localhost:5173
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { createDesk } from './desk.mjs';
import { hub, config } from './core/hub.mjs';
import { runStep, autoplay, steps, SCRIPT_LINES, PATIENT } from './scenario/goldenPath.mjs';
import { gateway } from './models/gateway.mjs';
import { REFUSALS, GATES, A2A_TYPES } from '../contracts/catalog.mjs';

// Minimal .env loader (no dependencies). Real env vars win.
if (fs.existsSync('.env')) {
  for (const line of fs.readFileSync('.env', 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*([^#\n]*?)\s*(#.*)?$/);
    if (m && m[2] !== '' && process.env[m[1]] === undefined) process.env[m[1]] = m[2];
  }
}
const PORT = Number(process.env.PORT || 5173);
const FRONT = path.resolve('frontend');
const ctx = createDesk();
ctx.cortex.wipeAll();
ctx.channel.startTelegram(PATIENT);

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.ico': 'image/x-icon' };

function send(res, code, body, type = 'application/json') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}
async function body(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString('utf8');
  return raw ? JSON.parse(raw) : {};
}

function state() {
  const seats = ctx.runtime.ids().map((id) => {
    const m = ctx.runtime.manifest(id);
    const s = ctx.runtime.seat(id);
    return { id, manifest: m, versions: ctx.runtime.versions(id).map((v) => v.version), metrics: s?.metrics, shadow: !!s?.shadow };
  });
  const memory = ctx.cortex.partitions().map((p) => ({ partition: p, chain: ctx.cortex.verifyChain(p), evidence: ctx.cortex.evidence(p).map(({ content, ...e }) => e), knowledge: ctx.cortex.knowledge(p) }));
  return {
    patient: PATIENT, seats, board: ctx.board.list(), case: ctx.cases.get(PATIENT), memory,
    steps: steps().map(({ n, short, title, door }) => ({ n, short, title, door })),
    lang: config.lang, script: SCRIPT_LINES[config.lang],
    models: gateway.status(), catalog: { refusals: REFUSALS, gates: GATES, a2a: A2A_TYPES },
    config: { pace: config.pace, escalationSeconds: config.escalationSeconds, telegram: !!process.env.TELEGRAM_BOT_TOKEN, fhir: process.env.FHIR_BASE_URL || null, dicomweb: process.env.DICOMWEB_URL || null, ct_model: process.env.CT_MODEL_URL || null },
    history: hub.history.slice(-600),
  };
}

const busy = { running: false };
async function guarded(res, fn) {
  try { send(res, 200, (await fn()) ?? { ok: true }); }
  catch (e) { send(res, e.code === 'REFUSED' ? 403 : 400, { ok: false, error: String(e.message || e), refusal: e.refusal }); }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const p = url.pathname;
  try {
    // ---------------- live events
    if (p === '/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
      res.write(`retry: 1500\n\n`);
      const on = (e) => res.write(`data: ${JSON.stringify(e)}\n\n`);
      hub.on('event', on);
      const ping = setInterval(() => res.write(': ping\n\n'), 15000);
      req.on('close', () => { hub.off('event', on); clearInterval(ping); });
      return;
    }
    if (p === '/api/state' && req.method === 'GET') return send(res, 200, state());

    // ---------------- door 1: Hospital Director -> Chief of Staff
    if (p === '/api/director' && req.method === 'POST') return guarded(res, async () => ctx.doors.director((await body(req)).text || ''));

    // ---------------- door 2: Patient -> Companion
    if (p === '/api/patient/message' && req.method === 'POST') {
      const b = await body(req);
      return guarded(res, async () => { await ctx.doors.patient.say(b.patient || PATIENT, b.text || '', b.features || null); return { ok: true }; });
    }
    if (p === '/api/patient/attach-lab' && req.method === 'POST') return guarded(res, async () => { await ctx.doors.patient.attachLab(PATIENT); return { ok: true }; });

    // ---------------- door 3: Clinicians -> Clinical Board
    if (p === '/api/board/act' && req.method === 'POST') {
      const b = await body(req);
      return guarded(res, async () => {
        const { item, action, clinician } = b;
        if (action === 'acknowledge') return ctx.doors.clinician.acknowledge(item, clinician);
        if (action === 'review') return ctx.doors.clinician.review(item, clinician, b.role || 'radiologist');
        if (action === 'confirm' || action === 'reject') return ctx.doors.clinician.confirmRule(item, clinician, action === 'confirm');
        if (action === 'sign') return ctx.doors.clinician.sign(item, { clinician, items: b.items, note: b.note });
        throw new Error('unknown action');
      });
    }

    // ---------------- systems & scenario
    if (p === '/api/pacs/new-study' && req.method === 'POST') return guarded(res, async () => { await ctx.doors.pacs.newStudy(PATIENT); return { ok: true }; });
    if (p === '/api/scenario/step' && req.method === 'POST') {
      const { n } = await body(req);
      if (busy.running) return send(res, 409, { ok: false, error: 'a step is already running' });
      busy.running = true;
      runStep(ctx, Number(n)).catch((e) => hub.publish('error', { seat: 'scenario', message: String(e.message || e) })).finally(() => { busy.running = false; });
      return send(res, 202, { ok: true, started: n });
    }
    if (p === '/api/scenario/autoplay' && req.method === 'POST') {
      if (busy.running) return send(res, 409, { ok: false, error: 'already running' });
      busy.running = true;
      autoplay(ctx).catch((e) => hub.publish('error', { seat: 'scenario', message: String(e.message || e) })).finally(() => { busy.running = false; });
      return send(res, 202, { ok: true });
    }
    if (p === '/api/reset' && req.method === 'POST') { ctx.reset(); return send(res, 200, { ok: true }); }

    // ---------------- agent page: compile & test
    const mSeat = p.match(/^\/api\/seats\/([a-z0-9_]+)(\/(compile|persist))?$/);
    if (mSeat) {
      const id = mSeat[1];
      if (req.method === 'GET') return send(res, 200, { manifest: ctx.runtime.manifest(id), versions: ctx.runtime.versions(id) });
      if (mSeat[3] === 'compile') { const b = await body(req); return guarded(res, () => ctx.doors.director(`${id}: ${b.instruction}`)); }
      if (mSeat[3] === 'persist') return guarded(res, () => { ctx.runtime.persist(id); return { ok: true }; });
    }

    // ---------------- MCP over HTTP (JSON-RPC 2.0)
    const mMcp = p.match(/^\/mcp\/([a-z]+)$/);
    if (mMcp && req.method === 'POST') {
      const srv = ctx.mcp.servers.get(mMcp[1]);
      if (!srv) return send(res, 404, { error: 'unknown MCP server' });
      const r = await srv.handle(await body(req));
      return r ? send(res, 200, r) : send(res, 202, '');
    }
    if (p === '/mcp' && req.method === 'GET') return send(res, 200, [...ctx.mcp.servers.values()].map((s) => ({ name: s.name, description: s.description, tools: [...s.tools.keys()] })));

    // ---------------- A2A agent cards
    const mCard = p.match(/^\/a2a\/([a-z_]+)\/\.well-known\/agent-card\.json$/);
    if (mCard) {
      const m = ctx.runtime.manifest(mCard[1]);
      if (!m) return send(res, 404, { error: 'unknown seat' });
      return send(res, 200, { protocolVersion: '1.0', name: m.seat, description: m.role, url: `/a2a/${m.seat}`, skills: (m.skills || []).map((s) => ({ id: s })), capabilities: { streaming: true }, securitySchemes: { ed25519: { type: 'signature', alg: 'Ed25519', jwk: ctx.keyring.publicJwk(m.seat) } } });
    }

    // ---------------- static front end
    let f = path.join(FRONT, p === '/' ? 'index.html' : p);
    if (!f.startsWith(FRONT)) return send(res, 403, 'forbidden', 'text/plain');
    if (fs.existsSync(f) && fs.statSync(f).isFile()) return send(res, 200, fs.readFileSync(f), TYPES[path.extname(f)] || 'application/octet-stream');
    send(res, 404, 'not found', 'text/plain');
  } catch (e) {
    send(res, 500, { error: String(e.message || e) });
  }
});

server.listen(PORT, () => {
  console.log(`\n  Kidney Care Desk v2  →  http://localhost:${PORT}\n`);
  console.log(`  models : edge=${gateway.status().edge} | cloud=${gateway.status().cloud}`);
  console.log(`  fhir   : ${process.env.FHIR_BASE_URL || 'local synthetic bundle'}`);
  console.log(`  pacs   : ${process.env.DICOMWEB_URL || 'local demo study'} | ct model: ${process.env.CT_MODEL_URL || 'cached v1 inference'}`);
  console.log(`  channel: ${ctx.channel.describe()}`);
  console.log(`  demo language: ${config.lang} (set DEMO_LANG=it for Italian)\n`);
});
