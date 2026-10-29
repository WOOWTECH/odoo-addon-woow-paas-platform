"""woow-security-sweep-launch 的 launch.py 單元測試（#1978 階段 B；不打網路、不需 omnigent）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "sweep_launch", REPO / ".agents/skills/woow-security-sweep-launch/scripts/launch.py")
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)
su = la.su

SWEEP_ID = "agent123"
NOW = 1_800_000_000.0
SESSIONS = f"/v1/sessions?agent_id={SWEEP_ID}&kind=any&limit=20&sort_by=updated_at&order=desc"


class FakeApi:
    """依路徑回傳固定答案；記錄 POST／PATCH。"""

    def __init__(self, routes: dict[str, tuple[int, object]]):
        self.routes = dict(routes)
        self.posts: list[tuple[str, dict | None]] = []
        self.patches: list[tuple[str, dict]] = []
        self.post_results: dict[str, tuple[int, object]] = {}

    def get(self, path):
        return self.routes.get(path, (404, {}))

    def post(self, path, body=None):
        self.posts.append((path, body))
        return self.post_results.get(path, (202, {"triggered": True}))

    def patch(self, path, body):
        self.patches.append((path, body))
        return 200, {}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve()
        self.sdir = su.state_dir(self.ws)
        self.repos = {}
        for name in ("woow_line_base", "Woow_odoo_line_liff", "other"):
            repo = self.ws / "WOOW" / name
            (repo / ".git").mkdir(parents=True)
            self.repos[name] = str(repo)
        su.write_preset(self.ws, "line-daily", [self.repos["woow_line_base"], self.repos["Woow_odoo_line_liff"]],
                        "incremental", "auto", None, None)
        su.write_preset(self.ws, "other-daily", [self.repos["other"]], "incremental", "auto", None, None)
        su.save_agent_record(self.sdir, {"anchor_session": "a" * 32, "agent_id": SWEEP_ID})

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, **kw):
        base = {"id": "t1", "name": "woow-security-sweep line-daily", "agent_id": SWEEP_ID, "host_id": "h1",
                "workspace": str(self.ws), "permission_mode": "auto", "max_cost_usd": 30.0, "state": "active",
                "rrule": "FREQ=WEEKLY;BYDAY=TU,WE;BYHOUR=8", "prompt": "preset: line-daily", "timezone": "Asia/Taipei"}
        return dict(base, **kw)

    def manual(self, **kw):
        return self.task(**{"id": "m1", "name": la.MANUAL_PREFIX + "line-daily", "state": "paused",
                            "rrule": la.MANUAL_RRULE, **kw})

    def api(self, tasks, sessions=(), runs=None, extra=None):
        routes = {"/v1/scheduled-tasks": (200, {"scheduled_tasks": list(tasks)}),
                  SESSIONS: (200, {"data": list(sessions)})}
        for tid, rows in (runs or {}).items():
            routes[f"/v1/scheduled-tasks/{tid}/runs?limit=10"] = (200, {"runs": rows})
        routes.update(extra or {})
        return FakeApi(routes)

    def session(self, sid="s1", status="idle", updated_ago=60, created_ago=600, pending=0):
        return {"id": sid, "agent_id": SWEEP_ID, "status": status, "updated_at": int(NOW - updated_ago),
                "created_at": int(NOW - created_ago), "pending_elicitations_count": pending}

    def commit(self, name, ago):
        path = self.sdir / "repos" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(NOW - ago, dt.timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps({"last_sha": "x", "updated_at": stamp}))

    def run_once(self, api):
        clock = iter([NOW, NOW + 3, NOW + 6, NOW + 100])
        return la.run_once(api, self.ws, "line-daily", now=lambda: next(clock), sleep=lambda _: None)


class FindManualTest(Base):
    def test_named_manual_preferred(self):
        got = la.find_manual([self.task(state="paused"), self.manual()], "line-daily", SWEEP_ID)
        self.assertEqual(got["id"], "m1")

    def test_unique_paused_fallback(self):
        got = la.find_manual([self.task(id="p1", name="whatever", state="paused")], "line-daily", SWEEP_ID)
        self.assertEqual(got["id"], "p1")

    def test_ambiguous_paused_refused(self):
        with self.assertRaises(su.SetupError):
            la.find_manual([self.task(id="p1", state="paused"), self.task(id="p2", state="paused")],
                           "line-daily", SWEEP_ID)

    def test_active_daily_is_not_manual(self):
        self.assertIsNone(la.find_manual([self.task()], "line-daily", SWEEP_ID))


class RunOnceTest(Base):
    def test_triggers_manual_and_reports_session(self):
        api = self.api([self.task(), self.manual()], extra={
            "/v1/scheduled-tasks/m1/runs?limit=1": (200, {"runs": [{"conversation_id": "new1", "fired_at": NOW + 1}]})})
        out = self.run_once(api)
        self.assertEqual(api.posts, [("/v1/scheduled-tasks/m1/run", None)])
        self.assertEqual((out["result"], out["conversation_id"]), ("TRIGGERED", "new1"))

    def test_old_run_row_not_mistaken_for_new(self):
        api = self.api([self.manual()], extra={
            "/v1/scheduled-tasks/m1/runs?limit=1": (200, {"runs": [{"conversation_id": "old", "fired_at": NOW - 9999}]})})
        self.assertIsNone(self.run_once(api)["conversation_id"])

    def test_no_manual_schedule(self):
        api = self.api([self.task()])
        with self.assertRaises(su.SetupError) as ctx:
            self.run_once(api)
        self.assertIn("--manual", str(ctx.exception))
        self.assertEqual(api.posts, [])

    def test_manual_must_be_paused(self):
        api = self.api([self.manual(state="active")])
        with self.assertRaises(su.SetupError):
            self.run_once(api)
        self.assertEqual(api.posts, [])

    def test_running_overlapping_session_refused(self):
        for status in ("running", "waiting"):
            api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                           sessions=[self.session(status=status)])
            with self.subTest(status=status), self.assertRaises(su.SetupError) as ctx:
                self.run_once(api)
            self.assertIn("s1", str(ctx.exception))
            self.assertEqual(api.posts, [])

    def test_pending_inbox_question_refused(self):
        api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                       sessions=[self.session(pending=2)])
        with self.assertRaises(su.SetupError) as ctx:
            self.run_once(api)
        self.assertIn("Inbox", str(ctx.exception))

    def test_idle_uncommitted_within_window_refused(self):
        self.commit("woow_line_base", ago=60)      # 這一輪之後 commit 過
        self.commit("Woow_odoo_line_liff", ago=99999)  # 這一輪還沒 commit
        api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                       sessions=[self.session(updated_ago=600, created_ago=3600)])
        with self.assertRaises(su.SetupError) as ctx:
            self.run_once(api)
        self.assertIn("Woow_odoo_line_liff", str(ctx.exception))
        self.assertNotIn("woow_line_base", str(ctx.exception))

    def test_idle_committed_allowed(self):
        self.commit("woow_line_base", ago=60)
        self.commit("Woow_odoo_line_liff", ago=60)
        api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                       sessions=[self.session(updated_ago=30, created_ago=3600)])
        self.assertEqual(self.run_once(api)["result"], "TRIGGERED")

    def test_idle_old_session_allowed(self):
        api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                       sessions=[self.session(updated_ago=la.ACTIVE_WINDOW_S + 60, created_ago=la.ACTIVE_WINDOW_S + 99)])
        self.assertEqual(self.run_once(api)["result"], "TRIGGERED")

    def test_non_overlapping_running_session_allowed(self):
        other = self.task(id="t9", name="woow-security-sweep other-daily", prompt="preset: other-daily")
        api = self.api([other, self.manual()], sessions=[self.session(sid="s9", status="running")],
                       runs={"t9": [{"conversation_id": "s9"}]})
        self.assertEqual(self.run_once(api)["result"], "TRIGGERED")

    def test_unknown_session_counts_as_overlap(self):
        api = self.api([self.manual()], sessions=[self.session(sid="adhoc", status="running")])
        with self.assertRaises(su.SetupError) as ctx:
            self.run_once(api)
        self.assertIn("adhoc", str(ctx.exception))

    def test_session_list_unreadable_refuses(self):
        api = self.api([self.manual()])
        api.routes[SESSIONS] = (500, {})
        with self.assertRaises(su.SetupError):
            self.run_once(api)
        self.assertEqual(api.posts, [])

    def test_conflict_409(self):
        api = self.api([self.manual()])
        api.post_results["/v1/scheduled-tasks/m1/run"] = (409, {"detail": "in flight"})
        with self.assertRaises(su.SetupError) as ctx:
            self.run_once(api)
        self.assertIn("409", str(ctx.exception))

    def test_missing_sweep_record(self):
        (self.sdir / "agent.json").unlink()
        with self.assertRaises(su.SetupError):
            self.run_once(self.api([self.manual()]))


class CreateScheduleTest(Base):
    def api_with_readback(self, tasks, readback_override=None):
        api = self.api(tasks)
        api.post_results["/v1/scheduled-tasks"] = (200, {"id": "new"})
        original_patch = api.patch

        def patch(path, body):
            original_patch(path, body)
            posted = next(b for p, b in api.posts if p == "/v1/scheduled-tasks")
            got = {**posted, "id": "new", "state": body.get("state", "active"), "max_cost_usd": body["max_cost_usd"],
                   **(readback_override or {})}
            api.routes["/v1/scheduled-tasks/new"] = (200, got)
            return 200, got
        api.patch = patch
        return api

    def test_active_schedule_created_and_verified(self):
        api = self.api_with_readback([self.manual()])
        out = la.create_schedule(api, self.ws, "line-daily", host_id="h1",
                                 rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=30", max_cost=25)
        body = api.posts[0][1]
        self.assertEqual((body["prompt"], body["permission_mode"], body["timezone"], body["agent_id"], body["host_id"]),
                         ("preset: line-daily", "auto", "Asia/Taipei", SWEEP_ID, "h1"))
        self.assertEqual(api.patches, [("/v1/scheduled-tasks/new", {"max_cost_usd": 25})])
        self.assertEqual((out["result"], out["state"], out["max_cost_usd"]), ("CREATED", "active", 25))

    def test_manual_schedule_paused(self):
        api = self.api_with_readback([])
        out = la.create_schedule(api, self.ws, "line-daily", host_id="h1", manual=True)
        self.assertEqual(api.posts[0][1]["rrule"], la.MANUAL_RRULE)
        self.assertEqual(api.posts[0][1]["name"], la.MANUAL_PREFIX + "line-daily")
        self.assertEqual(api.patches[0][1], {"max_cost_usd": la.DEFAULT_MAX_COST, "state": "paused"})
        self.assertEqual((out["state"], out["manual"]), ("paused", True))

    def test_same_day_overlap_refused(self):
        api = self.api_with_readback([self.task()])  # TU,WE 已在掃同一組 targets
        with self.assertRaises(su.SetupError) as ctx:
            la.create_schedule(api, self.ws, "line-daily", host_id="h1", rrule="FREQ=WEEKLY;BYDAY=WE;BYHOUR=9",
                               name="另一支")
        self.assertIn("WE", str(ctx.exception))
        self.assertEqual(api.posts, [])

    def test_daily_rule_overlaps_everything(self):
        api = self.api_with_readback([self.task(rrule="FREQ=WEEKLY;BYDAY=SU;BYHOUR=8")])
        with self.assertRaises(su.SetupError):
            la.create_schedule(api, self.ws, "line-daily", host_id="h1", rrule="FREQ=DAILY;BYHOUR=9", name="x")

    def test_paused_and_other_targets_ignored(self):
        other = self.task(id="t2", name="woow-security-sweep other-daily", prompt="preset: other-daily")
        api = self.api_with_readback([self.manual(), self.task(id="t3", name="vacation", state="paused"), other])
        out = la.create_schedule(api, self.ws, "line-daily", host_id="h1", rrule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=8")
        self.assertEqual(out["result"], "CREATED")

    def test_unknown_preset_of_existing_schedule_counts_as_overlap(self):
        api = self.api_with_readback([self.task(prompt="preset: gone")])
        with self.assertRaises(su.SetupError):
            la.create_schedule(api, self.ws, "line-daily", host_id="h1", rrule="FREQ=WEEKLY;BYDAY=TU;BYHOUR=9",
                               name="x")

    def test_duplicate_manual_refused(self):
        api = self.api_with_readback([self.manual()])
        with self.assertRaises(su.SetupError):
            la.create_schedule(api, self.ws, "line-daily", host_id="h1", manual=True, name="另一個名字")
        self.assertEqual(api.posts, [])

    def test_readback_mismatch_reported(self):
        api = self.api_with_readback([], readback_override={"max_cost_usd": None, "permission_mode": None})
        with self.assertRaises(su.SetupError) as ctx:
            la.create_schedule(api, self.ws, "line-daily", host_id="h1", rrule="FREQ=WEEKLY;BYDAY=MO;BYHOUR=8")
        msg = str(ctx.exception)
        for word in ("new", "max_cost_usd", "permission_mode"):
            self.assertIn(word, msg)

    def test_argument_errors(self):
        api = self.api_with_readback([])
        for kw in ({"rrule": "FREQ=DAILY", "manual": True}, {}, {"rrule": "FREQ=DAILY", "max_cost": 0}):
            with self.subTest(kw=kw), self.assertRaises(su.SetupError):
                la.create_schedule(api, self.ws, "line-daily", host_id="h1", **kw)
        with self.assertRaises(su.SetupError):
            la.create_schedule(api, self.ws, "line-daily", host_id=None, manual=True)
        with self.assertRaises(su.SetupError):
            la.create_schedule(api, self.ws, "nope", host_id="h1", manual=True)
        self.assertEqual(api.posts, [])


class CheckTargetsTest(Base):
    """#2084：check() 交給 doctor 的 targets 來自 preset，不是寫死的 LINE 三件組。"""

    def test_check_passes_preset_targets_to_doctor(self):
        seen = {}

        def fake_doctor(ws, targets, **kw):
            seen["targets"], seen["kw"] = targets, kw
            return {"ready": True, "failed": [], "checks": [], "targets_from": kw["targets_from"]}

        with mock.patch.object(su, "doctor", side_effect=fake_doctor), \
                mock.patch.object(su, "omnigent_config", return_value={}):
            out = la.check(self.ws)
        expected = [Path(self.repos[n]).resolve() for n in ("woow_line_base", "Woow_odoo_line_liff", "other")]
        self.assertEqual(seen["targets"], expected)
        self.assertEqual(seen["kw"]["targets_from"], "presets")
        self.assertEqual(out["doctor"]["targets_from"], "presets")
        self.assertTrue(out["ready"])


