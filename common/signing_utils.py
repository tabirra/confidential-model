"""Ed25519 signing helpers shared by the producer and consumer.

The producer signs the encrypted artifact with a private key; the consumer
verifies that signature with the corresponding public key before decryption
is attempted, aborting if verification fails.

Trust note: the public key must reach the consumer through a channel the
Hugging Face Hub upload doesn't control (this pipeline delivers it via a
Kubernetes ConfigMap, see k8s/configmap.example.yaml). If the public key
were instead fetched from the same Hub repo as the artifact + signature,
an attacker able to rewrite that repo could replace all three together and
verification would pass against a forged key.
"""
from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidSignature  # noqa: F401  (re-exported)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "InvalidSignature",
    "generate_signing_key",
    "load_private_key",
    "load_public_key",
    "public_key_pem",
    "save_private_key",
    "save_public_key",
    "sign_bytes",
    "sign_file",
    "verify_bytes",
    "verify_file",
]


def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_key_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def save_private_key(key: Ed25519PrivateKey, path: str) -> None:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    Path(path).write_bytes(pem)


def save_public_key(key: Ed25519PublicKey, path: str) -> None:
    Path(path).write_bytes(public_key_pem(key))


def load_private_key(path: str) -> Ed25519PrivateKey:
    pem = Path(path).read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def load_public_key(path: str) -> Ed25519PublicKey:
    pem = Path(path).read_bytes()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} is not an Ed25519 public key")
    return key


def sign_bytes(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    return private_key.sign(data)


def verify_bytes(public_key: Ed25519PublicKey, data: bytes, signature: bytes) -> None:
    """Raises cryptography.exceptions.InvalidSignature if verification fails."""
    public_key.verify(signature, data)


def sign_file(private_key: Ed25519PrivateKey, path: str) -> bytes:
    with open(path, "rb") as f:
        return sign_bytes(private_key, f.read())


def verify_file(public_key: Ed25519PublicKey, path: str, signature: bytes) -> None:
    with open(path, "rb") as f:
        verify_bytes(public_key, f.read(), signature)
