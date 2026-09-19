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
sys.path.insert(0, str(PACKAGE))

from omni_msg.transport import MAX_BYTES, MAX_METADATA_BYTES


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
            return None
        return len(raw)

    def drop(self):
        # 模擬 upstream 斷線：不送任何 HTTP 回應就關閉連線（client 端沒有 status code）。
        self.close_connection = True

    def do_GET(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.path)
        path = url.path
        with self.server.graph_lock:
            self.server.reads.append(self.path)
            if self.server.drop_reads:
                return self.drop()
            if self.server.fail_reads:
                return self.respond(503, {})
            sid = path.removeprefix("/v1/sessions/").split("/")[0]
            node = self.server.graph.get(sid)
            if node is None:
                return self.respond(404, {})
            if path.endswith("/child_sessions"):
                if self.server.fail_child_reads:
                    return self.respond(503, {})
                children = [{"id": key} for key, row in self.server.graph.items()
                            if row["parent_session_id"] == sid]
                return self.respond(200, {"data": children, "has_more": False})
            return self.detail(sid, dict(node), url.query)

    def detail(self, sid, body, query):
        """模擬 session detail；可注入狀態碼、欄位覆寫、最近 items 與精確 response bytes。"""
        from urllib.parse import parse_qs
        count = self.server.detail_counts.get(sid, 0) + 1
        self.server.detail_counts[sid] = count
        status = self.server.detail_statuses.get(sid)
        if status:
            return self.respond(status, {})
        body.update(self.server.detail_overrides.get(sid, {}))
        size = self.server.detail_item_bytes.get(sid)
        if count in self.server.detail_oversize_calls.get(sid, set()):
            size = MAX_METADATA_BYTES
        if size:
            body["items"] = ["x" * size]
        recent = self.server.detail_recent_items.get(sid)
        if recent:
            # 模擬 omnigent 預設附最近 committed items；honor=False 代表 server 忽略 query。
            item_count, item_bytes, honor = recent
            omitted = honor and parse_qs(query).get("include_items") == ["false"]
            body["items"] = [] if omitted else [
                {"type": "message", "content": "x" * item_bytes} for _ in range(item_count)
            ]
        exact = self.server.detail_exact_bytes.get(sid)
        if exact is not None:
            body["padding"] = ""
            body["padding"] = "x" * (exact - len(json.dumps(body).encode()))
        written = self.respond(200, body)
        self.server.detail_sizes.setdefault(sid, []).append(written)
        return written

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.graph_lock:
            self.server.events.append((self.path, data))
        self.server.post_gate.wait(8)
        self.respond(202, self.server.ack)


class RegistrationHarness(unittest.TestCase):
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
        self.api.drop_reads = False
        self.api.fail_child_reads = False
        self.api.detail_counts = {}
        self.api.detail_statuses = {}
        self.api.detail_overrides = {}
        self.api.detail_item_bytes = {}
        self.api.detail_recent_items = {}
        self.api.detail_exact_bytes = {}
        self.api.detail_oversize_calls = {}
        self.api.detail_sizes = {}
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

    @staticmethod
    def member_ids(status):
        return {m["session_id"] for m in status["members"]}

    def wait_members(self, expected):
        return self.wait_status(
            lambda value: value["ready"] and self.member_ids(value) == expected
            and all(m["notification"]["delivery"] == "accepted" for m in value["members"]),
        )

    def add_explicit_root(self, alias, sid):
        with self.api.graph_lock:
            self.api.graph[sid] = self.node(sid, None)
        roots = json.loads(self.roots.read_text())
        roots[alias] = {"session_id": sid, "workspace": "/workspace"}
        self.roots.write_text(json.dumps(roots))

    def client_error(self, member):
        return self.cli("--config", member["client_config"], "list", expected=2)["error"]

    def issued_clients(self):
        return sorted(path.name for path in (self.state / "clients").glob("*.json"))

    @staticmethod
    def rejection(sid, reason, status, error, *, read_bytes=0):
        return {"session_id": sid, "reason": reason, "phase": "session_metadata", "status": status,
                "limit": MAX_METADATA_BYTES, "read_bytes": read_bytes, "error": error}


