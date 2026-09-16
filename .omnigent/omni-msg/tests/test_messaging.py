"""零外部依賴／零真 session 副作用；透過 CLI→bridge→fake Omnigent 驗 contract。"""

import copy
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni_msg.bridge import Bridge, Store, validate_actors
from omni_msg.cli import save_private
from omni_msg.server import BridgeServer
from omni_msg.transport import (
    MessageError,
    OmnigentTransport,
    decode_json,
    private_file,
    request_json,
    validate_url,
)

PACKAGE = Path(__file__).resolve().parents[1]
TOKENS = {name: name * 8 for name in ("leader", "worker", "outsider")}
ACTORS = {
    name: {"session_id": f"conv_{name}", "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
           "allow": {"leader": ["worker"], "worker": ["leader"], "outsider": []}[name]}
    for name, token in TOKENS.items()
}


def payload(kind="send", target="leader", text="完成", identifier=None):
    return {"id": identifier or str(uuid.uuid4()), "kind": kind, "target": target, "text": text}


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.result = ("accepted", "item-1", None)

    def deliver(self, session, text):
        self.calls.append((session, text))
        return self.result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.store = Store(self.path, ACTORS)
        self.transport = FakeTransport()
        self.bridge = Bridge(ACTORS, self.store, self.transport)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assert_error(self, code, fn, *args):
        with self.assertRaises(MessageError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_authentication_and_filtered_contacts(self):
        self.assertEqual(self.bridge.authenticate(TOKENS["worker"]), "worker")
        self.assert_error("unauthorized", self.bridge.authenticate, "wrong")
        self.assertEqual(self.bridge.contacts("worker"), {
            "self": "worker", "contacts": [{"name": "leader", "session_id": "conv_leader"}]})
        self.assertEqual(self.bridge.contacts("outsider")["contacts"], [])

    def test_worker_sends_upward_with_explicit_actor_identity(self):
        row = self.bridge.send("worker", payload(text="PR 已就緒"))
        self.assertEqual(row["delivery"], "accepted")
        session, text = self.transport.calls[0]
        self.assertEqual(session, "conv_leader")
        self.assertIn("非人類指令", text)
        envelope = json.loads(text.splitlines()[-1])
        self.assertEqual(envelope["from"], "worker")
        self.assertEqual(envelope["from_session"], "conv_worker")
        self.assertEqual(envelope["text"], "PR 已就緒")
        self.assertEqual(envelope["id"], row["id"])

    def test_ask_reply_correlates_in_both_directions(self):
        ask = self.bridge.send("worker", payload("ask", text="選哪個分支？"))
        self.assertFalse(self.bridge.check("worker", ask["id"])["answered"])
        reply = self.bridge.send("leader", payload("reply", ask["id"], "develop"))
        receipt = self.bridge.check("worker", ask["id"])
        self.assertTrue(receipt["answered"])
        self.assertEqual(receipt["reply"]["id"], reply["id"])
        self.assertEqual(receipt["reply"]["body"], "develop")
        self.assertEqual(self.transport.calls[1][0], "conv_worker")
        self.assertEqual(len(self.transport.calls), 2)

    def test_unrelated_message_does_not_answer_request(self):
        ask = self.bridge.send("worker", payload("ask"))
        self.bridge.send("leader", payload("send", "worker", "別件事"))
        self.assertFalse(self.bridge.check("worker", ask["id"])["answered"])

    def test_no_ack_loop_and_only_one_reply(self):
        send = self.bridge.send("worker", payload())
        self.assert_error("not_replyable", self.bridge.send, "leader", payload("reply", send["id"]))
        ask = self.bridge.send("worker", payload("ask"))
        reply = self.bridge.send("leader", payload("reply", ask["id"]))
        self.assert_error("already_replied", self.bridge.send, "leader", payload("reply", ask["id"]))
        self.assert_error("not_replyable", self.bridge.send, "worker", payload("reply", reply["id"]))

    def test_acl_and_no_sender_spoof(self):
        self.assert_error("target_not_allowed", self.bridge.send, "worker", payload(target="outsider"))
        self.assert_error("target_not_allowed", self.bridge.send, "worker", payload(target="worker"))
        self.assert_error("target_not_allowed", self.bridge.send, "worker", payload(target="conv_leader"))
        self.assert_error("invalid_message_fields", self.bridge.send, "worker", {**payload(), "from": "leader"})
        ask = self.bridge.send("worker", payload("ask"))
        self.assert_error("message_not_found", self.bridge.check, "outsider", ask["id"])
        self.assert_error("message_not_found", self.bridge.send, "outsider", payload("reply", ask["id"]))
        self.assert_error("message_not_found", self.bridge.send, "worker", payload("reply", ask["id"]))

    def test_reply_also_requires_outbound_acl(self):
        ask = self.bridge.send("worker", payload("ask"))
        actors = copy.deepcopy(ACTORS)
        actors["leader"]["allow"] = []
        restricted = Bridge(actors, self.store, self.transport)
        self.assert_error("target_not_allowed", restricted.send, "leader", payload("reply", ask["id"]))

    def test_idempotency_and_payload_conflict(self):
        msg = payload()
        first = self.bridge.send("worker", msg)
        self.assertEqual(first, self.bridge.send("worker", msg))
        self.assertEqual(len(self.transport.calls), 1)
        self.assert_error("message_id_conflict", self.bridge.send, "worker", {**msg, "text": "不同內容"})

    def test_concurrent_duplicate_requests_dispatch_once(self):
        msg = payload()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.bridge.send("worker", msg), range(20)))
        self.assertEqual({row["id"] for row in results}, {msg["id"]})
        self.assertEqual(len(self.transport.calls), 1)

    def test_unknown_post_is_not_retried(self):
        self.transport.result = ("unknown", None, "transport_unknown")
        msg = payload()
        self.assertEqual(self.bridge.send("worker", msg)["delivery"], "unknown")
        self.bridge.send("worker", msg)
        self.assertEqual(len(self.transport.calls), 1)

    def test_unexpected_delivery_failure_is_durable(self):
        msg = payload()
        with patch.object(self.transport, "deliver", side_effect=RuntimeError("secret")):
            self.assertEqual(self.bridge.send("worker", msg)["error"], "delivery_unknown")
        self.assertNotIn("secret", json.dumps(self.bridge.check("worker", msg["id"])))

    def test_restart_preserves_reply_and_never_replays_inflight(self):
        ask = self.bridge.send("worker", payload("ask"))
        self.bridge.send("leader", payload("reply", ask["id"], "已回答"))
        pending = self.bridge.send("worker", payload())
        with self.store.db:
            self.store.db.execute("UPDATE messages SET delivery='dispatching' WHERE id=?", (pending["id"],))
        self.store.close()
        self.store = Store(self.path, ACTORS)
        self.bridge = Bridge(ACTORS, self.store, self.transport)
        self.assertEqual(self.bridge.check("worker", ask["id"])["reply"]["body"], "已回答")
        self.assertEqual(self.bridge.check("worker", pending["id"])["error"], "bridge_restarted")
        self.assertEqual(len(self.transport.calls), 3)

    def test_actor_rebind_is_rejected(self):
        self.store.close()
        actors = copy.deepcopy(ACTORS)
        actors["worker"]["session_id"] = "conv_other"
        try:
            self.assert_error("actor_rebind_requires_new_ledger", Store, self.path, actors)
        finally:
            self.store = Store(self.path, ACTORS)

    def test_exclusive_store_lock(self):
        self.assert_error("bridge_already_running", Store, self.path, ACTORS)

    def test_config_rejects_unknown_targets_duplicate_tokens_and_sessions(self):
        for field, value in [("session_id", ACTORS["leader"]["session_id"]),
                             ("token_sha256", ACTORS["leader"]["token_sha256"]),
                             ("allow", ["unknown"]), ("allow", ["worker"])]:
            with self.subTest(field=field, value=value):
                actors = copy.deepcopy(ACTORS)
                actors["worker"][field] = value
                with self.assertRaises(MessageError):
                    validate_actors(actors)

    def test_bad_input_and_size_limit(self):
        for msg in [payload(text=""), payload(text="x" * 8193), payload(text="\x00"),
                    payload(identifier="../../x"), payload(kind=[]), payload(kind="raw"), []]:
            with self.subTest(msg=str(msg)[:50]), self.assertRaises(MessageError):
                self.bridge.send("worker", msg)
        self.assertEqual(self.transport.calls, [])

    def test_rejected_ask_cannot_be_replied(self):
        self.transport.result = ("rejected", None, "policy_denied")
        ask = self.bridge.send("worker", payload("ask"))
        self.assert_error("not_replyable", self.bridge.send, "leader", payload("reply", ask["id"]))

    def test_recorded_reply_can_be_checked_even_if_wake_failed(self):
        ask = self.bridge.send("worker", payload("ask"))
        self.transport.result = ("unknown", None, "transport_unknown")
        self.bridge.send("leader", payload("reply", ask["id"], "已回答但 wake 未確認"))
        checked = self.bridge.check("worker", ask["id"])
        self.assertTrue(checked["answered"])
        self.assertEqual(checked["reply"]["delivery"], "unknown")


