# Confidential Model Delivery in Kubernetes

A confidential model delivery pipeline: a **producer** encrypts an open
model and publishes the ciphertext to the Hugging Face Hub; a **consumer**
running as a Kubernetes pod pulls the ciphertext, obtains the decryption
key, and loads the model.

- **Layer 1 (required)**: encrypted distribution — AES-256-GCM, key
  delivered via a Kubernetes Secret.
- **Layer 3 (optional, this branch)**: attested key release — the
  Kubernetes Secret is replaced with a Kata/Confidential-Containers (CoCo)
  guest fetching the key from Trustee KBS through the in-guest Confidential
  Data Hub (CDH), only after the guest has passed remote attestation. The
  consumer no longer trusts the host (or kubelet) for key delivery.

```
 producer                          Hugging Face Hub                consumer (k8s pod)
┌───────────────┐   encrypt      ┌──────────────────┐   download   ┌───────────────────┐
│ download model│ ──────────────▶│ model.tar.gz.enc │ ────────────▶│ decrypt + load     │
│ (bert-tiny)    │                │ manifest.json    │               │ model w/ transformers│
└───────────────┘                └──────────────────┘               └─────────┬─────────┘
        │                                                                     │
        │ Layer 1: decryption key (base64, 32 bytes)                         │
        ├────────────────────────▶ kubectl create secret ──▶ Kubernetes Secret ─┤ (mounted volume)
        │                                                                     │
        │ Layer 3: same key, pushed as a KBS resource                        │
        └───▶ kbs-client set-resource ──▶ Trustee KBS ◀── attested fetch ─────┘
                                              ▲                (guest-local CDH
                                              │                 127.0.0.1:8006,
                                       resource policy           only after the
                                      (kbs/resource-policy.rego)  Kata/CoCo guest
                                                                   attests)
```

## Layout

```
common/crypto_utils.py           AES-256-GCM encrypt/decrypt helpers shared by both sides
producer/encrypt_and_push.py     Downloads the model, encrypts it, pushes to the Hub, saves the key
producer/Dockerfile              Container image for the producer (a one-shot job, not a k8s workload)
consumer/decrypt_and_load.py     Downloads the ciphertext; gets the key via Secret (L1) or attested
                                  KBS/CDH fetch (L3); decrypts and loads the model
consumer/Dockerfile              Container image for the consumer pod
k8s/secret.example.yaml          Documents the Secret shape (Layer 1, not a real secret)
k8s/consumer-pod.yaml            Pod that mounts the Secret and runs the consumer (Layer 1)
k8s/consumer-pod-coco.yaml       Pod using runtimeClassName kata-qemu-coco-dev + attested
                                  KBS/CDH key fetch instead of a Secret (Layer 3)
kbs/resource-policy.rego         Permissive KBS resource policy for sample TEE attestation
scripts/create_k8s_secret.sh     Creates the Secret from the key file the producer wrote (L1)
scripts/deploy_coco_kbs.sh       Installs the CoCo operator + Trustee KBS, sets up the
                                  kata-qemu-coco-dev runtime class (L3)
scripts/set_kbs_resource_policy.sh  Uploads kbs/resource-policy.rego to KBS (L3)
scripts/push_key_to_kbs.sh       Pushes the decryption key into KBS via kbs-client set-resource (L3)
scripts/build_producer_image.sh  Builds the producer image
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
tests/test_crypto_utils.py       Local round-trip + tamper-detection test for the crypto helpers
tests/test_kbs_fetch.py          Local test of the CDH fetch path against a mock HTTP server (L3)
```

## Prerequisites

