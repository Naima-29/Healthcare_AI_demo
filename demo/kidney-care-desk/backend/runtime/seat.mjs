// Seat base class. A seat is a job: its manifest (compiled from the job
// description) decides which tools it can call, which memory layers it can
// touch, what it refuses and where a human must sign.
import { checkAction, checkContent, RefusalError } from './guard.mjs';
import { SURFACES } from '../../contracts/catalog.mjs';
import { hub } from '../core/hub.mjs';

export class Seat {
  constructor(ctx, id) {
    this.ctx = ctx;
    this.id = id;
    this.metrics = { messages_in: 0, messages_out: 0, tool_calls: 0, refusals: 0, gates_opened: 0, tc: 0, errors: 0 };
  }
  get m() { return this.ctx.runtime.manifest(this.id); }

  async tool(name, args) {
    const m = this.m;
    if (!m.tools.includes(name)) {
      hub.publish('tool_blocked', { seat: this.id, tool: name, reason: 'not in manifest' });
      throw new RefusalError(this.id, 'tool_not_in_manifest', name);
    }
    const surface = Object.entries(SURFACES).find(([, t]) => t.includes(name))?.[0];
    if (surface && !m.surfaces.includes(surface)) throw new RefusalError(this.id, 'surface_not_allowed', surface);
    if (name === 'cortex.knowledge_propose' && !m.memory.write.includes(args.layer)) {
      throw new RefusalError(this.id, 'memory_scope', `write to L${args.layer} not allowed`);
    }
    this.metrics.tool_calls += 1;
    const out = await this.ctx.mcp.call(name, args);
    hub.publish('tool', { seat: this.id, tool: name, server: this.ctx.mcp.serverFor(name), args: summarize(args) });
    return out;
  }

  act(action) {
    try { checkAction(this.m, action); } catch (e) { this.metrics.refusals += 1; throw e; }
  }
  content(text, release) {
    try { return checkContent(this.m, text, release); } catch (e) { this.metrics.refusals += 1; throw e; }
  }

  async send(type, to, patient, payload, correlation) {
    this.metrics.messages_out += 1;
    return this.ctx.bus.send(this.id, { type, to, patient, payload, correlation }, { allowedPeers: this.m.a2a_peers });
  }

  meter(units = 1) {
    this.metrics.tc += (this.m.unit?.tc || 0) * units;
    hub.publish('meter', { seat: this.id, tc: this.metrics.tc });
  }

  gate(spec) {
    this.metrics.gates_opened += 1;
    return this.ctx.board.create({ ...spec, by: this.id });
  }

  async receive(msg) {
    this.metrics.messages_in += 1;
    try { await this.onMessage(msg); } catch (e) {
      this.metrics.errors += 1;
      hub.publish('error', { seat: this.id, message: String(e.message || e), on: msg.type });
    }
  }
  async onMessage() {}
}

function summarize(a) {
  const s = JSON.stringify(a);
  return s.length > 160 ? s.slice(0, 160) + '…' : JSON.parse(s);
}