class PresetsAndStatusTest(Base):
    def test_presets_list_with_schedules(self):
        out = {p["name"]: p for p in la.list_presets(self.ws, [self.task(), self.manual()], SWEEP_ID)}
        self.assertEqual(sorted(out), ["line-daily", "other-daily"])
        self.assertTrue(out["line-daily"]["valid"])
        self.assertEqual(out["line-daily"]["manual_schedule"], "m1")
        self.assertEqual(len(out["line-daily"]["schedules"]), 2)
        self.assertIsNone(out["other-daily"]["manual_schedule"])

    def test_invalid_preset_reported_not_raised(self):
        (self.sdir / "presets" / "broken.yaml").write_text("targets: []\n")
        out = {p["name"]: p for p in la.list_presets(self.ws)}
        self.assertFalse(out["broken"]["valid"])

    def test_status(self):
        (self.sdir / "runs" / "20261025-0830" / "woow_line_base").mkdir(parents=True)
        (self.sdir / "runs" / "20261025-0830" / "report.md").write_text("x")
        api = self.api([self.task(last_run_conversation_id="s1"), self.manual()],
                       sessions=[self.session(status="running")])
        out = la.status(api, self.ws, now=NOW)
        self.assertEqual([s["manual"] for s in out["schedules"]], [False, True])
        self.assertEqual(out["in_progress"][0]["session"], "s1")
        self.assertEqual(out["recent_runs"], [{"run": "20261025-0830", "targets": ["woow_line_base"], "report": True}])


if __name__ == "__main__":
    unittest.main()
