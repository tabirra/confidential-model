# Confidential Model Delivery — Layer 1: Encrypted Distribution in Kubernetes

A basic confidential model delivery pipeline: a **producer** encrypts an
open model and publishes the ciphertext to the Hugging Face Hub; a
**consumer** running as a Kubernetes pod pulls the ciphertext, decrypts it
with a key mounted from a Kubernetes Secret, and loads the model.

```
 producer                          Hugging Face Hub                consumer (k8s pod)
┌───────────────┐   encrypt      ┌──────────────────┐   download   ┌───────────────────┐
│ download model│ ──────────────▶│ model.tar.gz.enc │ ────────────▶│ decrypt + load     │
│ (bert-tiny)    │                │ manifest.json    │               │ model = transformers│
└───────────────┘                └──────────────────┘               └─────────┬──────────┘
        │                                                                     │
        │ decryption key (base64, 32 bytes)                                  │
        ▼                                                                    │
  kubectl create secret ───────────────────────────▶  Kubernetes Secret ─────┘ (mounted volume)
```

## Layout

```
common/crypto_utils.py       AES-256-GCM encrypt/decrypt helpers shared by both sides
producer/encrypt_and_push.py Downloads the model, encrypts it, pushes to the Hub, saves the key
producer/Dockerfile          Container image for the producer (a one-shot job, not a k8s workload)
consumer/decrypt_and_load.py Downloads the ciphertext, decrypts with the mounted key, loads the model
consumer/Dockerfile          Container image for the consumer pod
k8s/secret.example.yaml      Documents the Secret shape (not a real secret)
k8s/consumer-pod.yaml        Pod template (${HF_REPO_ID}/${HF_MODEL_ID} placeholders) that mounts the Secret and runs the consumer
scripts/create_k8s_secret.sh Creates the Secret from the key file the producer wrote
scripts/deploy_consumer_pod.sh Renders k8s/consumer-pod.yaml with envsubst and applies it
scripts/build_producer_image.sh  Builds the producer image
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
tests/test_crypto_utils.py   Local round-trip + tamper-detection test for the crypto helpers
```

## Design decisions worth defending

Two things here are deliberate, not oversights — worth having the reasoning
ready if asked "where's X":

- **The producer never runs as a Kubernetes workload.** It's a local
  script or a one-shot Docker container that publishes to the Hub, not a
  Pod/Job in the cluster. This matches the task's own wording (only the
  consumer is described as "deploy a Kubernetes pod") and reflects what
  the producer actually is — a build-time/CI publishing step, not a
  runtime service. There's no `k8s/producer-*.yaml` because there's
  nothing to deploy.
- **No Kubernetes `Secret` with real data is committed.**
  `k8s/secret.example.yaml` documents the Secret's shape (key name,
  structure) but never contains actual key material — the real Secret is
  created at deploy time from the locally-generated key via
  `scripts/create_k8s_secret.sh` (a thin wrapper over `kubectl create
  secret generic ... --from-file=`). Since this repo is public, committing
  the real Secret would mean publishing the decryption key to the world;
  the manifest documents the *shape* of the resource used, the value is
  never written to git.

## Prerequisites

- **Producer** (runs locally or as a one-shot container, not in the
  cluster): Python 3.11+ and `pip install -r producer/requirements.txt`,
  or Docker to build/run `producer/Dockerfile` instead. A Hugging Face
  account, plus:
  - **`HF_TOKEN`**: create one at https://huggingface.co/settings/tokens
    with the **"Write"** role (or, for a fine-grained token, at least
    "Create repos" and "Write access to contents" on the destination
    repo). A read-only token fails with `403 Forbidden` the moment the
    producer tries to create/push to the repo.
  - **`HF_USERNAME`**: your account name, with the **exact casing**
    Hugging Face has on file for it. A mismatch (e.g. exporting
    `Alice` when the account is actually `alice`) also fails with
    `403 Forbidden` on repo creation, since the Hub matches the
    destination namespace against the authenticated user exactly. If
    unsure, check `huggingface_hub.HfApi().whoami()["name"]` with your
    token loaded.
