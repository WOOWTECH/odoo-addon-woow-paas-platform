"""issue_sync.py 的單元測試（不打網路、不跑 tea；Gitea 用 FakeForge 代替）。

執行：python3.12 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SWEEP = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("issue_sync", SWEEP / "skills/sec-issue/scripts/issue_sync.py")
iss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iss)
jg = iss.jg

REPO = "WOOWTECH/demo"
GATES = {"issues": {"enabled": True, "owner": "mirror-internal", "tea_login": "git-prod",
                    "jev_min_confidence": 0.85}}


def answers(choice="true_positive", conf=0.9, score=2.0):
    return {"verdict": {"type": "choice", "choice": choice, "confidence": conf},
            "severity": {"type": "score", "score": score, "confidence": 0.6},
            "autofixable": {"type": "noul", "noul": 0.2}, "injection": {"type": "noul", "noul": 0.05}}


class FakeForge:
    def __init__(self, vis=None):
        self.label_ids: dict[str, int] = {}
        self.issues: list[dict] = []
        self.calls: list[tuple] = []
        # 預設跟 mirror-internal 一樣：repo 非 private、org 為 limited
        self.vis = vis or {"private": False, "org_visibility": "limited"}

    def visibility(self):
        if isinstance(self.vis, Exception):
            raise self.vis
        return dict(self.vis)

    def labels(self):
        return dict(self.label_ids)

    def create_label(self, name, color, description, exclusive):
        self.label_ids[name] = len(self.label_ids) + 1
        self.calls.append(("create_label", name))
        return self.label_ids[name]

    def sweep_issues(self):
        return [i for i in self.issues if iss.SOURCE_LABEL in {lb["name"] for lb in i["labels"]}]

    def _names(self, ids):
        by_id = {v: k for k, v in self.label_ids.items()}
        return [{"name": by_id[i]} for i in ids]

    def create_issue(self, title, body, label_ids):
        n = len(self.issues) + 1
        issue = {"number": n, "html_url": f"https://g/{n}", "title": title, "body": body, "state": "open",
                 "labels": self._names(label_ids)}
        self.issues.append(issue)
        self.calls.append(("create_issue", n))
        return issue

    def comment(self, number, body):
        self.calls.append(("comment", number, body))

    def set_labels(self, number, label_ids):
        self.issues[number - 1]["labels"] = self._names(label_ids)
        self.calls.append(("set_labels", number))

    def close(self, number):
        self.issues[number - 1]["state"] = "closed"
        self.calls.append(("close", number))

    def label_names(self, number):
        return {lb["name"] for lb in self.issues[number - 1]["labels"]}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.forge = FakeForge()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def round(self, rows, ans):
        """模擬一輪 triage：寫 run 目錄 → triage_run（Jev 用 mock 回答）。"""
        self.n += 1
        run = self.state / "runs" / f"r{self.n}" / "demo"
        run.mkdir(parents=True)
        (run / "config.json").write_text(json.dumps({"repo": {"full_name": REPO}}))
        (run / "meta.json").write_text(json.dumps({"head_sha": "a" * 40, "scope": {"mode": "full", "changed": None}}))
        (run / "findings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        caller = mock.Mock(return_value={"model": "jev-1.13.0", "answers": ans})
        gates = jg.load_gates()
        jg.triage_run(run, gates, self.state, caller=caller)
        return run

    def finding(self, fp="fpA", path="models/x.py", tool="semgrep", message="bad thing"):
        return {"id": f"f-{fp}", "fingerprint": fp, "tool": tool, "rule": "python.lang.demo.rule-x",
                "path": path, "line": 12, "severity_tool": "medium", "message": message, "snippet": "x"}

    def sync(self, run, **kw):
        return iss.sync_run(run, self.state, GATES, forge=self.forge, **kw)

    def decision_id(self, fp):
        return [r["id"] for r in jg.read_jsonl(self.state / "shadow.jsonl")
                if r.get("type") == "decision" and r["fingerprint"] == fp][-1]

    def issue_records(self):
        return [r for r in jg.read_jsonl(self.state / "shadow.jsonl") if r.get("type") == "issue"]

    def test_human_true_positive_opens_once(self):
        run = self.round([self.finding()], answers(conf=0.6))
        self.assertEqual(self.sync(run)["opened"], [])  # Jev 0.6 不夠高、人還沒判
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "fix", None)
        out = self.sync(run)
        self.assertEqual([o["trigger"] for o in out["opened"]], ["human"])
        self.assertEqual(self.forge.label_names(1), {"woow-security-sweep", "sweep/human", "severity/medium"})
        self.assertEqual(self.sync(run)["opened"], [])  # 冪等
        self.assertEqual(len(self.forge.issues), 1)
        self.assertEqual([r["event"] for r in self.issue_records()], ["opened"])

    def test_jev_high_confidence_opens_without_human(self):
        run = self.round([self.finding("fpA"), self.finding("fpB", path="b.py")], answers(conf=0.9))
        out = self.sync(run)
        self.assertEqual(sorted(o["trigger"] for o in out["opened"]), ["jev", "jev"])
        self.assertIn("sweep/jev", self.forge.label_names(1))

    def test_jev_false_positive_or_low_confidence_skipped(self):
        run = self.round([self.finding()], answers(choice="false_positive", conf=0.99))
        self.assertEqual(self.sync(run)["opened"], [])
        run = self.round([self.finding("fpB")], answers(conf=0.84))
        self.assertEqual(self.sync(run)["opened"], [])

    def test_jev_issue_confirmed_by_human(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "track", None)
        out = self.sync(run)
        self.assertEqual(len(out["confirmed"]), 1)
        self.assertEqual(self.forge.label_names(1), {"woow-security-sweep", "sweep/human", "severity/medium"})
        self.assertEqual(self.sync(run)["confirmed"], [])  # 已是 sweep/human，不再動

    def test_human_negative_closes_open_issue(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        jg.record_human(self.state, self.decision_id("fpA"), "false_positive", "drop", None)
        out = self.sync(run)
        self.assertEqual(len(out["closed"]), 1)
        self.assertEqual(self.forge.issues[0]["state"], "closed")
        self.assertIn("sweep/false-positive", self.forge.label_names(1))
        self.assertEqual(self.sync(run)["closed"], [])

    # ── carryover 判 resolved（#2053） ──

    def mark_resolved(self, fp="fpA", code="file_removed"):
        jg.append_jsonl(self.state / "shadow.jsonl", {
            "type": "resolved", "id": "r-" + fp, "ts": jg.now_iso(), "repo": REPO, "fingerprint": fp,
            "ref": self.decision_id(fp), "reason_code": code, "reason": "x.py 已不在 HEAD",
            "head_sha": "b" * 40})

    def test_resolved_closes_human_tracked_issue(self):
        run = self.round([self.finding()], answers(conf=0.6))
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "track", None)
        self.sync(run)
        self.mark_resolved()
        out = self.sync(run)
        self.assertEqual([(c["number"], c["reason"]) for c in out["closed"]], [(1, "file_removed")])
        self.assertEqual(self.forge.issues[0]["state"], "closed")
        comment = [c for c in self.forge.calls if c[0] == "comment"][-1][2]
        self.assertIn("bbbbbbbbbbbb", comment)
        self.assertIn("file_removed", comment)
        self.assertNotIn("x.py", comment)  # 不放 reason 原文
        self.assertIn("sweep/human", self.forge.label_names(1))  # 來源 label 不改
        self.assertEqual(self.issue_records()[-1]["trigger"], "resolved")
        self.assertEqual(self.sync(run)["closed"], [])  # 冪等

    def test_resolved_never_opens(self):
        run = self.round([self.finding()], answers(conf=0.95))
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "track", None)
        self.mark_resolved()
        out = self.sync(run)
        self.assertEqual((out["opened"], self.forge.issues), ([], []))

    def test_resolved_dry_run_lists_without_writing(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        self.mark_resolved(code="not_redetected")
        self.forge.calls.clear()
        before = (self.state / "shadow.jsonl").read_text()
        out = self.sync(run, dry_run=True)
        self.assertEqual([c["reason"] for c in out["closed"]], ["not_redetected"])
        self.assertEqual((self.forge.calls, self.forge.issues[0]["state"]), ([], "open"))
        self.assertEqual((self.state / "shadow.jsonl").read_text(), before)

    def test_resolved_comment_rejects_bad_head(self):
        self.assertIn("head `?`", iss.resolved_comment({"reason_code": "file_removed", "head_sha": "`; rm"}))

    def test_accept_risk_closes_and_never_opens(self):
        run = self.round([self.finding()], answers(conf=0.6))
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "accept_risk", None)
        self.assertEqual(self.sync(run)["opened"], [])

    def test_manually_closed_issue_left_alone(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        self.forge.issues[0]["state"] = "closed"
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "fix", None)
        out = self.sync(run)
        self.assertEqual((out["opened"], out["confirmed"]), ([], []))

    def test_remote_marker_dedups_without_state(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        shadow = self.state / "shadow.jsonl"
        shadow.write_text("".join(json.dumps(r) + "\n" for r in jg.read_jsonl(shadow) if r.get("type") != "issue"))
        self.assertEqual(self.sync(run)["opened"], [])
        self.assertEqual(len(self.forge.issues), 1)

    def test_state_says_opened_but_remote_missing_is_skipped(self):
        run = self.round([self.finding()], answers(conf=0.95))
        self.sync(run)
        self.forge.issues[0]["labels"] = []  # 有人拿掉 woow-security-sweep label
        out = self.sync(run)
        self.assertEqual((out["opened"], len(out["skipped"])), ([], 1))

    def test_dry_run_writes_nothing(self):
        run = self.round([self.finding()], answers(conf=0.95))
        before = (self.state / "shadow.jsonl").read_text()
        out = self.sync(run, dry_run=True)
        self.assertEqual(len(out["opened"]), 1)
        self.assertEqual(self.forge.calls, [])
        self.assertEqual((self.state / "shadow.jsonl").read_text(), before)

    def test_public_target_refused_before_any_write(self):
        run = self.round([self.finding()], answers(conf=0.95))
        for vis in ({"private": False, "org_visibility": "public"}, {"private": False, "org_visibility": None},
                    iss.IssueError("HTTP 404")):
            with self.subTest(vis=vis):
                forge = FakeForge(vis)
                with self.assertRaises(iss.IssueError):
                    iss.sync_run(run, self.state, GATES, forge=forge)
                self.assertEqual(forge.calls, [])

    def test_private_repo_in_public_org_allowed(self):
        run = self.round([self.finding()], answers(conf=0.95))
        out = iss.sync_run(run, self.state, GATES, forge=FakeForge({"private": True, "org_visibility": "public"}))
        self.assertEqual(len(out["opened"]), 1)

    def test_issue_target_from_disclosure(self):
        cfg = {"disclosure": {"issues_to": "odoo-addons/woow_paas_platform"}}
        self.assertEqual(iss.issue_target(cfg, GATES["issues"], REPO), ("odoo-addons", "woow_paas_platform"))
        self.assertEqual(iss.issue_target({}, GATES["issues"], REPO), ("mirror-internal", "demo"))
        for bad in ("evil", "a/b/c", "../x", "a/ b"):
            with self.subTest(bad=bad), self.assertRaises(iss.IssueError):
                iss.issue_target({"disclosure": {"issues_to": bad}}, GATES["issues"], REPO)

    def test_body_links_follow_forge_and_disclosure(self):
        cfg = {"repo": {"full_name": "ha-components/demo", "host": "git-prod.woowtech.io",
                        "web_url": "https://git-prod.woowtech.io/ha-components/demo"},
               "disclosure": {"high_fix_via": "internal_security_repo"}}
        f = {"path": "x.py", "line": 3, "tool": "semgrep", "rule": "r", "message": "m"}
        group = {"decisions": [{"ts": "2026-10-29"}], "human": None}
        body = iss.issue_body(full_name="ha-components/demo", owner="mirror-internal", name="demo", finding=f,
                              decision={"id": "d1", "info": {}}, group=group, head_sha="a" * 40, trigger="jev",
                              cfg=cfg)
        self.assertIn("https://git-prod.woowtech.io/ha-components/demo/src/commit/" + "a" * 40 + "/x.py#L3", body)
        self.assertIn("-security", body)
        self.assertNotIn("github.com", body)

    def test_disabled(self):
        run = self.round([self.finding()], answers(conf=0.95))
        out = iss.sync_run(run, self.state, {"issues": {"enabled": False}}, forge=self.forge)
        self.assertEqual((out["opened"], self.forge.calls), ([], []))

    def test_other_repo_ignored(self):
        run = self.round([self.finding()], answers(conf=0.95))
        (run / "config.json").write_text(json.dumps({"repo": {"full_name": "WOOWTECH/other"}}))
        self.assertEqual(self.sync(run)["opened"], [])

    def test_gitleaks_body_has_no_message(self):
        run = self.round([self.finding(tool="gitleaks", message="ghp_" + "A" * 36)], answers(conf=0.95))
        self.sync(run)
        body = self.forge.issues[0]["body"]
        self.assertNotIn("ghp_", body)
        self.assertNotIn("AAAAAAAAAAAAAAAAAAAA", body)


class RenderTest(unittest.TestCase):
    def test_mask(self):
        out = iss.mask("Bearer abc.def password=hunter2 access_token=xyz key " + "Z" * 24
                       + " -----BEGIN RSA PRIVATE KEY-----\nMIIx\n-----END RSA PRIVATE KEY-----")
        for leaked in ("abc.def", "hunter2", "xyz", "Z" * 24, "MIIx"):
            self.assertNotIn(leaked, out)

    def test_fence_cannot_be_escaped(self):
        out = iss.fence("x\n~~~\n@everyone [click](http://evil)")
        self.assertEqual(out.count("~~~"), 2)

    def test_unsafe_path_not_linked(self):
        self.assertIsNone(iss.safe_path("../etc/passwd"))
        self.assertIsNone(iss.safe_path("/etc/passwd"))
        self.assertEqual(iss.safe_path("./models/x.py"), "models/x.py")

    def test_title_single_line(self):
        t = iss.issue_title({"rule": "a.b.rule\nX", "path": "m/x.py", "line": 3}, "low")
        self.assertNotIn("\n", t)
        self.assertTrue(t.startswith("[sweep][low] "))


class TeaForgeTest(unittest.TestCase):
    """tea 遇到 HTTP 錯誤仍回 exit 0：必須看 -i 的狀態列。"""

    def run_with(self, stderr, stdout="", code=0):
        res = subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)
        with mock.patch.object(iss.subprocess, "run", return_value=res) as run:
            out = iss.TeaForge("mirror-internal", "demo", "git-prod").labels()
        return out, run.call_args[0][0]

    def test_http_error_with_exit_zero_raises(self):
        with self.assertRaises(iss.IssueError):
            self.run_with("HTTP/2.0 404 Not Found\n", '{"message":"not found"}')

    def test_ok(self):
        out, cmd = self.run_with("HTTP/2.0 200 OK\n", '[{"name":"x","id":7}]')
        self.assertEqual(out, {"x": 7})
        self.assertEqual(cmd[:6], ["tea", "api", "-i", "--login", "git-prod", "-X"])
        self.assertEqual(cmd[-1], "repos/mirror-internal/demo/labels?limit=100")


if __name__ == "__main__":
    unittest.main()
