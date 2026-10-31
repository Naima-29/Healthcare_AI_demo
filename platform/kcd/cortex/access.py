"""Seat access to Cortex, scoped by the manifest's memory read/write layers.

A seat gets a CortexView, never the Cortex itself. The view has no confirm, reject,
re-verify, retire or erase methods: those are human-only and live on Cortex, called
by the Clinical Board.
"""
from __future__ import annotations

from typing import Any, Iterable

from kcd.cortex.store import Cortex
from kcd.seatkit import Manifest, RefusalError


class MemoryScopeError(RefusalError):
    pass


class CortexView:
    def __init__(self, cortex: Cortex, manifest: Manifest):
        self._cortex = cortex
        self.seat = manifest.seat
        self.read_layers = frozenset(manifest.memory_read)
        self.write_layers = frozenset(manifest.memory_write)
        self._tools = frozenset(manifest.tools)

    def _need(self, tool: str, layer: int, mode: str) -> None:
        if tool not in self._tools:
            raise MemoryScopeError(self.seat, "tool_not_in_manifest", tool)
        allowed = self.read_layers if mode == "read" else self.write_layers
        if layer not in allowed:
            raise MemoryScopeError(self.seat, "memory_scope", f"{mode} L{layer} not allowed")

    # evidence
    def append_evidence(self, layer: int, partition: str, **kwargs: Any) -> dict:
        self._need("cortex.evidence_append", layer, "write")
        kwargs.setdefault("source", f"seat:{self.seat}")
        return self._cortex.append_evidence(layer, partition, **kwargs)

    def get_evidence(self, layer: int, partition: str, evidence_id: str) -> dict:
        self._need("cortex.knowledge_read", layer, "read")
        return self._cortex.get_evidence(layer, partition, evidence_id)

    def list_evidence(self, layer: int, partition: str, kind: str | None = None) -> list[dict]:
        self._need("cortex.knowledge_read", layer, "read")
        return self._cortex.list_evidence(layer, partition, kind)

    def read_blob(self, layer: int, partition: str, sha: str) -> bytes:
        self._need("cortex.knowledge_read", layer, "read")
        return self._cortex.read_blob(layer, partition, sha)

    # knowledge
    def propose(self, layer: int, partition: str, **kwargs: Any) -> dict:
        self._need("cortex.knowledge_propose", layer, "write")
        kwargs["proposed_by"] = f"seat:{self.seat}"
        return self._cortex.propose(layer, partition, **kwargs)

    def list_knowledge(self, layer: int, partition: str, status: str | None = None) -> list[dict]:
        self._need("cortex.knowledge_read", layer, "read")
        return self._cortex.list_knowledge(layer, partition, status)

    def active_rules(self, partitions: Iterable[tuple[int, str]]) -> list[dict]:
        partitions = list(partitions)
        for layer, _ in partitions:
            self._need("cortex.knowledge_read", layer, "read")
        return self._cortex.active_rules(partitions)

    # idempotency (bookkeeping, not clinical memory: allowed where the seat may write)
    def mark_processed(self, key: str, layer: int, partition: str, result: dict | None = None) -> bool:
        self._need("cortex.evidence_append", layer, "write")
        return self._cortex.mark_processed(f"{self.seat}:{key}", layer, partition, result)

    def processed_result(self, key: str) -> dict | None:
        return self._cortex.processed_result(f"{self.seat}:{key}")