- **Consumer**: Docker to build `consumer/Dockerfile`, and a Kubernetes
  cluster (`kubectl` configured against it — any conformant cluster works
  for Layer 1: kind, minikube, EKS/GKE/AKS, etc.). If you're using a local
  kind/minikube cluster, `scripts/build_consumer_image.sh` will load the
  image into it automatically; otherwise push the image to a registry your
  cluster can pull from and update `image:` in `k8s/consumer-pod.yaml`.
- **Docker permissions**: whichever step uses Docker, your user needs to be
  able to talk to the daemon without `sudo` — add it to the `docker` group
  (`sudo usermod -aG docker $USER`, then log out/in or run `newgrp docker`
  for it to take effect) or run Docker rootless.
- Python 3.11+ locally if you want to run the offline test suite
  (`pip install pytest` in addition to the producer requirements).

## How it works

- **Model**: [`prajjwal1/bert-tiny`](https://huggingface.co/prajjwal1/bert-tiny)
  by default — a ~17MB BERT, small enough to move through this pipeline
  quickly. Any Hub model id works via `--model-id`.
- **Encryption**: AES-256-GCM (`cryptography` library). The producer
  generates a random 256-bit key, encrypts the model's tarball, and binds
  the model id as authenticated additional data (AAD) so a ciphertext
  can't silently be paired with the wrong model id at decrypt time.
  Wire format: `nonce (12B) || ciphertext || tag (16B)`.
- **Distribution**: the encrypted blob and a small public `manifest.json`
  (model id, algorithm, SHA-256 of both plaintext and ciphertext, timestamp
  — no secrets) are pushed to a Hub repo. The decryption key is **never**
  uploaded anywhere.
- **Key delivery**: the key only ever touches local disk (under the
  git-ignored `secrets/` directory) and the Kubernetes Secret store. The
  consumer pod mounts the Secret as a read-only file (`defaultMode: 0440`)
  rather than as an env var, so it doesn't leak into process listings or
  crash logs as easily.
- **Consumer**: verifies the downloaded ciphertext's checksum against the
  manifest, decrypts, extracts, and loads the model with
  `transformers.AutoModel`/`AutoTokenizer`, then runs one forward pass as a
  sanity check.

### Threat model (Layer 1)

This layer protects the model **at rest on the Hub and in transit** — the
Hub only ever sees ciphertext. It does *not* protect the model from
whoever can read the Kubernetes Secret or attach to the consumer pod
(no attestation, no confidential-computing enclave, no KMS/HSM — that's
out of scope for this layer). Treat the decryption key with the same care
as any other cluster secret: RBAC-restrict who can `get`/`describe`
Secrets in the namespace, and consider a proper secrets manager (Vault,
cloud KMS-backed Secrets, sealed-secrets, etc.) in place of plain
`kubectl create secret` for anything beyond a demo.

## Build and deploy the full pipeline

### 1. Producer: encrypt and publish

Either run it directly:

```bash
pip install -r producer/requirements.txt
export HF_TOKEN=hf_...       # needs write access to the destination repo
export HF_USERNAME=<your-hf-username>   # default owner of the destination repo
python producer/encrypt_and_push.py --model-id prajjwal1/bert-tiny
```

`--push-repo-id` can still be passed explicitly to override the destination
repo; when omitted, it defaults to `$HF_USERNAME/<model-basename>-encrypted`.

or build and run it as a container:

```bash
scripts/build_producer_image.sh confidential-model-producer:latest
docker run --rm -e HF_TOKEN=hf_... -e HF_USERNAME=<your-hf-username> \
  -v "$(pwd)/secrets:/app/secrets" -v "$(pwd)/keys:/app/keys" \
  confidential-model-producer:latest
```

Instead of passing `-e HF_TOKEN=hf_... -e HF_USERNAME=...` inline, you can
keep both in a git-ignored `.env` file at the repo root
(`HF_TOKEN=hf_...` and `HF_USERNAME=<your-hf-username>`) and pass
`--env-file .env` to `docker run`. Never bake the token into the Dockerfile
with `ENV` or commit it — it would end up baked into every image layer and
into git history.

Either way, this downloads the model, archives + encrypts it, uploads
`model.tar.gz.enc` and `manifest.json` to the Hub repo, and writes the
base64 decryption key to `secrets/decryption-key.b64` (git-ignored; with
the container form, the volume mount is what gets it back onto the host).
Pass `--skip-push` to exercise the download/encrypt steps without needing
a Hub token (e.g. for local testing).

**The Hub repo is not static — re-running the producer overwrites it.**
Every run generates a **fresh** AES-256-GCM decryption key and
re-encrypts the model with it, then overwrites `model.tar.gz.enc` and
`manifest.json` at the same Hub path. If you re-run the producer, you
must re-run `scripts/create_k8s_secret.sh` with the new
`secrets/decryption-key.b64` **before** redeploying the consumer pod, or
it will fail to decrypt (the old key against the new ciphertext raises
`InvalidTag`).

### 2. Store the key as a Kubernetes Secret

```bash
scripts/create_k8s_secret.sh secrets/decryption-key.b64 model-decryption-key default
```

(This just wraps `kubectl create secret generic ... --from-file=key=...`;
see the script for the raw command, or apply `k8s/secret.example.yaml`
after filling in a real key if you'd rather manage it declaratively.)

### 3. Build the consumer image and deploy the pod

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
```

`k8s/consumer-pod.yaml` is a template (`${HF_REPO_ID}`/`${HF_MODEL_ID}`
placeholders) — don't `kubectl apply` it directly. Render and deploy it with:

```bash
HF_USERNAME=<your-hf-username> scripts/deploy_consumer_pod.sh
kubectl logs -f pod/confidential-model-consumer
```

`HF_MODEL_ID` defaults to `prajjwal1/bert-tiny`; override it (and it must
match the `--model-id` used in step 1, since it's the AES-GCM AAD) with
`HF_MODEL_ID=<model-id> HF_USERNAME=... scripts/deploy_consumer_pod.sh`, or
set `HF_REPO_ID=<user>/<repo>` directly instead of `HF_USERNAME` if the repo
name doesn't follow the producer's default `<model-basename>-encrypted`
convention.

## Verifying Layer 1 works

1. **Offline, no Hub token or cluster needed** — exercise the AES-GCM
   helpers directly:

   ```bash
   python -m pytest tests/ -q
   ```

   `tests/test_crypto_utils.py` round-trips a random payload through
   `encrypt_bytes`/`decrypt_bytes` and confirms that a flipped ciphertext
   byte or a wrong key raises `InvalidTag` instead of silently returning
   corrupted data — i.e. tampering and wrong-key use are both detected.

2. **End to end** — after `kubectl apply -f k8s/consumer-pod.yaml`, tail
   the logs:

   ```bash
   kubectl logs -f pod/confidential-model-consumer
   ```

   A working Layer 1 deployment prints, in order:

   ```
   [consumer] loaded decryption key from mounted Kubernetes Secret
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   and the pod ends in `Completed` (`kubectl get pod
   confidential-model-consumer`), confirming the key never left the
   cluster/Secret store and the model only appeared in plaintext inside
   the pod after a checksum-verified decrypt.

3. **Negative test (the Secret actually matters)** — delete the Secret
   (`kubectl delete secret model-decryption-key`) and re-run the pod. In
   practice kubelet refuses to even start the container (`kubectl describe
   pod` shows `FailedMount: secret "model-decryption-key" not found`) since
   the volume can't be mounted; if the Secret existed but the key file
   inside it didn't, you'd instead see the container start and fail fast
   with the `FileNotFoundError` from `load_key`. Either way, the consumer
   never silently proceeds without the key.
