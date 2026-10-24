"""woow-security-sweep-setup 的 setup.py 單元測試（#1978；不打網路、不需 omnigent）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "sweep_setup", REPO / ".agents/skills/woow-security-sweep-setup/scripts/setup.py")
su = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(su)

ANCHOR = "a" * 32
AGENT = {"id": "agent123", "name": "woow_security_sweep", "version": 1, "harness": "claude-native"}


def tar_of(files: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeApi:
    """依路徑回傳固定答案；記錄 PUT。"""

    def __init__(self, routes: dict[str, tuple[int, object]]):
        self.routes = dict(routes)
        self.puts: list[tuple[str, bytes]] = []
        self.put_result: tuple[int, object] = (200, AGENT)
        self.after_put: dict[str, tuple[int, object]] = {}

    def get(self, path):
        return self.routes.get(path, (404, {}))

    def put_bundle(self, path, data):
        self.puts.append((path, data))
        self.routes.update(self.after_put)
        return self.put_result


def agent_routes(level=4, agent=None, contents=b""):
    return {f"/v1/sessions/{ANCHOR}/agent": (200, agent or AGENT),
            f"/v1/sessions/{ANCHOR}": (200, {"permission_level": level}),
            f"/v1/sessions/{ANCHOR}/agent/contents": (200, contents),
            "/v1/me": (200, {"user_id": "eugene"})}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.sdir = su.state_dir(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, checks, cid):
        return next(c for c in checks if c["id"] == cid)["status"]


class IdentityHostTest(Base):
    def test_foreign_host_fails(self):
        api = FakeApi({"/v1/me": (200, {"user_id": "elmo"}), "/v1/hosts/h1": (403, {"detail": "not your host"})})
        checks, user = su.check_identity_and_host(api, "h1")
        self.assertEqual((user, self.status(checks, "host")), ("elmo", su.FAIL))
        self.assertIn("自己的主機", next(c for c in checks if c["id"] == "host")["fix"])

    def test_owned_online_host_ok(self):
        api = FakeApi({"/v1/me": (200, {"user_id": "eugene"}),
                       "/v1/hosts/h1": (200, {"owner": "eugene", "status": "online", "name": "pi3"})})
        checks, _ = su.check_identity_and_host(api, "h1")
        self.assertEqual(self.status(checks, "host"), su.OK)

    def test_offline_host_warns(self):
        api = FakeApi({"/v1/me": (200, {"user_id": "eugene"}),
                       "/v1/hosts/h1": (200, {"owner": "eugene", "status": "offline"})})
        checks, _ = su.check_identity_and_host(api, "h1")
        self.assertEqual(self.status(checks, "host"), su.WARN)


class AgentTest(Base):
    def record(self):
        su.save_agent_record(self.sdir, {"anchor_session": ANCHOR, "agent_id": "agent123", "recorded_by": "eugene"})

    def test_missing_record_fails_with_config_path(self):
        with mock.patch.dict(os.environ, {"OMNIGENT_RUNNER_WORKSPACE": str(self.ws)}):
            checks, record = su.check_agent(FakeApi({}), self.sdir, "eugene", self.ws)
        self.assertIsNone(record)
        self.assertEqual(checks[0]["status"], su.FAIL)
        self.assertIn('config_path="paas/woow_paas_platform/.omnigent/woow-security-sweep"', checks[0]["fix"])

    def test_owner_can_update(self):
        self.record()
        checks, _ = su.check_agent(FakeApi(agent_routes(level=4)), self.sdir, "eugene", self.ws)
        self.assertEqual((self.status(checks, "agent"), self.status(checks, "agent_owner")), (su.OK, su.OK))

    def test_shared_reader_warns_not_fails(self):
        self.record()
        checks, _ = su.check_agent(FakeApi(agent_routes(level=1)), self.sdir, "elmo", self.ws)
        self.assertEqual((self.status(checks, "agent"), self.status(checks, "agent_owner")), (su.OK, su.WARN))

    def test_unreadable_anchor_fails(self):
        self.record()
        checks, _ = su.check_agent(FakeApi({}), self.sdir, "elmo", self.ws)
        self.assertEqual(checks[0]["status"], su.FAIL)

    def test_mismatched_agent_id_fails(self):
        self.record()
        checks, _ = su.check_agent(FakeApi(agent_routes(agent=dict(AGENT, id="other"))), self.sdir, "eugene", self.ws)
        self.assertEqual(checks[0]["status"], su.FAIL)

    def test_config_path_relative_to_calling_session(self):
        with mock.patch.dict(os.environ, {"OMNIGENT_RUNNER_WORKSPACE": str(self.ws.parent)}):
            self.assertEqual(su.config_path_for_session(self.ws),
                             f"{self.ws.name}/paas/woow_paas_platform/.omnigent/woow-security-sweep")


class RecordAgentTest(Base):
    def test_rejects_bad_session_id(self):
        with self.assertRaises(su.SetupError):
            su.record_agent(FakeApi({}), self.ws, "not-hex", "eugene")

    def test_rejects_other_agent_name(self):
        with self.assertRaises(su.SetupError):
            su.record_agent(FakeApi(agent_routes(agent=dict(AGENT, name="woow_issue_leader"))), self.ws, ANCHOR, "eugene")

    def test_writes_record_with_update_right(self):
        rec = su.record_agent(FakeApi(agent_routes(level=1)), self.ws, ANCHOR, "elmo")
        self.assertEqual((rec["agent_id"], rec["can_update"]), ("agent123", False))
        self.assertEqual(su.load_agent_record(self.sdir)["anchor_session"], ANCHOR)


class ScheduleTest(Base):
    def task(self, **kw):
        base = {"id": "t1", "name": "woow-security-sweep 每日", "agent_id": "agent123", "host_id": "h1",
                "workspace": str(self.ws), "permission_mode": "auto", "max_cost_usd": 30.0, "state": "active",
                "rrule": "FREQ=WEEKLY;BYDAY=TU,WE;BYHOUR=8", "prompt": "targets:\n  - /r/a\nmode: incremental\n"}
        return dict(base, **kw)

    def run_check(self, tasks):
        api = FakeApi({"/v1/scheduled-tasks": (200, {"scheduled_tasks": tasks})})
        return su.check_schedules(api, {"agent_id": "agent123"}, "h1", self.ws)

    def test_healthy(self):
        self.assertEqual(self.status(self.run_check([self.task()]), "schedule:t1"), su.OK)

    def test_stale_agent_and_missing_cost_fail(self):
        checks = self.run_check([self.task(agent_id="old", max_cost_usd=None, permission_mode=None)])
        c = next(c for c in checks if c["id"] == "schedule:t1")
        self.assertEqual(c["status"], su.FAIL)
        for word in ("舊的", "費用上限", "permission_mode"):
            self.assertIn(word, c["detail"])

    def test_foreign_host_fails(self):
        self.assertEqual(self.status(self.run_check([self.task(host_id="h2")]), "schedule:t1"), su.FAIL)

    def test_same_day_same_target_overlap(self):
        checks = self.run_check([self.task(), self.task(id="t2", name="woow-security-sweep 每週",
                                                        rrule="FREQ=WEEKLY;BYDAY=WE;BYHOUR=9")])
        self.assertEqual(self.status(checks, "schedule_overlap"), su.FAIL)

    def test_different_days_no_overlap(self):
        checks = self.run_check([self.task(), self.task(id="t2", rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=8")])
        self.assertFalse([c for c in checks if c["id"] == "schedule_overlap"])

    def test_no_sweep_schedule_is_skip(self):
        checks = self.run_check([self.task(name="other", agent_id="x")])
        self.assertEqual(checks[0]["status"], su.SKIP)


class JevKeyTest(Base):
    def test_permissions(self):
        key = self.ws / "api_key"
        key.write_text("SECRET-VALUE-SHOULD-NOT-APPEAR")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(key)}):
            os.chmod(key, 0o644)
            loose = su.check_jev_key(self.ws, call_api=False)[0]
            os.chmod(key, 0o600)
            tight = su.check_jev_key(self.ws, call_api=False)[0]
        self.assertEqual((loose["status"], tight["status"]), (su.FAIL, su.OK))
        self.assertNotIn("SECRET", json.dumps([loose, tight]))  # 只 stat，不讀內容

    def test_missing(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(self.ws / "nope")}):
            self.assertEqual(su.check_jev_key(self.ws, call_api=False)[0]["status"], su.FAIL)


class ManifestTest(unittest.TestCase):
    def test_exec_bit_and_pycache(self):
        before = su.manifest(tar_of({"a.py": (b"x", 0o644), "__pycache__/a.pyc": (b"c", 0o644)}))
        after = su.manifest(tar_of({"a.py": (b"x", 0o755)}))
        self.assertEqual(su.diff_manifest(before, after),
                         {"added": [], "removed": ["__pycache__/a.pyc"], "changed": ["a.py"]})


class BundleTest(Base):
    STORED = tar_of({"AGENTS.md": (b"old", 0o644)})
    NEW = tar_of({"AGENTS.md": (b"new", 0o644)})

    def plan(self, api, candidate):
        def snapshot(repo, dest):
            (dest / su.BUNDLE_REL).mkdir(parents=True)
            return "f" * 40
        record = {"anchor_session": ANCHOR, "agent_id": "agent123"}
        return su.bundle_plan(api, self.ws, record, packer=lambda _: candidate, snapshot=snapshot)

    def test_non_owner_cannot_plan(self):
        with self.assertRaises(su.SetupError):
            self.plan(FakeApi(agent_routes(level=1, contents=self.STORED)), self.NEW)

    def test_noop_when_identical(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.STORED)
        self.assertTrue(plan["noop"])
        out = su.bundle_apply(FakeApi(agent_routes(contents=self.STORED)), self.ws, Path(plan["plan_dir"]),
                              plan["plan_digest"])
        self.assertEqual(out["result"], "NO-OP")

    def test_apply_puts_once_and_verifies(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.NEW)
        self.assertEqual(plan["diff"]["changed"], ["AGENTS.md"])
        self.assertEqual(oct(os.stat(plan["plan_dir"]).st_mode & 0o777), "0o700")
        su.save_agent_record(self.sdir, {"anchor_session": ANCHOR, "agent_id": "agent123"})
        api = FakeApi(agent_routes(contents=self.STORED))
        api.after_put = {f"/v1/sessions/{ANCHOR}/agent": (200, dict(AGENT, version=2)),
                         f"/v1/sessions/{ANCHOR}/agent/contents": (200, self.NEW)}
        out = su.bundle_apply(api, self.ws, Path(plan["plan_dir"]), plan["plan_digest"])
        self.assertEqual((out["result"], out["version"], len(api.puts)), ("DEPLOYED", 2, 1))
        self.assertEqual(su.load_agent_record(self.sdir)["version"], 2)

    def test_digest_mismatch_refused(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.NEW)
        api = FakeApi(agent_routes(contents=self.STORED))
        with self.assertRaises(su.SetupError):
            su.bundle_apply(api, self.ws, Path(plan["plan_dir"]), "0" * 64)
        self.assertEqual(api.puts, [])

    def test_server_changed_since_plan_refused(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.NEW)
        api = FakeApi(agent_routes(contents=tar_of({"AGENTS.md": (b"someone else", 0o644)})))
        with self.assertRaises(su.SetupError):
            su.bundle_apply(api, self.ws, Path(plan["plan_dir"]), plan["plan_digest"])
        self.assertEqual(api.puts, [])

    def test_put_failure_not_retried(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.NEW)
        api = FakeApi(agent_routes(contents=self.STORED))
        api.put_result = (500, {})
        with self.assertRaises(su.SetupError) as ctx:
            su.bundle_apply(api, self.ws, Path(plan["plan_dir"]), plan["plan_digest"])
        self.assertEqual(len(api.puts), 1)
        self.assertIn("不要直接重送", str(ctx.exception))

    def test_agent_id_change_after_put_detected(self):
        plan = self.plan(FakeApi(agent_routes(contents=self.STORED)), self.NEW)
        api = FakeApi(agent_routes(contents=self.STORED))
        api.after_put = {f"/v1/sessions/{ANCHOR}/agent": (200, dict(AGENT, id="new-id")),
                         f"/v1/sessions/{ANCHOR}/agent/contents": (200, self.NEW)}
        with self.assertRaises(su.SetupError):
            su.bundle_apply(api, self.ws, Path(plan["plan_dir"]), plan["plan_digest"])


class SkillLayoutTest(unittest.TestCase):
    def test_claude_entry_points_to_agents_skill(self):
        link = REPO / ".claude/skills/woow-security-sweep-setup"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (REPO / ".agents/skills/woow-security-sweep-setup").resolve())

    def test_doctor_ids_documented(self):
        text = (REPO / ".agents/skills/woow-security-sweep-setup/SKILL.md").read_text(encoding="utf-8")
        for cid in ("python", "jev_key", "issues:<repo>", "host", "agent_owner", "schedule_overlap"):
            self.assertIn(f"`{cid}`", text)


if __name__ == "__main__":
    unittest.main()
