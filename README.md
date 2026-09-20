# Confidential Model Delivery in Kubernetes

A confidential model delivery pipeline: a **producer** encrypts an open
model and publishes the ciphertext to the Hugging Face Hub; a **consumer**
running as a Kubernetes pod pulls the ciphertext, obtains the decryption
key, and loads the model.

- **Layer 1**: encrypted distribution — AES-256-GCM, key
  delivered via a Kubernetes Secret.
- **Layer 3**: attested key release — the
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
k8s/consumer-pod.yaml            Pod template (${HF_REPO_ID}/${HF_MODEL_ID} placeholders) that mounts the Secret and runs the consumer (Layer 1)
k8s/consumer-pod-coco.yaml       Pod using runtimeClassName kata-qemu-coco-dev + attested
                                  KBS/CDH key fetch instead of a Secret (Layer 3)
kbs/resource-policy.rego         Permissive KBS resource policy for sample TEE attestation
scripts/create_k8s_secret.sh     Creates the Secret from the key file the producer wrote (L1)
scripts/deploy_consumer_pod.sh   Renders k8s/consumer-pod.yaml with envsubst and applies it (L1)
scripts/deploy_coco_kbs.sh       Installs the CoCo operator + Trustee KBS, sets up the
                                  kata-qemu-coco-dev runtime class (L3)
scripts/generate_kbs_admin_token.sh  Signs a KBS admin bearer JWT with kbs/kbs-admin.key (L3)
scripts/set_kbs_resource_policy.sh  Uploads kbs/resource-policy.rego to KBS (L3)
scripts/push_key_to_kbs.sh       Pushes the decryption key into KBS via kbs-client set-resource (L3)
scripts/build_producer_image.sh  Builds the producer image
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
tests/test_crypto_utils.py       Local round-trip + tamper-detection test for the crypto helpers
tests/test_kbs_fetch.py          Local test of the CDH fetch path against a mock HTTP server (L3)
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
  never written to git. Same reasoning for the Layer 3 KBS admin key
  (`kbs/kbs-admin.key`) — `scripts/deploy_coco_kbs.sh` generates it
  locally rather than committing anything.

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
- **Consumer — Layer 1 only**: Docker to build `consumer/Dockerfile`, and
  any conformant Kubernetes cluster (`kubectl` configured against it: kind,
  minikube, EKS/GKE/AKS, etc.).
- **Docker permissions**: whichever step uses Docker, your user needs to be
  able to talk to the daemon without `sudo` — add it to the `docker` group
  (`sudo usermod -aG docker $USER`, then log out/in or run `newgrp docker`
  for it to take effect) or run Docker rootless.
- **Consumer — Layer 3**: additionally requires cluster-admin access to
  install the CoCo operator and Trustee KBS (`scripts/deploy_coco_kbs.sh`
  — this part works fine on a `kind`/minikube dev cluster with a
  `containerd` runtime; it's Kata's own microVMs that need real or nested
  KVM), and the `kbs-client` CLI built locally to push the key and set the
  resource policy:
  ```bash
  git clone https://github.com/confidential-containers/trustee.git
  cd trustee && cargo build --release -p kbs-client
  # binary at target/release/kbs-client
  ```
  Actually launching a `kata-qemu-coco-dev` pod needs a cluster whose
  nodes support Kata Containers (bare-metal or nested-virtualization
  -capable nodes — Kata needs to launch real QEMU VMs, which most managed
  Kubernetes node pools and plain `kind`/`minikube` don't support out of
  the box).
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
  annotation `io.katacontainers.config.hypervisor.kernel_params`
  (`agent.aa_kbc_params=cc_kbc::<KBS URL>` on the guest kernel command
  line) to attest against KBS; once attestation succeeds, the guest-local Confidential
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
  against. As of the `main` branch these scripts were last verified
  against: KBS auth is a `role: admin` JWT bearer token (`--admin-token-file`,
  see `scripts/generate_kbs_admin_token.sh`), not a raw private key file;
  the `CcRuntime` CRD nests runtime config under `spec.config` (not
  `spec.ccRuntimeConfig`) and requires a `pulltype` per runtime class; and
  Trustee's KBS kustomize base deploys into the `coco-tenant` namespace.
  Rego policies also need `import rego.v1` syntax (`allow if { ... }`,
  `default allow := false`) — `kbs/resource-policy.rego` already uses it.
- Trustee KBS's own admin access (who can push resources / set policy) is
  itself a trust root — protect `kbs/kbs-admin.key` (and any
  `KBS_ADMIN_TOKEN_FILE` signed with it) at least as carefully as the
  Layer 1 decryption key.

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

**The Hub repo is not static — re-running the producer overwrites it.**
Every run generates a **fresh** AES-256-GCM decryption key and
re-encrypts the model with it, then overwrites `model.tar.gz.enc` and
`manifest.json` at the same Hub path. If you re-run the producer, you
must re-sync whichever key-delivery mechanism you're using —
`scripts/create_k8s_secret.sh` (Layer 1) and/or `scripts/push_key_to_kbs.sh`
(Layer 3) — with the new `secrets/decryption-key.b64` **before**
redeploying the consumer pod, or it will fail to decrypt (the old key
against the new ciphertext raises `InvalidTag`).

### 2a. Layer 1: store the key as a Kubernetes Secret

```bash
scripts/create_k8s_secret.sh secrets/decryption-key.b64 model-decryption-key default
```

