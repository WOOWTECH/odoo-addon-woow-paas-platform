import http.server
import importlib.util
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

MODULE_PATH = Path(__file__).resolve().parents[1] / "src/services/llm_canary_probe.py"
SPEC = importlib.util.spec_from_file_location("llm_canary_probe_under_test", MODULE_PATH)
probe_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe_module
SPEC.loader.exec_module(probe_module)


API_KEY = "sk-" + "A" * 32
PROMPT = "Reply with the single word: pong."


class ProbeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"
    requests_seen = 0
    payload_seen = None
    secret_seen = None

    def log_message(self, *_args):
        pass

    def do_POST(self):
        type(self).requests_seen += 1
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        type(self).payload_seen = json.loads(body)
        type(self).secret_seen = self.headers.get("Authorization", "")
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", f"https://127.0.0.1:{self.server.server_port}/other")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "oversize":
            data = b"{" + b" " * (17 * 1024)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.mode == "drip":
            data = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                for byte in data:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass
            return
        if self.mode == "echo_secret":
            data = json.dumps({"error": self.secret_seen}).encode()
            status = 500
        elif self.mode == "non_json":
            data = b"not-json"
            status = 200
        elif self.mode == "bad_shape":
            data = b'{"choices":[]}'
            status = 200
        elif self.mode == "empty_content":
            data = b'{"choices":[{"message":{"content":"  \t "}}]}'
            status = 200
        else:
            data = b'{"choices":[{"message":{"content":"pong"}}]}'
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProbeServer:
    def __init__(self, mode):
        self.temp = tempfile.TemporaryDirectory()
        self.cert_path, key_path = self._make_certificate(Path(self.temp.name))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        ProbeHandler.mode = mode
        ProbeHandler.requests_seen = 0
        ProbeHandler.payload_seen = None
        ProbeHandler.secret_seen = None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(key_path))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _make_certificate(directory):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))
                ]), critical=False).sign(key, hashes.SHA256()))
        cert_path, key_path = directory / "cert.pem", directory / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        return cert_path, key_path

    @property
    def base_url(self):
        return f"https://localhost:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()


