"""jev_gate.py 的單元測試（不打網路）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SWEEP = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("jev_gate", SWEEP / "skills/sec-gate/scripts/jev_gate.py")
jg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jg)

TODAY = dt.date(2026, 10, 21)


def gates(mode: str = "shadow") -> dict:
    g = jg.load_gates()
    g["mode"] = mode
    return g


def answers(choice="true_positive", conf=0.95, score=2.0, autofix=0.9, injection=0.05) -> dict:
    return {
        "verdict": {"type": "choice", "choice": choice, "confidence": conf, "probabilities": {}},
        "severity": {"type": "score", "score": score, "confidence": 0.8, "legend": {}, "probabilities": {}},
        "autofixable": {"type": "noul", "noul": autofix},
        "injection": {"type": "noul", "noul": injection},
    }


def finding(path="models/x.py", sev="low", rule="python.lang.foo", message="something") -> dict:
    return {"id": "f1", "fingerprint": f"semgrep:{rule}:{path}:abc", "tool": "semgrep", "rule": rule,
            "path": path, "line": 3, "severity_tool": sev, "message": message, "snippet": "x = 1"}


CFG = {
    "repo": {"full_name": "WOOWTECH/demo"},
    "always_human": {"paths": ["security/", "__manifest__.py", "controllers/webhook.py"],
                     "severities": ["critical"], "topics": ["signature"]},
    "autofix": {"enabled": True},
    "verify": {"regression_tests": ["tests/test_security_fixes.py"]},
    "baseline": [],
}


class GatesFileTest(unittest.TestCase):
    def test_committed_mode_is_shadow(self):
        # 改成 enforce 必須經人拍板；這條測試讓誤改在 CI 就紅
        self.assertEqual(jg.load_gates()["mode"], "shadow")

    def test_model_is_pinned_version(self):
        self.assertNotIn(jg.load_gates()["model"], ("jev-latest", "jev-preview"))


class PathMatchTest(unittest.TestCase):
    def test_dot_security_prefix_kept(self):
        self.assertTrue(jg._path_matches(".security/sweep.yaml", ".security/"))
        self.assertTrue(jg._path_matches("./security/x.csv", "security/"))

    def test_exact_file(self):
        self.assertTrue(jg._path_matches("__manifest__.py", "__manifest__.py"))
        self.assertFalse(jg._path_matches("foo/__manifest__.py.bak", "__manifest__.py"))


class RouteTriageTest(unittest.TestCase):
    def route(self, f, a, cfg=CFG, mode="shadow"):
        return jg.route_triage(f, a, cfg, gates(mode), TODAY)

    def test_hard_rule_path_beats_confident_false_positive(self):
        r, reasons, _ = self.route(finding(path="controllers/webhook.py"), answers("false_positive", 0.99, 1))
        self.assertEqual(r, "human")
        self.assertIn("always_human.paths:controllers/webhook.py", reasons)

    def test_hard_rule_topic_and_severity(self):
        r, reasons, _ = self.route(finding(message="missing signature check"), answers())
        self.assertEqual(r, "human")
        r, reasons, _ = self.route(finding(), answers(score=4.0))
        self.assertIn("always_human.severities:critical", reasons)

    def test_tool_severity_counts_when_higher_than_jev(self):
        r, _, info = self.route(finding(sev="critical"), answers(score=0.0))
        self.assertEqual((r, info["level"]), ("human", "critical"))

    def test_security_dir_always_human(self):
        r, _, _ = self.route(finding(path=".security/sweep.yaml"), answers("false_positive", 1.0, 0))
        self.assertEqual(r, "human")

    def test_baseline_accepted_drops_until_expiry(self):
        f = finding()
        cfg = dict(CFG, baseline=[{"fingerprint": f["fingerprint"], "status": "accepted", "expires": "2027-01-01"}])
        self.assertEqual(self.route(f, answers(), cfg)[0], "drop")
        cfg["baseline"][0]["expires"] = "2026-01-01"
        r, reasons, _ = self.route(f, answers(), cfg)
        self.assertNotEqual(r, "drop")
        self.assertTrue(any("expired" in x for x in reasons))

    def test_baseline_accepted_without_expiry_is_invalid(self):
        f = finding()
        cfg = dict(CFG, baseline=[{"fingerprint": f["fingerprint"], "status": "accepted"}])
        self.assertNotEqual(self.route(f, answers(), cfg)[0], "drop")

    def test_injection_goes_human(self):
        self.assertEqual(self.route(finding(), answers(injection=0.8))[0], "human")

    def test_confident_false_positive_low_severity_drops(self):
        self.assertEqual(self.route(finding(), answers("false_positive", 0.95, 1))[0], "drop")

    def test_confident_false_positive_high_severity_needs_human(self):
        self.assertEqual(self.route(finding(), answers("false_positive", 0.95, 3))[0], "human")

    def test_uncertain_goes_human(self):
        self.assertEqual(self.route(finding(), answers("needs_context", 0.9))[0], "human")
        self.assertEqual(self.route(finding(), answers("true_positive", 0.3))[0], "human")

    def test_true_positive_high_goes_private_human(self):
        r, reasons, _ = self.route(finding(), answers(score=3.0))
        self.assertEqual(r, "human")
        self.assertTrue(any("Advisory" in x for x in reasons))

    def test_true_positive_medium_autofix(self):
        self.assertEqual(self.route(finding(), answers(score=2.0, autofix=0.9))[0], "auto_fix")
        self.assertEqual(self.route(finding(), answers(score=2.0, autofix=0.3))[0], "issue_only")
        cfg = dict(CFG, autofix={"enabled": False})
        self.assertEqual(self.route(finding(), answers(score=2.0, autofix=0.9), cfg)[0], "issue_only")


class TriageRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        (self.run / "config.json").write_text(json.dumps(CFG))
        rows = [finding(), dict(finding(), id="f2", fingerprint="fp2"), dict(finding(), id="f3", fingerprint="fp3")]
        (self.run / "findings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def tearDown(self):
        self.tmp.cleanup()

    def test_shadow_effective_is_always_human(self):
        caller = mock.Mock(return_value={"model": "jev-1.13.0", "answers": answers(score=2.0)})
        summary = jg.triage_run(self.run, gates("shadow"), self.state, caller=caller, today=TODAY)
        self.assertEqual(summary["auto_fix"], 3)
        recs = jg.read_jsonl(self.state / "shadow.jsonl")
        self.assertEqual({r["effective"] for r in recs}, {"human"})
        self.assertEqual({r["proposed"] for r in recs}, {"auto_fix"})

    def test_enforce_uses_proposed(self):
        caller = mock.Mock(return_value={"model": "m", "answers": answers(score=2.0)})
        jg.triage_run(self.run, gates("enforce"), self.state, caller=caller, today=TODAY)
        self.assertEqual({r["effective"] for r in jg.read_jsonl(self.state / "shadow.jsonl")}, {"auto_fix"})

    def test_errors_fail_closed(self):
        caller = mock.Mock(side_effect=jg.GateError("boom"))
        summary = jg.triage_run(self.run, gates("enforce"), self.state, caller=caller, today=TODAY)
        self.assertEqual((summary["human"], summary["errors"]), (3, 3))

    def test_auth_error_stops_further_calls(self):
        caller = mock.Mock(side_effect=jg.AuthError("401"))
        summary = jg.triage_run(self.run, gates(), self.state, caller=caller, today=TODAY)
        self.assertEqual(caller.call_count, 1)
        self.assertTrue(summary["auth_failed"])
        self.assertEqual(summary["human"], 3)

    def test_malformed_answer_fails_closed(self):
        caller = mock.Mock(return_value={"model": "m", "answers": {"verdict": {}}})
        summary = jg.triage_run(self.run, gates("enforce"), self.state, caller=caller, today=TODAY)
        self.assertEqual(summary["human"], 3)


class CarryoverTest(unittest.TestCase):
    """前幾輪未決的 finding 要帶到下一輪（#1932）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.caller = mock.Mock(return_value={"model": "m", "answers": answers(score=2.0)})

    def tearDown(self):
        self.tmp.cleanup()

    def run_round(self, name, rows, *, scanned=None, tools_ok=("semgrep",), repo="WOOWTECH/demo", head=None):
        """模擬一輪：寫 run 目錄 → triage → carryover。scanned=None 代表 full。

        head：本輪 HEAD 上存在的檔案（會建一個真的 git repo）；None 代表舊版 meta（沒有 repo_path）。
        """
        run = self.state / "runs" / name / repo.split("/")[1]
        run.mkdir(parents=True)
        (run / "config.json").write_text(json.dumps(dict(CFG, repo={"full_name": repo})))
        extra = {} if head is None else self.git_head(name, head)
        (run / "meta.json").write_text(json.dumps(
            {"scope": {"mode": "full" if scanned is None else "incremental", "changed": scanned},
             **({} if scanned is None else {"scanned_files": scanned}), **extra}))
        (run / "tools.json").write_text(json.dumps({t: {"status": "ok"} for t in tools_ok}))
        (run / "findings.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        jg.triage_run(run, gates(), self.state, caller=self.caller, today=TODAY)
        return run, jg.carryover_run(run, self.state)

    def git_head(self, name, files):
        repo = self.state / "git" / name
        for f in files:
            (repo / f).parent.mkdir(parents=True, exist_ok=True)
            (repo / f).write_text("x\n")
        repo.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()
        return {"repo_path": str(repo), "head_sha": head}

    def resolved(self):
        return [r for r in jg.read_jsonl(self.state / "shadow.jsonl") if r.get("type") == "resolved"]

    def carried(self, run):
        return {r["fingerprint"] for r in jg.read_jsonl(run / "carryover.jsonl")}

    def decision_id(self, fp):
        return next(r["id"] for r in jg.read_jsonl(self.state / "shadow.jsonl")
                    if r.get("type") == "decision" and r["fingerprint"] == fp)

    def two_findings(self):
        return [dict(finding("a.py"), id="fa", fingerprint="fpA"), dict(finding("b.py"), id="fb", fingerprint="fpB")]

    def test_unanswered_carried_to_incremental_round(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=[])
        self.assertEqual(self.carried(run2), {"fpA", "fpB"})
        self.assertEqual((summary["carried"], summary["resolved"]), (2, 0))
        item = jg.read_jsonl(run2 / "carryover.jsonl")[0]
        self.assertEqual(item["finding"]["path"], "a.py")  # finding 本體從 decisions.jsonl 找回
        self.assertEqual(item["seen_runs"], 1)

    def test_human_decided_not_carried(self):
        self.run_round("r1", self.two_findings())
        jg.record_human(self.state, self.decision_id("fpA"), "false_positive", "drop", None)
        run2, _ = self.run_round("r2", [], scanned=[])
        self.assertEqual(self.carried(run2), {"fpB"})

    def test_redetected_this_round_not_duplicated(self):
        self.run_round("r1", self.two_findings())
        run2, _ = self.run_round("r2", [self.two_findings()[0]], scanned=["a.py"])
        self.assertEqual(self.carried(run2), {"fpB"})

    def test_scanned_but_gone_is_resolved_once(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=["a.py"])
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpB"}, 1))
        run3, summary = self.run_round("r3", [], scanned=[])
        self.assertEqual((self.carried(run3), summary["resolved"]), ({"fpB"}, 0))

    def test_not_resolved_when_tool_did_not_run(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=["a.py"], tools_ok=())
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpA", "fpB"}, 0))

    def test_full_round_resolves_everything_not_redetected(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [self.two_findings()[1]])
        self.assertEqual((self.carried(run2), summary["resolved"]), (set(), 1))

    def test_resolved_then_redetected_is_open_again(self):
        self.run_round("r1", self.two_findings())
        self.run_round("r2", [], scanned=["a.py"])                       # fpA resolved
        self.run_round("r3", [self.two_findings()[0]], scanned=["a.py"])  # fpA 又出現
        run4, _ = self.run_round("r4", [], scanned=[])
        self.assertEqual(self.carried(run4), {"fpA", "fpB"})

    # ── 檔案已不在 HEAD（#2053） ──

    def test_file_removed_resolved_without_tool(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=[], tools_ok=(), head=["b.py"])
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpB"}, 1))
        rec = self.resolved()[0]
        self.assertEqual((rec["fingerprint"], rec["reason_code"]), ("fpA", "file_removed"))
        item = jg.read_jsonl(run2 / "resolved.jsonl")[0]
        self.assertEqual((item["path"], item["human"]), ("a.py", None))
        run3, summary = self.run_round("r3", [], scanned=[], tools_ok=(), head=["b.py"])
        self.assertEqual((self.carried(run3), summary["resolved"]), ({"fpB"}, 0))  # 只寫一次

    def test_file_present_but_tool_failed_still_carried(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=["a.py"], tools_ok=(), head=["a.py", "b.py"])
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpA", "fpB"}, 0))

    def test_human_decided_then_file_removed_is_resolved(self):
        self.run_round("r1", self.two_findings())
        jg.record_human(self.state, self.decision_id("fpA"), "true_positive", "track", None)
        run2, summary = self.run_round("r2", [], scanned=[], head=["b.py"])
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpB"}, 1))
        item = jg.read_jsonl(run2 / "resolved.jsonl")[0]
        self.assertEqual(item["human"], {"label": "true_positive", "action": "track"})
        group = jg.triage_history(jg.read_jsonl(self.state / "shadow.jsonl"))[("WOOWTECH/demo", "fpA")]
        self.assertEqual((group["last"], group["resolved"]["reason_code"]), ("resolved", "file_removed"))

    def test_gitleaks_not_resolved_by_file_removal(self):
        rows = [dict(finding("a.py"), id="fa", fingerprint="fpA", tool="gitleaks")]
        self.run_round("r1", rows)
        run2, summary = self.run_round("r2", [], scanned=[], head=["b.py"])
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpA"}, 0))

    def test_no_repo_path_falls_back_to_old_rule(self):
        self.run_round("r1", self.two_findings())
        run2, summary = self.run_round("r2", [], scanned=[], tools_ok=())  # 舊 meta：沒有 repo_path
        self.assertEqual((self.carried(run2), summary["resolved"]), ({"fpA", "fpB"}, 0))

    def test_redetected_after_file_removed_is_open_again(self):
        self.run_round("r1", self.two_findings())
        self.run_round("r2", [], scanned=[], head=["b.py"])                       # fpA file_removed
        self.run_round("r3", [self.two_findings()[0]], scanned=["a.py"], head=["a.py", "b.py"])
        run4, _ = self.run_round("r4", [], scanned=[], tools_ok=(), head=["a.py", "b.py"])
        self.assertEqual(self.carried(run4), {"fpA", "fpB"})

    def test_other_repo_not_mixed(self):
        self.run_round("r1", self.two_findings(), repo="WOOWTECH/other")
        run2, _ = self.run_round("r2", [], scanned=[])
        self.assertEqual(self.carried(run2), set())

    def test_prior_human_marked_on_redetection(self):
        self.run_round("r1", self.two_findings())
        jg.record_human(self.state, self.decision_id("fpA"), "false_positive", "drop", "常數")
        run2, _ = self.run_round("r2", self.two_findings())
        recs = {r["fingerprint"]: r for r in jg.read_jsonl(run2 / "decisions.jsonl")}
        self.assertEqual(recs["fpA"]["prior_human"]["action"], "drop")
        self.assertNotIn("prior_human", recs["fpB"])


class ReadyTest(unittest.TestCase):
    GOOD = {"addresses": {"noul": 0.95}, "unrelated": {"noul": 0.05}, "weakens": {"noul": 0.02},
            "injection": {"noul": 0.01}}

    def case(self, **kw):
        base = {"finding": finding(), "ci": "pass", "rescan": {"finding_gone": True, "new_findings": 0},
                "review": {"verdict": "approve"}, "changed_files": ["models/x.py"], "attempt": 1}
        return base | kw

    def test_ready(self):
        self.assertEqual(jg.route_ready(self.case(), self.GOOD, CFG, gates())[0], "ready")

    def test_ci_fail_retries_then_abandons(self):
        self.assertEqual(jg.route_ready(self.case(ci="fail"), None, CFG, gates())[0], "retry")
        self.assertEqual(jg.route_ready(self.case(ci="fail", attempt=2), None, CFG, gates())[0], "abandon")

    def test_finding_not_gone_retries(self):
        c = self.case(rescan={"finding_gone": False})
        self.assertEqual(jg.route_ready(c, None, CFG, gates())[0], "retry")

    def test_touching_always_human_or_regression_tests(self):
        c = self.case(changed_files=["security/ir.model.access.csv"])
        self.assertEqual(jg.route_ready(c, None, CFG, gates())[0], "human")
        c = self.case(deleted_or_modified_tests=["tests/test_security_fixes.py"])
        self.assertEqual(jg.route_ready(c, None, CFG, gates())[0], "human")

    def test_weakens_goes_human(self):
        a = dict(self.GOOD, weakens={"noul": 0.7})
        self.assertEqual(jg.route_ready(self.case(), a, CFG, gates())[0], "human")

    def test_not_approved_review_retries(self):
        c = self.case(review={"verdict": "request_changes"})
        self.assertEqual(jg.route_ready(c, self.GOOD, CFG, gates())[0], "retry")


class HumanAndCalibrateTest(unittest.TestCase):
    def test_record_and_calibrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp)
            for i, (choice, conf, proposed) in enumerate([("false_positive", 0.95, "drop"),
                                                          ("true_positive", 0.6, "issue_only")]):
                jg.append_jsonl(sdir / "shadow.jsonl", {
                    "type": "decision", "id": f"d{i}", "gate": "triage", "proposed": proposed,
                    "answers": answers(choice, conf)})
            with self.assertRaises(jg.GateError):
                jg.record_human(sdir, "nope", "false_positive", "drop", None)
            jg.record_human(sdir, "d0", "false_positive", "drop", None)
            jg.record_human(sdir, "d1", "false_positive", "drop", "其實是誤報")
            out = jg.calibrate(sdir, "triage", 50)
            self.assertEqual(out["samples"], 2)
            self.assertFalse(out["enough"])
            by = {b["confidence"]: b for b in out["buckets"]}
            self.assertEqual(by["0.9–1.0"]["label_agreement"], 1.0)
            self.assertEqual(by["0.5–0.7"]["label_agreement"], 0.0)


class SecretHandlingTest(unittest.TestCase):
    def test_key_read_from_file_and_redacted_in_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "api_key"
            key_file.write_text("sk-test-SECRET\n")
            os.chmod(key_file, 0o600)
            body = io.BytesIO(b'{"detail": "bad key sk-test-SECRET"}')
            err = urllib.error.HTTPError("u", 500, "x", {}, body)
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(key_file)}), \
                    mock.patch.object(jg.urllib.request, "urlopen", side_effect=err):
                with self.assertRaises(jg.GateError) as ctx:
                    jg.http_json("GET", "/v1/models", gates())
            self.assertNotIn("sk-test-SECRET", str(ctx.exception))
            self.assertIn("***", str(ctx.exception))

    def test_401_is_auth_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "api_key"
            key_file.write_text("k")
            err = urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}"))
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(key_file)}), \
                    mock.patch.object(jg.urllib.request, "urlopen", side_effect=err):
                with self.assertRaises(jg.AuthError):
                    jg.http_json("GET", "/v1/models", gates())

    def test_missing_key_file(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": "/nonexistent/api_key"}):
            with self.assertRaises(jg.GateError):
                jg.load_api_key()


if __name__ == "__main__":
    unittest.main()
