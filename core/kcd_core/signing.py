"""Ed25519 signatures over RFC 8785 (JCS) canonical JSON.

Dev keys live in memory or on disk; production keys come from Vault behind the same KeyRing interface.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .contracts import Envelope, Release


class SignatureError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj: dict[str, Any]) -> bytes:
    return rfc8785.dumps({k: v for k, v in obj.items() if k != "sig"})


@dataclass(frozen=True)
class Signer:
    key_id: str
    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls, key_id: str) -> Signer:
        return cls(key_id, Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def public_bytes(self) -> bytes:
        return self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign_bytes(self, data: bytes) -> str:
        return _b64(self.private_key.sign(data))


@dataclass
class KeyRing:
    """Public keys by key id (seat id for seats, clinician id for clinicians)."""

    keys: dict[str, Ed25519PublicKey] = field(default_factory=dict)

    def add(self, key_id: str, key: Ed25519PublicKey) -> None:
        self.keys[key_id] = key

    def verify_bytes(self, key_id: str, data: bytes, sig: str | None) -> None:
        if not sig:
            raise SignatureError("missing signature")
        key = self.keys.get(key_id)
        if key is None:
            raise SignatureError(f"unknown key id '{key_id}'")
        try:
            key.verify(_unb64(sig), data)
        except (InvalidSignature, ValueError) as exc:
            raise SignatureError(f"bad signature for '{key_id}'") from exc


def sign_envelope(env: Envelope, signer: Signer) -> Envelope:
    if signer.key_id != env.from_:
        raise SignatureError(f"signer '{signer.key_id}' cannot sign for sender '{env.from_}'")
    return env.model_copy(update={"sig": signer.sign_bytes(canonical(env.wire()))})


def verify_envelope(env: Envelope, keyring: KeyRing) -> None:
    keyring.verify_bytes(env.from_, canonical(env.wire()), env.sig)


def sign_release(release: Release, signer: Signer) -> Release:
    if signer.key_id != release.clinician_id:
        raise SignatureError("a release is signed by the clinician it names")
    return release.model_copy(update={"sig": signer.sign_bytes(canonical(release.model_dump(mode="json")))})


def verify_release(release: Release, keyring: KeyRing) -> None:
    keyring.verify_bytes(release.clinician_id, canonical(release.model_dump(mode="json")), release.sig)
