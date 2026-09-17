"""AES-256-GCM helpers shared by the producer and consumer.

Wire format for an encrypted artifact is a single binary blob:

    nonce (12 bytes) || ciphertext (variable) || tag (16 bytes, appended by AESGCM)

The model id is passed in as additional authenticated data (AAD) so a
ciphertext can't be silently paired with the wrong manifest/model id.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_SIZE_BYTES = 32  # AES-256
NONCE_SIZE_BYTES = 12  # standard GCM nonce size


def generate_key() -> bytes:
    """Generate a fresh random 256-bit key."""
    return os.urandom(KEY_SIZE_BYTES)


def key_to_b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def key_from_b64(key_b64: str) -> bytes:
    key = base64.b64decode(key_b64.strip())
    if len(key) != KEY_SIZE_BYTES:
        raise ValueError(f"decryption key must be {KEY_SIZE_BYTES} bytes, got {len(key)}")
    return key


def encrypt_bytes(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Encrypt plaintext with AES-256-GCM, returning nonce||ciphertext||tag."""
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
    return nonce + ciphertext


def decrypt_bytes(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """Reverse of encrypt_bytes. Raises InvalidTag if the key/AAD is wrong
    or the artifact was tampered with."""
    if len(blob) < NONCE_SIZE_BYTES:
        raise ValueError("ciphertext blob is too short to contain a nonce")
    aesgcm = AESGCM(key)
    nonce, ciphertext = blob[:NONCE_SIZE_BYTES], blob[NONCE_SIZE_BYTES:]
    return aesgcm.decrypt(nonce, ciphertext, aad)


def encrypt_file(key: bytes, in_path: str, out_path: str, aad: bytes = b"") -> None:
    with open(in_path, "rb") as f:
        plaintext = f.read()
    blob = encrypt_bytes(key, plaintext, aad)
    with open(out_path, "wb") as f:
        f.write(blob)


def decrypt_file(key: bytes, in_path: str, out_path: str, aad: bytes = b"") -> None:
    with open(in_path, "rb") as f:
        blob = f.read()
    plaintext = decrypt_bytes(key, blob, aad)
    with open(out_path, "wb") as f:
        f.write(plaintext)


def sha256_hex(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
