# Confidential Model Delivery in Kubernetes

A confidential model delivery pipeline: a **producer** encrypts and signs
an open model and publishes the artifacts to the Hugging Face Hub; a
**consumer** running as a Kubernetes pod pulls them, verifies the
signature, decrypts with a key mounted from a Kubernetes Secret, and loads
the model.

- **Layer 1 (required)**: encrypted distribution — AES-256-GCM, key via
  Kubernetes Secret.
- **Layer 2 (optional)**: model signing & verification — Ed25519, public
  key via Kubernetes ConfigMap, verified *before* decryption.

```
 producer                                Hugging Face Hub                     consumer (k8s pod)
┌────────────────┐  encrypt+sign      ┌──────────────────────┐  download   ┌─────────────────────────┐
│ download model  │ ──────────────────▶│ model.tar.gz.enc     │ ───────────▶│ verify signature (abort  │
│ (bert-tiny)     │                     │ model.tar.gz.enc.sig │             │  on failure) → decrypt   │
└────────────────┘                     │ manifest.json        │             │  → load w/ transformers  │
        │                              └──────────────────────┘             └──────────┬───────┬───────┘
        │ decryption key (b64, 32B)                                                    │       │
        ▼                                                                              │       │
  kubectl create secret ──────────────────────▶ Kubernetes Secret ─────────────────────┘       │
        │                                        (model-decryption-key)                          │
        │ signing public key (PEM)                                                                │
        ▼                                                                                         │
  kubectl create configmap ────────────────────▶ Kubernetes ConfigMap ───────────────────────────┘
                                                  (model-signing-public-key)
```

