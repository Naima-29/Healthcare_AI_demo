// Seat runtime: loads manifests from seats/*.manifest.json, validates them,
// instantiates seats and hot-reloads new manifest versions compiled by the
// Chief of Staff. (Production target: OpenClaw / Loop Nexus runtime.)
import fs from 'node:fs';
import path from 'node:path';
import { validateManifest } from '../../contracts/manifestSchema.mjs';
import { hub } from '../core/hub.mjs';
import { Seat } from './seat.mjs';

const SEATS_DIR = process.env.SEATS_DIR || path.resolve('seats');

export class Runtime {
  constructor(ctx) {
    this.ctx = ctx;
    this.manifests = new Map();
    this.history = new Map();
    this.seats = new Map();
  }
  loadSeedManifests() {
    for (const f of fs.readdirSync(SEATS_DIR).filter((x) => x.endsWith('.manifest.json'))) {
      const m = JSON.parse(fs.readFileSync(path.join(SEATS_DIR, f), 'utf8'));
      const v = validateManifest(m);
      if (!v.ok) throw new Error(`Seed manifest ${f} invalid: ${v.errors.join('; ')}`);
      this.manifests.set(m.seat, m);
      this.history.set(m.seat, [m]);
    }
  }
  attach(seat) {
    this.seats.set(seat.id, seat);
    this.ctx.bus.subscribe(seat.id, (msg) => seat.receive(msg));
  }
  ids() { return [...this.manifests.keys()]; }
  manifest(id) { return this.manifests.get(id); }
  seat(id) { return this.seats.get(id); }
  versions(id) { return this.history.get(id) || []; }

  hotReload(m) {
    const v = validateManifest(m);
    if (!v.ok) throw new Error(`invalid manifest: ${v.errors.join('; ')}`);
    this.manifests.set(m.seat, m);
    this.history.set(m.seat, [...(this.history.get(m.seat) || []), m]);
    hub.publish('manifest', { op: 'hot_reload', seat: m.seat, version: m.version, manifest: m });
    return m;
  }
  register(m) {
    this.hotReload(m);
    if (!this.seats.has(m.seat)) {
      const s = new Seat(this.ctx, m.seat); // generic seat: shadow mode until wired
      s.shadow = true;
      this.attach(s);
    }
    hub.publish('manifest', { op: 'create', seat: m.seat, manifest: m });
  }
  persist(id, dir = SEATS_DIR) {
    fs.writeFileSync(path.join(dir, `${id}.manifest.json`), JSON.stringify(this.manifest(id), null, 2));
  }
}
