#!/usr/bin/env python3
"""Consumer side of the confidential model delivery pipeline.

Runs inside a Kubernetes pod that has the signing public key mounted from
a Kubernetes ConfigMap, and obtains the decryption key one of two ways:

1. **Layer 1 (default)**: from a Kubernetes Secret mounted as a file (see
   k8s/consumer-pod.yaml). The host/kubelet controls delivery; the
   consumer trusts whoever populated the Secret.
2. **Layer 3 (attested key release)**: fetched from Trustee KBS through
   the in-guest Confidential Data Hub (CDH) API, which only releases the
   resource after the pod's Kata/CoCo guest has passed remote attestation
   (see k8s/consumer-pod-coco.yaml, kbs/, scripts/deploy_coco_kbs.sh,
   scripts/push_key_to_kbs.sh). The consumer never trusts the host for key
   delivery in this mode — set KBS_RESOURCE_PATH to switch to it.

Either way, it then:

1. Obtains the decryption key (Secret file, or attested KBS/CDH fetch) and
   the signing public key (ConfigMap).
2. Downloads the encrypted artifact + signature + manifest from the
   Hugging Face Hub.
3. Verifies the ciphertext checksum against the manifest.
4. Verifies the Ed25519 signature over the ciphertext against the
   trusted public key, and ABORTS before any decryption is attempted if
   verification fails.
5. Decrypts the archive with AES-256-GCM.
6. Extracts it and loads the model with `transformers`.
7. Runs a small sanity inference to prove the model is usable.

Configuration is via environment variables so the same image can be reused
across models/repos without rebuilding:

    HF_REPO_ID               Hub repo holding the encrypted artifact (required)
    HF_REPO_TYPE              "model" or "dataset" (default: model)
    HF_MODEL_ID               Original model id, used as AES-GCM AAD (required,
                              must match what the producer used)
    HF_TOKEN                  Hugging Face token, only needed if HF_REPO_ID is private
    SIGNING_PUBLIC_KEY_PATH   Path to the mounted ConfigMap public key file
                              (default: /etc/keys/signing-public-key/public-key.pem)
    WORK_DIR                  Scratch directory (default: /tmp/confidential-model)

    # Layer 1 key delivery (used when KBS_RESOURCE_PATH is unset):
    DECRYPTION_KEY_PATH       Path to the mounted secret key file
                              (default: /etc/secrets/decryption-key/key)

    # Layer 3 key delivery (attested release via KBS/CDH):
    KBS_RESOURCE_PATH         KBS resource path the producer pushed the key
                              to, e.g. default/key/my-model. Setting this
                              switches key delivery to the CDH fetch below
                              and DECRYPTION_KEY_PATH is ignored.
    CDH_RESOURCE_URL_BASE     Base URL of the in-guest CDH resource API
                              (default: http://127.0.0.1:8006/cdh/resource)
"""
from __future__ import annotations

import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import crypto_utils, signing_utils  # noqa: E402

ENCRYPTED_ARTIFACT_NAME = "model.tar.gz.enc"
SIGNATURE_NAME = ENCRYPTED_ARTIFACT_NAME + ".sig"
MANIFEST_NAME = "manifest.json"
DEFAULT_CDH_RESOURCE_URL_BASE = "http://127.0.0.1:8006/cdh/resource"


def load_key(key_path: str) -> bytes:
    """Layer 1: read the key from a Kubernetes Secret mounted as a file."""
    path = Path(key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"decryption key not found at {path}. Is the Kubernetes Secret "
            "mounted correctly? See k8s/consumer-pod.yaml."
        )
    return crypto_utils.key_from_b64(path.read_text())


def fetch_key_from_kbs(resource_path: str, cdh_base_url: str = DEFAULT_CDH_RESOURCE_URL_BASE) -> bytes:
    """Layer 3: fetch the key from Trustee KBS via the in-guest CDH API.

    The CDH sidecar (part of the Kata/CoCo guest, reachable only from
    inside the confidential VM at 127.0.0.1) proxies this request to KBS
    after driving the full attestation handshake with the attestation
    agent. If the guest's TEE evidence doesn't satisfy the KBS resource
    policy, CDH never returns the key and this call fails — the consumer
    never sees a key it hasn't been attested for, and the host has no
    part in releasing it.
    """
    url = f"{cdh_base_url.rstrip('/')}/{resource_path.lstrip('/')}"
    print(f"[consumer] fetching decryption key from CDH (attested KBS release): {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"failed to fetch key resource '{resource_path}' from CDH at {cdh_base_url}: {exc}. "
            "Is this pod running under the kata-qemu-coco-dev runtime class with "
            "agent.aa_kbc_params configured, and does the KBS resource policy allow release "
            "to this attestation? See k8s/consumer-pod-coco.yaml and kbs/resource-policy.rego."
        ) from exc
    return crypto_utils.key_from_b64(body.decode("utf-8"))