class RegistrationCLITests(RegistrationHarness):
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

    def test_detail_query_omits_items_and_uses_metadata_limit(self):
        self.api.detail_item_bytes["root_a"] = 70 * 1024
        self.api.detail_item_bytes["child_a"] = 70 * 1024
        self.start()
        status = self.ready()
        self.assertEqual({m["session_id"] for m in status["members"]},
                         {"root_a", "child_a", "root_b", "child_b"})
        from urllib.parse import parse_qs, urlsplit
        detail_reads = [path for path in self.api.reads
                        if urlsplit(path).path.count("/") == 3]
        self.assertTrue(detail_reads)
        self.assertTrue(all(parse_qs(urlsplit(path).query) == {"include_items": ["false"]}
                            for path in detail_reads))

    def test_detail_honoring_include_items_omits_recent_items_for_root_and_descendant(self):
        with self.api.graph_lock:
            for sid in ["root_a", "child_a"]:
                # 100 個約 2 KiB 的 committed items，未帶 query 時總量超過 64 KiB。
                self.api.detail_recent_items[sid] = (100, 2048, True)
        self.start()
        status = self.ready()
        self.assertEqual(self.member_ids(status), {"root_a", "child_a", "root_b", "child_b"})
        self.assertEqual(status["session_rejections"], [])
        from urllib.parse import parse_qs, urlsplit
        from urllib.request import urlopen
        with self.api.graph_lock:
            detail_reads = [path for path in self.api.reads
                            if urlsplit(path).path.count("/") == 3]
            served = {sid: list(self.api.detail_sizes[sid]) for sid in ["root_a", "child_a"]}
        self.assertTrue(detail_reads)
        self.assertTrue(all(parse_qs(urlsplit(path).query) == {"include_items": ["false"]}
                            for path in detail_reads))
        for sid, sizes in served.items():
            with self.subTest(sid=sid):
                self.assertTrue(sizes)
                self.assertTrue(all(size < MAX_BYTES for size in sizes), sizes)
        # 對照組：同一個 fake 不帶 query 時確實會附上超過 64 KiB 的 items。
        with urlopen(f"http://127.0.0.1:{self.api.server_port}/v1/sessions/root_a", timeout=5) as response:
            self.assertGreater(len(response.read()), MAX_BYTES)

    def test_detail_ignoring_include_items_between_64k_and_1m_is_admitted(self):
        with self.api.graph_lock:
            for sid in ["root_a", "child_a"]:
                self.api.detail_recent_items[sid] = (100, 2048, False)
        self.start()
        status = self.ready()
        self.assertEqual(self.member_ids(status), {"root_a", "child_a", "root_b", "child_b"})
        self.assertEqual(status["session_rejections"], [])
        with self.api.graph_lock:
            served = {sid: list(self.api.detail_sizes[sid]) for sid in ["root_a", "child_a"]}
        for sid, sizes in served.items():
            with self.subTest(sid=sid):
                self.assertTrue(sizes)
                self.assertTrue(all(MAX_BYTES < size < MAX_METADATA_BYTES for size in sizes), sizes)

    def test_detail_ignoring_include_items_over_1m_rejects_only_that_root(self):
        with self.api.graph_lock:
            # 100 個約 11 KiB 的 items，總量超過 1 MiB metadata 上限。
            self.api.detail_recent_items["root_a"] = (100, 11 * 1024, False)
        self.start()
        status = self.wait_members({"root_b", "child_b"})
        self.assertEqual(status["session_rejections"], [self.rejection(
            "root_a", "session_metadata_unverifiable", 502, "response_too_large",
            read_bytes=MAX_METADATA_BYTES + 1,
        )])
        self.assertNotEqual(status["session_rejections"][0]["reason"], "bundle_unverifiable")
        members = {m["session_id"]: m for m in status["members"]}
        self.assertEqual(
            self.cli("--config", members["root_b"]["client_config"], "list")["contacts"],
            [{"name": members["child_b"]["alias"], "session_id": "child_b"}],
        )
        self.assertEqual(len(self.issued_clients()), 2)

    def test_registrar_root_detail_size_boundaries_are_exact(self):
        # 64 KiB 是訊息 ACK 上限，不得套用到 metadata；1 MiB 才是 metadata 上限。
        sizes = {"root_a": 65536, "root_b": 65537,
                 "root_c": MAX_METADATA_BYTES, "root_d": MAX_METADATA_BYTES + 1}
        self.add_explicit_root("team-c", "root_c")
        self.add_explicit_root("team-d", "root_d")
        with self.api.graph_lock:
            self.api.detail_exact_bytes.update(sizes)
        self.start()
        status = self.wait_members({"root_a", "child_a", "root_b", "child_b", "root_c"})
        self.assertEqual(status["session_rejections"], [self.rejection(
            "root_d", "session_metadata_unverifiable", 502, "response_too_large",
            read_bytes=MAX_METADATA_BYTES + 1,
        )])
        with self.api.graph_lock:
            served = {sid: list(self.api.detail_sizes[sid]) for sid in sizes}
        for sid, size in sizes.items():
            with self.subTest(sid=sid, size=size):
                self.assertTrue(served[sid])
                self.assertEqual(set(served[sid]), {size})
        self.assertEqual(len(self.issued_clients()), 5)

    def test_explicit_root_detail_failure_is_isolated_from_other_root(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        with self.api.graph_lock:
            self.api.detail_statuses["root_a"] = 503
        status = self.wait_status(
            lambda value: value["ready"]
            and {m["session_id"] for m in value["members"]} == {"root_b", "child_b"},
        )
        self.assertEqual(status["session_rejections"], [{
            "session_id": "root_a",
            "reason": "session_metadata_unverifiable",
            "phase": "session_metadata",
            "status": 503,
            "limit": MAX_METADATA_BYTES,
            "read_bytes": 0,
            "error": "http_503",
        }])
        self.assertEqual(
            self.cli("--config", members["root_a"]["client_config"], "list", expected=2)["error"],
            "http_401",
        )
        self.assertEqual(
            self.cli("--config", members["child_a"]["client_config"], "list", expected=2)["error"],
            "http_401",
        )
        self.assertEqual(
            {row["session_id"] for row in self.cli(
                "--config", members["root_b"]["client_config"], "list",
            )["contacts"]},
            {"child_b"},
        )

    def test_explicit_root_oversize_403_404_do_not_block_other_root(self):
        self.add_explicit_root("team-c", "root_c")
        self.add_explicit_root("team-d", "root_d")
        with self.api.graph_lock:
            self.api.detail_item_bytes["root_a"] = MAX_METADATA_BYTES
            self.api.detail_statuses["root_c"] = 403
            self.api.detail_statuses["root_d"] = 404
        self.start()
        status = self.wait_members({"root_b", "child_b"})
        rejected = {row["session_id"]: row for row in status["session_rejections"]}
        self.assertEqual(rejected, {
            "root_a": self.rejection("root_a", "session_metadata_unverifiable", 502,
                                     "response_too_large", read_bytes=MAX_METADATA_BYTES + 1),
            "root_c": self.rejection("root_c", "session_access_unavailable", 403, "http_403"),
            "root_d": self.rejection("root_d", "session_access_unavailable", 404, "http_404"),
        })
        # 被跳過的 root 不配發 credential，也不投遞設定通知。
        self.assertEqual(len(self.issued_clients()), 2)
        self.assertEqual(len(self.api.events), 2)

    def test_skipped_root_recovers_with_same_credential_without_renotifying(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        before = {sid: Path(m["client_config"]).read_bytes() for sid, m in members.items()}
        issued = self.issued_clients()
        with self.api.graph_lock:
            self.api.detail_statuses["root_a"] = 503
        self.wait_members({"root_b", "child_b"})
        self.assertEqual(self.client_error(members["root_a"]), "http_401")
        with self.api.graph_lock:
            del self.api.detail_statuses["root_a"]
        restored = {m["session_id"]: m for m in self.ready()["members"]}
        self.assertEqual(before, {sid: Path(m["client_config"]).read_bytes() for sid, m in restored.items()})
        self.assertEqual(issued, self.issued_clients())
        self.assertEqual({sid: m["notification"] for sid, m in members.items()},
                         {sid: m["notification"] for sid, m in restored.items()})
        self.assertEqual(
            self.cli("--config", members["root_a"]["client_config"], "list")["contacts"],
            [{"name": members["child_a"]["alias"], "session_id": "child_a"}],
        )
        self.assertEqual(len(self.api.events), 4)

    def assert_descendant_failure_blocks_only_its_subtree(self, inject, expected):
        with self.api.graph_lock:
            self.api.graph["grandchild_a"] = self.node("grandchild_a", "child_a")
        self.start()
        status = self.wait_members({"root_a", "child_a", "grandchild_a", "root_b", "child_b"})
        members = {m["session_id"]: m for m in status["members"]}
        with self.api.graph_lock:
            inject(self.api)
        status = self.wait_members({"root_a", "root_b", "child_b"})
        self.assertEqual(status["session_rejections"], [expected])
        self.assertEqual(
            self.cli("--config", members["root_a"]["client_config"], "list")["contacts"], [],
        )
        self.assertEqual(
            self.cli("--config", members["root_b"]["client_config"], "list")["contacts"],
            [{"name": members["child_b"]["alias"], "session_id": "child_b"}],
        )
        for sid in ["child_a", "grandchild_a"]:
            with self.subTest(sid=sid):
                self.assertEqual(self.client_error(members[sid]), "http_401")
        self.assertEqual(len(self.api.events), 5)

    def test_unverifiable_descendant_blocks_its_subtree_only(self):
        self.assert_descendant_failure_blocks_only_its_subtree(
            lambda api: api.detail_statuses.__setitem__("child_a", 503),
            self.rejection("child_a", "session_metadata_unverifiable", 503, "http_503"),
        )

    def test_oversized_descendant_blocks_its_subtree_only(self):
        self.assert_descendant_failure_blocks_only_its_subtree(
            lambda api: api.detail_exact_bytes.__setitem__("child_a", MAX_METADATA_BYTES + 1),
            self.rejection("child_a", "session_metadata_unverifiable", 502, "response_too_large",
                           read_bytes=MAX_METADATA_BYTES + 1),
        )

    def test_descendant_lineage_mismatch_fails_closed_for_every_group(self):
        self.start()
        members = self.ready()["members"]
        with self.api.graph_lock:
            # child listing 仍說 child_a 在 root_a 底下，但 detail 自稱屬於 root_b。
            self.api.detail_overrides["child_a"] = {"parent_session_id": "root_b"}
        status = self.wait_status(lambda s: not s["ready"] and s["error"] == "session_lineage_mismatch")
        self.assertEqual(status["members"], [])
        for member in members:
            with self.subTest(sid=member["session_id"]):
                self.assertEqual(self.client_error(member), "http_503")
        with self.api.graph_lock:
            del self.api.detail_overrides["child_a"]
        self.ready()
        self.assertEqual(len(self.api.events), 4)

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

    def test_child_inventory_failure_revokes_snapshot_then_recovers_without_renotifying(self):
        self.start()
        member = self.ready()["members"][0]
        self.api.fail_child_reads = True
        self.wait_status(lambda s: not s["ready"] and s["error"] == "http_503")
        error = self.cli("--config", member["client_config"], "list", expected=2)
        self.assertEqual(error["error"], "http_503")
        self.api.fail_child_reads = False
        self.ready()
        self.assertEqual(self.cli("--config", member["client_config"], "list")["self"], member["alias"])
        self.assertEqual(len(self.api.events), 4)

    def test_dropped_upstream_connections_revoke_snapshot_then_recover(self):
        self.start()
        members = self.ready()["members"]
        before = {m["session_id"]: Path(m["client_config"]).read_bytes() for m in members}
        with self.api.graph_lock:
            self.api.drop_reads = True
        status = self.wait_status(lambda s: not s["ready"] and s["error"] == "transport_unknown")
        self.assertEqual(status["members"], [])
        for member in members:
            with self.subTest(sid=member["session_id"]):
                self.assertEqual(self.client_error(member), "http_503")
        with self.api.graph_lock:
            self.api.drop_reads = False
        restored = self.ready()["members"]
        self.assertEqual(before, {m["session_id"]: Path(m["client_config"]).read_bytes() for m in restored})
        for member in members:
            with self.subTest(sid=member["session_id"]):
                self.assertEqual(self.cli("--config", member["client_config"], "list")["self"], member["alias"])
        self.assertEqual(len(self.api.events), 4)

    def test_no_readable_root_with_upstream_5xx_fails_closed(self):
        self.start()
        members = self.ready()["members"]
        with self.api.graph_lock:
            # 一個 root 5xx、另一個 404：本輪沒有任何可讀 root，且有 upstream 不可用的證據。
            self.api.detail_statuses.update({"root_a": 503, "root_b": 404})
        status = self.wait_status(lambda s: not s["ready"] and s["error"] == "http_503")
        self.assertEqual(status["members"], [])
        for member in members:
            with self.subTest(sid=member["session_id"]):
                self.assertEqual(self.client_error(member), "http_503")

    def test_all_roots_access_unavailable_is_revocation_not_outage(self):
        self.start()
        members = self.ready()["members"]
        with self.api.graph_lock:
            # 403/404 代表 server 有回應：屬正常撤權，不是 upstream 不可用。
            self.api.detail_statuses.update({"root_a": 403, "root_b": 404})
        status = self.wait_status(lambda s: s["ready"] and not s["members"])
        self.assertEqual(status["session_rejections"], [
            self.rejection("root_a", "session_access_unavailable", 403, "http_403"),
            self.rejection("root_b", "session_access_unavailable", 404, "http_404"),
        ])
        for member in members:
            with self.subTest(sid=member["session_id"]):
                self.assertEqual(self.client_error(member), "http_401")

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
