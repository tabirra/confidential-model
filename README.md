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
consumer/decrypt_and_load.py Downloads the ciphertext, decrypts with the mounted key, loads the model
consumer/Dockerfile          Container image for the consumer pod
k8s/secret.example.yaml      Documents the Secret shape (not a real secret)
k8s/consumer-pod.yaml        Pod that mounts the Secret and runs the consumer
scripts/create_k8s_secret.sh Creates the Secret from the key file the producer wrote
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
tests/test_crypto_utils.py   Local round-trip + tamper-detection test for the crypto helpers
```

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

## Usage

### 0. Install dependencies

```bash
pip install -r producer/requirements.txt   # for the producer, run locally
```

### 1. Producer: encrypt and publish

```bash
export HF_TOKEN=hf_...   # needs write access to the destination repo
python producer/encrypt_and_push.py \
  --model-id prajjwal1/bert-tiny \
  --push-repo-id <your-hf-username>/bert-tiny-encrypted
```

This downloads the model, archives + encrypts it, uploads
`model.tar.gz.enc` and `manifest.json` to the Hub repo, and writes the
base64 decryption key to `secrets/decryption-key.b64` (git-ignored). Pass
`--skip-push` to exercise the download/encrypt steps without needing a Hub
token (e.g. for local testing).

### 2. Store the key as a Kubernetes Secret

```bash
scripts/create_k8s_secret.sh secrets/decryption-key.b64 model-decryption-key default
```

(This just wraps `kubectl create secret generic ... --from-file=key=...`;
see the script for the raw command.)

### 3. Build the consumer image and deploy the pod

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
```

Edit `k8s/consumer-pod.yaml`: set `HF_REPO_ID` to the repo you pushed to in
step 1 and `HF_MODEL_ID` to the same `--model-id` you used (it must match —
it's the AES-GCM AAD). Then:

```bash
kubectl apply -f k8s/consumer-pod.yaml
kubectl logs -f pod/confidential-model-consumer
```

Expected tail of the logs:

```
[consumer] loaded decryption key from mounted Kubernetes Secret
[consumer] downloading encrypted artifact from '<repo>' ...
[consumer] ciphertext checksum verified against manifest
[consumer] decrypting ... 
[consumer] loading model from ... 
[consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
[consumer] done: model decrypted and loaded successfully
```

## Testing without a Hub token or a cluster

The crypto helpers (`common/crypto_utils.py`) can be exercised fully
offline:

```bash
python -m pytest tests/ -q
```

This round-trips a random payload through `encrypt_bytes`/`decrypt_bytes`
and confirms that a flipped ciphertext byte or wrong key raises
`InvalidTag` instead of silently returning corrupted data.
