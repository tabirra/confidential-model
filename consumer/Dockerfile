FROM python:3.11-slim

WORKDIR /app

COPY consumer/requirements.txt /app/consumer/requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    -r /app/consumer/requirements.txt

COPY common /app/common
COPY consumer/decrypt_and_load.py /app/consumer/decrypt_and_load.py

# Defaults; override via the Pod/Deployment env in k8s/consumer-pod.yaml
ENV DECRYPTION_KEY_PATH=/etc/secrets/decryption-key/key \
    WORK_DIR=/tmp/confidential-model \
    HF_REPO_TYPE=model

ENTRYPOINT ["python", "/app/consumer/decrypt_and_load.py"]
