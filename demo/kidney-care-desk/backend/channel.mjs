// Patient channel. Always mirrors to the "Patient phone" view in the UI.
// If TELEGRAM_BOT_TOKEN is set, it also talks to the real Telegram bot
// (your v1 LoopAI_DemoBot): long-polling getUpdates, sendMessage.
import { hub } from './core/hub.mjs';

export class PatientChannel {
  constructor() {
    this.token = process.env.TELEGRAM_BOT_TOKEN || null;
    this.chatByPatient = new Map();
    if (process.env.TELEGRAM_CHAT_ID) this.chatByPatient.set(process.env.DEMO_PATIENT || 'marco-r', process.env.TELEGRAM_CHAT_ID);
    this.offset = 0;
    this.onInbound = null;
    this.polling = false;
  }
  describe() { return this.token ? 'Patient channel: UI phone + Telegram Bot API' : 'Patient channel: UI phone (set TELEGRAM_BOT_TOKEN for Telegram)'; }

  async send(patient, text, kind = 'text') {
    const msg = { patient, from: 'companion', text, kind, ts: new Date().toISOString() };
    hub.publish('patient_out', msg);
    const chat = this.chatByPatient.get(patient);
    if (this.token && chat) {
      try {
        await fetch(`https://api.telegram.org/bot${this.token}/sendMessage`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ chat_id: chat, text }),
        });
      } catch (e) { hub.publish('channel_error', { error: String(e) }); }
    }
    return { delivered: true, transports: this.token && chat ? ['ui', 'telegram'] : ['ui'] };
  }

  startTelegram(patient = 'marco-r') {
    if (!this.token || this.polling) return;
    this.polling = true;
    const loop = async () => {
      while (this.polling) {
        try {
          const r = await fetch(`https://api.telegram.org/bot${this.token}/getUpdates?timeout=25&offset=${this.offset}`);
          const data = await r.json();
          for (const u of data.result || []) {
            this.offset = u.update_id + 1;
            const m = u.message;
            if (!m?.text) continue;
            this.chatByPatient.set(patient, String(m.chat.id));
            this.onInbound?.(patient, m.text, { transport: 'telegram' });
          }
        } catch {
          await new Promise((r) => setTimeout(r, 3000));
        }
      }
    };
    loop();
  }
  stop() { this.polling = false; }
}
