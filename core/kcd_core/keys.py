"""Key files. Seats sign with ed25519; the dispatcher only needs the public halves.

Layout of KCD_KEYS_DIR:  <id>.pub  (public, base64url raw 32 bytes)   and, on the machine that runs that seat only,
<id>.key (private, same encoding, mode 0600). In production the private keys come from Vault/KMS-mounted files;
this module only reads files, so nothing changes in the code.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from .signing import KeyRing, Signer, _b64, _unb64


def load_keyring(directory: Path) -> KeyRing:
    ring = KeyRing()
    for path in sorted(directory.glob("*.pub")):
        ring.add(path.stem, Ed25519PublicKey.from_public_bytes(_unb64(path.read_text().strip())))
    return ring


def load_signer(directory: Path, key_id: str) -> Signer:
    path = directory / f"{key_id}.key"
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must not be readable by group/others (chmod 600)")
    return Signer(key_id, Ed25519PrivateKey.from_private_bytes(_unb64(path.read_text().strip())))


def generate_keypair(directory: Path, key_id: str, *, overwrite: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pub, priv = directory / f"{key_id}.pub", directory / f"{key_id}.key"
    if (pub.exists() or priv.exists()) and not overwrite:
        raise FileExistsError(f"keys for '{key_id}' already exist")
    signer = Signer.generate(key_id)
    raw = signer.private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(_b64(raw))
    pub.write_text(_b64(signer.public_bytes()))