- **Producer** (runs locally or as a one-shot container, not in the
  cluster): Python 3.11+ and `pip install -r producer/requirements.txt`,
  or Docker to build/run `producer/Dockerfile` instead. A Hugging Face
  account and a `HF_TOKEN` with write access to the destination repo
  (create one at https://huggingface.co/settings/tokens).
- **Consumer — Layer 1 only**: Docker to build `consumer/Dockerfile`, and
  any conformant Kubernetes cluster (`kubectl` configured against it: kind,
  minikube, EKS/GKE/AKS, etc.).
- **Docker permissions**: whichever step uses Docker, your user needs to be
  able to talk to the daemon without `sudo` — add it to the `docker` group
  (`sudo usermod -aG docker $USER`, then log out/in or run `newgrp docker`
  for it to take effect) or run Docker rootless.
- **Consumer — Layer 3**: additionally requires a cluster whose nodes
  support Kata Containers (bare-metal or nested-virtualization-capable
  nodes — Kata needs to launch real QEMU VMs, which most managed
  Kubernetes node pools and plain `kind`/`minikube` don't support out of
  the box), the [`kbs-client`](https://github.com/confidential-containers/trustee)
  CLI built locally to push the key and set the resource policy, and
  cluster-admin access to install the CoCo operator and Trustee KBS
  (`scripts/deploy_coco_kbs.sh`).
- If you're using a local kind/minikube cluster for Layer 1,
  `scripts/build_consumer_image.sh` will load the image into it
  automatically; otherwise (and always for Layer 3, since it needs a
  Kata-capable cluster) push the image to a registry your cluster can pull
  from and update `image:` in the relevant pod manifest.
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
  uploaded to the Hub.
- **Key delivery — Layer 1**: the key only ever touches local disk (under
  the git-ignored `secrets/` directory) and the Kubernetes Secret store.
  The consumer pod mounts the Secret as a read-only file
  (`defaultMode: 0440`) rather than as an env var, so it doesn't leak into
  process listings or crash logs as easily.
- **Key delivery — Layer 3**: instead of a Secret, the producer pushes the
  key into Trustee KBS as a resource (`scripts/push_key_to_kbs.sh`, via
  `kbs-client set-resource`) under a path like `default/key/my-model`. The
  consumer pod runs under the `kata-qemu-coco-dev` RuntimeClass so its
  containers execute inside a Kata/CoCo confidential VM instead of a plain
  container. That guest's attestation agent is configured via the pod
  annotation `io.katacontainers.config.agent.aa_kbc_params` to attest
  against KBS; once attestation succeeds, the guest-local Confidential
  Data Hub (CDH) — reachable only from inside the VM at
  `127.0.0.1:8006/cdh/resource/...` — proxies resource fetches to KBS. The
  consumer just does an HTTP GET to that local address
  (`fetch_key_from_kbs` in `consumer/decrypt_and_load.py`); if attestation
  never happened or failed the KBS resource policy, CDH never returns the
  key and the fetch fails loudly. The host and kubelet are never in the
  key-delivery path at all in this mode.
- **Consumer**: verifies the downloaded ciphertext's checksum against the
  manifest, decrypts, extracts, and loads the model with
  `transformers.AutoModel`/`AutoTokenizer`, then runs one forward pass as a
  sanity check. Setting `KBS_RESOURCE_PATH` switches it from the Layer 1
  Secret-file path to the Layer 3 attested KBS/CDH fetch; the rest of the
  flow (download, checksum, decrypt, load) is identical either way.

### Threat model (Layer 1)

This layer protects the model **at rest on the Hub and in transit** — the
Hub only ever sees ciphertext. It does *not* protect the model from
whoever can read the Kubernetes Secret or attach to the consumer pod
(no attestation, no confidential-computing enclave, no KMS/HSM). Treat the
decryption key with the same care as any other cluster secret:
RBAC-restrict who can `get`/`describe` Secrets in the namespace, and
consider a proper secrets manager (Vault, cloud KMS-backed Secrets,
sealed-secrets, etc.) in place of plain `kubectl create secret` for
anything beyond a demo.

### Threat model (Layer 3)

Layer 3 removes the host/kubelet from the key-delivery trust boundary:
the key sits in KBS, gated by a resource policy, and is only released to
a guest that has proven — via TEE evidence from inside a Kata/CoCo VM —
that it matches what the policy expects. A cluster operator with kubectl
access to the node can no longer just `kubectl get secret` the key; they'd
have to compromise the guest itself or the attestation chain.

Caveats specific to this implementation:
- **This is dev/sample-attester mode**, per the task scope ("Confidential
  Containers in development mode", "permissive resource policy ...
  allowing key release to sample TEE attestation"). `kbs/resource-policy.rego`
  allows release to any guest that produced *some* verified attestation
  claim — it does not pin specific measurements, SVNs, or a real hardware
  TEE. On real hardware (SEV-SNP, TDX, etc.) you'd tighten the policy to
  check those claims; the wiring (KBS, CDH, the runtime class, the
  annotation) stays the same.
- **KBS/kbs-client versions differ.** The exact `kbs-client` subcommands
  and the CoCo operator/Trustee manifest layout used by
  `scripts/deploy_coco_kbs.sh`, `scripts/push_key_to_kbs.sh`, and
  `scripts/set_kbs_resource_policy.sh` move between releases; the scripts
  are commented accordingly and point at the upstream repos to confirm
  against.
- Trustee KBS's own admin access (who can push resources / set policy) is
  itself a trust root — protect `KBS_AUTH_PRIVATE_KEY` at least as
  carefully as the Layer 1 decryption key.

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
  -v "$(pwd)/secrets:/app/secrets" \
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
a Hub token (e.g. for local testing). The script prints both delivery
options (Secret vs. KBS) at the end.

### 2a. Layer 1: store the key as a Kubernetes Secret

```bash
scripts/create_k8s_secret.sh secrets/decryption-key.b64 model-decryption-key default
```

Then build the image, edit `HF_REPO_ID`/`HF_MODEL_ID` in
`k8s/consumer-pod.yaml`, and:

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
kubectl apply -f k8s/consumer-pod.yaml
kubectl logs -f pod/confidential-model-consumer
```

### 2b. Layer 3: attested key release via Kata+CoCo/KBS instead

```bash
# One-time cluster setup: CoCo operator + Trustee KBS + kata-qemu-coco-dev
# runtime class. Requires a cluster whose nodes support Kata containers.
scripts/deploy_coco_kbs.sh

# Point these at the KBS the previous step deployed (see its printed
# "Next steps" for how to find them).
export KBS_URL=http://kbs.confidential-containers-system.svc.cluster.local:8080
export KBS_AUTH_PRIVATE_KEY=./kbs/kbs-admin.key

scripts/set_kbs_resource_policy.sh kbs/resource-policy.rego
scripts/push_key_to_kbs.sh secrets/decryption-key.b64 default/key/my-model
```

Edit `k8s/consumer-pod-coco.yaml`: set `HF_REPO_ID`/`HF_MODEL_ID` as above,
`KBS_RESOURCE_PATH` to match what you pushed to (`default/key/my-model`),
and the `io.katacontainers.config.agent.aa_kbc_params` annotation to your
`KBS_URL`. Then:

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
kubectl apply -f k8s/consumer-pod-coco.yaml
kubectl logs -f pod/confidential-model-consumer-coco
```

## Verifying each layer works

1. **Offline, no Hub token or cluster needed** — exercise the crypto
   helpers and the Layer 3 CDH fetch contract directly:

   ```bash
   python -m pytest tests/ -q
   ```

   - `test_crypto_utils.py` round-trips a random payload through
     `encrypt_bytes`/`decrypt_bytes` and confirms a flipped ciphertext
     byte or wrong key raises `InvalidTag` instead of silently returning
     corrupted data.
   - `test_kbs_fetch.py` spins up a local `http.server` shaped like the
     CDH resource API and verifies `fetch_key_from_kbs` round-trips a key
     correctly and raises clearly on a missing resource or an unreachable
     CDH endpoint — useful when a real Kata/CoCo/KBS stack isn't at hand,
     since it still exercises the exact HTTP contract the consumer relies
     on.

2. **Layer 1, end to end** — after `kubectl apply -f k8s/consumer-pod.yaml`,
   tail the logs (`kubectl logs -f pod/confidential-model-consumer`); a
   working deployment prints, in order:

   ```
   [consumer] loaded decryption key from mounted Kubernetes Secret (Layer 1)
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   and the pod ends in `Completed`. Deleting the Secret
   (`kubectl delete secret model-decryption-key`) and re-running the pod
   should fail fast with `FileNotFoundError` from `load_key`, confirming
   the consumer really depends on it.

3. **Layer 3, end to end** — after `kubectl apply -f k8s/consumer-pod-coco.yaml`,
   tail the logs (`kubectl logs -f pod/confidential-model-consumer-coco`);
   a working deployment prints:

   ```
   [consumer] fetching decryption key from CDH (attested KBS release): http://127.0.0.1:8006/cdh/resource/default/key/my-model
   [consumer] obtained decryption key via attested KBS release (Layer 3)
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   confirming the key came from the attested CDH fetch, not a mounted
   Secret (`k8s/consumer-pod-coco.yaml` mounts no decryption-key Secret at
   all — `kubectl describe pod confidential-model-consumer-coco` should
   show no such volume).

4. **Layer 3, negative case (attestation actually gates the key)** —
   remove the guest's ability to reach KBS/CDH and confirm the fetch fails
   rather than the consumer falling back to some other source. The
   simplest version: apply `k8s/consumer-pod-coco.yaml` *without*
   `runtimeClassName: kata-qemu-coco-dev` (i.e. as a plain container) —
   there's no CDH sidecar at `127.0.0.1:8006` in a non-Kata pod, so
   `fetch_key_from_kbs` should raise `RuntimeError: failed to fetch key
   resource ...` and the pod should end in `Error`/`CrashLoopBackOff`
   instead of silently succeeding. (On a real Kata/CoCo cluster, an
   equivalent test is tightening `kbs/resource-policy.rego` to `default
   allow = false` with no matching rule, re-uploading it with
   `scripts/set_kbs_resource_policy.sh`, and confirming the same pod that
   worked before now fails the CDH fetch.)
