FROM python:3.11-slim

WORKDIR /app

COPY producer/requirements.txt /app/producer/requirements.txt
RUN pip install --no-cache-dir -r /app/producer/requirements.txt

COPY common /app/common
COPY producer/encrypt_and_push.py /app/producer/encrypt_and_push.py
COPY producer/server.py /app/producer/server.py
COPY k8s/consumer-pod.yaml /app/k8s/consumer-pod.yaml

# The default entrypoint is the one-shot job: it downloads, encrypts,
# signs, and pushes, then exits. Run it with `docker run`, passing
# HF_TOKEN and --push-repo-id (see README.md). Local output (the
# decryption key, signing key, manifest, etc.) is written under
# /app/secrets and /app/keys — mount those as volumes to get the files
# back on the host, e.g.:
#   docker run --rm -e HF_TOKEN=hf_... \
#     -v "$(pwd)/secrets:/app/secrets" -v "$(pwd)/keys:/app/keys" \
#     confidential-model-producer:latest --push-repo-id <user>/bert-tiny-encrypted
#
# The same image also runs producer/server.py, the optional /mermelada
# trigger-server mode (master branch, k8s/producer-pod.yaml) — a
# Kubernetes pod spec overrides this ENTRYPOINT with
# `command: ["python", "/app/producer/server.py"]` to select it; see
# README.md.
ENTRYPOINT ["python", "/app/producer/encrypt_and_push.py"]
