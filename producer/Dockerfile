FROM python:3.11-slim

WORKDIR /app

COPY producer/requirements.txt /app/producer/requirements.txt
RUN pip install --no-cache-dir -r /app/producer/requirements.txt

COPY common /app/common
COPY producer/encrypt_and_push.py /app/producer/encrypt_and_push.py

# The producer is a one-shot job, not a long-running service: it downloads,
# encrypts, and pushes, then exits. Run it with `docker run`, passing
# HF_TOKEN and --push-repo-id (see README.md). Local output (the
# decryption key, manifest, etc.) is written under /app/secrets — mount
# it as a volume to get the files back on the host, e.g.:
#   docker run --rm -e HF_TOKEN=hf_... \
#     -v "$(pwd)/secrets:/app/secrets" \
#     confidential-model-producer:latest --push-repo-id <user>/bert-tiny-encrypted
ENTRYPOINT ["python", "/app/producer/encrypt_and_push.py"]
