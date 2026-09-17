"""經 CLI／HTTP 公開介面驗證 operator 授權的自動登記。"""
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


class SessionAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        from urllib.parse import urlsplit
        path = urlsplit(self.path).path
        with self.server.graph_lock:
            self.server.reads.append(path)
            if self.server.fail_reads:
                return self.respond(503, {})
            sid = path.removeprefix("/v1/sessions/").split("/")[0]
            node = self.server.graph.get(sid)
            if node is None:
                return self.respond(404, {})
            if path.endswith("/child_sessions"):
                children = [{"id": key} for key, row in self.server.graph.items()
                            if row["parent_session_id"] == sid]
                return self.respond(200, {"data": children, "has_more": False})
            return self.respond(200, node)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.graph_lock:
            self.server.events.append((self.path, data))
        self.server.post_gate.wait(8)
        self.respond(202, self.server.ack)


class RegistrationCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        self.env.pop("OMNI_MSG_CONFIG", None)
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), SessionAPI)
        self.api.graph_lock = threading.Lock()
        self.api.graph = {}
        self.api.events = []
        self.api.reads = []
        self.api.fail_reads = False
        self.api.ack = {"queued": True, "item_id": "fake-item"}
        self.api.post_gate = threading.Event()
        self.api.post_gate.set()
        for sid, parent in [("root_a", None), ("child_a", "root_a"),
                            ("root_b", None), ("child_b", "root_b"), ("unapproved", None)]:
            self.api.graph[sid] = self.node(sid, parent)
        self.thread = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.root / "token"
        self.token.write_text("upstream-secret")
        self.token.chmod(0o600)
        self.roots = self.root / "roots.json"
        self.roots.write_text(json.dumps({
            "team-a": {"session_id": "root_a", "workspace": "/workspace"},
            "team-b": {"session_id": "root_b", "workspace": "/workspace"},
        }))
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.process = None
        # 選擇空閒 port，不啟動其他 bridge。
        probe = ThreadingHTTPServer(("127.0.0.1", 0), SessionAPI)
        self.port = probe.server_port
        probe.server_close()

    @staticmethod
    def node(sid, parent):
        return {"id": sid, "parent_session_id": parent, "archived": False,
                "workspace": None if parent else "/workspace", "runner_id": "runner-1",
                "permission_level": 4, "agent_name": "worker" if parent else "woow_issue_leader"}

    def tearDown(self):
        self.api.post_gate.set()
        self.stop()
        self.api.shutdown()
        self.api.server_close()
        self.thread.join()
        self.temp.cleanup()

    def cli(self, *args, expected=0):
        proc = subprocess.run([sys.executable, "-m", "omni_msg.cli", *args],
                              env=self.env, text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        self.assertNotIn("upstream-secret", proc.stdout + proc.stderr)
        return json.loads((proc.stdout or proc.stderr).splitlines()[-1])

    def start(self, provision=True):
        if provision:
            self.cli("provision", "--directory", str(self.config), "--roots", str(self.roots),
                     "--server-url", f"http://127.0.0.1:{self.api.server_port}",
                     "--token-file", str(self.token), "--port", str(self.port), "--poll-seconds", "0.1")
        self.process = subprocess.Popen([
            sys.executable, "-m", "omni_msg.cli", "serve", "--server-config", str(self.config / "server.json"),
            "--state-dir", str(self.state), "--port", str(self.port),
        ], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stderr, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), "bridge 未啟動")
            ready = json.loads(self.process.stderr.readline())
        self.assertEqual(ready.get("listening"), f"http://127.0.0.1:{self.port}", ready)

    def stop(self):
        if self.process is not None:
            self.process.send_signal(signal.SIGINT)
            stdout, stderr = self.process.communicate(timeout=15)
            self.assertEqual(self.process.returncode, 0, stdout + stderr)
            self.process = None

    def wait_status(self, predicate):
        deadline = time.monotonic() + 10
        last = None
        while time.monotonic() < deadline:
            proc = subprocess.run([sys.executable, "-m", "omni_msg.cli", "registrations", "--state-dir", str(self.state)],
                                  env=self.env, text=True, capture_output=True, timeout=3, check=False)
            if proc.returncode == 0:
                last = json.loads(proc.stdout)
                if predicate(last):
                    return last
            time.sleep(0.05)
        self.fail(f"registration 未達預期狀態：{last}")

    def ready(self):
        return self.wait_status(lambda s: s["ready"] and len(s["members"]) == 4
                                and all(m["notification"]["delivery"] == "accepted" for m in s["members"]))

    def test_groups_receive_credentials_and_only_message_within_root(self):
        self.start()
        status = self.ready()
        members = {m["session_id"]: m for m in status["members"]}
        self.assertEqual(set(members), {"root_a", "child_a", "root_b", "child_b"})
        self.assertNotIn("/v1/sessions/unapproved", self.api.reads)
        tokens = set()
        for member in members.values():
            config = Path(member["client_config"])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)
            tokens.add(json.loads(config.read_text())["token"])
        self.assertEqual(len(tokens), 4)
        a, child, b = (members[key] for key in ["root_a", "child_a", "root_b"])
        listed = self.cli("--config", a["client_config"], "list")
        self.assertEqual(listed, {"self": "team-a", "contacts": [{"name": child["alias"], "session_id": "child_a"}]})
        ask = self.cli("--config", a["client_config"], "ask", child["alias"], "PING")
        self.assertEqual(ask["delivery"], "accepted")
        self.cli("--config", child["client_config"], "reply", ask["id"], "PONG")
        self.assertTrue(self.cli("--config", a["client_config"], "check", ask["id"])["answered"])
        self.cli("--config", a["client_config"], "send", b["alias"], "跨組", expected=2)
        self.cli("--config", b["client_config"], "send", a["alias"], "跨組", expected=2)

    def test_new_descendant_joins_and_archived_branch_loses_access(self):
        self.start()
        self.ready()
        with self.api.graph_lock:
            self.api.graph["grandchild_a"] = self.node("grandchild_a", "child_a")
        status = self.wait_status(lambda s: s["ready"] and len(s["members"]) == 5
                                  and all(m["notification"]["delivery"] == "accepted" for m in s["members"]))
        members = {m["session_id"]: m for m in status["members"]}
        grand = members["grandchild_a"]
        contacts = self.cli("--config", grand["client_config"], "list")["contacts"]
        self.assertEqual({c["session_id"] for c in contacts}, {"root_a", "child_a"})
        with self.api.graph_lock:
            self.api.graph["child_a"]["archived"] = True
        self.wait_status(lambda s: s["ready"] and len(s["members"]) == 3)
        self.cli("--config", grand["client_config"], "list", expected=2)
        self.cli("--config", members["child_a"]["client_config"], "list", expected=2)
        self.assertEqual(self.cli("--config", members["root_a"]["client_config"], "list")["contacts"], [])
        self.assertEqual(len(self.api.events), 5, "membership 變更不重發既有設定通知")

    def test_restart_preserves_credentials_receipts_and_notification_ids(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        before = {key: Path(m["client_config"]).read_bytes() for key, m in members.items()}
        root, child = members["root_a"], members["child_a"]
        ask = self.cli("--config", root["client_config"], "ask", child["alias"], "restart")
        self.cli("--config", child["client_config"], "reply", ask["id"], "saved")
        self.stop()
        self.start(provision=False)
        current = {m["session_id"]: m for m in self.ready()["members"]}
        self.assertEqual(before, {key: Path(m["client_config"]).read_bytes() for key, m in current.items()})
        self.assertEqual({key: m["notification"] for key, m in members.items()},
                         {key: m["notification"] for key, m in current.items()})
        self.assertEqual(len(self.api.events), 6)
        self.assertTrue(self.cli("--config", root["client_config"], "check", ask["id"])["answered"])

    def test_failed_discovery_revokes_snapshot_then_recovers_without_renotifying(self):
        self.start()
        member = self.ready()["members"][0]
        self.api.fail_reads = True
        self.wait_status(lambda s: not s["ready"] and s["error"] == "http_503")
        error = self.cli("--config", member["client_config"], "list", expected=2)
        self.assertEqual(error["error"], "http_503")
        self.api.fail_reads = False
        self.ready()
        self.assertEqual(self.cli("--config", member["client_config"], "list")["self"], member["alias"])
        self.assertEqual(len(self.api.events), 4)

    def test_uncertain_notification_is_not_retried_after_restart(self):
        self.api.ack = {"queued": False}
        self.start()
        status = self.wait_status(lambda s: s["ready"] and len(s["members"]) == 4
                                  and all(m["notification"]["delivery"] == "unknown" for m in s["members"]))
        self.assertEqual(len(self.api.events), 4)
        self.stop()
        self.api.ack = {"queued": True, "item_id": "must-not-retry"}
        self.start(provision=False)
        self.wait_status(lambda s: s["ready"] and len(s["members"]) == 4)
        self.assertEqual(len(self.api.events), 4)
        current = self.cli("registrations", "--state-dir", str(self.state))
        self.assertEqual([m["notification"] for m in status["members"]],
                         [m["notification"] for m in current["members"]])

    def test_reparenting_cannot_move_existing_credential_to_another_group(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        with self.api.graph_lock:
            self.api.graph["child_a"]["parent_session_id"] = "root_b"
        self.wait_status(lambda s: not s["ready"] and s["error"] == "registration_rebind_forbidden")
        self.cli("--config", members["child_a"]["client_config"], "list", expected=2)
        self.assertEqual(len(self.api.events), 4)

    def test_killed_inflight_notification_becomes_unknown_without_replay(self):
        self.api.post_gate.clear()
        self.start()
        deadline = time.monotonic() + 5
        while not self.api.events and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.api.events), 1)
        original_path = self.api.events[0][0]
        self.process.kill()
        self.process.communicate(timeout=5)
        self.process = None
        self.api.post_gate.set()
        self.start(provision=False)
        status = self.wait_status(lambda s: s["ready"] and len(s["members"]) == 4
                                  and all(m["notification"]["delivery"] in {"accepted", "unknown"} for m in s["members"]))
        uncertain = [m for m in status["members"] if m["notification"]["delivery"] == "unknown"]
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]["notification"]["error"], "registrar_restarted")
        self.assertEqual(sum(path == original_path for path, _ in self.api.events), 1)
        self.assertEqual(len(self.api.events), 4)


if __name__ == "__main__":
    unittest.main()