Then build the image and deploy. `k8s/consumer-pod.yaml` is a template
(`${HF_REPO_ID}`/`${HF_MODEL_ID}` placeholders) — don't `kubectl apply` it
directly, render it with `scripts/deploy_consumer_pod.sh`:

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
HF_USERNAME=<your-hf-username> scripts/deploy_consumer_pod.sh
kubectl logs -f pod/confidential-model-consumer
```

`HF_MODEL_ID` defaults to `prajjwal1/bert-tiny`; override it (and it must
match the `--model-id` used in step 1) with `HF_MODEL_ID=<model-id>
HF_USERNAME=... scripts/deploy_consumer_pod.sh`, or set
`HF_REPO_ID=<user>/<repo>` directly instead of `HF_USERNAME` if the repo
name doesn't follow the producer's default `<model-basename>-encrypted`
convention.

### 2b. Layer 3: attested key release via Kata+CoCo/KBS instead

```bash
# One-time cluster setup: cert-manager + CoCo operator + Trustee KBS +
# kata-qemu-coco-dev runtime class. Generates kbs/kbs-admin.key (the KBS
# admin signing key) if it doesn't already exist.
scripts/deploy_coco_kbs.sh

# Turn that admin key into a bearer token kbs-client can use (KBS
# authenticates admin requests via a JWT signed with kbs-admin.key, not
# the key file directly).
scripts/generate_kbs_admin_token.sh kbs/kbs-admin.key kbs/admin-token

# Point these at the KBS the previous step deployed (see its printed
# "Next steps" for how to find them — the namespace defaults to
# coco-tenant, not confidential-containers-system, as of Trustee's
# current kustomize base).
export KBS_URL=http://kbs.coco-tenant.svc.cluster.local:8080
export KBS_ADMIN_TOKEN_FILE=kbs/admin-token

scripts/set_kbs_resource_policy.sh kbs/resource-policy.rego
scripts/push_key_to_kbs.sh secrets/decryption-key.b64 default/key/my-model
```

`k8s/consumer-pod-coco.yaml` is a template, like `k8s/consumer-pod.yaml` —
render and apply it with `scripts/deploy_consumer_pod.sh coco` instead of
`kubectl apply -f` directly:

```bash
scripts/build_consumer_image.sh confidential-model-consumer:latest
HF_USERNAME=<your-hf-username> KBS_NAMESPACE=coco-tenant \
  scripts/deploy_consumer_pod.sh coco
kubectl logs -f pod/confidential-model-consumer-coco
```

`KBS_NAMESPACE` must match whatever `scripts/deploy_coco_kbs.sh` printed
for your cluster (its "Next steps" output) — it defaults to `coco-tenant`,
not the CoCo operator's own `confidential-containers-system` namespace.
If `KBS_RESOURCE_PATH` (in the manifest, default `default/key/my-model`)
or `HF_MODEL_ID` need to differ from their defaults, edit the manifest or
extend the script the same way as for Layer 1.

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

2. **Layer 1, end to end** — after `scripts/deploy_consumer_pod.sh`,
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
   (`kubectl delete secret model-decryption-key`) and re-running the pod:
   in practice kubelet refuses to even start the container (`kubectl
   describe pod` shows `FailedMount: secret "model-decryption-key" not
   found`) since the volume can't be mounted; if the Secret existed but
   the key file inside it didn't, you'd instead see the container start
   and fail fast with `FileNotFoundError` from `load_key`. Either way, the
   consumer never silently proceeds without the key.

3. **Layer 3, end to end** — after `scripts/deploy_consumer_pod.sh coco`,
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

5. **If the Kata guest never boots on a memory-constrained dev cluster.**
   `containerd-shim-kata-v2` may launch its QEMU sandbox successfully
   (confirmed via QMP: vCPU state `running`) but never manage to connect
   to the guest's vsock — `EHOSTUNREACH` at the host-kernel level,
   persisting for the full dial timeout. This turned out, in this
   project's own testing, to be plain guest-memory exhaustion rather than
   a fundamental limitation of nested virtualization or a specific guest
   kernel version: Kata's default hypervisor config asks for ~4GB of
   guest memory (`default_memory` in `configuration-qemu.toml`, plus a
   matching NUMA memory-backend-file), and a dev cluster node (e.g. a
   minikube profile running inside a container with a constrained cgroup)
   may not have that much headroom free after Kubernetes' own system
   components. The guest never gets far enough into its boot to bring up
   the vsock-listening agent, which host-side tooling reports as a dial
   failure rather than an obvious OOM. If you hit this, try lowering
   `default_memory` (e.g. to `512`) in the node's
   `configuration-qemu.toml` and retrying — the per-pod annotation
   `io.katacontainers.config.hypervisor.default_memory` is a cleaner fix
   *if* your build's `enable_annotations` allowlist includes
   `default_memory`; if not, the node-level config edit is the only way to
   test this without more cluster memory.

   Independently of whether the guest boots, you can validate the
   KBS/attestation half directly with `kbs-client` (built from
   `confidential-containers/trustee`, `tools/kbs-client`):
   ```bash
   kbs-client --url "$KBS_URL" get-resource --path default/key/my-model
   ```
   This drives the real RCAR attestation handshake (falling back to the
   "Sample Attester" when no TEE hardware is present) against the real
   deployed KBS and the real `kbs/resource-policy.rego`, and returns the
   real key pushed by `scripts/push_key_to_kbs.sh` — proving the
   attestation-gated release path end to end, independently of whether a
   Kata guest can boot in your environment.
