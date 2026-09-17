"""Offline test for the Layer 3 attested key-release path.

Spins up a local http.server that mimics the shape of the in-guest CDH
resource API (GET /cdh/resource/<path> -> raw resource bytes) and points
consumer.decrypt_and_load.fetch_key_from_kbs at it. This exercises the real
HTTP fetch + parsing code without needing an actual Kata/CoCo guest,
Trustee KBS, or attestation hardware.
"""
from __future__ import annotations

import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import crypto_utils  # noqa: E402
from consumer.decrypt_and_load import fetch_key_from_kbs  # noqa: E402

RESOURCE_PATH = "default/key/my-model"


class _FakeCdhHandler(http.server.BaseHTTPRequestHandler):
    key_b64_body: bytes = b""

    def do_GET(self):  # noqa: N802 (stdlib method name)
        expected = f"/cdh/resource/{RESOURCE_PATH}"
        if self.path == expected:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(self.key_b64_body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture
def fake_cdh_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeCdhHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_fetch_key_from_kbs_roundtrip(fake_cdh_server):
    key = crypto_utils.generate_key()
    _FakeCdhHandler.key_b64_body = (crypto_utils.key_to_b64(key) + "\n").encode("utf-8")

    port = fake_cdh_server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/cdh/resource"

    fetched = fetch_key_from_kbs(RESOURCE_PATH, base_url)
    assert fetched == key


def test_fetch_key_from_kbs_raises_on_missing_resource(fake_cdh_server):
    _FakeCdhHandler.key_b64_body = b"irrelevant"
    port = fake_cdh_server.server_address[1]
    base_url = f"http://127.0.0.1:{port}/cdh/resource"

    with pytest.raises(RuntimeError, match="failed to fetch key resource"):
        fetch_key_from_kbs("default/key/some-other-model", base_url)


def test_fetch_key_from_kbs_raises_when_cdh_unreachable():
    # Nothing listening on this port -> simulates attestation never having
    # succeeded / CDH not running, which is exactly the failure mode Layer
    # 3 should surface loudly rather than silently falling back.
    with pytest.raises(RuntimeError, match="failed to fetch key resource"):
        fetch_key_from_kbs(RESOURCE_PATH, "http://127.0.0.1:1/cdh/resource")
