# Confidential Model Delivery in Kubernetes

A confidential model delivery pipeline: a **producer** encrypts and signs
an open model and publishes the artifacts to the Hugging Face Hub; a
**consumer** running as a Kubernetes pod pulls them, verifies the
signature, obtains the decryption key, and loads the model.

- **Layer 1**: encrypted distribution — AES-256-GCM, key via
  Kubernetes Secret.
- **Layer 2**: model signing & verification — Ed25519, public
  key via Kubernetes ConfigMap, verified *before* decryption.
- **Layer 3**: attested key release — the
  Kubernetes Secret is replaced with a Kata/Confidential-Containers (CoCo)
  guest fetching the key from Trustee KBS through the in-guest Confidential
  Data Hub (CDH), only after the guest has passed remote attestation. The
  consumer no longer trusts the host for key delivery. Layers 2 and 3 are
  independent and compose: the consumer still verifies the signature
  before decrypting, whichever way it got the key.

```
 producer                                Hugging Face Hub                     consumer (k8s pod)
┌────────────────┐  encrypt+sign      ┌──────────────────────┐  download   ┌─────────────────────────┐
│ download model  │ ──────────────────▶│ model.tar.gz.enc     │ ───────────▶│ verify signature (abort  │
│ (bert-tiny)     │                     │ model.tar.gz.enc.sig │             │  on failure) → decrypt   │
└────────────────┘                     │ manifest.json        │             │  → load w/ transformers  │
        │                              └──────────────────────┘             └───┬─────┬─────┬─────────┘
        │ signing public key (PEM)                                              │     │     │
        ▼                                                                       │     │     │
  kubectl create configmap ──────────────▶ Kubernetes ConfigMap ────────────────┘     │     │
                                            (model-signing-public-key)                 │     │
        │ decryption key (b64, 32B)                                                   │     │
        │                                                                             │     │
        ├── Layer 1 ──▶ kubectl create secret ──▶ Kubernetes Secret ──────────────────┘     │
        │                                          (model-decryption-key, mounted)           │
        │                                                                                    │
        └── Layer 3 ──▶ kbs-client set-resource ──▶ Trustee KBS ◀── attested fetch ──────────┘
                                                          ▲             (guest-local CDH
                                                          │              127.0.0.1:8006,
                                                   resource policy         only after the
                                                  (kbs/resource-policy.rego)  Kata/CoCo guest
                                                                              attests)
```

## Layout

```
common/crypto_utils.py           AES-256-GCM encrypt/decrypt helpers shared by both sides
common/signing_utils.py          Ed25519 sign/verify + PEM key helpers shared by both sides
producer/encrypt_and_push.py     Downloads the model, encrypts + signs it, pushes to the Hub, saves the keys
producer/server.py               Optional: runs the producer as a k8s pod, triggered by POST /mermelada
producer/Dockerfile              Container image for the producer (one-shot by default; server.py is opt-in)
consumer/decrypt_and_load.py     Downloads the artifact; verifies the signature; gets the decryption
                                  key via Secret (L1) or attested KBS/CDH fetch (L3); decrypts and loads
consumer/Dockerfile              Container image for the consumer pod
k8s/secret.example.yaml          Documents the Secret shape (Layer 1, not a real secret)
k8s/configmap.example.yaml       Documents the ConfigMap shape for the signing public key (Layer 2)
k8s/consumer-pod.yaml            Pod template (${HF_REPO_ID}/${HF_MODEL_ID} placeholders) that mounts the Secret + ConfigMap and runs the consumer (L1+L2)
k8s/consumer-pod-coco.yaml       Pod using runtimeClassName kata-qemu-coco-dev + attested KBS/CDH key
                                  fetch instead of a Secret, still mounting the signing ConfigMap (L2+L3)
k8s/producer-pod.yaml            Optional: pod template running producer/server.py (${HF_USERNAME}/${HF_MODEL_ID})
k8s/producer-rbac.yaml           ServiceAccount/Role/RoleBinding producer-pod.yaml needs (manage the
                                  Secret/ConfigMap it delivers the key through + the consumer Pod it deploys)
k8s/producer-trigger-token.example.yaml  Documents the shape of the /mermelada bearer-token Secret
kbs/resource-policy.rego         Permissive KBS resource policy for sample TEE attestation (L3)
scripts/create_k8s_secret.sh     Creates the Secret from the key file the producer wrote (L1)
scripts/create_k8s_configmap.sh  Creates the ConfigMap from the public key file the producer wrote (L2)
scripts/deploy_consumer_pod.sh   Renders k8s/consumer-pod.yaml with envsubst and applies it (L1+L2); `coco [--publish]` for L3
scripts/deploy_coco_kbs.sh       Installs the CoCo operator + Trustee KBS, sets up the
                                  kata-qemu-coco-dev runtime class (L3)
scripts/generate_kbs_admin_token.sh  Signs a KBS admin bearer JWT with kbs/kbs-admin.key (L3)
scripts/set_kbs_resource_policy.sh  Uploads kbs/resource-policy.rego to KBS (L3)
scripts/push_key_to_kbs.sh       Pushes the decryption key into KBS via kbs-client set-resource (L3)
scripts/build_producer_image.sh  Builds the producer image
scripts/build_consumer_image.sh  Builds the consumer image (and loads it into kind/minikube if present)
scripts/publish_consumer_image.sh  Builds + pushes the consumer image to a public registry and verifies anonymous pull (L3)
scripts/lib/load_env.sh          Shared helper: reads whitelisted variables from .env without overriding the environment
scripts/generate_producer_trigger_token.sh  Generates the /mermelada bearer token locally (optional)
scripts/create_producer_trigger_secret.sh   Stores that token as a Kubernetes Secret (optional)
scripts/deploy_producer_pod.sh   Renders + applies k8s/producer-pod.yaml and k8s/producer-rbac.yaml (optional)
tests/test_crypto_utils.py       Round-trip + tamper-detection tests for the AES-GCM helpers
tests/test_signing_utils.py      Round-trip + tamper/wrong-key-detection tests for the signing helpers
tests/test_kbs_fetch.py          Local test of the CDH fetch path against a mock HTTP server (L3)
tests/test_producer_server.py    Local test of the /mermelada auth gate and routing (mocks the pipeline)
```