class FakeOmnigent(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []
    response_body: ClassVar[dict] = {"queued": True, "item_id": "fake-item"}
    response_status = 202
    redirect = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append((self.path, self.headers.get("Authorization"), data))
        self.send_response(self.response_status)
        if self.redirect:
            self.send_header("Location", self.redirect)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.response_body).encode())


def launch(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class CLIE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        FakeOmnigent.calls = []
        FakeOmnigent.response_body = {"queued": True, "item_id": "fake-item"}
        FakeOmnigent.response_status = 202
        FakeOmnigent.redirect = None
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeOmnigent)
        self.upstream_thread = launch(self.upstream)
        self.token_file = self.root / "upstream-token"
        self.token_file.write_text("upstream-secret")
        self.token_file.chmod(0o600)
        self.transport = OmnigentTransport(f"http://127.0.0.1:{self.upstream.server_port}", self.token_file)
        self.store = Store(self.root / "ledger", ACTORS)
        self.server = BridgeServer(("127.0.0.1", 0), Bridge(ACTORS, self.store, self.transport))
        self.thread = launch(self.server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        for name, token in TOKENS.items():
            save_private(self.root / f"{name}.json", {"bridge_url": self.url, "token": token})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()
        self.temp.cleanup()

    def cli(self, actor, *args, expected=0, stdin=None):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE), "OMNI_MSG_CONFIG": str(self.root / f"{actor}.json")}
        proc = subprocess.run([sys.executable, "-m", "omni_msg.cli", *args],
                              env=env, text=True, input=stdin, capture_output=True, timeout=15, check=False)
        self.assertEqual(proc.returncode, expected, proc.stderr + proc.stdout)
        self.assertNotIn("upstream-secret", proc.stderr + proc.stdout)
        self.assertNotIn(TOKENS[actor], proc.stderr + proc.stdout)
        return json.loads((proc.stdout or proc.stderr).splitlines()[-1])

    def test_cli_worker_to_leader_ask_reply_and_idempotency(self):
        self.assertEqual(self.cli("worker", "list")["contacts"][0]["name"], "leader")
        request = self.cli("worker", "ask", "leader", "要選哪個方案？")
        self.assertEqual(request["delivery"], "accepted")
        self.assertNotIn("upstream-secret", (self.root / "worker.json").read_text())
        self.assertFalse(self.cli("worker", "check", request["id"])["answered"])
        reply = self.cli("leader", "reply", request["id"], "-", stdin="選方案 A\n附理由")
        self.cli("leader", "reply", request["id"], "選方案 A\n附理由", "--id", reply["id"])
        receipt = self.cli("worker", "check", request["id"])
        self.assertEqual(receipt["reply"]["body"], "選方案 A\n附理由")
        self.assertEqual(len(FakeOmnigent.calls), 2)
        path, auth, data = FakeOmnigent.calls[0]
        self.assertEqual(path, "/v1/sessions/conv_leader/events")
        self.assertEqual(auth, "Bearer upstream-secret")
        self.assertEqual(set(data), {"type", "data"})
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["data"]["role"], "user")
        self.assertEqual(data["data"]["content"][0]["type"], "input_text")
        self.assertEqual(FakeOmnigent.calls[1][0], "/v1/sessions/conv_worker/events")

    def test_http_acl_and_auth(self):
        self.assertEqual(self.cli("outsider", "list")["contacts"], [])
        self.cli("outsider", "send", "worker", "越權", expected=2)
        request = self.cli("worker", "send", "leader", "通知")
        self.cli("outsider", "check", request["id"], expected=2)
        with self.assertRaises(MessageError) as caught:
            request_json(f"{self.url}/v1/contacts", "bad-token")
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(len(FakeOmnigent.calls), 1)

    def test_policy_deny_and_upstream_failures_surface_delivery(self):
        for status, body, expected in [
            (200, {"queued": False, "denied": True, "reason": "upstream-secret"}, "rejected"),
            (403, {"error": "upstream-secret"}, "rejected"),
            (503, {}, "unknown"), (200, {"queued": False}, "unknown"),
            (200, {"queued": "true"}, "unknown"),
        ]:
            with self.subTest(status=status, body=body):
                FakeOmnigent.response_status = status
                FakeOmnigent.response_body = body
                row = self.cli("worker", "send", "leader", "通知", expected=3)
                self.assertEqual(row["delivery"], expected)
                self.cli("worker", "send", "leader", "通知", "--id", row["id"], expected=3)
        self.assertEqual(len(FakeOmnigent.calls), 5)

    def test_redirect_is_not_followed(self):
        FakeOmnigent.response_status = 307
        FakeOmnigent.redirect = f"http://127.0.0.1:{self.upstream.server_port}/steal"
        row = self.cli("worker", "send", "leader", "通知", expected=3)
        self.assertEqual(row["delivery"], "unknown")
        self.assertEqual(len(FakeOmnigent.calls), 1)

    def test_provision_creates_private_configs_without_leaking_credentials(self):
        registry = {name: {k: v for k, v in actor.items() if k != "token_sha256"}
                    for name, actor in ACTORS.items()}
        registry_file = self.root / "registry.json"
        registry_file.write_text(json.dumps(registry))
        directory = self.root / "configured"
        result = self.cli("leader", "provision", "--directory", str(directory),
                          "--registry", str(registry_file), "--server-url", self.transport.url,
                          "--token-file", str(self.token_file))
        self.assertEqual(result["server_config"], str(directory / "server.json"))
        server = json.loads(private_file(directory / "server.json"))
        client = json.loads(private_file(directory / "clients/worker.json"))
        self.assertEqual(hashlib.sha256(client["token"].encode()).hexdigest(),
                         server["actors"]["worker"]["token_sha256"])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "clients/worker.json").stat().st_mode & 0o777, 0o600)
        # 已有設定不可覆寫。
        self.cli("leader", "provision", "--directory", str(directory),
                 "--registry", str(registry_file), "--server-url", self.transport.url,
                 "--token-file", str(self.token_file), expected=2)

    def test_serve_entrypoint_starts_and_handles_sigint(self):
        config = self.root / "server.json"
        save_private(config, {"server_url": self.transport.url, "token_file": str(self.token_file), "actors": ACTORS})
        # 用已綁定的 port 驗失敗；再釋放後由 CLI 啟動同一 port。
        port = self.server.server_port
        self.cli("leader", "serve", "--server-config", str(config),
                 "--state-dir", str(self.root / "other-state"), "--port", str(port), expected=2)
        self.server.shutdown()
        self.server.server_close()
        proc = subprocess.Popen([sys.executable, "-m", "omni_msg.cli", "serve",
                                 "--server-config", str(config), "--state-dir", str(self.root / "other-state"),
                                 "--port", str(port)], env={**os.environ, "PYTHONPATH": str(PACKAGE)},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stderr, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "serve 沒有回報 ready")
                ready = json.loads(proc.stderr.readline())
            self.assertEqual(ready["listening"], self.url)
            self.assertEqual(self.cli("worker", "list")["self"], "worker")
        finally:
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stdout + stderr)


class BundleSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = PACKAGE.parent / "woow-issue-leader"
        spec = importlib.util.spec_from_file_location("generate_worker_test", cls.bundle / "generate-worker.py")
        cls.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.generator)

    def test_static_skill_copies_match_authoritative_source(self):
        source = (self.bundle / "skills/omni-msg/SKILL.md").read_bytes()
        for role in ("repo_leader_cc", "repo_leader_pi"):
            self.assertEqual((self.bundle / "agents" / role / "skills/omni-msg/SKILL.md").read_bytes(), source)

    def test_every_dynamic_worker_gets_the_shared_skill(self):
        gw = self.generator
        # 此測試只驗配送 seam；上游 model／bundle parser 由 build-bundles --check 真跑。
        with tempfile.TemporaryDirectory() as td, \
                patch.object(gw, "_check_model_family"), \
                patch.object(gw, "_verify_generated", return_value={}):
            for purpose, row in gw.ROSTER.items():
                if row["template"] is None:
                    continue
                for harness in row["template"]:
                    out = Path(td) / f"{purpose}-{harness}"
                    gw.generate(purpose, harness, out, **gw.sample_kwargs(purpose))
                    self.assertEqual((out / "skills/omni-msg/SKILL.md").read_bytes(),
                                     (gw.MESSAGING_SKILL_DIR / "SKILL.md").read_bytes())

    def test_missing_shared_skill_fails_before_output(self):
        gw = self.generator
        with tempfile.TemporaryDirectory() as td, \
                patch.object(gw, "_check_model_family"), \
                patch.object(gw, "MESSAGING_SKILL_DIR", Path(td) / "missing"):
            out = Path(td) / "output"
            with self.assertRaises(gw.GenerateFailure):
                gw.generate("explore", "pi", out)
            self.assertFalse(out.exists())


