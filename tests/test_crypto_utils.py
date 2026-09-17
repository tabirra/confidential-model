import os
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import crypto_utils


def test_key_generation_and_b64_roundtrip():
    key = crypto_utils.generate_key()
    assert len(key) == crypto_utils.KEY_SIZE_BYTES
    b64 = crypto_utils.key_to_b64(key)
    assert crypto_utils.key_from_b64(b64) == key


def test_key_from_b64_rejects_wrong_length():
    with pytest.raises(ValueError):
        crypto_utils.key_from_b64("short")


def test_encrypt_decrypt_roundtrip():
    key = crypto_utils.generate_key()
    plaintext = os.urandom(4096)
    aad = b"prajjwal1/bert-tiny"

    blob = crypto_utils.encrypt_bytes(key, plaintext, aad)
    assert blob != plaintext
    assert len(blob) == crypto_utils.NONCE_SIZE_BYTES + len(plaintext) + 16  # GCM tag

    recovered = crypto_utils.decrypt_bytes(key, blob, aad)
    assert recovered == plaintext


def test_decrypt_fails_with_wrong_key():
    key = crypto_utils.generate_key()
    other_key = crypto_utils.generate_key()
    blob = crypto_utils.encrypt_bytes(key, b"secret model bytes", b"aad")

    with pytest.raises(InvalidTag):
        crypto_utils.decrypt_bytes(other_key, blob, b"aad")


def test_decrypt_fails_with_wrong_aad():
    key = crypto_utils.generate_key()
    blob = crypto_utils.encrypt_bytes(key, b"secret model bytes", b"model-a")

    with pytest.raises(InvalidTag):
        crypto_utils.decrypt_bytes(key, blob, b"model-b")


def test_decrypt_fails_on_tampered_ciphertext():
    key = crypto_utils.generate_key()
    blob = bytearray(crypto_utils.encrypt_bytes(key, b"secret model bytes", b"aad"))
    blob[-1] ^= 0xFF  # flip a byte in the tag/ciphertext

    with pytest.raises(InvalidTag):
        crypto_utils.decrypt_bytes(key, bytes(blob), b"aad")


def test_encrypt_decrypt_file_roundtrip(tmp_path):
    key = crypto_utils.generate_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(os.urandom(1024))

    enc = tmp_path / "plain.bin.enc"
    crypto_utils.encrypt_file(key, str(src), str(enc), aad=b"model-id")

    out = tmp_path / "plain.roundtrip.bin"
    crypto_utils.decrypt_file(key, str(enc), str(out), aad=b"model-id")

    assert out.read_bytes() == src.read_bytes()


def test_sha256_hex_is_deterministic(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"hello world")
    assert crypto_utils.sha256_hex(str(f)) == crypto_utils.sha256_hex(str(f))
    assert len(crypto_utils.sha256_hex(str(f))) == 64