## Design decisions worth defending

Two things here are deliberate, not oversights — worth having the reasoning
ready if asked "where's X":

- **The producer never runs as a Kubernetes workload — by default.** It's
  a local script or a one-shot Docker container that publishes to the
  Hub, not a Pod/Job in the cluster. This matches the task's own wording
  (only the consumer is described as "deploy a Kubernetes pod") and
  reflects what the producer actually is — a build-time/CI publishing
  step, not a runtime service. This branch (`master` only) additionally
  offers an **explicit, opt-in exception**: `producer/server.py`, deployed
  via `k8s/producer-pod.yaml`, runs the producer as a long-lived pod that
  waits for an authenticated `POST /mermelada` instead of a human running
  the steps by hand — see "Optional: trigger the producer via a signal"
  below. It coexists with, and doesn't replace, the default flow above;
  nothing about the default one-shot path changes.
- **No Kubernetes `Secret` with real data is committed.**
  `k8s/secret.example.yaml` documents the Secret's shape (key name,
  structure) but never contains actual key material — the real Secret is
  created at deploy time from the locally-generated key via
  `scripts/create_k8s_secret.sh` (a thin wrapper over `kubectl create
  secret generic ... --from-file=`). Since this repo is public, committing
  the real Secret would mean publishing the decryption key to the world;
  the manifest documents the *shape* of the resource used, the value is
  never written to git. Same reasoning for `k8s/configmap.example.yaml`
  (Layer 2 signing public key — not secret, but same pattern) and the
  Layer 3 KBS admin key (`kbs/kbs-admin.key`, generated locally by
  `scripts/deploy_coco_kbs.sh` rather than committed).

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
- **Consumer — Layers 1 & 2**: Docker to build `consumer/Dockerfile`, and
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
- If you're using a local kind/minikube cluster for Layers 1–2,
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
  **never** uploaded to the Hub.
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
  key-delivery path at all in this mode. Setting `KBS_RESOURCE_PATH`
  switches the consumer from the Layer 1 Secret-file path to this fetch —
  everything else about the pipeline is unchanged.
- **Public key delivery**: the signing public key isn't secret, but it's
  still delivered to the consumer via a Kubernetes ConfigMap — a channel
  independent of the Hub repo — rather than being fetched from the Hub
  alongside the artifact. See the Layer 2 threat model below. This is
  orthogonal to Layer 3: `k8s/consumer-pod-coco.yaml` mounts the same
  ConfigMap even though the decryption key itself no longer comes from a
  Secret.