class SafetyTests(unittest.TestCase):
    def test_duplicate_json_keys_are_rejected(self):
        for raw in ['{"worker": {}, "worker": {}}', '{"queued": true, "queued": false}']:
            with self.assertRaises(ValueError):
                decode_json(raw)

    def test_timeout_is_unknown_not_rejected_or_retried(self):
        with tempfile.TemporaryDirectory() as td:
            token = Path(td) / "token"
            token.write_text("secret")
            token.chmod(0o600)
            transport = OmnigentTransport("http://127.0.0.1:6767", token)
            with patch("omni_msg.transport.request_json", side_effect=MessageError("transport_unknown", 502)) as call:
                self.assertEqual(transport.deliver("conv_test", "message"), ("unknown", None, "transport_unknown"))
                self.assertEqual(call.call_count, 1)

    def test_urls_are_constrained(self):
        for url in ["http://evil.test", "https://user:secret@example.com", "https://example.com/v1",
                    "https://example.com?q=x", "https://example.com#fragment", "http://127.0.0.1:0",
                    "https://example.com:bad", "https://example.com\n"]:
            with self.subTest(url=url), self.assertRaises(MessageError):
                validate_url(url)
        self.assertEqual(validate_url("https://example.com/"), "https://example.com")
        self.assertEqual(validate_url("http://127.0.0.1:6767"), "http://127.0.0.1:6767")
        with self.assertRaises(MessageError):
            validate_url("https://example.com", loopback=True)

    def test_private_file_rejects_permissions_symlink_and_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secret = root / "secret"
            secret.write_text("secret")
            secret.chmod(0o644)
            with self.assertRaises(MessageError):
                private_file(secret)
            secret.chmod(0o600)
            self.assertEqual(private_file(secret), "secret")
            link = root / "link"
            link.symlink_to(secret)
            with self.assertRaises(MessageError):
                private_file(link)
            with self.assertRaises(MessageError):
                private_file(root)

    def test_bridge_cannot_bind_public_interface(self):
        with self.assertRaises(MessageError):
            BridgeServer(("0.0.0.0", 6778), None)


if __name__ == "__main__":
    unittest.main()
