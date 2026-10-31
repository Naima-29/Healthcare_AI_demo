"""Loop Cortex memory: evidence + knowledge, four layers, one partition per patient."""
from kcd.cortex.access import CortexView, MemoryScopeError
from kcd.cortex.blobs import BlobCorrupted, FileBlobStore, MemoryBlobStore
from kcd.cortex.store import (
    Cortex, CortexError, EvidenceRequired, HumanActor, HumanRequired, InvalidTransition, NotFound,
)

HOSPITAL_PARTITION = "hospital"


def patient_partition(patient_id: str) -> tuple[int, str]:
    return (3, patient_id)


__all__ = [
    "BlobCorrupted", "Cortex", "CortexError", "CortexView", "EvidenceRequired", "FileBlobStore",
    "HOSPITAL_PARTITION", "HumanActor", "HumanRequired", "InvalidTransition", "MemoryBlobStore",
    "MemoryScopeError", "NotFound", "patient_partition",
]
