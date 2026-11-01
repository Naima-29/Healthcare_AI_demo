// A2A bus (in-process, hub-and-spoke through the Dispatcher).
// Every message is schema-validated and Ed25519-signed by its sender. Only
// the Dispatcher may address seats directly; every other seat sends to the
// Dispatcher, which verifies, acknowledges and routes.
// Swap for Redis Streams / NATS in production: keep send()/subscribe().
import { validateMessage, newMessage } from '../../contracts/a2aSchema.mjs';
import { hub, pace } from '../core/hub.mjs';

export class Bus {
  constructor(keyring) {
    this.keyring = keyring;
    this.handlers = new Map();
    this.log = [];
  }
  subscribe(seatId, handler) { this.handlers.set(seatId, handler); }

  async send(fromSeat, { type, to, patient, payload, correlation }, { allowedPeers } = {}) {
    if (fromSeat !== 'dispatcher' && to !== 'dispatcher') {
      throw new Error(`${fromSeat} may only address the Dispatcher (got ${to})`);
    }
    if (allowedPeers && !allowedPeers.includes('*') && !allowedPeers.includes(to) && to !== 'board') {
      throw new Error(`${fromSeat} is not allowed to message ${to}`);
    }
    const raw = newMessage({ type, from: fromSeat, to, patient, payload, correlation });
    const v = validateMessage(raw);
    if (!v.ok) {
      hub.publish('a2a_rejected', { from: fromSeat, type, errors: v.errors });
      throw new Error(`A2A ${type} invalid: ${v.errors.join('; ')}`);
    }
    const msg = this.keyring.sign(raw);
    this.log.push(msg);
    hub.publish('a2a', { ...msg, verified: this.keyring.verify(msg) });
    await pace();
    const h = this.handlers.get(to);
    if (h) {
      // deliver asynchronously; errors are surfaced, never swallowed silently
      Promise.resolve().then(() => h(msg)).catch((e) => hub.publish('error', { seat: to, message: String(e.message || e), on: msg.type }));
    }
    return msg;
  }
  reset() { this.log = []; }
}
