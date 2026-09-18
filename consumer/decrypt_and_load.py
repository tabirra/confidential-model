#!/usr/bin/env python3
"""Consumer side of the confidential model delivery pipeline.

Runs inside a Kubernetes pod that has the decryption key mounted from a
Kubernetes Secret (see k8s/consumer-pod.yaml). It:

1. Reads the decryption key from the mounted Secret volume.
2. Downloads the encrypted artifact + manifest from the Hugging Face Hub.
3. Verifies the ciphertext checksum against the manifest.
4. Decrypts the archive with AES-256-GCM.
5. Extracts it and loads the model with `transformers`.
6. Runs a small sanity inference to prove the model is usable.

Configuration is via environment variables so the same image can be reused
across models/repos without rebuilding:

    HF_REPO_ID           Hub repo holding the encrypted artifact (required)
    HF_REPO_TYPE         "model" or "dataset" (default: model)
    HF_MODEL_ID          Original model id, used as AES-GCM AAD (required,
                          must match what the producer used)
    HF_TOKEN              Hugging Face token, only needed if HF_REPO_ID is private
    DECRYPTION_KEY_PATH   Path to the mounted secret key file
                          (default: /etc/secrets/decryption-key/key)
    WORK_DIR              Scratch directory (default: /tmp/confidential-model)
"""
from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path

from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import crypto_utils  # noqa: E402

ENCRYPTED_ARTIFACT_NAME = "model.tar.gz.enc"
MANIFEST_NAME = "manifest.json"


def load_key(key_path: str) -> bytes:
    path = Path(key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"decryption key not found at {path}. Is the Kubernetes Secret "
            "mounted correctly? See k8s/consumer-pod.yaml."
        )
    return crypto_utils.key_from_b64(path.read_text())


def download_encrypted_artifact(repo_id: str, repo_type: str, dest_dir: Path) -> tuple[Path, Path]:
    from huggingface_hub import hf_hub_download

    print(f"[consumer] downloading encrypted artifact from '{repo_id}' ...")
    enc_path = hf_hub_download(repo_id=repo_id, repo_type=repo_type,
                                filename=ENCRYPTED_ARTIFACT_NAME, local_dir=dest_dir)
    manifest_path = hf_hub_download(repo_id=repo_id, repo_type=repo_type,
                                     filename=MANIFEST_NAME, local_dir=dest_dir)
    print(f"[consumer] downloaded {enc_path} and {manifest_path}")
    return Path(enc_path), Path(manifest_path)


def verify_manifest(enc_path: Path, manifest_path: Path) -> dict:
    import json

    manifest = json.loads(manifest_path.read_text())
    actual = crypto_utils.sha256_hex(str(enc_path))
    expected = manifest.get("ciphertext_sha256")
    if actual != expected:
        raise ValueError(
            f"ciphertext checksum mismatch: expected {expected}, got {actual}. "
            "The artifact may be corrupted or tampered with."
        )
    print("[consumer] ciphertext checksum verified against manifest")
    return manifest


def decrypt_and_extract(key: bytes, enc_path: Path, aad: bytes, extract_dir: Path) -> Path:
    tar_path = enc_path.with_suffix("")  # strip .enc -> model.tar.gz
    print(f"[consumer] decrypting {enc_path} -> {tar_path}")
    try:
        crypto_utils.decrypt_file(key, str(enc_path), str(tar_path), aad=aad)
    except InvalidTag as exc:
        raise ValueError(
            "decryption FAILED: the mounted key does not match this ciphertext "
            "(wrong key, or the model id doesn't match the AAD used at encrypt "
            "time). If the producer was re-run since this key was delivered, "
            "re-run scripts/create_k8s_secret.sh with the new "
            "secrets/decryption-key.b64 and redeploy."
        ) from exc

    print(f"[consumer] extracting {tar_path} -> {extract_dir}")
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    entries = [p for p in extract_dir.iterdir() if p.is_dir()]
    if not entries:
        raise RuntimeError(f"no model directory found after extracting into {extract_dir}")
    return entries[0]


def _patch_missing_model_type(model_dir: Path, default: str = "bert") -> None:
    # Pre-2021 Hub checkpoints (e.g. the default prajjwal1/bert-tiny) predate
    # the `model_type` field in config.json. Loading such a repo id directly
    # from the Hub still works because the Hub API backfills model_type from
    # repo metadata, but this consumer only ever loads from a local
    # directory post-decrypt, where AutoConfig has nothing to fall back on.
    config_path = model_dir / "config.json"
    config = json.loads(config_path.read_text())
    if "model_type" not in config:
        config["model_type"] = default
        config_path.write_text(json.dumps(config))


def load_and_sanity_check(model_dir: Path) -> None:
    from transformers import AutoModel, AutoTokenizer

    _patch_missing_model_type(model_dir)

    print(f"[consumer] loading model from {model_dir} ...")
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModel.from_pretrained(model_dir)
    model.eval()

    inputs = tokenizer("Confidential model delivery works.", return_tensors="pt")
    import torch

    with torch.no_grad():
        outputs = model(**inputs)

    print(f"[consumer] loaded model OK, last_hidden_state shape={tuple(outputs.last_hidden_state.shape)}")


def main() -> None:
    repo_id = os.environ.get("HF_REPO_ID")
    if not repo_id:
        raise SystemExit("HF_REPO_ID environment variable is required")
    repo_type = os.environ.get("HF_REPO_TYPE", "model")
    model_id = os.environ.get("HF_MODEL_ID")
    if not model_id:
        raise SystemExit("HF_MODEL_ID environment variable is required (must match the producer's --model-id)")

    key_path = os.environ.get("DECRYPTION_KEY_PATH", "/etc/secrets/decryption-key/key")
    work_dir = Path(os.environ.get("WORK_DIR", "/tmp/confidential-model"))
    work_dir.mkdir(parents=True, exist_ok=True)

    key = load_key(key_path)
    print("[consumer] loaded decryption key from mounted Kubernetes Secret")

    enc_path, manifest_path = download_encrypted_artifact(repo_id, repo_type, work_dir)
    verify_manifest(enc_path, manifest_path)

    model_dir = decrypt_and_extract(key, enc_path, model_id.encode("utf-8"), work_dir / "extracted")
    load_and_sanity_check(model_dir)

    print("[consumer] done: model decrypted and loaded successfully")


if __name__ == "__main__":
    main()