- **Consumer**: downloads the artifact + signature + manifest, checks the
  ciphertext's SHA-256 against the manifest, then **verifies the Ed25519
  signature against the trusted public key and aborts before any
  decryption is attempted if verification fails**. Only after that does it
  obtain the decryption key (Secret or attested KBS/CDH fetch), decrypt,
  extract, and load the model with
  `transformers.AutoModel`/`AutoTokenizer`, running one forward pass as a
  sanity check.

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

### Threat model (Layer 3)

Layer 3 removes the host/kubelet from the key-delivery trust boundary:
the key sits in KBS, gated by a resource policy, and is only released to
a guest that has proven — via TEE evidence from inside a Kata/CoCo VM —
that it matches what the policy expects. A cluster operator with kubectl
access to the node can no longer just `kubectl get secret` the key; they'd
have to compromise the guest itself or the attestation chain. Combined
with Layer 2, a tampered or unsigned artifact is caught by signature
verification even if it somehow got decrypted, and a non-attested consumer
never gets the key to decrypt anything in the first place — the two checks
are independent and don't substitute for each other.

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
- The signing private key still only ever touches local disk and is never
  attested over — Layer 3 attests the *consumer*, not the producer.

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
needing a Hub token (e.g. for local testing). The script prints both
decryption-key delivery options (Secret vs. KBS) at the end.

**The Hub repo is not static — re-running the producer overwrites it.**
Every run generates a **fresh** AES-256-GCM decryption key (unlike the
signing key, which is reused if `secrets/signing-key.pem` already exists)
and re-encrypts the model with it, then overwrites `model.tar.gz.enc`,
`model.tar.gz.enc.sig`, and `manifest.json` at the same Hub path. If you
re-run the producer, you must re-sync whichever key-delivery mechanism
you're using — `scripts/create_k8s_secret.sh` (Layer 1) and/or
`scripts/push_key_to_kbs.sh` (Layer 3) — with the new
`secrets/decryption-key.b64` **before** redeploying the consumer pod, or
it will fail to decrypt (the old key against the new ciphertext raises
`InvalidTag`).

### 1b. Optional: trigger the producer via a signal instead

An alternative to running the producer by hand (step 1): deploy it as a
pod that stays up and runs the full pipeline — encrypt, sign, push,
deliver the key + public key, and deploy the consumer pod — whenever it
receives an authenticated `POST /mermelada`. This is an **opt-in
exception** to this project's default "producer isn't a k8s workload"
design (see "Design decisions worth defending" above) and only covers the
Secret+ConfigMap (Layer 1+2) consumer path — it does not attempt to
deploy or drive the Kata/CoCo (Layer 3) one, which needs manual,
environment-specific cluster setup.

```bash
# One-time: a random bearer token, stored as a Kubernetes Secret. The
# request must present it via `Authorization: Bearer <token>` — anyone
# without it gets 401.
scripts/generate_producer_trigger_token.sh
scripts/create_producer_trigger_secret.sh

# Build the producer image (same image as step 1; producer/server.py is
# just an alternate entrypoint it also ships) and deploy the pod. Applies
# k8s/producer-rbac.yaml too — the ServiceAccount this pod runs as, scoped
# to only what it needs (manage the Secret/ConfigMap it delivers the key
# through, and the consumer Pod it deploys).
scripts/build_producer_image.sh confidential-model-producer:latest
# (load it into your kind/minikube cluster the same way as the consumer
# image, if applicable — see scripts/build_consumer_image.sh)
HF_USERNAME=<your-hf-username> scripts/deploy_producer_pod.sh
```

Trigger it:

```bash
kubectl port-forward pod/confidential-model-producer-trigger 8080:8080 &
curl -X POST --max-time 300 \
  -H "Authorization: Bearer $(cat secrets/producer-trigger-token.b64)" \
  http://127.0.0.1:8080/mermelada
```

The request stays open until the whole pipeline finishes (roughly a
minute or two, mostly the Hub download/upload) and returns
`{"status": "done"}`. `GET /healthz` is available for a readiness probe
(already wired into `k8s/producer-pod.yaml`). Re-triggering re-runs
everything from scratch — a fresh key, a fresh Hub push, and the consumer
pod redeployed (deleted and recreated, since Pod specs are largely
immutable) against the new artifact.

### 2. Distribute the signing public key (Layer 2, needed either way)

```bash
scripts/create_k8s_configmap.sh keys/signing-public-key.pem model-signing-public-key default
```

### 3a. Layer 1: store the decryption key as a Kubernetes Secret

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

