"""collect.py 的單元測試（不打網路、不跑掃描工具）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SWEEP = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("collect", SWEEP / "skills/sec-collect/scripts/collect.py")
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          env=os.environ | GIT_ENV).stdout.strip()


PUBLIC = """version: 1
repo: {full_name: WOOWTECH/demo, default_branch: main}
scan:
  exclude: [tests/, "**/__pycache__/"]
  tools: {}
always_human: {paths: [security/], severities: [critical], topics: []}
autofix: {enabled: true}
verify: {regression_tests: [tests/test_x.py]}
"""


class RepoFixture(unittest.TestCase):
    """bare origin ＋ clone（clone 名稱即 repo 名，對應 targets/<name>.yaml）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        sh("git", "init", "--bare", "-b", "main", str(self.origin), cwd=root)
        self.repo = root / "demo"
        sh("git", "clone", "-q", str(self.origin), str(self.repo), cwd=root)
        sh("git", "checkout", "-q", "-b", "main", cwd=self.repo)
        (self.repo / "models").mkdir()
        (self.repo / "models" / "x.py").write_text("a = 1\nb = 2\n")
        (self.repo / ".security").mkdir()
        (self.repo / ".security" / "sweep.yaml").write_text(PUBLIC)
        self.commit("init")
        sh("git", "remote", "set-head", "origin", "main", cwd=self.repo)
        self.targets = root / "targets"
        self.targets.mkdir()
        self.state = root / "state"
        self.runs = root / "runs"

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg: str) -> str:
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", msg, cwd=self.repo)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=self.repo)
        sh("git", "fetch", "-q", "origin", cwd=self.repo)
        return sh("git", "rev-parse", "HEAD", cwd=self.repo)


class ConfigTest(RepoFixture):
    def test_reads_committed_main_not_working_tree(self):
        (self.repo / ".security" / "sweep.yaml").write_text("autofix: {enabled: false}\n")  # 未 commit
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertTrue(cfg["autofix"]["enabled"])
        self.assertEqual(cfg["_sources"]["public"], "origin/main:.security/sweep.yaml")

    def test_private_merges_only_private_keys(self):
        (self.targets / "demo.yaml").write_text(
            "context: {summary: secret map}\nbaseline: []\nalways_human: {paths: []}\n")
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertEqual(cfg["context"]["summary"], "secret map")
        self.assertEqual(cfg["always_human"]["paths"], ["security/"])  # private 不能放寬 public 規則

    def test_default_when_missing(self):
        sh("git", "rm", "-q", "-r", ".security", cwd=self.repo)
        self.commit("drop config")
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertEqual(cfg["_sources"]["public"], "default")
        self.assertFalse(cfg["autofix"]["enabled"])


class ExcludeTest(unittest.TestCase):
    def test_patterns(self):
        pats = ["tests/", "**/__pycache__/", "*.po"]
        self.assertTrue(cl.is_excluded("tests/test_a.py", pats))
        self.assertTrue(cl.is_excluded("models/__pycache__/a.pyc", pats))
        self.assertTrue(cl.is_excluded("i18n/zh_TW.po", pats))
        self.assertFalse(cl.is_excluded("models/tests_helper.py", pats))


class FingerprintTest(unittest.TestCase):
    def test_stable_when_line_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "a.py").write_text("x = 1\neval(y)\n")
            f1 = cl.make_finding(src, tool="t", rule="r", path="a.py", line=2, severity="low", message="m")
            (src / "a.py").write_text("# new\nx = 1\neval(y)\n")
            f2 = cl.make_finding(src, tool="t", rule="r", path="a.py", line=3, severity="low", message="m")
            self.assertEqual(f1["fingerprint"], f2["fingerprint"])
            self.assertIn("eval(y)", f2["snippet"])


