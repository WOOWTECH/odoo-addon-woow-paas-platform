"""透過公開 CLI／HTTP seam 驗證 root admission，不以 agent 名稱當授權。"""
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_registration import RegistrationHarness, SessionAPI

from omni_msg.root_admission import MAX_BUNDLE_BYTES
from omni_msg.transport import MAX_METADATA_BYTES

PACKAGE = Path(__file__).resolve().parents[1]


def bundle_bytes(*, text="approved", mtime=1, reverse=False, executable=False, pax=None):
    files = [("config.yaml", b"spec_version: 1\nname: woow_issue_leader\n"),
             ("AGENTS.md", text.encode())]
    if reverse:
        files.reverse()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:") as archive:
        for name, body in files:
            info = tarfile.TarInfo(("./" if reverse else "") + name)
            info.size = len(body)
            info.mtime = mtime
            info.uid = mtime
            info.mode = 0o755 if executable else 0o644
            info.pax_headers = pax or {}
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(raw.getvalue(), mtime=mtime)


class FingerprintCLITests(unittest.TestCase):
    def test_repacking_preserves_fingerprint_but_instructions_and_execute_bits_do_not(self):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.tar.gz"
            results = []
            for raw in [bundle_bytes(), bundle_bytes(mtime=99, reverse=True),
                        bundle_bytes(text="different instructions"), bundle_bytes(executable=True)]:
                path.write_bytes(raw)
                result = subprocess.run([sys.executable, "-m", "omni_msg.cli", "fingerprint", "--bundle", str(path)],
                                        env=env, text=True, capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                results.append(json.loads(result.stdout)["fingerprint"])
            self.assertRegex(results[0], r"^omni-msg-bundle-v1:[a-f0-9]{64}$")
            self.assertEqual(results[0], results[1])
            self.assertNotEqual(results[0], results[2])
            self.assertNotEqual(results[0], results[3])

    def test_truncated_gzip_returns_safe_error_without_traceback(self):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.tar.gz"
            path.write_bytes(bundle_bytes()[:-3])
            result = subprocess.run([sys.executable, "-m", "omni_msg.cli", "fingerprint", "--bundle", str(path)],
                                    env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr), {"error": "invalid_bundle"})
            self.assertNotIn("Traceback", result.stderr)

    def test_untrusted_pax_execution_metadata_is_not_ignored(self):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pax.tar.gz"
            path.write_bytes(bundle_bytes(pax={"SCHILY.xattr.security.capability": "untrusted"}))
            result = subprocess.run([sys.executable, "-m", "omni_msg.cli", "fingerprint", "--bundle", str(path)],
                                    env=env, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stderr), {"error": "unsupported_bundle_metadata"})

    def test_unsafe_archive_entries_are_rejected_without_extraction(self):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE)}
        for name, kind in [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("./config.yaml", tarfile.REGTYPE), ("link", tarfile.SYMTYPE),
                           ("hardlink", tarfile.LNKTYPE), ("pipe", tarfile.FIFOTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode="w:") as archive:
                    good = tarfile.TarInfo("config.yaml")
                    good.size = 1
                    archive.addfile(good, io.BytesIO(b"x"))
                    bad = tarfile.TarInfo(name)
                    bad.type = kind
                    bad.linkname = "config.yaml" if kind in {tarfile.LNKTYPE, tarfile.SYMTYPE} else ""
                    archive.addfile(bad, io.BytesIO())
                path = Path(directory) / "bad.tar.gz"
                path.write_bytes(gzip.compress(raw.getvalue()))
                proc = subprocess.run([sys.executable, "-m", "omni_msg.cli", "fingerprint", "--bundle", str(path)],
                                      env=env, capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("error", json.loads(proc.stderr))
                self.assertEqual([p.name for p in Path(directory).iterdir()], ["bad.tar.gz"])


class RootSessionAPI(SessionAPI):
    def do_POST(self):
        if self.path == "/v1/sessions":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if set(body) != {"agent_id", "host_id", "workspace"}:
                return self.respond(422, {})
            with self.server.graph_lock:
                self.server.app_creates += 1
                sid = "app-root-" + str(self.server.app_creates)
                node = RegistrationHarness.node(sid, None)
                node.update(body, owner="alice")
                self.server.graph[sid] = node
            return self.respond(201, node)
        super().do_POST()

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/v1/sessions":
            with self.server.graph_lock:
                self.server.reads.append(self.path)
                if self.server.fail_reads:
                    return self.respond(503, {})
                query = parse_qs(url.query)
                self.server.root_queries.append(query)
                name = query.get("agent_name", [None])[0]
                ordered = list(self.server.graph.values())
                cursor = query.get("after", [None])[0]
                if cursor:
                    # Cursor 先於 archive 篩選定位，模擬 server 的 keyset pagination。
                    position = next((i for i, row in enumerate(ordered) if row["id"] == cursor), None)
                    if position is None:
                        return self.respond(400, {})
                    ordered = ordered[position + 1:]
                rows = [dict(row) for row in ordered
                        if row["parent_session_id"] is None and not row["archived"] and row["agent_name"] == name]
                page = rows[:self.server.page_size]
                last = cursor if getattr(self.server, "repeat_cursor", False) and cursor else (page[-1]["id"] if page else None)
                return self.respond(200, {"data": page, "has_more": len(rows) > len(page), "last_id": last})
        if url.path.endswith(("/agent", "/agent/contents")):
            with self.server.graph_lock:
                self.server.reads.append(self.path)
                sid = url.path.removeprefix("/v1/sessions/").split("/")[0]
                node = self.server.graph.get(sid)
                if node is None:
                    return self.respond(404, {})
                agent_id = node["agent_id"]
                if url.path.endswith("/contents"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.end_headers()
                    self.wfile.write(self.server.bundles[agent_id])
                    return
                return self.respond(200, {"id": agent_id, "name": node["agent_name"],
                                          "version": self.server.agent_versions[agent_id]})
        if url.path.startswith("/v1/sessions/") and url.path.count("/") == 3:
            with self.server.graph_lock:
                self.server.reads.append(self.path)
                sid = url.path.rsplit("/", 1)[-1]
                node = self.server.graph.get(sid)
                if node is None:
                    return self.respond(404, {})
                count = self.server.detail_counts.get(sid, 0) + 1
                self.server.detail_counts[sid] = count
                status = self.server.detail_statuses.get(sid)
                if status:
                    return self.respond(status, {})
                # 真實 detail endpoint 沒有 owner；owner 來自 authenticated list response。
                body = {k: v for k, v in node.items() if k != "owner"}
                size = self.server.detail_item_bytes.get(sid)
                if count in self.server.detail_oversize_calls.get(sid, set()):
                    size = MAX_METADATA_BYTES
                if size:
                    body["items"] = ["x" * size]
                return self.respond(200, body)
        super().do_GET()


class RootAdmissionCLITests(RegistrationHarness):
    def setUp(self):
        super().setUp()
        self.api.RequestHandlerClass = RootSessionAPI
        self.api.root_queries = []
        self.api.app_creates = 0
        self.api.page_size = 1
        self.api.bundles = {"trusted-agent": bundle_bytes(), "forged-agent": bundle_bytes(text="unapproved")}
        self.api.agent_versions = {"trusted-agent": 1, "forged-agent": 1}
        for sid, node in self.api.graph.items():
            node.update(owner="alice", host_id="host-one", agent_id="trusted-agent")
            if sid == "unapproved":
                node["agent_id"] = "forged-agent"
        approved = self.root / "approved.tar.gz"
        approved.write_bytes(self.api.bundles["trusted-agent"])
        fingerprint = self.cli("fingerprint", "--bundle", str(approved))["fingerprint"]
        self.policy_file = self.root / "auto-roots.json"
        self.policy = {"roots": {}, "rules": [{"agent_name": "woow_issue_leader", "owner": "alice",
                       "host_id": "host-one", "workspace": "/workspace", "bundle_fingerprint": fingerprint}]}
        self.policy_file.write_text(json.dumps(self.policy))

    def start(self, provision=True, poll_seconds="0.1"):
        if provision:
            self.cli("provision", "--directory", str(self.config), "--auto-roots", str(self.policy_file),
                     "--server-url", f"http://127.0.0.1:{self.api.server_port}", "--token-file", str(self.token),
                     "--port", str(self.port), "--poll-seconds", poll_seconds)
        super().start(provision=False)

    def test_same_profile_new_roots_are_admitted_as_distinct_groups(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        self.assertEqual(set(members), {"root_a", "child_a", "root_b", "child_b"})
        a, b, child = (members[key] for key in ["root_a", "root_b", "child_a"])
        self.assertNotEqual(a["alias"], b["alias"])
        self.assertTrue(a["alias"].startswith("root-"))
        self.assertEqual(self.cli("--config", a["client_config"], "list")["contacts"],
                         [{"name": child["alias"], "session_id": "child_a"}])
        ask = self.cli("--config", a["client_config"], "ask", child["alias"], "PING")
        self.cli("--config", child["client_config"], "reply", ask["id"], "PONG")
        self.assertTrue(self.cli("--config", a["client_config"], "check", ask["id"])["answered"])
        self.cli("--config", a["client_config"], "send", b["alias"], "cross root", expected=2)
        self.cli("--config", b["client_config"], "send", a["alias"], "cross root", expected=2)
        status = self.cli("registrations", "--state-dir", str(self.state))
        self.assertIn({"session_id": "unapproved", "reason": "bundle_fingerprint_mismatch"},
                      status["root_admission"]["rejected"])
        self.assertTrue(any(q.get("after") for q in self.api.root_queries), "必須走完分頁")
        self.assertTrue(all(q.get("kind") == ["default"] for q in self.api.root_queries))

    def test_detail_ignores_query_but_root_over_64k_is_still_admitted(self):
        self.api.detail_item_bytes["root_a"] = 70 * 1024
        self.api.detail_item_bytes["child_a"] = 70 * 1024
        self.start(poll_seconds="60")
        status = self.ready()
        self.assertIn("root_a", status["root_admission"]["matched"])
        detail_reads = [path for path in self.api.reads
                        if urlsplit(path).path.count("/") == 3]
        self.assertTrue(detail_reads)
        self.assertTrue(all(parse_qs(urlsplit(path).query) == {"include_items": ["false"]}
                            for path in detail_reads))

    def test_session_agent_and_bundle_failures_have_distinct_reasons(self):
        failures = {
            "bad_session": "trusted-agent",
            "bad_agent": "bad-agent",
            "bad_bundle": "bad-bundle",
        }
        with self.api.graph_lock:
            for sid, agent_id in failures.items():
                node = self.node(sid, None)
                node.update(owner="alice", host_id="host-one", agent_id=agent_id)
                self.api.graph[sid] = node
            self.api.detail_item_bytes["bad_session"] = MAX_METADATA_BYTES
            self.api.agent_versions["bad-agent"] = 0
            self.api.bundles["bad-agent"] = bundle_bytes()
            self.api.agent_versions["bad-bundle"] = 1
            self.api.bundles["bad-bundle"] = b"not-a-gzip-bundle"
        self.start(poll_seconds="60")
        status = self.ready()
        rejected = {row["session_id"]: row for row in status["root_admission"]["rejected"]}
        self.assertEqual(
            (rejected["bad_session"]["reason"], rejected["bad_session"]["phase"]),
            ("session_metadata_unverifiable", "session_metadata"),
        )
        self.assertEqual(rejected["bad_session"]["limit"], MAX_METADATA_BYTES)
        self.assertEqual(rejected["bad_session"]["read_bytes"], MAX_METADATA_BYTES + 1)
        self.assertEqual(
            (rejected["bad_agent"]["reason"], rejected["bad_agent"]["phase"]),
            ("agent_metadata_unverifiable", "agent_metadata"),
        )
        self.assertEqual(rejected["bad_agent"]["status"], 502)
        self.assertEqual(rejected["bad_agent"]["limit"], MAX_METADATA_BYTES)
        self.assertIsNone(rejected["bad_agent"]["read_bytes"])
        self.assertEqual(
            (rejected["bad_bundle"]["reason"], rejected["bad_bundle"]["phase"]),
            ("bundle_unverifiable", "bundle"),
        )
        self.assertEqual(rejected["bad_bundle"]["status"], 502)
        self.assertEqual(rejected["bad_bundle"]["limit"], MAX_BUNDLE_BYTES)
        self.assertEqual(rejected["bad_bundle"]["read_bytes"], len(b"not-a-gzip-bundle"))

    def test_automatic_root_second_detail_failure_is_isolated(self):
        self.api.detail_oversize_calls["root_a"] = {2}
        self.start(poll_seconds="60")
        status = self.wait_status(
            lambda value: value["ready"]
            and {m["session_id"] for m in value["members"]} == {"root_b", "child_b"},
        )
        self.assertIn("root_a", status["root_admission"]["matched"])
        self.assertEqual(status["session_rejections"], [{
            "session_id": "root_a",
            "reason": "session_metadata_unverifiable",
            "phase": "session_metadata",
            "status": 502,
            "limit": MAX_METADATA_BYTES,
            "read_bytes": MAX_METADATA_BYTES + 1,
        }])
        self.assertEqual(len(list((self.state / "clients").glob("*.json"))), 2)

    def test_app_json_created_root_joins_without_editing_policy(self):
        from urllib.request import Request, urlopen
        self.start()
        original = self.ready()
        original_files = {m["session_id"]: Path(m["client_config"]).read_bytes() for m in original["members"]}
        policy_before = (self.config / "server.json").read_bytes()
        # 模擬 app 的既有 agent_id + host/workspace JSON create，不把新 ID 加到 policy。
        request = Request(f"http://127.0.0.1:{self.api.server_port}/v1/sessions",
                          data=json.dumps({"agent_id": "trusted-agent", "host_id": "host-one", "workspace": "/workspace"}).encode(),
                          headers={"Authorization": "Bearer upstream-secret", "Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            created = json.load(response)
        status = self.wait_status(lambda s: s["ready"] and len(s["members"]) == 5
                                  and all(m["notification"]["delivery"] == "accepted" for m in s["members"]))
        members = {m["session_id"]: m for m in status["members"]}
        added = members[created["id"]]
        self.assertEqual(self.cli("--config", added["client_config"], "list")["contacts"], [])
        self.assertEqual(policy_before, (self.config / "server.json").read_bytes())
        for sid, raw in original_files.items():
            self.assertEqual(raw, Path(members[sid]["client_config"]).read_bytes())
        self.assertEqual(len(self.api.events), 5)

    def test_owner_host_workspace_and_edit_permission_are_checked_independently(self):
        invalid = {"other_owner": {"owner": "bob"}, "missing_owner": {"owner": None},
                   "other_host": {"host_id": "host-two"}, "other_workspace": {"workspace": "/elsewhere"},
                   "read_only": {"permission_level": 1}}
        with self.api.graph_lock:
            for sid, change in invalid.items():
                node = self.node(sid, None)
                node.update(owner="alice", host_id="host-one", agent_id="trusted-agent")
                node.update(change)
                self.api.graph[sid] = node
        self.start()
        status = self.ready()
        self.assertEqual({m["session_id"] for m in status["members"]}, {"root_a", "child_a", "root_b", "child_b"})
        rejected = {r["session_id"]: r["reason"] for r in status["root_admission"]["rejected"]}
        self.assertEqual(rejected["other_owner"], "owner_mismatch")
        self.assertEqual(rejected["missing_owner"], "owner_mismatch")
        self.assertEqual(rejected["other_host"], "host_id_mismatch")
        self.assertEqual(rejected["other_workspace"], "workspace_mismatch")
        self.assertEqual(rejected["read_only"], "edit_permission_required")
        self.assertEqual(len(list((self.state / "clients").glob("*.json"))), 4)

    def test_changed_bundle_revokes_then_repacking_restores_without_rotating_credentials(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        root = members["root_a"]
        original = Path(root["client_config"]).read_bytes()
        with self.api.graph_lock:
            self.api.bundles["trusted-agent"] = bundle_bytes(text="untrusted new code")
            self.api.agent_versions["trusted-agent"] += 1
        self.wait_status(lambda s: s["ready"] and not s["members"])
        self.cli("--config", root["client_config"], "list", expected=2)
        with self.api.graph_lock:
            self.api.bundles["trusted-agent"] = bundle_bytes(mtime=100, reverse=True)
            self.api.agent_versions["trusted-agent"] += 1
        restored = self.ready()
        self.assertEqual(original, Path(root["client_config"]).read_bytes())
        self.assertEqual(len(self.api.events), 4)
        self.assertEqual(members["root_a"]["notification"],
                         next(m["notification"] for m in restored["members"] if m["session_id"] == "root_a"))

    def test_explicit_root_migration_preserves_alias_credentials_and_receipts(self):
        RegistrationHarness.start(self)
        original = {m["session_id"]: m for m in self.ready()["members"]}
        before = {sid: Path(m["client_config"]).read_bytes() for sid, m in original.items()}
        a, child = original["root_a"], original["child_a"]
        ask = self.cli("--config", a["client_config"], "ask", child["alias"], "migration")
        self.cli("--config", child["client_config"], "reply", ask["id"], "kept")
        self.stop()
        path = self.config / "server.json"
        config = json.loads(path.read_text())
        config["registration"]["roots"] = {}
        config["registration"]["auto_roots"] = self.policy["rules"]
        path.write_text(json.dumps(config))
        self.start(provision=False)
        migrated = {m["session_id"]: m for m in self.ready()["members"]}
        self.assertEqual(original, migrated)
        self.assertEqual(before, {sid: Path(m["client_config"]).read_bytes() for sid, m in migrated.items()})
        self.assertEqual(len(self.api.events), 6)
        self.assertTrue(self.cli("--config", a["client_config"], "check", ask["id"])["answered"])
        self.stop()
        self.start(provision=False)
        self.ready()
        self.assertEqual(len(self.api.events), 6)

    def test_owner_change_revokes_only_that_root_and_archive_removes_its_branch(self):
        self.start()
        members = {m["session_id"]: m for m in self.ready()["members"]}
        with self.api.graph_lock:
            self.api.graph["root_a"]["owner"] = "bob"
        self.wait_status(lambda s: s["ready"] and {m["session_id"] for m in s["members"]} == {"root_b", "child_b"})
        self.cli("--config", members["root_a"]["client_config"], "list", expected=2)
        self.cli("--config", members["child_a"]["client_config"], "list", expected=2)
        contacts = self.cli("--config", members["root_b"]["client_config"], "list")["contacts"]
        self.assertEqual({c["session_id"] for c in contacts}, {"child_b"})
        with self.api.graph_lock:
            self.api.graph["root_b"]["archived"] = True
        self.wait_status(lambda s: s["ready"] and not s["members"])
        self.cli("--config", members["child_b"]["client_config"], "list", expected=2)
        self.assertEqual(len(self.api.events), 4)

    def test_unavailable_discovery_fails_closed_without_stale_admission(self):
        self.start()
        member = self.ready()["members"][0]
        self.api.fail_reads = True
        self.wait_status(lambda s: not s["ready"] and s["error"] == "http_503")
        self.cli("--config", member["client_config"], "list", expected=2)
        self.api.fail_reads = False
        self.ready()
        self.assertEqual(len(self.api.events), 4)

    def test_repeated_cursor_never_publishes_partial_membership(self):
        self.api.repeat_cursor = True
        self.start()
        self.wait_status(lambda s: not s["ready"] and s["error"] == "invalid_root_session_cursor")
        self.assertEqual(len(self.api.events), 0)
        self.assertEqual(list((self.state / "clients").glob("*.json")), [])

    def test_root_limit_is_bounded_before_credentials_are_issued(self):
        with self.api.graph_lock:
            self.api.graph.clear()
            for n in range(17):
                sid = f"root_{n}"
                node = self.node(sid, None)
                node.update(owner="alice", host_id="host-one", agent_id="trusted-agent")
                self.api.graph[sid] = node
        self.start()
        self.wait_status(lambda s: not s["ready"] and s["error"] == "duplicate_or_oversized_root_admission")
        self.assertEqual(self.api.events, [])
        self.assertEqual(list((self.state / "clients").glob("*.json")), [])

    def test_invalid_policy_does_not_fall_back_to_name_only(self):
        for change in [{"owner": None}, {"bundle_fingerprint": "0" * 64}, {"workspace": "relative"},
                       {"allow_any_owner": True}, {"host_id": "*"}]:
            with self.subTest(change=change):
                policy = {"roots": {}, "rules": [{**self.policy["rules"][0], **change}]}
                self.policy_file.write_text(json.dumps(policy))
                self.cli("provision", "--directory", str(self.config), "--auto-roots", str(self.policy_file),
                         "--server-url", f"http://127.0.0.1:{self.api.server_port}", "--token-file", str(self.token), expected=2)
                self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
