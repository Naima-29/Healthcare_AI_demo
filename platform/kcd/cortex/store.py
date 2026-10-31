"""Loop Cortex: evidence store + knowledge store, one partition per patient (deck slide 24).

* Evidence ("what proves"): append-only, sha256 hash chain per partition, optional blob.
* Knowledge ("what is believed"): typed, versioned rules. New rules start in trial
  (status "shadow") and only a verified human can confirm them. Confidence decays with a
  half-life unless a human re-verifies the rule.
* Erasure: deleting an L3 partition removes the patient's evidence, rules and blobs,
  runs the registered hooks (e.g. a per-patient adapter) and leaves an anonymous receipt.

Seats never touch this class directly: they get a CortexView scoped by their manifest
(see access.py). Confirm, reject, re-verify, retire and erase are human-only operations
called by the Clinical Board.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy import and_, create_engine, delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from kcd.cortex import schema
from kcd.cortex.blobs import BlobStore, MemoryBlobStore
from kcd.util import canonical_json, iso, new_id, parse_iso, sha256_hex, utcnow

GENESIS = "0" * 64
LAYERS = (0, 1, 2, 3)
STATUSES = ("shadow", "confirmed", "rejected", "superseded", "retired")
HUMAN_ROLES = frozenset({
    "clinician", "treating_clinician", "nephrologist", "radiologist", "uro_oncologist", "oncologist",
    "geneticist", "oncall_clinician", "director", "dpo",
})
DEFAULT_HALF_LIFE_DAYS = 180.0
APPEND_RETRIES = 10


class CortexError(Exception):
    pass


class NotFound(CortexError, KeyError):
    pass


class InvalidTransition(CortexError):
    pass


class EvidenceRequired(CortexError, PermissionError):
    """Refusal store_without_evidence."""


class HumanRequired(CortexError, PermissionError):
    """Refusal auto_confirm_rule: only a verified human may decide."""


@dataclass(frozen=True)
class HumanActor:
    """A logged-in person acting on the Clinical Board. release_ref links to their signed release."""
    id: str
    role: str
    release_ref: str | None = None


HumanVerifier = Callable[[HumanActor], bool]


def default_human_verifier(actor: HumanActor) -> bool:
    """Development verifier. Core replaces it with a check of the clinician's signed release."""
    return (isinstance(actor, HumanActor) and bool(actor.id and actor.id.strip())
            and not actor.id.startswith("seat:") and actor.role in HUMAN_ROLES)


def _check_partition(layer: int, partition: str) -> None:
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {LAYERS}, got {layer!r}")
    if not isinstance(partition, str) or not partition.strip() or len(partition) > 200:
        raise ValueError("partition must be a non-empty string of at most 200 characters")


