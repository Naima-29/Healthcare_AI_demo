"""Seat access to Cortex through kcd_core's policy enforcement point.

kcd_core's memory layers (contract C1) are the deck's, the same numbering Cortex uses:
0-1 licensed knowledge (read-only for seats, served by the Knowledge Model), 2 the hospital,
3 the patient. Every read or write asks the PEP first (ctx.check_memory):

    layer 3  patient   ->  evidence + knowledge + idempotency bookkeeping, partition (3, patient id)
    layer 2  hospital  ->  evidence + knowledge, partition (2, hospital id)

Proposing a rule also needs the catalogue tool cortex.knowledge_propose (only the Archivist holds it),
because evidence and rules share a layer. A seat gets a CoreMemory, never the Cortex itself. There is no
confirm, reject, re-verify, retire or erase here: those are human-only and live on Cortex, called by the
Clinical Board.
"""

from __future__ import annotations

from typing import Any, Protocol

from kcd.cortex.store import Cortex

PATIENT = 3
HOSPITAL = 2
DEFAULT_HOSPITAL = "hospital"


class MemoryGuard(Protocol):
    """What CoreMemory needs from kcd_core.runtime.SeatContext (or the PEP itself)."""

    def check_memory(self, layer: Any, mode: str) -> None: ...


class CoreMemory:
    def __init__(self, cortex: Cortex, ctx: MemoryGuard, seat: str, patient_id: str,
                 hospital: str = DEFAULT_HOSPITAL):
        self._cortex = cortex
        self._ctx = ctx
        self.seat = seat
        self.patient_id = patient_id
        self.hospital = hospital

    def _check(self, layer: int, mode: str) -> None:
        self._ctx.check_memory(layer, mode)  # raises kcd_core.pep.PolicyViolation when not allowed

    def _check_propose(self, layer: int) -> None:
        pep = getattr(self._ctx, "pep", self._ctx)  # a SeatContext, or the PEP itself
        pep.check_tool("cortex.knowledge_propose", patient_id=self.patient_id)
        self._check(layer, "write")

    # layer 3: the patient --------------------------------------------------------------------
    def append_evidence(self, *, kind: str, content: dict, blob: bytes | None = None, meta: dict | None = None,
                        source: str | None = None) -> dict:
        self._check(PATIENT, "write")
        return self._cortex.append_evidence(PATIENT, self.patient_id, kind=kind, source=source or f"seat:{self.seat}",
                                            content=content, blob=blob, meta=meta)

    def get_evidence(self, evidence_id: str) -> dict:
        self._check(PATIENT, "read")
        return self._cortex.get_evidence(PATIENT, self.patient_id, evidence_id)

    def list_evidence(self, kind: str | None = None) -> list[dict]:
        self._check(PATIENT, "read")
        return self._cortex.list_evidence(PATIENT, self.patient_id, kind)

    def read_blob(self, sha: str) -> bytes:
        self._check(PATIENT, "read")
        return self._cortex.read_blob(PATIENT, self.patient_id, sha)

    def propose_patient_rule(self, **kwargs: Any) -> dict:
        self._check_propose(PATIENT)
        kwargs["proposed_by"] = f"seat:{self.seat}"
        return self._cortex.propose(PATIENT, self.patient_id, **kwargs)

    def patient_rules(self, status: str | None = None) -> list[dict]:
        self._check(PATIENT, "read")
        return self._cortex.list_knowledge(PATIENT, self.patient_id, status)

    # layer 2: the hospital -------------------------------------------------------------------
    def append_hospital_evidence(self, *, kind: str, content: dict, meta: dict | None = None) -> dict:
        self._check(HOSPITAL, "write")
        return self._cortex.append_evidence(HOSPITAL, self.hospital, kind=kind, source=f"seat:{self.seat}",
                                            content=content, meta=meta)

    def propose_hospital_rule(self, **kwargs: Any) -> dict:
        self._check_propose(HOSPITAL)
        kwargs["proposed_by"] = f"seat:{self.seat}"
        return self._cortex.propose(HOSPITAL, self.hospital, **kwargs)

    def hospital_rules(self, status: str | None = None) -> list[dict]:
        self._check(HOSPITAL, "read")
        return self._cortex.list_knowledge(HOSPITAL, self.hospital, status)

    def active_patient_rules(self) -> list[dict]:
        """Confirmed, still-confident rules for this patient only."""
        self._check(PATIENT, "read")
        return self._cortex.active_rules([(PATIENT, self.patient_id)])

    def active_rules(self) -> list[dict]:
        """Confirmed, still-confident rules for this patient and their hospital."""
        self._check(PATIENT, "read")
        self._check(HOSPITAL, "read")
        return self._cortex.active_rules([(PATIENT, self.patient_id), (HOSPITAL, self.hospital)])

    # idempotency bookkeeping (patient partition, erased with it) -----------------------------
    def mark_processed(self, key: str, result: dict | None = None) -> bool:
        self._check(PATIENT, "write")
        return self._cortex.mark_processed(f"{self.seat}:{key}", PATIENT, self.patient_id, result)

    def processed_result(self, key: str) -> dict | None:
        self._check(PATIENT, "read")
        return self._cortex.processed_result(f"{self.seat}:{key}")