class TestLlmCanaryProbe(unittest.TestCase):
    def _child_call(self, server, *, trusted=True):
        payload = {
            "base_url": server.base_url,
            "api_key": API_KEY,
            "prompt": PROMPT,
            "max_tokens": 32,
            "model": "qwen3-4b-aicoder",
            "verify": str(server.cert_path) if trusted else True,
        }
        return probe_module._child_request(payload, 2.0)

    def test_public_probe_rejects_unapproved_inputs_without_spawn(self):
        with mock.patch.object(probe_module.subprocess, "Popen") as popen:
            for base in ("http://newapi.woowtech.io/v1", "https://evil.example/v1",
                         "https://newapi.woowtech.io/v1/"):
                with self.subTest(base=base):
                    result = probe_module.probe(base, API_KEY, deadline=time.monotonic() + 1)
                    self.assertEqual(result.code, "invalid_input")
            result = probe_module.probe("https://newapi.woowtech.io/v1", "sk-short",
                                        deadline=time.monotonic() + 1)
            self.assertEqual(result.code, "invalid_input")
            result = probe_module.probe("https://newapi.woowtech.io/v1", API_KEY,
                                        deadline=time.monotonic() - 1)
            self.assertEqual(result.code, "deadline")
            popen.assert_not_called()

    def test_secret_is_sent_only_over_stdin(self):
        observed = {}

        class CompletedChild:
            returncode = 0

            def communicate(self, *, input, timeout):
                observed["stdin"] = input
                observed["timeout"] = timeout
                return b'{"code":"ok","status":200}', b""

            def poll(self):
                return self.returncode

        def fake_popen(command, **kwargs):
            observed["command"] = command
            observed["env"] = kwargs["env"]
            return CompletedChild()

        with mock.patch.object(probe_module.subprocess, "Popen", side_effect=fake_popen):
            result = probe_module.probe("https://newapi.woowtech.io/v1", API_KEY,
                                        deadline=time.monotonic() + 5)
        self.assertEqual(result.code, "ok")
        self.assertIn(API_KEY.encode(), observed["stdin"])
        self.assertNotIn(API_KEY, repr(observed["command"]))
        self.assertNotIn(API_KEY, repr(observed["env"]))
        self.assertEqual(set(observed["env"]), {"PATH", "LANG", "LC_ALL"})
        self.assertNotIn("PYTHONPATH", observed["env"])
        self.assertFalse(any(name.lower().endswith("_proxy") for name in observed["env"]))

    def test_valid_chat_shape_uses_fixed_request_and_returns_only_result(self):
        with ProbeServer("ok") as server:
            payload = {
                "base_url": server.base_url, "api_key": API_KEY, "prompt": PROMPT,
                "max_tokens": 32, "model": "qwen3-4b-aicoder",
                "verify": str(server.cert_path),
            }
            result = probe_module._spawn_child(payload, deadline=time.monotonic() + 4)
        self.assertEqual(result.code, "ok")
        self.assertEqual(result.status, 200)
        self.assertIsInstance(result.latency_ms, int)
        self.assertEqual(ProbeHandler.secret_seen, f"Bearer {API_KEY}")
        self.assertEqual(ProbeHandler.payload_seen, {
            "model": "qwen3-4b-aicoder", "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 32, "stream": False,
        })
        self.assertEqual(set(result.__dataclass_fields__), {"code", "latency_ms", "status"})
        self.assertNotIn(API_KEY, repr(result))

    def test_child_startup_consumes_the_fixed_ten_second_deadline(self):
        observed = {}

        class CompletedChild:
            returncode = 0

            def communicate(self, *, input, timeout):
                observed["timeout"] = timeout
                return b'{"code":"ok","status":200}', b""

        def delayed_start(*_args, **_kwargs):
            clock[0] += 3.0
            return CompletedChild()

        clock = [100.0]
        payload = {"api_key": API_KEY}
        with mock.patch.object(probe_module.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(probe_module.subprocess, "Popen", side_effect=delayed_start):
            result = probe_module._spawn_child(payload, deadline=150.0, started=100.0)
        self.assertEqual(result.code, "ok")
        self.assertEqual(observed["timeout"], 6.0)

    def test_timeout_with_kill_failure_returns_fixed_code_and_never_waits_unbounded(self):
        observed = []

        class UnkillableChild:
            returncode = None
            stdin = mock.Mock()
            stdout = mock.Mock()
            stderr = None

            def communicate(self, *, input=None, timeout=None):
                observed.append(timeout)
                raise probe_module.subprocess.TimeoutExpired("child", timeout)

            def kill(self):
                raise OSError("synthetic secret-bearing exception")

            def poll(self):
                return None

        with mock.patch.object(probe_module.subprocess, "Popen", return_value=UnkillableChild()):
            result = probe_module._spawn_child({"api_key": API_KEY},
                                               deadline=time.monotonic() + 3)
        self.assertEqual(result.code, "cleanup_error")
        UnkillableChild.stdin.close.assert_called_once_with()
        UnkillableChild.stdout.close.assert_called_once_with()
        self.assertTrue(observed)
        self.assertTrue(all(timeout is not None for timeout in observed))
        self.assertNotIn("synthetic secret-bearing exception", repr(result))

    def test_tls_validation_failure_is_fixed_code(self):
        with ProbeServer("ok") as server:
            result = self._child_call(server, trusted=False)
        self.assertEqual(result["code"], "tls_error")
        self.assertNotIn(API_KEY, repr(result))

    def test_redirect_is_not_followed(self):
        with ProbeServer("redirect") as server:
            result = self._child_call(server)
        self.assertEqual(result["code"], "redirect")
        self.assertEqual(ProbeHandler.requests_seen, 1)

    def test_decoded_response_body_is_bounded(self):
        with ProbeServer("oversize") as server:
            result = self._child_call(server)
        self.assertEqual(result["code"], "body_too_large")

    def test_invalid_chat_shape_and_http_error_never_echo_secret(self):
        with ProbeServer("bad_shape") as server:
            malformed = self._child_call(server)
        self.assertEqual(malformed["code"], "invalid_response")
        with ProbeServer("echo_secret") as server:
            payload = {
                "base_url": server.base_url, "api_key": API_KEY, "prompt": PROMPT,
                "max_tokens": 32, "model": "qwen3-4b-aicoder",
                "verify": str(server.cert_path),
            }
            failed = probe_module._spawn_child(payload, deadline=time.monotonic() + 4)
        self.assertEqual(failed.code, "http_error")
        self.assertNotIn(API_KEY, repr(failed))

    def test_non_json_and_blank_chat_content_are_not_success(self):
        with ProbeServer("non_json") as server:
            non_json = self._child_call(server)
        self.assertEqual(non_json["code"], "invalid_response")
        with ProbeServer("empty_content") as server:
            blank = self._child_call(server)
        self.assertEqual(blank["code"], "invalid_response")

    def test_malformed_child_payload_returns_only_fixed_code(self):
        with ProbeServer("ok") as server:
            payload = {
                "base_url": server.base_url, "api_key": API_KEY, "prompt": PROMPT,
                "max_tokens": 32, "model": None,
                "verify": str(server.cert_path),
            }
            result = probe_module._spawn_child(payload, deadline=time.monotonic() + 4)
        self.assertEqual(result.code, "invalid_input")
        self.assertIsNone(result.status)
        self.assertNotIn(API_KEY, repr(result))
        self.assertEqual(ProbeHandler.requests_seen, 0)

    def test_parent_kills_and_reaps_child_on_slow_drip(self):
        with ProbeServer("drip") as server:
            payload = {
                "base_url": server.base_url, "api_key": API_KEY, "prompt": PROMPT,
                "max_tokens": 32, "model": "qwen3-4b-aicoder",
                "verify": str(server.cert_path),
            }
            procs = []
            real_popen = probe_module.subprocess.Popen

            def tracked_popen(*args, **kwargs):
                proc = real_popen(*args, **kwargs)
                procs.append(proc)
                return proc

            started = time.monotonic()
            with mock.patch.object(probe_module.subprocess, "Popen", side_effect=tracked_popen):
                result = probe_module._spawn_child(payload, deadline=started + 1.4)
            elapsed = time.monotonic() - started
        self.assertEqual(result.code, "timeout")
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll(), "timed-out child must be reaped")
        self.assertNotIn(API_KEY, repr(result))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux ITIMER_REAL watchdog")
    def test_orphaned_child_watchdog_stops_direct_slow_drip(self):
        with ProbeServer("drip") as server:
            payload = {
                "base_url": server.base_url, "api_key": API_KEY, "prompt": PROMPT,
                "max_tokens": 32, "model": "qwen3-4b-aicoder",
                "verify": str(server.cert_path),
            }
            script = (
                "import importlib.util, json, sys; "
                "spec=importlib.util.spec_from_file_location('probe', sys.argv[1]); "
                "mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; "
                "spec.loader.exec_module(mod); "
                "payload=json.load(sys.stdin); mod._arm_child_deadline(0.2); "
                "mod._child_request(payload, 2.0)"
            )
            child = subprocess.Popen(
                [sys.executable, "-I", "-c", script, str(MODULE_PATH)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8",
                     "LC_ALL": "C.UTF-8"},
            )
            stdout, _ = child.communicate(json.dumps(payload).encode(), timeout=3)
        self.assertEqual(child.returncode, -probe_module.signal.SIGALRM)
        self.assertEqual(stdout, b"")
        self.assertNotIn(API_KEY.encode(), stdout)


if __name__ == "__main__":
    unittest.main()
