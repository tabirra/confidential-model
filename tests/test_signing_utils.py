import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import signing_utils


def test_generate_and_pem_roundtrip(tmp_path):
    key = signing_utils.generate_signing_key()
    priv_path = tmp_path / "signing-key.pem"
    pub_path = tmp_path / "signing-public-key.pem"

    signing_utils.save_private_key(key, str(priv_path))
    signing_utils.save_public_key(key.public_key(), str(pub_path))

    loaded_priv = signing_utils.load_private_key(str(priv_path))
    loaded_pub = signing_utils.load_public_key(str(pub_path))

    assert signing_utils.public_key_pem(loaded_priv.public_key()) == signing_utils.public_key_pem(key.public_key())
    assert signing_utils.public_key_pem(loaded_pub) == signing_utils.public_key_pem(key.public_key())


def test_sign_verify_roundtrip():
    key = signing_utils.generate_signing_key()
    data = b"this stands in for an encrypted model artifact"

    signature = signing_utils.sign_bytes(key, data)
    signing_utils.verify_bytes(key.public_key(), data, signature)  # does not raise


def test_verify_fails_with_wrong_key():
    key = signing_utils.generate_signing_key()
    other_key = signing_utils.generate_signing_key()
    data = b"artifact bytes"

    signature = signing_utils.sign_bytes(key, data)
    with pytest.raises(signing_utils.InvalidSignature):
        signing_utils.verify_bytes(other_key.public_key(), data, signature)


def test_verify_fails_on_tampered_data():
    key = signing_utils.generate_signing_key()
    data = bytearray(b"artifact bytes")
    signature = signing_utils.sign_bytes(key, bytes(data))

    data[-1] ^= 0xFF  # simulate tampering after signing
    with pytest.raises(signing_utils.InvalidSignature):
        signing_utils.verify_bytes(key.public_key(), bytes(data), signature)


def test_verify_fails_on_tampered_signature():
    key = signing_utils.generate_signing_key()
    data = b"artifact bytes"
    signature = bytearray(signing_utils.sign_bytes(key, data))
    signature[0] ^= 0xFF

    with pytest.raises(signing_utils.InvalidSignature):
        signing_utils.verify_bytes(key.public_key(), data, bytes(signature))


def test_sign_verify_file_roundtrip(tmp_path):
    key = signing_utils.generate_signing_key()
    artifact = tmp_path / "model.tar.gz.enc"
    artifact.write_bytes(b"\x01\x02\x03" * 1000)

    signature = signing_utils.sign_file(key, str(artifact))
    signing_utils.verify_file(key.public_key(), str(artifact), signature)  # does not raise


def test_verify_file_fails_when_artifact_modified_after_signing(tmp_path):
    key = signing_utils.generate_signing_key()
    artifact = tmp_path / "model.tar.gz.enc"
    artifact.write_bytes(b"original ciphertext bytes")

    signature = signing_utils.sign_file(key, str(artifact))

    artifact.write_bytes(b"tampered ciphertext bytes")
    with pytest.raises(signing_utils.InvalidSignature):
        signing_utils.verify_file(key.public_key(), str(artifact), signature)


def test_load_private_key_rejects_non_ed25519_pem(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "rsa-key.pem"
    path.write_bytes(pem)

    with pytest.raises(ValueError):
        signing_utils.load_private_key(str(path))
