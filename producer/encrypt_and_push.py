#!/usr/bin/env python3
"""Producer side of the confidential model delivery pipeline.

1. Download a small open model from the Hugging Face Hub.
2. Archive it and encrypt the archive with AES-256-GCM.
3. Sign the encrypted artifact with an Ed25519 private key (Layer 2).
4. Push the encrypted artifact + signature (+ a public, non-secret
   manifest) to a Hugging Face Hub repo.
5. Print the commands needed to store the decryption key as a Kubernetes
   Secret and the signing public key as a Kubernetes ConfigMap, for the
   consumer workload to mount.

The decryption key and the signing private key are never uploaded
anywhere; they only ever touch local disk (git-ignored) and the
Kubernetes Secret store. The signing public key is not secret — it's
published locally for distribution via a ConfigMap (see
k8s/configmap.example.yaml) through a channel independent of the Hub.

Usage:
    export HF_TOKEN=hf_...                      # needs write access to the destination repo
    export HF_USERNAME=<your-hf-username>       # default owner of the destination repo
    python producer/encrypt_and_push.py --model-id prajjwal1/bert-tiny

If --push-repo-id is omitted, it defaults to "$HF_USERNAME/<model-basename>-encrypted".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import crypto_utils, signing_utils  # noqa: E402

ENCRYPTED_ARTIFACT_NAME = "model.tar.gz.enc"
SIGNATURE_NAME = ENCRYPTED_ARTIFACT_NAME + ".sig"
MANIFEST_NAME = "manifest.json"
ALGORITHM = "AES-256-GCM"
SIGNING_ALGORITHM = "Ed25519"


def download_model(model_id: str, dest_dir: Path) -> Path:
    from huggingface_hub import snapshot_download

    print(f"[producer] downloading '{model_id}' from the Hugging Face Hub ...")
    local_dir = snapshot_download(repo_id=model_id, local_dir=dest_dir / "model")
    print(f"[producer] downloaded to {local_dir}")
    return Path(local_dir)


def archive_model(model_dir: Path, out_tar: Path) -> None:
    print(f"[producer] archiving {model_dir} -> {out_tar}")
    with tarfile.open(out_tar, "w:gz") as tar:
        tar.add(model_dir, arcname=model_dir.name)


def get_or_create_signing_key(private_key_path: Path, public_key_path: Path) -> signing_utils.Ed25519PrivateKey:
    if private_key_path.exists():
        print(f"[producer] reusing existing signing key at {private_key_path}")
        signing_key = signing_utils.load_private_key(str(private_key_path))
    else:
        print(f"[producer] generating new Ed25519 signing key -> {private_key_path}")
        signing_key = signing_utils.generate_signing_key()
        private_key_path.parent.mkdir(parents=True, exist_ok=True)
        signing_utils.save_private_key(signing_key, str(private_key_path))
        os.chmod(private_key_path, 0o600)

    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    signing_utils.save_public_key(signing_key.public_key(), str(public_key_path))
    return signing_key


def push_to_hub(repo_id: str, repo_type: str, files: dict[str, Path]) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    print(f"[producer] ensuring repo '{repo_id}' (type={repo_type}) exists ...")
    api.create_repo(repo_id=repo_id, repo_type=repo_type, exist_ok=True)
    for path_in_repo, local_path in files.items():
        print(f"[producer] uploading {local_path} -> {repo_id}/{path_in_repo}")
        api.upload_file(
            path_or_fileobj=str(local_path),
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type=repo_type,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default="prajjwal1/bert-tiny",
                         help="Source model on the Hugging Face Hub (default: prajjwal1/bert-tiny)")
    parser.add_argument("--push-repo-id", default=None,
                         help="Destination Hub repo for the encrypted artifact, e.g. myuser/bert-tiny-encrypted "
                              "(default: $HF_USERNAME/<model-basename>-encrypted)")
    parser.add_argument("--repo-type", default="model", choices=["model", "dataset"],
                         help="Hub repo type to push the encrypted artifact to (default: model)")
    parser.add_argument("--work-dir", default=None, help="Scratch directory (default: temp dir)")
    parser.add_argument("--key-out", default="secrets/decryption-key.b64",
                         help="Local path to save the base64 decryption key (git-ignored)")
    parser.add_argument("--signing-key-out", default="secrets/signing-key.pem",
                         help="Local path to save/reuse the Ed25519 private signing key (git-ignored)")
    parser.add_argument("--signing-public-key-out", default="keys/signing-public-key.pem",
                         help="Local path to save the Ed25519 public key, for ConfigMap distribution")
    parser.add_argument("--k8s-secret-name", default="model-decryption-key")
    parser.add_argument("--k8s-configmap-name", default="model-signing-public-key")
    parser.add_argument("--k8s-namespace", default="default")
    parser.add_argument("--skip-push", action="store_true",
                         help="Do everything except the Hub upload (useful for local testing)")
    args = parser.parse_args()

    if args.push_repo_id is None:
        hf_username = os.environ.get("HF_USERNAME")
        if not args.skip_push and not hf_username:
            parser.error("--push-repo-id not given and HF_USERNAME is not set")
        if hf_username:
            model_basename = args.model_id.rstrip("/").rsplit("/", 1)[-1]
            args.push_repo_id = f"{hf_username}/{model_basename}-encrypted"

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="confidential-model-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    model_dir = download_model(args.model_id, work_dir)

    tar_path = work_dir / "model.tar.gz"
    archive_model(model_dir, tar_path)

    key = crypto_utils.generate_key()
    enc_path = work_dir / ENCRYPTED_ARTIFACT_NAME
    aad = args.model_id.encode("utf-8")
    crypto_utils.encrypt_file(key, str(tar_path), str(enc_path), aad=aad)
    print(f"[producer] encrypted artifact written to {enc_path}")

    signing_key_path = Path(args.signing_key_out)
    public_key_path = Path(args.signing_public_key_out)
    signing_key = get_or_create_signing_key(signing_key_path, public_key_path)
    signature = signing_utils.sign_file(signing_key, str(enc_path))
    sig_path = work_dir / SIGNATURE_NAME
    sig_path.write_bytes(signature)
    print(f"[producer] signed {enc_path} -> {sig_path}")

    manifest = {
        "model_id": args.model_id,
        "algorithm": ALGORITHM,
        "artifact_filename": ENCRYPTED_ARTIFACT_NAME,
        "plaintext_archive_sha256": crypto_utils.sha256_hex(str(tar_path)),
        "ciphertext_sha256": crypto_utils.sha256_hex(str(enc_path)),
        "signature_filename": SIGNATURE_NAME,
        "signing_algorithm": SIGNING_ALGORITHM,
        "signing_public_key_sha256": crypto_utils.sha256_hex(str(public_key_path)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = work_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[producer] manifest written to {manifest_path}")

    key_out = Path(args.key_out)
    key_out.parent.mkdir(parents=True, exist_ok=True)
    key_out.write_text(crypto_utils.key_to_b64(key) + "\n")
    os.chmod(key_out, 0o600)
    print(f"[producer] decryption key saved locally to {key_out} (keep this file secret; it is git-ignored)")

    if args.skip_push:
        print("[producer] --skip-push set, not uploading to the Hugging Face Hub")
    else:
        push_to_hub(
            args.push_repo_id,
            args.repo_type,
            {
                ENCRYPTED_ARTIFACT_NAME: enc_path,
                SIGNATURE_NAME: sig_path,
                MANIFEST_NAME: manifest_path,
            },
        )
        print(f"[producer] pushed encrypted artifact + signature + manifest to "
              f"https://huggingface.co/{'datasets/' if args.repo_type == 'dataset' else ''}{args.push_repo_id}")

    print()
    print("[producer] next steps:")
    print("  1. Deliver the decryption key to the consumer, either:")
    print("       Option A (Layer 1): store it as a Kubernetes Secret:")
    print(
        f"         kubectl create secret generic {args.k8s_secret_name} \\\n"
        f"           --namespace {args.k8s_namespace} \\\n"
        f"           --from-file=key={key_out}"
    )
    print("         (or run scripts/create_k8s_secret.sh, which wraps this)")
    print("       Option B (Layer 3): push it into Trustee KBS for attested release instead:")
    print(f"         scripts/push_key_to_kbs.sh {key_out} default/key/my-model")
    print("         (requires a KBS already deployed via scripts/deploy_coco_kbs.sh; "
          "see k8s/consumer-pod-coco.yaml for the matching consumer config)")
    print("  2. Distribute the signing public key as a Kubernetes ConfigMap")
    print("     (NOT via the Hub repo above, so a compromised Hub repo alone can't forge a trusted signer):")
    print(
        f"       kubectl create configmap {args.k8s_configmap_name} \\\n"
        f"         --namespace {args.k8s_namespace} \\\n"
        f"         --from-file=public-key.pem={public_key_path}"
    )
    print("       (or run scripts/create_k8s_configmap.sh, which wraps this)")


if __name__ == "__main__":
    main()