class RunTest(RepoFixture):
    def run_once(self, mode="incremental"):
        return cl.cmd_run(self.repo, self.runs, mode, self.state, fetch=False, targets_dir=self.targets)

    def test_first_run_full_then_no_change(self):
        meta = self.run_once()
        self.assertEqual(meta["scope"]["mode"], "full")
        self.assertTrue((self.runs / "demo" / "src" / "models" / "x.py").exists())
        self.assertIn("models/x.py", meta["scanned_files"])  # carryover 判 resolved 用
        cl.cmd_commit(Path(meta["run_dir"]), self.state)
        meta = self.run_once()
        self.assertEqual((meta["scope"]["mode"], meta["scope"]["changed"]), ("incremental", []))
        self.assertEqual(meta["tools"]["_all"]["status"], "skipped")
        self.assertEqual(meta["scanned_files"], [])

    def test_security_change_creates_finding(self):
        cl.cmd_commit(Path(self.run_once()["run_dir"]), self.state)
        (self.repo / ".security" / "sweep.yaml").write_text(PUBLIC + "baseline: []\n")
        self.commit("touch config")
        meta = self.run_once()
        self.assertTrue(meta["config_changed"])
        rows = [json.loads(x) for x in (self.runs / "demo" / "findings.jsonl").read_text().splitlines()]
        self.assertIn("security-config-changed", {r["rule"] for r in rows})

    def test_symlink_outside_snapshot_is_reported(self):
        os.symlink("/etc/passwd", self.repo / "models" / "leak")
        self.commit("add symlink")
        meta = self.run_once()
        self.assertTrue(meta["unsafe_members"])
        self.assertFalse((self.runs / "demo" / "src" / "models" / "leak").exists())
        rows = [json.loads(x) for x in (self.runs / "demo" / "findings.jsonl").read_text().splitlines()]
        self.assertIn("unsafe-archive-member", {r["rule"] for r in rows})

    def test_snapshot_refuses_without_data_filter(self):
        # 模擬 Python < 3.11.4：沒有 data_filter 時要中止，不能在無檢查下解開（#1934）
        with mock.patch.object(cl, "tarfile", types.SimpleNamespace()):
            with self.assertRaisesRegex(cl.CollectError, "python3.12"):
                cl.snapshot(self.repo, "HEAD", self.runs / "snap")
        self.assertFalse((self.runs / "snap").exists())

    def test_import_rejects_untrusted_rows(self):
        run_dir = Path(self.run_once()["run_dir"])
        src = Path(self.tmp.name) / "review.jsonl"
        rows = [{"rule": "sudo-without-check", "path": "models/x.py", "line": 2, "severity": "HIGH",
                 "message": "m"},
                {"rule": "r", "path": "../../etc/passwd", "message": "escape"},
                {"rule": "r", "path": "models/nope.py", "message": "missing file"},
                {"path": "models/x.py", "message": "no rule"}]
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        out = cl.cmd_import(run_dir, src)
        self.assertEqual(out["added"], 1)
        self.assertEqual(len(out["rejected"]), 4)
        again = cl.cmd_import(run_dir, src)  # 重複匯入以 fingerprint 去重
        self.assertEqual(again["added"], 0)
        last = json.loads((run_dir / "findings.jsonl").read_text().splitlines()[-1])
        self.assertEqual((last["tool"], last["severity_tool"]), ("review", "high"))

    def test_force_push_falls_back_to_full(self):
        (self.state / "repos").mkdir(parents=True)
        (self.state / "repos" / "demo.json").write_text(json.dumps({"last_sha": "0" * 40}))
        meta = self.run_once()
        self.assertEqual(meta["scope"]["mode"], "full")



class PresetTest(unittest.TestCase):
    """排程 prompt 只放 `preset: <名稱>`（#1989：長 prompt 在 Omnigent 0.16.0 不會送出）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.state = self.ws / ".omnigent-runtime" / "woow-security-sweep"
        (self.state / "presets").mkdir(parents=True)
        self.repo = self.ws / "WOOW" / "demo"
        (self.repo / ".git").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, name="line-daily"):
        (self.state / "presets" / f"{name}.yaml").write_text(text)

    def test_defaults_and_dedupe(self):
        self.write(f"targets: [{self.repo}, {self.repo}]\n")
        got = cl.cmd_preset("line-daily", self.state)
        self.assertEqual((got["targets"], got["mode"], got["deep_review"], got["roster"]),
                         ([str(self.repo.resolve())], "incremental", "auto", {}))

    def test_cli_exit_codes(self):
        self.write(f"targets: [{self.repo}]\nmode: full\n")
        self.assertEqual(cl.main(["--state-dir", str(self.state), "preset", "--name", "line-daily"]), 0)
        self.assertEqual(cl.main(["--state-dir", str(self.state), "preset", "--name", "missing"]), 2)

    def test_rejections(self):
        cases = {
            "unknown key": f"targets: [{self.repo}]\nignore_checks: true\n",
            "empty targets": "targets: []\n",
            "relative": "targets: [WOOW/demo]\n",
            "outside ws": "targets: [/etc]\n",
            "bad mode": f"targets: [{self.repo}]\nmode: yolo\n",
            "bad roster": f"targets: [{self.repo}]\nroster: {{fix: codex}}\n",
            "not yaml": "targets: [\n",
        }
        for why, text in cases.items():
            with self.subTest(why=why):
                self.write(text)
                with self.assertRaises(cl.CollectError):
                    cl.cmd_preset("line-daily", self.state)

    def test_bad_name(self):
        for name in ("../x", "Line", "", "a" * 40):
            with self.subTest(name=name), self.assertRaises(cl.CollectError):
                cl.cmd_preset(name, self.state)

if __name__ == "__main__":
    unittest.main()
