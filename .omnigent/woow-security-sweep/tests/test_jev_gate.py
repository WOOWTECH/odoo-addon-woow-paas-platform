"""jev_gate.py 的單元測試（不打網路）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
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