### 3b. Layer 3: attested key release via Kata+CoCo/KBS instead

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
docker login ghcr.io -u <user>   # classic PAT with write:packages (or your registry's login)
HF_USERNAME=<your-hf-username> KBS_NAMESPACE=coco-tenant \
CONSUMER_REGISTRY=ghcr.io/<user> \
  scripts/deploy_consumer_pod.sh coco --publish
kubectl logs -f pod/confidential-model-consumer-coco
```

`--publish` (or `PUBLISH_IMAGE=1`) makes the deploy build and push the
consumer image first and verify it is pullable anonymously, as one step of
the pipeline. Without it, the script only deploys an image you already
published. Any registry works — `CONSUMER_REGISTRY=quay.io/<org>`,
`docker.io/<user>`, a self-hosted registry with a publicly-trusted
certificate, … — and the image becomes
`<CONSUMER_REGISTRY>/confidential-model-consumer:<CONSUMER_TAG, default latest>`.
To use a fully custom name, set `CONSUMER_IMAGE` instead (it wins over
`CONSUMER_REGISTRY`). All of these can live in `.env`.

`scripts/deploy_consumer_pod.sh` also reads `HF_USERNAME`, `HF_REPO_ID`,
`HF_MODEL_ID`, `KBS_NAMESPACE`, `CONSUMER_IMAGE`, `CONSUMER_REGISTRY` and
`CONSUMER_TAG` from the git-ignored
`.env` file (plain `KEY=value` lines); variables already set in the
environment take precedence.

`scripts/publish_consumer_image.sh` builds the consumer image, pushes it, and
then checks that it can be pulled **anonymously over publicly-trusted TLS**
(the registry-API check the guest effectively performs), failing with a
hint if not. On ghcr.io a new package starts private and can only be made
public from the GitHub web UI, so the first run typically pushes, reports
the pending visibility change, and passes on a re-run after you flip it. The
image defaults to `$CONSUMER_IMAGE` (environment or `.env`) or is derived
from `$CONSUMER_REGISTRY`. You can also run it on its own:
`scripts/publish_consumer_image.sh quay.io/<org>/confidential-model-consumer:latest`.

`CONSUMER_IMAGE` is required in `coco` mode. Unlike Layer 1, the Kata guest
VM pulls the image itself (via the nydus snapshotter and the in-guest CDH),
so a local/minikube-loaded image doesn't work. Requirements verified
end to end:

- The image must be in a registry the guest can reach anonymously with
  publicly-trusted TLS (e.g. a **public** ghcr.io package — a private one
  returns 403).
- The guest unpacks the image into its own RAM, so the guest memory must
  exceed the unpacked image size. The consumer image (~1.7 GB unpacked)
  needs `default_memory = 4096` (MB) or more in the Kata config
  (`configuration-qemu-coco-dev.toml`); 512/2048 MB fail with
  `Failed to unpack layer`. On minikube, the node container's own memory
  limit must be raised accordingly (e.g. `docker update --memory 7g
  --memory-swap 7g <node-container>`).

`KBS_NAMESPACE` must match whatever `scripts/deploy_coco_kbs.sh` printed
for your cluster (its "Next steps" output) — it defaults to `coco-tenant`,
not the CoCo operator's own `confidential-containers-system` namespace.
If `KBS_RESOURCE_PATH` (in the manifest, default `default/key/my-model`)
or `HF_MODEL_ID` need to differ from their defaults, edit the manifest or
extend the script the same way as for Layer 1.

Expected tail of the logs (Layer 3, combined with Layer 2):

```
[consumer] fetching decryption key from CDH (attested KBS release): http://127.0.0.1:8006/cdh/resource/default/key/my-model
[consumer] obtained decryption key via attested KBS release (Layer 3)
[consumer] loaded trusted signing public key from mounted Kubernetes ConfigMap
[consumer] downloading encrypted artifact from '<repo>' ...
[consumer] ciphertext checksum verified against manifest
[consumer] Ed25519 signature verified against the trusted public key
[consumer] decrypting ...
[consumer] loading model from ...
[consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
[consumer] done: model decrypted and loaded successfully
```

If attestation fails or the KBS resource policy denies release, the CDH
fetch fails and `fetch_key_from_kbs` raises before any decryption is
attempted. If the artifact was tampered with or wasn't signed by the
trusted key, `verify_signature` raises instead — either failure aborts
before decryption, independently of the other.

## Verifying each layer works

1. **Offline, no Hub token or cluster needed** — exercise the crypto,
   signing, and Layer 3 CDH fetch helpers directly:

   ```bash
   python -m pytest tests/ -q
   ```

   - `test_crypto_utils.py` round-trips a random payload through
     `encrypt_bytes`/`decrypt_bytes` and confirms that a flipped
     ciphertext byte or wrong key raises `InvalidTag` instead of silently
     returning corrupted data.
   - `test_signing_utils.py` round-trips a payload through Ed25519
     sign/verify and confirms tampering, a wrong key, or a wrong
     signature all raise `InvalidSignature`.
   - `test_kbs_fetch.py` spins up a local `http.server` shaped like the
     CDH resource API and verifies `fetch_key_from_kbs` round-trips a key
     correctly and raises clearly on a missing resource or an unreachable
     CDH endpoint — useful when a real Kata/CoCo/KBS stack isn't at hand,
     since it still exercises the exact HTTP contract the consumer relies
     on.

2. **Layer 1 + 2, end to end** — after `scripts/deploy_consumer_pod.sh`,
   tail the logs
   (`kubectl logs -f pod/confidential-model-consumer`); a working
   deployment prints, in order:

   ```
   [consumer] loaded decryption key from mounted Kubernetes Secret (Layer 1)
   [consumer] loaded trusted signing public key from mounted Kubernetes ConfigMap
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] Ed25519 signature verified against the trusted public key
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   and the pod ends in `Completed`. Two negative tests confirm the checks
   actually gate things rather than just logging:
   - **Layer 1**: `kubectl delete secret model-decryption-key`, then
     re-run the pod. In practice kubelet refuses to even start the
     container (`kubectl describe pod` shows `FailedMount: secret
     "model-decryption-key" not found`) since the volume can't be
     mounted; if the Secret existed but the key file inside it didn't,
     you'd instead see the container start and fail fast with
     `FileNotFoundError` from `load_key`.
   - **Layer 2**: download the artifact the producer just pushed, flip a
     byte, re-upload it to the same Hub repo path, then re-run the pod —
     it should print `signature verification FAILED ... Aborting before
     decryption` and exit non-zero, never reaching the decrypt step. Rerun
     the producer afterward to restore a validly signed artifact.

