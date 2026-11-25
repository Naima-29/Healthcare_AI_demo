"""Content-addressed blob storage for evidence (CT images, heat maps, panel files).

Folders are named by a hash of the partition, so patient ids never appear on disk.
FileBlobStore is the development store; MinIO replaces it in the pilot behind the same methods.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from kcd.util import sha256_hex


class BlobCorrupted(IOError):
    pass


class BlobStore(Protocol):
    def put(self, layer: int, partition: str, data: bytes) -> str: ...
    def get(self, layer: int, partition: str, sha: str) -> bytes: ...
    def exists(self, layer: int, partition: str, sha: str) -> bool: ...
    def delete_partition(self, layer: int, partition: str) -> int: ...


def _partition_dir_name(layer: int, partition: str) -> str:
    return sha256_hex(f"{layer}:{partition}")[:32]


def _check_sha(sha: str) -> None:
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"not a sha256 hex digest: {sha!r}")


class MemoryBlobStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    def put(self, layer: int, partition: str, data: bytes) -> str:
        sha = sha256_hex(data)
        self._data[(_partition_dir_name(layer, partition), sha)] = bytes(data)
        return sha

    def get(self, layer: int, partition: str, sha: str) -> bytes:
        try:
            data = self._data[(_partition_dir_name(layer, partition), sha)]
        except KeyError:
            raise FileNotFoundError(sha) from None
        if sha256_hex(data) != sha:
            raise BlobCorrupted(sha)
        return data

    def exists(self, layer: int, partition: str, sha: str) -> bool:
        return (_partition_dir_name(layer, partition), sha) in self._data

    def delete_partition(self, layer: int, partition: str) -> int:
        d = _partition_dir_name(layer, partition)
        keys = [k for k in self._data if k[0] == d]
        for k in keys:
            del self._data[k]
        return len(keys)


class FileBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, layer: int, partition: str, sha: str) -> Path:
        _check_sha(sha)
        return self.root / _partition_dir_name(layer, partition) / sha

    def put(self, layer: int, partition: str, data: bytes) -> str:
        sha = sha256_hex(data)
        path = self._path(layer, partition, sha)
        if path.exists():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return sha

    def get(self, layer: int, partition: str, sha: str) -> bytes:
        data = self._path(layer, partition, sha).read_bytes()
        if sha256_hex(data) != sha:
            raise BlobCorrupted(sha)
        return data

    def exists(self, layer: int, partition: str, sha: str) -> bool:
        return self._path(layer, partition, sha).exists()

    def delete_partition(self, layer: int, partition: str) -> int:
        d = self.root / _partition_dir_name(layer, partition)
        if not d.exists():
            return 0
        count = sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith(".tmp-"))
        shutil.rmtree(d)
        return count
