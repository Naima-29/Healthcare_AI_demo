// Loop Cortex Memory - evidence store + knowledge store, one partition per patient
// (L3) plus the hospital partition (L2).
//  * Evidence: immutable, append-only, hash-chained (sha256). "What proves."
//  * Knowledge: typed, versioned objects with scope, confidence, source,
//    confirmer, status (shadow -> confirmed | rejected), last_verified.
//    Confidence decays if not re-verified. "What is believed."
//  * Erase = delete the partition (and the LoRA adapter, if any). No retraining.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { hub, config, uid } from '../core/hub.mjs';

const sha = (s) => crypto.createHash('sha256').update(s).digest('hex');
const HALF_LIFE_DAYS = 180;

export class Cortex {
  constructor(root = process.env.CORTEX_DIR || path.resolve('data/runtime/cortex')) {
    this.root = root;
    fs.mkdirSync(root, { recursive: true });
  }
  dir(partition) {
    const d = path.join(this.root, partition.replace(/[^a-z0-9_-]/gi, '_'));
    fs.mkdirSync(d, { recursive: true });
    return d;
  }

  // ---- evidence -----------------------------------------------------------
  evidence(partition) {
    const f = path.join(this.dir(partition), 'evidence.jsonl');
    if (!fs.existsSync(f)) return [];
    return fs.readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  }
  appendEvidence(partition, { kind, source, content, meta = {} }) {
    if (content == null) throw new Error('evidence needs content');
    const chain = this.evidence(partition);
    const prev = chain.length ? chain[chain.length - 1].chain_hash : 'GENESIS';
    const body = typeof content === 'string' ? content : JSON.stringify(content);
    const content_hash = sha(body);
    const entry = { id: uid('ev'), ts: new Date().toISOString(), partition, kind, source, content_hash, prev_hash: prev, meta, content: body.length > 4000 ? body.slice(0, 4000) + '…' : body };
    entry.chain_hash = sha(prev + content_hash + entry.ts + kind);
    fs.appendFileSync(path.join(this.dir(partition), 'evidence.jsonl'), JSON.stringify(entry) + '\n');
    hub.publish('memory', { op: 'evidence_append', partition, entry: { ...entry, content: undefined } });
    return entry;
  }
  verifyChain(partition) {
    let prev = 'GENESIS';
    for (const e of this.evidence(partition)) {
      if (e.prev_hash !== prev) return { ok: false, broken_at: e.id };
      if (sha(prev + e.content_hash + e.ts + e.kind) !== e.chain_hash) return { ok: false, broken_at: e.id };
      prev = e.chain_hash;
    }
    return { ok: true, length: this.evidence(partition).length };
  }

  // ---- knowledge ----------------------------------------------------------
  knowledgeFile(partition) { return path.join(this.dir(partition), 'knowledge.json'); }
  knowledge(partition) {
    const f = this.knowledgeFile(partition);
    const list = fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : [];
    const now = config.now().getTime();
    return list.map((k) => {
      if (k.status !== 'confirmed' || !k.last_verified) return k;
      const days = (now - new Date(k.last_verified).getTime()) / 86400000;
      return { ...k, conf_effective: Number((k.conf * Math.pow(0.5, Math.max(0, days) / HALF_LIFE_DAYS)).toFixed(3)) };
    });
  }
  saveKnowledge(partition, list) {
    fs.writeFileSync(this.knowledgeFile(partition), JSON.stringify(list.map(({ conf_effective, ...k }) => k), null, 2));
  }
  propose(partition, { layer, key, value, conf = 0.9, source, evidence = [] }) {
    if (!evidence.length) {
      const e = new Error('refused: store_without_evidence');
      e.code = 'REFUSED'; e.refusal = 'store_without_evidence';
      throw e;
    }
    const list = this.knowledge(partition);
    const existing = list.find((k) => k.key === key && k.status !== 'rejected');
    const obj = {
      id: uid('ko'), partition, layer, key, value, conf, source, evidence,
      status: 'shadow', version: existing ? existing.version + 1 : 1, confirmer: null,
      created: new Date().toISOString(), last_verified: null,
    };
    list.push(obj);
    this.saveKnowledge(partition, list);
    hub.publish('memory', { op: 'knowledge_propose', partition, object: obj });
    return obj;
  }
  confirm(partition, id, { confirmer, confirm = true }) {
    if (!confirmer || confirmer.startsWith('seat:')) {
      const e = new Error('refused: auto_confirm_rule (a human must confirm)');
      e.code = 'REFUSED'; e.refusal = 'auto_confirm_rule';
      throw e;
    }
    const list = this.knowledge(partition);
    const k = list.find((x) => x.id === id);
    if (!k) throw new Error('unknown knowledge object');
    k.status = confirm ? 'confirmed' : 'rejected';
    k.confirmer = confirmer;
    k.last_verified = config.now().toISOString();
    if (confirm) for (const o of list) if (o.key === k.key && o.id !== k.id && o.status === 'confirmed') o.status = 'superseded';
    this.saveKnowledge(partition, list);
    hub.publish('memory', { op: confirm ? 'knowledge_confirm' : 'knowledge_reject', partition, object: k });
    return k;
  }
  confirmed(partition) { return this.knowledge(partition).filter((k) => k.status === 'confirmed'); }

  erase(partition) {
    fs.rmSync(this.dir(partition), { recursive: true, force: true });
    hub.publish('memory', { op: 'erase', partition });
  }
  partitions() { return fs.existsSync(this.root) ? fs.readdirSync(this.root) : []; }
  wipeAll() { fs.rmSync(this.root, { recursive: true, force: true }); fs.mkdirSync(this.root, { recursive: true }); }
}
