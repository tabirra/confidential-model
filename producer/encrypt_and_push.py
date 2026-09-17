#!/usr/bin/env python3
"""Producer side of the confidential model delivery pipeline.

1. Download a small open model from the Hugging Face Hub.
2. Archive it and encrypt the archive with AES-256-GCM.
3. Push the encrypted artifact (+ a public, non-secret manifest) to a
   Hugging Face Hub repo.
4. Print the commands needed to store the decryption key as a Kubernetes
   Secret for the consumer workload to mount.

The decryption key itself is never uploaded anywhere; it only ever touches
local disk (git-ignored) and the Kubernetes Secret store.

Usage:
    export HF_TOKEN=hf_...                     # needs write access to --push-repo-id
    python producer/encrypt_and_push.py \
        --model-id prajjwal1/bert-tiny \
        --push-repo-id <your-hf-username>/bert-tiny-encrypted
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
from common import crypto_utils  # noqa: E402

ENCRYPTED_ARTIFACT_NAME = "model.tar.gz.enc"
MANIFEST_NAME = "manifest.json"
ALGORITHM = "AES-256-GCM"


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
    parser.add_argument("--push-repo-id", required=True,
                         help="Destination Hub repo for the encrypted artifact, e.g. myuser/bert-tiny-encrypted")
    parser.add_argument("--repo-type", default="model", choices=["model", "dataset"],
                         help="Hub repo type to push the encrypted artifact to (default: model)")
    parser.add_argument("--work-dir", default=None, help="Scratch directory (default: temp dir)")
    parser.add_argument("--key-out", default="secrets/decryption-key.b64",
                         help="Local path to save the base64 decryption key (git-ignored)")
    parser.add_argument("--k8s-secret-name", default="model-decryption-key")
    parser.add_argument("--k8s-namespace", default="default")
    parser.add_argument("--skip-push", action="store_true",
                         help="Do everything except the Hub upload (useful for local testing)")
    args = parser.parse_args()

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

    manifest = {
        "model_id": args.model_id,
        "algorithm": ALGORITHM,
        "artifact_filename": ENCRYPTED_ARTIFACT_NAME,
        "plaintext_archive_sha256": crypto_utils.sha256_hex(str(tar_path)),
        "ciphertext_sha256": crypto_utils.sha256_hex(str(enc_path)),
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
                MANIFEST_NAME: manifest_path,
            },
        )
        print(f"[producer] pushed encrypted artifact + manifest to "
              f"https://huggingface.co/{'datasets/' if args.repo_type == 'dataset' else ''}{args.push_repo_id}")

    print()
    print("[producer] next step: store the decryption key as a Kubernetes Secret, e.g.:")
    print(
        f"  kubectl create secret generic {args.k8s_secret_name} \\\n"
        f"    --namespace {args.k8s_namespace} \\\n"
        f"    --from-file=key={key_out}"
    )
    print("  (or run scripts/create_k8s_secret.sh, which wraps this)")


if __name__ == "__main__":
    main()
