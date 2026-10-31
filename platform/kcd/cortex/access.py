"""Seat access to Cortex through kcd_core's policy enforcement point.

Every read or write asks the PEP first (ctx.check_memory), using kcd_core's memory layers:

    kcd_core layer                     Cortex storage
    L0  episode working memory     ->  idempotency bookkeeping, partition (3, patient)
    L1  patient evidence           ->  evidence store,  partition (3, patient)
    L2  patient knowledge          ->  knowledge store, partition (3, patient)
    L3  shared hospital knowledge  ->  evidence + knowledge, partition (2, hospital)

(Cortex keeps the deck's numbering internally: 3 = the patient, 2 = the hospital. The deck's
L0 medical knowledge and L1 specialty playbook live in the Knowledge Model, reached via km.lookup.)

A seat gets a CoreMemory, never the Cortex itself. There is no confirm, reject, re-verify,
retire or erase here: those are human-only and live on Cortex, called by the Clinical Board.
"""

from __future__ import annotations

from typing import Any, Protocol

from kcd.cortex.store import Cortex

PATIENT = 3
HOSPITAL = 2
DEFAULT_HOSPITAL = "hospital"


class MemoryGuard(Protocol):
    """What CoreMemory needs from kcd_core.runtime.SeatContext."""

    def check_memory(self, layer: Any, mode: str) -> None: ...


class CoreMemory:
    def __init__(self, cortex: Cortex, ctx: MemoryGuard, seat: str, patient_id: str,
                 hospital: str = DEFAULT_HOSPITAL):
        self._cortex = cortex
        self._ctx = ctx
        self.seat = seat
        self.patient_id = patient_id
        self.hospital = hospital

    def _check(self, layer: str, mode: str) -> None:
        self._ctx.check_memory(layer, mode)  # raises kcd_core.pep.PolicyViolation when not allowed

    # L1: patient evidence ------------------------------------------------------------------
    def append_evidence(self, *, kind: str, content: dict, blob: bytes | None = None, meta: dict | None = None,
                        source: str | None = None) -> dict:
        self._check("L1", "write")
        return self._cortex.append_evidence(PATIENT, self.patient_id, kind=kind, source=source or f"seat:{self.seat}",
                                            content=content, blob=blob, meta=meta)

    def get_evidence(self, evidence_id: str) -> dict:
        self._check("L1", "read")
        return self._cortex.get_evidence(PATIENT, self.patient_id, evidence_id)

    def list_evidence(self, kind: str | None = None) -> list[dict]:
        self._check("L1", "read")
        return self._cortex.list_evidence(PATIENT, self.patient_id, kind)

    def read_blob(self, sha: str) -> bytes:
        self._check("L1", "read")
        return self._cortex.read_blob(PATIENT, self.patient_id, sha)

    # L2: patient knowledge -----------------------------------------------------------------
    def propose_patient_rule(self, **kwargs: Any) -> dict:
        self._check("L2", "write")
        kwargs["proposed_by"] = f"seat:{self.seat}"
        return self._cortex.propose(PATIENT, self.patient_id, **kwargs)

    def patient_rules(self, status: str | None = None) -> list[dict]:
        self._check("L2", "read")
        return self._cortex.list_knowledge(PATIENT, self.patient_id, status)

    # L3: shared hospital knowledge ---------------------------------------------------------
    def append_hospital_evidence(self, *, kind: str, content: dict, meta: dict | None = None) -> dict:
        self._check("L3", "write")
        return self._cortex.append_evidence(HOSPITAL, self.hospital, kind=kind, source=f"seat:{self.seat}",
                                            content=content, meta=meta)

    def propose_hospital_rule(self, **kwargs: Any) -> dict:
        self._check("L3", "write")
        kwargs["proposed_by"] = f"seat:{self.seat}"
        return self._cortex.propose(HOSPITAL, self.hospital, **kwargs)

    def hospital_rules(self, status: str | None = None) -> list[dict]:
        self._check("L3", "read")
        return self._cortex.list_knowledge(HOSPITAL, self.hospital, status)

    def active_rules(self) -> list[dict]:
        """Confirmed, still-confident rules for this patient (L2) and their hospital (L3)."""
        self._check("L2", "read")
        self._check("L3", "read")
        return self._cortex.active_rules([(PATIENT, self.patient_id), (HOSPITAL, self.hospital)])

    # L0: episode bookkeeping (idempotency) -------------------------------------------------
    def mark_processed(self, key: str, result: dict | None = None) -> bool:
        self._check("L0", "write")
        return self._cortex.mark_processed(f"{self.seat}:{key}", PATIENT, self.patient_id, result)

    def processed_result(self, key: str) -> dict | None:
        self._check("L0", "read")
        return self._cortex.processed_result(f"{self.seat}:{key}")