class Cortex:
    def __init__(self, url: str = "sqlite://", *, blobs: BlobStore | None = None,
                 clock: Callable[[], datetime] = utcnow, human_verifier: HumanVerifier = default_human_verifier,
                 min_active_confidence: float = 0.5, engine: Engine | None = None,
                 receipt_key: bytes | None = None):
        self.engine = engine or _make_engine(url)
        schema.metadata.create_all(self.engine)
        self.blobs = blobs if blobs is not None else MemoryBlobStore()
        self.clock = clock
        self.human_verifier = human_verifier
        self.min_active_confidence = min_active_confidence
        self._erase_hooks: list[Callable[[str], None]] = []
        # With a secret key the receipt's subject hash is an HMAC, so a list of patient ids can't be
        # tested against stored receipts. Without one it is a plain sha256 (development only).
        self._receipt_key = receipt_key

    # ------------------------------------------------------------------ evidence
    def append_evidence(self, layer: int, partition: str, *, kind: str, source: str, content: dict,
                        blob: bytes | None = None, meta: dict | None = None) -> dict:
        _check_partition(layer, partition)
        if not kind or not source:
            raise ValueError("evidence needs a kind and a source")
        if not isinstance(content, dict):
            raise ValueError("evidence content must be a JSON object")
        content_json = canonical_json(content)
        meta_json = canonical_json(meta or {})
        blob_sha = self.blobs.put(layer, partition, blob) if blob is not None else None
        for _ in range(APPEND_RETRIES):
            try:
                with self.engine.begin() as conn:
                    last = conn.execute(
                        select(schema.evidence.c.seq, schema.evidence.c.chain_hash)
                        .where(_part(schema.evidence, layer, partition))
                        .order_by(schema.evidence.c.seq.desc()).limit(1)
                    ).first()
                    seq, prev = (last.seq + 1, last.chain_hash) if last else (1, GENESIS)
                    row = {
                        "id": new_id("ev"), "layer": layer, "partition": partition, "seq": seq, "kind": kind,
                        "source": source, "recorded_at": iso(self.clock()), "content": content_json,
                        "content_sha256": sha256_hex(content_json), "blob_sha256": blob_sha, "meta": meta_json,
                        "prev_hash": prev,
                    }
                    row["chain_hash"] = _chain_hash(row)
                    conn.execute(insert(schema.evidence).values(**row))
                    return _evidence_out(row)
            except IntegrityError:
                continue  # another writer took this chain position; read the new tail and retry
        raise CortexError(f"could not append evidence to L{layer}/{partition} after {APPEND_RETRIES} attempts")

    def get_evidence(self, layer: int, partition: str, evidence_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(schema.evidence).where(
                and_(_part(schema.evidence, layer, partition), schema.evidence.c.id == evidence_id))).first()
        if row is None:
            raise NotFound(f"evidence {evidence_id} not in L{layer}/{partition}")
        return _evidence_out(row._asdict())

    def list_evidence(self, layer: int, partition: str, kind: str | None = None) -> list[dict]:
        q = select(schema.evidence).where(_part(schema.evidence, layer, partition))
        if kind:
            q = q.where(schema.evidence.c.kind == kind)
        with self.engine.connect() as conn:
            return [_evidence_out(r._asdict()) for r in conn.execute(q.order_by(schema.evidence.c.seq))]

    def read_blob(self, layer: int, partition: str, sha: str) -> bytes:
        return self.blobs.get(layer, partition, sha)

    def verify_chain(self, layer: int, partition: str) -> dict:
        """Recompute every hash in the partition. Detects edited, deleted or reordered rows and bad blobs."""
        with self.engine.connect() as conn:
            rows = [r._asdict() for r in conn.execute(
                select(schema.evidence).where(_part(schema.evidence, layer, partition)).order_by(schema.evidence.c.seq))]
        prev = GENESIS
        for expected_seq, row in enumerate(rows, 1):
            problem = None
            if row["seq"] != expected_seq:
                problem = f"gap or reorder: expected seq {expected_seq}, found {row['seq']}"
            elif row["prev_hash"] != prev:
                problem = "prev_hash does not match the previous entry"
            elif sha256_hex(row["content"]) != row["content_sha256"]:
                problem = "content was modified"
            elif _chain_hash(row) != row["chain_hash"]:
                problem = "chain_hash does not match the entry"
            elif row["blob_sha256"]:
                try:
                    self.blobs.get(layer, partition, row["blob_sha256"])
                except (FileNotFoundError, OSError) as e:
                    problem = f"blob missing or corrupted ({type(e).__name__})"
            if problem:
                return {"ok": False, "length": len(rows), "broken_at": row["id"], "seq": row["seq"], "reason": problem}
            prev = row["chain_hash"]
        return {"ok": True, "length": len(rows), "broken_at": None, "seq": None, "reason": None}

    # ------------------------------------------------------------------ knowledge
    def propose(self, layer: int, partition: str, *, key: str, value: Any, source: str,
                evidence_ids: Iterable[str], proposed_by: str, confidence: float = 0.9,
                half_life_days: float = DEFAULT_HALF_LIFE_DAYS, note: str | None = None) -> dict:
        _check_partition(layer, partition)
        evidence_ids = list(dict.fromkeys(evidence_ids or []))
        if not key or not source or not proposed_by:
            raise ValueError("a rule needs a key, a source and a proposer")
        if not 0.0 < float(confidence) <= 1.0:
            raise ValueError("confidence must be in (0, 1]")
        if not float(half_life_days) > 0:
            raise ValueError("half_life_days must be positive")
        if not evidence_ids:
            raise EvidenceRequired("refused (store_without_evidence): a rule must cite evidence")
        value_json = canonical_json(value)
        for _ in range(APPEND_RETRIES):
            try:
                with self.engine.begin() as conn:
                    found = set(conn.execute(select(schema.evidence.c.id).where(and_(
                        _part(schema.evidence, layer, partition), schema.evidence.c.id.in_(evidence_ids)))).scalars())
                    missing = [e for e in evidence_ids if e not in found]
                    if missing:
                        raise EvidenceRequired(
                            f"refused (store_without_evidence): evidence {missing} not found in L{layer}/{partition}")
                    version = (conn.execute(select(func.max(schema.knowledge.c.version)).where(and_(
                        _part(schema.knowledge, layer, partition), schema.knowledge.c.key == key))).scalar() or 0) + 1
                    row = {
                        "id": new_id("ko"), "layer": layer, "partition": partition, "key": key, "version": version,
                        "value": value_json, "confidence": float(confidence), "half_life_days": float(half_life_days),
                        "source": source, "evidence_ids": json.dumps(evidence_ids), "status": "shadow",
                        "proposed_by": proposed_by, "proposed_at": iso(self.clock()), "decided_by": None,
                        "decided_role": None, "decided_at": None, "last_verified": None, "note": note,
                    }
                    conn.execute(insert(schema.knowledge).values(**row))
                    return self._knowledge_out(row)
            except IntegrityError:
                continue  # concurrent proposal took this version number
        raise CortexError(f"could not propose rule {key!r} after {APPEND_RETRIES} attempts")

    def confirm(self, knowledge_id: str, actor: HumanActor) -> dict:
        self._require_human(actor)
        now = iso(self.clock())
        with self.engine.begin() as conn:
            row = self._load_for_update(conn, knowledge_id)
            if row["status"] != "shadow":
                raise InvalidTransition(f"only a trial (shadow) rule can be confirmed; this one is {row['status']}")
            conn.execute(update(schema.knowledge).where(and_(
                _part(schema.knowledge, row["layer"], row["partition"]), schema.knowledge.c.key == row["key"],
                schema.knowledge.c.status == "confirmed")).values(status="superseded"))
            changes = {"status": "confirmed", "decided_by": actor.id, "decided_role": actor.role,
                       "decided_at": now, "last_verified": now}
            conn.execute(update(schema.knowledge).where(schema.knowledge.c.id == knowledge_id).values(**changes))
            row.update(changes)
        return self._knowledge_out(row)

    def reject(self, knowledge_id: str, actor: HumanActor, reason: str) -> dict:
        return self._decide(knowledge_id, actor, from_status="shadow", to_status="rejected", note=reason)

    def retire(self, knowledge_id: str, actor: HumanActor, reason: str) -> dict:
        return self._decide(knowledge_id, actor, from_status="confirmed", to_status="retired", note=reason)

    def reverify(self, knowledge_id: str, actor: HumanActor) -> dict:
        """A human re-checks a confirmed rule: its confidence stops decaying from now."""
        self._require_human(actor)
        now = iso(self.clock())
        with self.engine.begin() as conn:
            row = self._load_for_update(conn, knowledge_id)
            if row["status"] != "confirmed":
                raise InvalidTransition(f"only a confirmed rule can be re-verified; this one is {row['status']}")
            changes = {"last_verified": now, "decided_by": actor.id, "decided_role": actor.role}
            conn.execute(update(schema.knowledge).where(schema.knowledge.c.id == knowledge_id).values(**changes))
            row.update(changes)
        return self._knowledge_out(row)

    def get_knowledge(self, knowledge_id: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(schema.knowledge).where(schema.knowledge.c.id == knowledge_id)).first()
        if row is None:
            raise NotFound(f"rule {knowledge_id} not found")
        return self._knowledge_out(row._asdict())

    def list_knowledge(self, layer: int, partition: str, status: str | None = None) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        q = select(schema.knowledge).where(_part(schema.knowledge, layer, partition))
        if status:
            q = q.where(schema.knowledge.c.status == status)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(schema.knowledge.c.key, schema.knowledge.c.version)).all()
        return [self._knowledge_out(r._asdict()) for r in rows]

    def active_rules(self, partitions: Iterable[tuple[int, str]]) -> list[dict]:
        """Confirmed rules whose decayed confidence is still at or above min_active_confidence."""
        out = []
        for layer, partition in partitions:
            for k in self.list_knowledge(layer, partition, status="confirmed"):
                if k["confidence_effective"] >= self.min_active_confidence:
                    out.append(k)
        return out

    # ------------------------------------------------------------------ idempotency
    def mark_processed(self, key: str, layer: int, partition: str, result: dict | None = None) -> bool:
        """Record that a seat handled `key`. Returns False if it was already recorded."""
        _check_partition(layer, partition)
        try:
            with self.engine.begin() as conn:
                conn.execute(insert(schema.processed).values(
                    key=key, layer=layer, partition=partition,
                    result=canonical_json(result) if result is not None else None, at=iso(self.clock())))
            return True
        except IntegrityError:
            return False

    def processed_result(self, key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(schema.processed).where(schema.processed.c.key == key)).first()
        if row is None:
            return None
        return json.loads(row.result) if row.result else {}

    # ------------------------------------------------------------------ erasure
    def register_erase_hook(self, hook: Callable[[str], None]) -> None:
        """Called with the patient id after their partition is deleted (e.g. drop a per-patient adapter)."""
        self._erase_hooks.append(hook)

    def erase_patient(self, patient_id: str, actor: HumanActor, reason: str) -> dict:
        """Right to erasure (GDPR art. 17 / Law 09-08): delete the patient's L3 partition. No retraining needed."""
        self._require_human(actor)
        _check_partition(3, patient_id)
        if not reason or not reason.strip():
            raise ValueError("an erasure needs a reason")
        with self.engine.begin() as conn:
            ev = conn.execute(delete(schema.evidence).where(_part(schema.evidence, 3, patient_id))).rowcount
            kn = conn.execute(delete(schema.knowledge).where(_part(schema.knowledge, 3, patient_id))).rowcount
            conn.execute(delete(schema.processed).where(_part(schema.processed, 3, patient_id)))
        blobs = self.blobs.delete_partition(3, patient_id)
        hook_errors = []
        for hook in self._erase_hooks:
            try:
                hook(patient_id)
            except Exception as e:  # keep erasing; the receipt records what failed
                hook_errors.append(f"{getattr(hook, '__name__', 'hook')}: {type(e).__name__}: {e}")
        receipt = {
            "id": new_id("er"), "subject_hash": self._subject_hash(patient_id), "requested_by": actor.id,
            "requested_role": actor.role, "reason": reason, "erased_at": iso(self.clock()),
            "evidence_rows": ev, "knowledge_rows": kn, "blobs_deleted": blobs, "hook_errors": json.dumps(hook_errors),
        }
        with self.engine.begin() as conn:
            conn.execute(insert(schema.erasure_receipts).values(**receipt))
        return {**receipt, "hook_errors": hook_errors, "complete": not hook_errors}

    # ------------------------------------------------------------------ internals
    def _subject_hash(self, patient_id: str) -> str:
        msg = f"3:{patient_id}".encode("utf-8")
        if self._receipt_key:
            return hmac.new(self._receipt_key, msg, hashlib.sha256).hexdigest()
        return sha256_hex(msg)

    def _require_human(self, actor: HumanActor) -> None:
        if not self.human_verifier(actor):
            raise HumanRequired("refused (auto_confirm_rule): this action needs a verified human")

    def _decide(self, knowledge_id: str, actor: HumanActor, *, from_status: str, to_status: str, note: str) -> dict:
        self._require_human(actor)
        if not note or not note.strip():
            raise ValueError(f"{to_status} needs a reason")
        now = iso(self.clock())
        with self.engine.begin() as conn:
            row = self._load_for_update(conn, knowledge_id)
            if row["status"] != from_status:
                raise InvalidTransition(f"rule is {row['status']}; only {from_status} rules can become {to_status}")
            changes = {"status": to_status, "decided_by": actor.id, "decided_role": actor.role,
                       "decided_at": now, "note": note}
            conn.execute(update(schema.knowledge).where(schema.knowledge.c.id == knowledge_id).values(**changes))
            row.update(changes)
        return self._knowledge_out(row)

    @staticmethod
    def _load_for_update(conn: Connection, knowledge_id: str) -> dict:
        row = conn.execute(select(schema.knowledge).where(schema.knowledge.c.id == knowledge_id)
                           .with_for_update()).first()
        if row is None:
            raise NotFound(f"rule {knowledge_id} not found")
        return row._asdict()

    def _knowledge_out(self, row: dict) -> dict:
        out = dict(row)
        out["value"] = json.loads(row["value"])
        out["evidence_ids"] = json.loads(row["evidence_ids"])
        out["confidence_effective"] = self._effective_confidence(row)
        return out

    def _effective_confidence(self, row: dict) -> float:
        if row["status"] != "confirmed" or not row["last_verified"]:
            return 0.0
        days = max(0.0, (self.clock() - parse_iso(row["last_verified"])).total_seconds() / 86400.0)
        return round(row["confidence"] * 0.5 ** (days / row["half_life_days"]), 4)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if url in ("sqlite://", "sqlite:///:memory:"):
            # One shared connection, so every caller sees the same in-memory database.
            return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True)


def _part(table, layer: int, partition: str):
    return and_(table.c.layer == layer, table.c.partition == partition)


def _chain_hash(row: dict) -> str:
    fields = {k: row[k] for k in ("layer", "partition", "seq", "kind", "source", "recorded_at",
                                  "content_sha256", "blob_sha256", "meta")}
    return sha256_hex(row["prev_hash"] + "|" + canonical_json(fields))


def _evidence_out(row: dict) -> dict:
    out = dict(row)
    out["content"] = json.loads(row["content"])
    out["meta"] = json.loads(row["meta"])
    return out
