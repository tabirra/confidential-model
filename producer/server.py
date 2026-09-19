#!/usr/bin/env python3
"""Optional HTTP-triggered mode for the producer (master branch only).

By default the producer is a one-shot script/container (see
encrypt_and_push.py and README.md's "Design decisions worth defending").
This file is an additional, opt-in way to run it: as a long-lived
Kubernetes pod (k8s/producer-pod.yaml) that listens for a signal instead
of a human running the pipeline steps by hand.

POST /mermelada with `Authorization: Bearer <token>` (matching
PRODUCER_TRIGGER_TOKEN) triggers, synchronously:
  1. encrypt_and_push.py (encrypt, sign, push to the Hub)
  2. create/update the model-decryption-key Secret and
     model-signing-public-key ConfigMap from its output
  3. deploy k8s/consumer-pod.yaml — the Secret+ConfigMap delivery path
     only; this does not attempt the Kata/CoCo path (k8s/consumer-pod-coco.yaml),
     which needs manual, environment-specific cluster setup (see README.md).

The request stays open until the whole pipeline finishes (roughly a
minute or two, mostly the Hub download/upload) — callers should use a
generous timeout, e.g. `curl --max-time 300`.

Env vars:
  PRODUCER_TRIGGER_TOKEN   required, bearer token /mermelada checks against
  HF_USERNAME              required, used to derive the push repo id
  HF_MODEL_ID              default source model (default: prajjwal1/bert-tiny)
  K8S_NAMESPACE            namespace for the Secret/ConfigMap/pod (default: default)
  PORT                     listen port (default: 8080)
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

REPO_ROOT = Path(__file__).resolve().parent.parent
DECRYPTION_KEY_PATH = REPO_ROOT / "secrets" / "decryption-key.b64"
SIGNING_PUBLIC_KEY_PATH = REPO_ROOT / "keys" / "signing-public-key.pem"
CONSUMER_POD_TEMPLATE = REPO_ROOT / "k8s" / "consumer-pod.yaml"

_run_lock = threading.Lock()


def is_authorized(auth_header: str | None, expected_token: str) -> bool:
    """Constant-time bearer-token check against PRODUCER_TRIGGER_TOKEN."""
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer "):]
    return hmac.compare_digest(presented, expected_token)


def run_encrypt_and_push(model_id: str, push_repo_id: str) -> None:
    print(f"[producer-server] running encrypt_and_push.py --model-id {model_id} "
          f"--push-repo-id {push_repo_id}", flush=True)
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "producer" / "encrypt_and_push.py"),
         "--model-id", model_id, "--push-repo-id", push_repo_id],
        check=True,
        cwd=REPO_ROOT,
    )


def _apply_secret(core, namespace: str, name: str, string_data: dict) -> None:
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    body = client.V1Secret(metadata=client.V1ObjectMeta(name=name), string_data=string_data)
    try:
        core.create_namespaced_secret(namespace, body)
        print(f"[producer-server] created Secret '{name}'", flush=True)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core.replace_namespaced_secret(name, namespace, body)
        print(f"[producer-server] updated Secret '{name}'", flush=True)


def _apply_configmap(core, namespace: str, name: str, data: dict) -> None:
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
    try:
        core.create_namespaced_config_map(namespace, body)
        print(f"[producer-server] created ConfigMap '{name}'", flush=True)
    except ApiException as exc:
        if exc.status != 409:
            raise
        core.replace_namespaced_config_map(name, namespace, body)
        print(f"[producer-server] updated ConfigMap '{name}'", flush=True)


def deliver_key_and_configmap(namespace: str) -> None:
    from kubernetes import client

    key_b64 = DECRYPTION_KEY_PATH.read_text().strip()
    public_key_pem = SIGNING_PUBLIC_KEY_PATH.read_text()

    core = client.CoreV1Api()
    _apply_secret(core, namespace, "model-decryption-key", {"key": key_b64})
    _apply_configmap(core, namespace, "model-signing-public-key", {"public-key.pem": public_key_pem})


def deploy_consumer_pod(namespace: str, hf_repo_id: str, hf_model_id: str) -> None:
    import yaml
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    rendered = Template(CONSUMER_POD_TEMPLATE.read_text()).safe_substitute(
        HF_REPO_ID=hf_repo_id, HF_MODEL_ID=hf_model_id
    )
    manifest = yaml.safe_load(rendered)
    name = manifest["metadata"]["name"]

    core = client.CoreV1Api()
    try:
        core.delete_namespaced_pod(name, namespace)
        print(f"[producer-server] deleted existing pod '{name}'", flush=True)
    except ApiException as exc:
        if exc.status != 404:
            raise
    core.create_namespaced_pod(namespace, manifest)
    print(f"[producer-server] deployed pod '{name}'", flush=True)


def run_pipeline(model_id: str, hf_username: str, namespace: str) -> None:
    from kubernetes import config as k8s_config

    push_repo_id = f"{hf_username}/{model_id.rstrip('/').rsplit('/', 1)[-1]}-encrypted"
    run_encrypt_and_push(model_id, push_repo_id)

    k8s_config.load_incluster_config()
    deliver_key_and_configmap(namespace)
    deploy_consumer_pod(namespace, push_repo_id, model_id)
    print("[producer-server] pipeline complete", flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "confidential-model-producer/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib method name)
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 (stdlib method name)
        if self.path != "/mermelada":
            self._send_json(404, {"error": "not found"})
            return

        # .strip(): tolerate a trailing newline in the Secret value (e.g. if
        # it was created from a file written with a trailing newline) —
        # command-substitution callers like `$(cat token-file)` strip theirs.
        expected_token = os.environ["PRODUCER_TRIGGER_TOKEN"].strip()
        if not is_authorized(self.headers.get("Authorization"), expected_token):
            self._send_json(401, {"error": "unauthorized"})
            return

        if not _run_lock.acquire(blocking=False):
            self._send_json(409, {"error": "a pipeline run is already in progress"})
            return

        try:
            run_pipeline(
                model_id=os.environ.get("HF_MODEL_ID", "prajjwal1/bert-tiny"),
                hf_username=os.environ["HF_USERNAME"],
                namespace=os.environ.get("K8S_NAMESPACE", "default"),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
            print(f"[producer-server] pipeline FAILED: {exc}", flush=True)
            self._send_json(500, {"error": str(exc)})
            return
        finally:
            _run_lock.release()

        self._send_json(200, {"status": "done"})

    def log_message(self, fmt, *args):  # quieter, timestamped-by-print logging
        print(f"[producer-server] {self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    if "PRODUCER_TRIGGER_TOKEN" not in os.environ:
        print("error: PRODUCER_TRIGGER_TOKEN must be set", file=sys.stderr)
        sys.exit(1)
    if "HF_USERNAME" not in os.environ:
        print("error: HF_USERNAME must be set", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[producer-server] listening on :{port} — POST /mermelada to trigger the pipeline", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
