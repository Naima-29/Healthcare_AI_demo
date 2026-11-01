// Event hub: everything observable in the demo (A2A traffic, refusals, board
// changes, memory writes, patient chat) is published here and streamed to the
// front end over Server-Sent Events.
import { EventEmitter } from 'node:events';

class Hub extends EventEmitter {
  constructor() {
    super();
    this.setMaxListeners(100);
    this.history = [];
    this.limit = 2000;
  }
  publish(kind, data) {
    const evt = { seq: this.history.length + 1, ts: new Date().toISOString(), kind, data };
    this.history.push(evt);
    if (this.history.length > this.limit) this.history.shift();
    this.emit('event', evt);
    return evt;
  }
  reset() {
    this.history = [];
    this.emit('event', { seq: 0, ts: new Date().toISOString(), kind: 'reset', data: {} });
  }
}

export const hub = new Hub();

// Read lazily so tests / CLI can set env vars before the first use.
export const config = {
  // Visual pacing between agent hops (ms). 0 in tests / CLI.
  get pace() { return Number(process.env.DEMO_PACE ?? 700); },
  // Escalation acknowledgement timer (seconds). 10 min in production.
  get escalationSeconds() { return Number(process.env.ESCALATION_SECONDS ?? 90); },
  // Demo clock: the golden path is set on this date so data freshness is stable.
  now: () => new Date(process.env.DEMO_NOW || '2026-10-12T21:40:00+02:00'),
  get staleDays() { return Number(process.env.STALE_DAYS ?? 90); },
  get calibrationT() { return Number(process.env.CALIBRATION_T ?? 2.5); },
  // Demo language: 'en' (default) or 'it'. Agents still answer in the language they are addressed in.
  get lang() { return (process.env.DEMO_LANG || 'en').toLowerCase().startsWith('it') ? 'it' : 'en'; },
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const pace = (mult = 1) => (config.pace > 0 ? sleep(config.pace * mult) : Promise.resolve());

let idc = 0;
export const uid = (p) => `${p}-${Date.now().toString(36)}${(++idc).toString(36)}`;
