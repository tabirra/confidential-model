"""Offline tests for the producer's optional /mermelada trigger server.

Covers the auth gate and request routing in producer/server.py without a
real Hugging Face Hub, Kubernetes cluster, or network access:
run_pipeline is monkeypatched, and the HTTP server is spun up on a random
local port.
"""
from __future__ import annotations

import http.client
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from producer import server as producer_server  # noqa: E402


class TestIsAuthorized(unittest.TestCase):
    def test_missing_header_rejected(self):
        self.assertFalse(producer_server.is_authorized(None, "secret"))

    def test_wrong_scheme_rejected(self):
        self.assertFalse(producer_server.is_authorized("Basic secret", "secret"))

    def test_wrong_token_rejected(self):
        self.assertFalse(producer_server.is_authorized("Bearer wrong", "secret"))

    def test_correct_token_accepted(self):
        self.assertTrue(producer_server.is_authorized("Bearer secret", "secret"))


class TestMermeladaEndpoint(unittest.TestCase):
    def setUp(self):
        os.environ["PRODUCER_TRIGGER_TOKEN"] = "test-token"
        os.environ["HF_USERNAME"] = "testuser"

        self.pipeline_calls = []
        self._orig_run_pipeline = producer_server.run_pipeline
        producer_server.run_pipeline = lambda *a, **kw: self.pipeline_calls.append((a, kw))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), producer_server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        producer_server.run_pipeline = self._orig_run_pipeline

    def _post(self, path: str, token: str | None = None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        conn.request("POST", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return status

    def test_rejects_missing_token(self):
        self.assertEqual(self._post("/mermelada"), 401)
        self.assertEqual(self.pipeline_calls, [])

    def test_rejects_wrong_token(self):
        self.assertEqual(self._post("/mermelada", token="wrong"), 401)
        self.assertEqual(self.pipeline_calls, [])

    def test_accepts_correct_token_and_runs_pipeline(self):
        self.assertEqual(self._post("/mermelada", token="test-token"), 200)
        self.assertEqual(len(self.pipeline_calls), 1)

    def test_trailing_newline_in_secret_value_does_not_break_auth(self):
        # Regression: `kubectl create secret --from-file` stores the raw
        # file bytes verbatim (including a trailing newline if the file had
        # one), while a caller presenting the token via `$(cat file)`
        # command substitution has that newline stripped by the shell —
        # the two must still be considered equal.
        os.environ["PRODUCER_TRIGGER_TOKEN"] = "test-token\n"
        self.assertEqual(self._post("/mermelada", token="test-token"), 200)

    def test_unknown_path_is_404(self):
        self.assertEqual(self._post("/nope", token="test-token"), 404)

    def test_healthz(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.read()
        conn.close()


if __name__ == "__main__":
    unittest.main()