3. **Layer 3, end to end** — after `scripts/deploy_consumer_pod.sh coco`,
   tail the logs (`kubectl logs -f pod/confidential-model-consumer-coco`);
   a working deployment prints:

   ```
   [consumer] fetching decryption key from CDH (attested KBS release): http://127.0.0.1:8006/cdh/resource/default/key/my-model
   [consumer] obtained decryption key via attested KBS release (Layer 3)
   [consumer] loaded trusted signing public key from mounted Kubernetes ConfigMap
   [consumer] downloading encrypted artifact from '<repo>' ...
   [consumer] ciphertext checksum verified against manifest
   [consumer] Ed25519 signature verified against the trusted public key
   [consumer] decrypting ...
   [consumer] loading model from ...
   [consumer] loaded model OK, last_hidden_state shape=(1, N, 128)
   [consumer] done: model decrypted and loaded successfully
   ```

   confirming the key came from the attested CDH fetch rather than a
   mounted Secret (`k8s/consumer-pod-coco.yaml` mounts no decryption-key
   Secret at all — `kubectl describe pod confidential-model-consumer-coco`
   should show no such volume), while signature verification (Layer 2)
   still runs and passes as before.

4. **Layer 3, negative case (attestation actually gates the key)** —
   confirm the fetch fails rather than falling back to some other source
   when attestation can't happen. The simplest version: apply
   `k8s/consumer-pod-coco.yaml` *without* `runtimeClassName:
   kata-qemu-coco-dev` (i.e. as a plain container) — there's no CDH
   sidecar at `127.0.0.1:8006` in a non-Kata pod, so `fetch_key_from_kbs`
   should raise `RuntimeError: failed to fetch key resource ...` and the
   pod should end in `Error`/`CrashLoopBackOff` instead of silently
   succeeding. (On a real Kata/CoCo cluster, an equivalent test is
   tightening `kbs/resource-policy.rego` to `default allow = false` with
   no matching rule, re-uploading it with
   `scripts/set_kbs_resource_policy.sh`, and confirming the same pod that
   worked before now fails the CDH fetch.)

   If attestation fails or the KBS resource policy denies release, the
   CDH fetch fails and `fetch_key_from_kbs` raises before any decryption
   is attempted. If the artifact was tampered with or wasn't signed by
   the trusted key, `verify_signature` raises instead — either failure
   aborts before decryption, independently of the other, whichever
   key-delivery mode is in use.

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