Note the public key reaches the consumer via the ConfigMap, **not** via the
Hub repo that carries the artifact + signature — see [Threat model
(Layer 2)](#threat-model-layer-2) for why that separation matters.

## Layout

```
common/crypto_utils.py         AES-256-GCM encrypt/decrypt helpers shared by both sides
common/signing_utils.py        Ed25519 sign/verify + PEM key helpers shared by both sides
producer/encrypt_and_push.py   Downloads the model, encrypts + signs it, pushes to the Hub, saves the keys
producer/Dockerfile            Container image for the producer (a one-shot job, not a k8s workload)
consumer/decrypt_and_load.py   Downloads the artifact, verifies the signature, decrypts, loads the model
consumer/Dockerfile            Container image for the consumer pod
k8s/secret.example.yaml        Documents the Secret shape (not a real secret)
k8s/configmap.example.yaml     Documents the ConfigMap shape for the signing public key
k8s/consumer-pod.yaml          Pod that mounts the Secret + ConfigMap and runs the consumer
scripts/create_k8s_secret.sh   Creates the Secret from the key file the producer wrote
scripts/create_k8s_configmap.sh  Creates the ConfigMap from the public key file the producer wrote
scripts/build_producer_image.sh  Builds the producer image
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
tests/test_crypto_utils.py     Round-trip + tamper-detection tests for the AES-GCM helpers
tests/test_signing_utils.py    Round-trip + tamper/wrong-key-detection tests for the signing helpers
```

## Prerequisites

- **Producer** (runs locally or as a one-shot container, not in the
  cluster): Python 3.11+ and `pip install -r producer/requirements.txt`,
  or Docker to build/run `producer/Dockerfile` instead. A Hugging Face
  account and a `HF_TOKEN` with write access to the destination repo
  (create one at https://huggingface.co/settings/tokens).
- **Consumer**: Docker to build `consumer/Dockerfile`, and a Kubernetes
  cluster (`kubectl` configured against it — any conformant cluster works
  for Layers 1–2: kind, minikube, EKS/GKE/AKS, etc.). If you're using a
  local kind/minikube cluster, `scripts/build_consumer_image.sh` will load
  the image into it automatically; otherwise push the image to a registry
  your cluster can pull from and update `image:` in
  `k8s/consumer-pod.yaml`.
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
- **Signing**: Ed25519 (`cryptography` library). The producer signs the
  *encrypted* artifact (the ciphertext bytes, not the plaintext model) with
  a private key it keeps local, and publishes the signature alongside the
  artifact. Re-running the producer against an existing `--signing-key-out`
  path reuses the same signing identity instead of generating a new one
  each time.
- **Distribution**: `model.tar.gz.enc`, `model.tar.gz.enc.sig`, and a small
  public `manifest.json` (model id, algorithm, SHA-256 of plaintext and
  ciphertext, signature filename, signing algorithm — no secrets) are
  pushed to a Hub repo. The decryption key and the signing private key are
  **never** uploaded anywhere.
- **Key delivery**: the decryption key and signing private key only ever
  touch local disk (under the git-ignored `secrets/` directory) and the
  Kubernetes Secret store. The consumer pod mounts the decryption-key
  Secret as a read-only file (`defaultMode: 0440`) rather than as an env
  var, so it doesn't leak into process listings or crash logs as easily.
- **Public key delivery**: the signing public key isn't secret, but it's
  still delivered to the consumer via a Kubernetes ConfigMap — a channel
  independent of the Hub repo — rather than being fetched from the Hub
  alongside the artifact. See the threat model below.
- **Consumer**: downloads the artifact + signature + manifest, checks the
  ciphertext's SHA-256 against the manifest, then **verifies the Ed25519
  signature against the trusted public key and aborts before any
  decryption is attempted if verification fails**. Only after that does it
  decrypt, extract, and load the model with
  `transformers.AutoModel`/`AutoTokenizer`, running one forward pass as a
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

### Threat model (Layer 2)

Signing adds **integrity and authenticity**: even if the Hub repo is
compromised or a network intermediary tampers with the artifact, the
consumer detects it and refuses to decrypt. This only holds if the
consumer's copy of the public key is trustworthy — if the public key were
fetched from the same Hub repo as the artifact and signature, an attacker
able to rewrite that repo could replace all three together (forge a new
keypair, sign with it, publish a new "public key") and verification would
pass against the forged key. That's why the public key is delivered via a
Kubernetes ConfigMap the operator populates directly from the producer's
local output, not via the Hub. It still doesn't provide confidentiality
against anyone who can read the cluster's Secrets/ConfigMaps or exec into
the pod, and there's no revocation mechanism — rotating the signing key
means updating the ConfigMap out of band.

## Build and deploy the full pipeline

### 1. Producer: encrypt, sign, and publish

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

Either way, this downloads the model, archives + encrypts it, signs the
encrypted artifact with an Ed25519 key, uploads `model.tar.gz.enc`,
`model.tar.gz.enc.sig`, and `manifest.json` to the Hub repo, and writes
locally (all git-ignored; with the container form, the volume mounts are
what get these back onto the host):

- `secrets/decryption-key.b64` — the AES-256-GCM decryption key
- `secrets/signing-key.pem` — the Ed25519 private signing key (reused on
  future runs if it already exists)
- `keys/signing-public-key.pem` — the Ed25519 public key, for the consumer

Pass `--skip-push` to exercise the download/encrypt/sign steps without
needing a Hub token (e.g. for local testing).

### 2. Store the decryption key as a Kubernetes Secret

```bash
scripts/create_k8s_secret.sh secrets/decryption-key.b64 model-decryption-key default
```

### 3. Store the signing public key as a Kubernetes ConfigMap

```bash
scripts/create_k8s_configmap.sh keys/signing-public-key.pem model-signing-public-key default
```

(Both scripts just wrap `kubectl create secret/configmap ... --from-file=...`;
see the scripts for the raw commands, or apply `k8s/secret.example.yaml` /
`k8s/configmap.example.yaml` after filling in real values if you'd rather
manage them declaratively.)

### 4. Build the consumer image and deploy the pod

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

## Verifying each layer works

1. **Offline, no Hub token or cluster needed** — exercise the crypto and
   signing helpers directly:

   ```bash
   python -m pytest tests/ -q
   ```

   `tests/test_crypto_utils.py` round-trips a random payload through
   AES-GCM and confirms a flipped ciphertext byte or wrong key raises
   `InvalidTag`. `tests/test_signing_utils.py` round-trips a payload
   through Ed25519 sign/verify and confirms a flipped byte, a wrong
   signing key, or a mismatched public key all raise `InvalidSignature`.

2. **Layer 1, end to end** — after `kubectl apply -f k8s/consumer-pod.yaml`,
   tail the logs (`kubectl logs -f pod/confidential-model-consumer`); a
   working deployment prints, in order:

   ```
   [consumer] loaded decryption key from mounted Kubernetes Secret
   [consumer] loaded trusted signing public key from mounted Kubernetes ConfigMap
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] Ed25519 signature verified against the trusted public key
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   and the pod ends in `Completed` (`kubectl get pod
   confidential-model-consumer`). Deleting the Secret
   (`kubectl delete secret model-decryption-key`) and re-running the pod
   should fail fast with `FileNotFoundError` from `load_key`, confirming
   the consumer really depends on it rather than silently proceeding.

3. **Layer 2, positive case** — the `Ed25519 signature verified` line
   above is the signature check succeeding; it only prints if
   `verify_signature` didn't raise.

4. **Layer 2, negative case (the check actually matters)** — after step 1
   of the pipeline, corrupt the published artifact and confirm the
   consumer refuses to decrypt it rather than silently accepting a
   tampered model:

   ```bash
   # download the artifact the producer just pushed, flip a byte, re-upload it
   python - <<'PY'
   from huggingface_hub import hf_hub_download, HfApi
   path = hf_hub_download(repo_id="<your-hf-username>/bert-tiny-encrypted", filename="model.tar.gz.enc")
   data = bytearray(open(path, "rb").read())
   data[-1] ^= 0xFF
   open(path, "wb").write(data)
   HfApi().upload_file(path_or_fileobj=path, path_in_repo="model.tar.gz.enc",
                        repo_id="<your-hf-username>/bert-tiny-encrypted")
   PY
   kubectl delete pod confidential-model-consumer --ignore-not-found
   kubectl apply -f k8s/consumer-pod.yaml
   kubectl logs -f pod/confidential-model-consumer
   ```

   The consumer should print `signature verification FAILED ... Aborting
   before decryption` and exit non-zero — it never reaches the decrypt
   step on a tampered artifact. Re-run the producer afterward to restore a
   validly signed artifact.