def load_trusted_public_key(public_key_path: str) -> signing_utils.Ed25519PublicKey:
    path = Path(public_key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"signing public key not found at {path}. Is the Kubernetes ConfigMap "
            "mounted correctly? See k8s/consumer-pod.yaml."
        )
    return signing_utils.load_public_key(str(path))


def download_encrypted_artifact(repo_id: str, repo_type: str, dest_dir: Path) -> tuple[Path, Path, Path]:
    from huggingface_hub import hf_hub_download

    print(f"[consumer] downloading encrypted artifact from '{repo_id}' ...")
    enc_path = hf_hub_download(repo_id=repo_id, repo_type=repo_type,
                                filename=ENCRYPTED_ARTIFACT_NAME, local_dir=dest_dir)
    sig_path = hf_hub_download(repo_id=repo_id, repo_type=repo_type,
                                filename=SIGNATURE_NAME, local_dir=dest_dir)
    manifest_path = hf_hub_download(repo_id=repo_id, repo_type=repo_type,
                                     filename=MANIFEST_NAME, local_dir=dest_dir)
    print(f"[consumer] downloaded {enc_path}, {sig_path} and {manifest_path}")
    return Path(enc_path), Path(sig_path), Path(manifest_path)


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


def verify_signature(public_key: signing_utils.Ed25519PublicKey, enc_path: Path, sig_path: Path) -> None:
    signature = sig_path.read_bytes()
    try:
        signing_utils.verify_file(public_key, str(enc_path), signature)
    except signing_utils.InvalidSignature as exc:
        raise signing_utils.InvalidSignature(
            f"signature verification FAILED for {enc_path} against the trusted public key "
            f"at the mounted ConfigMap. Aborting before decryption — the artifact may have "
            "been tampered with, or was signed by an untrusted key."
        ) from exc
    print("[consumer] Ed25519 signature verified against the trusted public key")


def decrypt_and_extract(key: bytes, enc_path: Path, aad: bytes, extract_dir: Path) -> Path:
    tar_path = enc_path.with_suffix("")  # strip .enc -> model.tar.gz
    print(f"[consumer] decrypting {enc_path} -> {tar_path}")
    try:
        crypto_utils.decrypt_file(key, str(enc_path), str(tar_path), aad=aad)
    except InvalidTag as exc:
        raise ValueError(
            "decryption FAILED: the key does not match this ciphertext "
            "(wrong key, or the model id doesn't match the AAD used at encrypt "
            "time). If the producer was re-run since this key was delivered, "
            "re-sync it — re-run scripts/create_k8s_secret.sh (Layer 1) and/or "
            "scripts/push_key_to_kbs.sh (Layer 3) with the new "
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
    import json

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

    public_key_path = os.environ.get("SIGNING_PUBLIC_KEY_PATH", "/etc/keys/signing-public-key/public-key.pem")
    work_dir = Path(os.environ.get("WORK_DIR", "/tmp/confidential-model"))
    work_dir.mkdir(parents=True, exist_ok=True)

    kbs_resource_path = os.environ.get("KBS_RESOURCE_PATH")
    if kbs_resource_path:
        cdh_base_url = os.environ.get("CDH_RESOURCE_URL_BASE", DEFAULT_CDH_RESOURCE_URL_BASE)
        key = fetch_key_from_kbs(kbs_resource_path, cdh_base_url)
        print("[consumer] obtained decryption key via attested KBS release (Layer 3)")
    else:
        key_path = os.environ.get("DECRYPTION_KEY_PATH", "/etc/secrets/decryption-key/key")
        key = load_key(key_path)
        print("[consumer] loaded decryption key from mounted Kubernetes Secret (Layer 1)")

    public_key = load_trusted_public_key(public_key_path)
    print("[consumer] loaded trusted signing public key from mounted Kubernetes ConfigMap")

    enc_path, sig_path, manifest_path = download_encrypted_artifact(repo_id, repo_type, work_dir)
    verify_manifest(enc_path, manifest_path)

    # Signature verification MUST happen before decryption is attempted.
    verify_signature(public_key, enc_path, sig_path)

    model_dir = decrypt_and_extract(key, enc_path, model_id.encode("utf-8"), work_dir / "extracted")
    load_and_sanity_check(model_dir)

    print("[consumer] done: model decrypted and loaded successfully")


if __name__ == "__main__":
    main()
