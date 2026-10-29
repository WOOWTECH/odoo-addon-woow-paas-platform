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
        self.assertTrue(cfg["_sources"]["public"].startswith("default"))
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


class UpstreamAndDeletedTest(RepoFixture):
    """context.upstream 的唯讀快照（#2064）與增量範圍裡已刪除檔案的分列。"""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        origin = root / "base.git"
        sh("git", "init", "--bare", "-b", "main", str(origin), cwd=root)
        self.base = root / "base"
        sh("git", "clone", "-q", str(origin), str(self.base), cwd=root)
        sh("git", "checkout", "-q", "-b", "main", cwd=self.base)
        (self.base / "models").mkdir()
        (self.base / "models" / "api.py").write_text("def verify():\n    return True\n")
        sh("git", "add", "-A", cwd=self.base)
        sh("git", "commit", "-q", "-m", "init", cwd=self.base)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=self.base)
        sh("git", "fetch", "-q", "origin", cwd=self.base)
        sh("git", "remote", "set-head", "origin", "main", cwd=self.base)
        # 只認 origin 指向 targets 寫的 owner/repo 的 clone
        sh("git", "remote", "set-url", "origin", "https://github.com/WOOWTECH/base.git", cwd=self.base)
        (self.targets / "demo.yaml").write_text(
            "context:\n  upstream: [WOOWTECH/base, WOOWTECH/missing, 'bad name']\n")

    def run_once(self, mode="incremental"):
        return cl.cmd_run(self.repo, self.runs, mode, self.state, fetch=False, targets_dir=self.targets)

    def test_upstream_snapshot_and_status(self):
        meta = self.run_once()
        ups = {u["repo"]: u for u in meta["upstream"]}
        self.assertEqual(ups["WOOWTECH/base"]["status"], "ok")
        self.assertEqual(ups["WOOWTECH/base"]["path"], "upstream/base")
        self.assertTrue((self.runs / "demo" / "upstream" / "base" / "models" / "api.py").is_file())
        self.assertEqual(ups["WOOWTECH/missing"]["status"], "missing")
        self.assertEqual(ups["bad name"]["status"], "error")
        # 上游只給 worker 讀：不進掃描範圍
        self.assertFalse(any(p.startswith("upstream") for p in meta["scanned_files"]))

    def test_origin_mismatch_is_not_upstream(self):
        sh("git", "remote", "set-url", "origin", "https://github.com/evil/base.git", cwd=self.base)
        meta = self.run_once()
        self.assertEqual({u["repo"]: u["status"] for u in meta["upstream"]}["WOOWTECH/base"], "missing")
        self.assertFalse((self.runs / "demo" / "upstream").exists())

    def test_deleted_files_listed_separately(self):
        (self.repo / "models" / "y.py").write_text("c = 3\n")
        self.commit("add y")
        cl.cmd_commit(Path(self.run_once()["run_dir"]), self.state)
        (self.repo / "models" / "y.py").unlink()
        (self.repo / "models" / "x.py").write_text("a = 9\n")
        self.commit("rm y")
        meta = self.run_once()
        self.assertEqual(meta["deleted_files"], ["models/y.py"])
        self.assertEqual(meta["scanned_files"], ["models/x.py"])


class NewFormatImportTest(RepoFixture):
    """deep review 新回報格式：根因 fingerprint、confirmed／needs_validation、hardening、coverage、verdict、promote。"""

    def setUp(self):
        super().setUp()
        meta = cl.cmd_run(self.repo, self.runs, "full", self.state, fetch=False, targets_dir=self.targets)
        self.run_dir = Path(meta["run_dir"])
        up = self.run_dir / "upstream" / "base" / "models"
        up.mkdir(parents=True)
        (up / "api.py").write_text("def verify():\n    return True\n")

    def imp(self, rows):
        src = Path(self.tmp.name) / "review.jsonl"
        src.write_text("\n".join(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        return cl.cmd_import(self.run_dir, src)

    def nv(self, **kw):
        row = {"type": "finding", "verdict": "needs_validation", "fingerprint": "email-claim-links-partner",
               "title": "以 email 綁定既有 partner", "path": "models/x.py", "line": 1, "root_cause": "email 當成所有權證明",
               "trace": [{"kind": "entrypoint", "file": "models/x.py", "line": 1, "description": "入口"},
                         {"kind": "sink", "file": "upstream/base/models/api.py", "line": 1, "description": "上游驗證"}],
               "blockers": ["Odoo core 的行為"], "validation_plan": {"local": "fixture 步驟"}}
        row.update(kw)
        return row

    def findings(self):
        return cl.read_jsonl(self.run_dir / "findings.jsonl")

    def test_needs_validation_uses_root_cause_fingerprint(self):
        out = self.imp([self.nv(severity="high")])
        self.assertEqual(out["added"], 1)
        self.assertTrue(any("severity" in w for w in out["warnings"]))
        f = self.findings()[-1]
        self.assertEqual(f["fingerprint"], "review:email-claim-links-partner")
        self.assertEqual((f["verdict"], f["severity_tool"]), ("needs_validation", None))
        self.assertEqual(f["trace"][-1]["file"], "upstream/base/models/api.py")
        self.assertIn("待確認：Odoo core 的行為", f["message"])
        # 同一根因換了行號 ⇒ 同一個 fingerprint，不會變成新的 finding
        self.assertEqual(self.imp([self.nv(line=2)])["added"], 0)

    def test_confirmed_requires_severity_and_remediation(self):
        row = self.nv(verdict="confirmed")
        out = self.imp([row])
        self.assertEqual((out["added"], len(out["rejected"])), (0, 1))
        row.update(severity="medium", remediation="綁定前驗證所有權")
        out = self.imp([row])
        self.assertEqual(out["added"], 1)
        f = self.findings()[-1]
        self.assertEqual((f["severity_tool"], f["severity"]), ("medium", "medium"))
        self.assertNotIn("blockers", f)

    def test_rejects_untrusted_rows(self):
        bad = [self.nv(path="upstream/base/models/api.py"),  # 主要位置不能在上游
               self.nv(fingerprint="has space"),
               self.nv(trace=[{"kind": "sink", "file": "models/x.py", "line": 1},
                              {"kind": "entrypoint", "file": "models/x.py", "line": 1}]),
               self.nv(trace=[{"kind": "entrypoint", "file": "../../etc/passwd", "line": 1}]),
               self.nv(blockers=[]),
               {"type": "weird"}]
        out = self.imp(bad)
        self.assertEqual((out["added"], len(out["rejected"])), (0, 6))

    def test_middle_sink_relabelled_with_warning(self):
        row = self.nv(trace=[{"kind": "entrypoint", "file": "models/x.py", "line": 1},
                             {"kind": "sink", "file": "models/x.py", "line": 2},
                             {"kind": "sink", "file": "models/x.py", "line": 2}])
        out = self.imp([row])
        self.assertEqual(out["added"], 1)
        self.assertEqual([s["kind"] for s in self.findings()[-1]["trace"]], ["entrypoint", "propagation", "sink"])
        self.assertTrue(out["warnings"])

    def test_prior_fingerprint_reused_verbatim(self):
        legacy = "review:sudo-without-check:models/x.py:abc123"
        (self.run_dir / "prior_findings.json").write_text(json.dumps([{"fingerprint": legacy}]))
        out = self.imp([self.nv(fingerprint=legacy),
                        {"type": "prior", "fingerprint": legacy, "status": "fixed", "note": "已加檢查"},
                        {"type": "prior", "fingerprint": "review:never-listed", "status": "fixed"}])
        self.assertEqual(self.findings()[-1]["fingerprint"], legacy)
        self.assertEqual((out["prior"], len(out["rejected"])), (1, 1))
        self.assertEqual(len(cl.read_jsonl(self.run_dir / "prior_status.jsonl")), 1)

    def test_hardening_and_coverage_are_not_findings(self):
        out = self.imp([{"type": "hardening", "path": "models/x.py", "line": 1, "note": "加上 CSP"},
                        {"type": "coverage", "entry": "/x", "status": "reviewed", "note": "看過"},
                        {"type": "coverage", "entry": "/y", "status": "maybe"}])
        self.assertEqual((out["hardening"], out["coverage"], len(out["rejected"])), (1, 1, 1))
        self.assertEqual(self.findings(), [])
        hid = cl.read_jsonl(self.run_dir / "hardening.jsonl")[0]["id"]
        res = cl.cmd_promote(self.run_dir, hid)
        f = self.findings()[-1]
        self.assertEqual((f["id"], f["verdict"], f["fingerprint"]),
                         (res["finding_id"], "promoted_hardening", f"review:hardening/{hid}"))
        self.assertTrue(cl.cmd_promote(self.run_dir, hid)["already"])
        self.assertEqual(len(self.findings()), 1)
        with self.assertRaises(cl.CollectError):
            cl.cmd_promote(self.run_dir, "nope")

    def test_verdict_rejected_then_replaced(self):
        self.imp([self.nv(), self.nv(fingerprint="second-root-cause")])
        a, b = self.findings()
        v = Path(self.tmp.name) / "verdict.json"
        v.write_text(json.dumps({"decision": "rejected", "record": {
            "fingerprint": "email-claim-links-partner", "title": "t", "reason": "上游已驗證所有權"}}))
        cl.cmd_verdict(self.run_dir, a["id"], v)
        self.assertEqual([f["id"] for f in self.findings()], [b["id"]])
        self.assertEqual(cl.read_jsonl(self.run_dir / "rejected.jsonl")[0]["reason"], "上游已驗證所有權")
        v.write_text(json.dumps({"decision": "confirmed", "record": {
            "fingerprint": "second-root-cause", "title": "t2", "root_cause": "rc2",
            "trace": [{"kind": "sink", "file": "models/x.py", "line": 2}], "severity": "low", "remediation": "r"}}))
        cl.cmd_verdict(self.run_dir, b["id"], v)
        f = self.findings()[0]
        self.assertEqual((f["verdict"], f["severity_tool"], f["verified"]["decision"]), ("confirmed", "low", "confirmed"))
        self.assertNotIn("blockers", f)
        v.write_text(json.dumps({"decision": "confirmed", "record": {"fingerprint": "another-root-cause", "title": "x"}}))
        with self.assertRaisesRegex(cl.CollectError, "不同根因"):
            cl.cmd_verdict(self.run_dir, b["id"], v)
        v.write_text(json.dumps({"decision": "rejected", "record": {}, "extra": 1}))
        with self.assertRaises(cl.CollectError):
            cl.cmd_verdict(self.run_dir, b["id"], v)


class StackAndRemoteTest(RepoFixture):
    """#2064 階段 B：stack profile、targets 新欄位、origin 解析。"""

    def no_public(self):
        sh("git", "rm", "-q", "-r", ".security", cwd=self.repo)
        self.commit("drop config")

    def test_parse_remote(self):
        cases = {
            "https://github.com/WOOWTECH/x.git": ("github.com", "WOOWTECH/x"),
            "git@github.com:WOOWTECH/x.git": ("github.com", "WOOWTECH/x"),
            "ssh://git@github.com/WOOWTECH/x": ("github.com", "WOOWTECH/x"),
            "https://git-prod.woowtech.io/ha-components/Woow_ha_ai_mcp-shc.git/": (
                "git-prod.woowtech.io", "ha-components/Woow_ha_ai_mcp-shc"),
            "/tmp/origin.git": (None, None),
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(cl.parse_remote(url), want)

    def test_stack_defaults_when_repo_has_no_config(self):
        self.no_public()
        (self.targets / "demo.yaml").write_text("stack: odoo\n")
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertIn("*/__manifest__.py", cfg["always_human"]["paths"])
        self.assertNotIn("bandit", cfg["scan"]["tools"])  # #2064：bandit 0／6 成立，預設拿掉
        self.assertEqual(cfg["stack"]["name"], "odoo")
        self.assertIn("odoo.md#sudo() scope creep", cfg["review_classes"])
        self.assertEqual(cfg["_sources"]["stack"], "odoo")

    def test_repo_config_wins_over_stack_and_scan_exclude(self):
        (self.targets / "demo.yaml").write_text("stack: odoo\nscan_exclude: [models/]\n")
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertEqual(cfg["always_human"]["paths"], ["security/"])
        self.assertNotIn("models/", cfg["scan"]["exclude"])  # repo 有 .security/sweep.yaml ⇒ targets 不能追加排除
        self.no_public()
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertIn("models/", cfg["scan"]["exclude"])

    def test_targets_review_classes_override_stack(self):
        (self.targets / "demo.yaml").write_text("stack: odoo\nreview_classes: ['ATTACK-CLASSES.md#Injection']\n")
        cfg = cl.load_config(self.repo, "origin/main", self.targets)
        self.assertEqual(cfg["review_classes"], ["ATTACK-CLASSES.md#Injection"])

    def test_bad_or_missing_stack_aborts(self):
        for body in ("stack: ../x\n", "stack: nope\n"):
            with self.subTest(body=body):
                (self.targets / "demo.yaml").write_text(body)
                with self.assertRaises(cl.CollectError):
                    cl.load_config(self.repo, "origin/main", self.targets)

    def test_gitea_origin_full_name_and_web_url(self):
        sh("git", "remote", "set-url", "origin", "https://git-prod.woowtech.io/ha-components/demo.git", cwd=self.repo)
        repo_cfg = cl.load_config(self.repo, "origin/main", self.targets)["repo"]
        self.assertEqual(repo_cfg["host"], "git-prod.woowtech.io")
        self.assertEqual(repo_cfg["web_url"], "https://git-prod.woowtech.io/WOOWTECH/demo")  # full_name 取自 public 設定

    def test_branch_from_targets(self):
        sh("git", "checkout", "-q", "-b", "delivery", cwd=self.repo)
        (self.repo / "models" / "z.py").write_text("z = 1\n")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "on delivery", cwd=self.repo)
        sh("git", "push", "-q", "origin", "HEAD:delivery", cwd=self.repo)
        sh("git", "fetch", "-q", "origin", cwd=self.repo)
        (self.targets / "demo.yaml").write_text("branch: delivery\n")
        meta = cl.cmd_run(self.repo, self.runs, "full", self.state, fetch=False, targets_dir=self.targets)
        self.assertEqual(meta["branch"], "delivery")
        self.assertIn("models/z.py", meta["scanned_files"])
        for bad in ("nope", "../main", "-x"):
            with self.subTest(bad=bad):
                (self.targets / "demo.yaml").write_text(f"branch: '{bad}'\n")
                with self.assertRaises(cl.CollectError):
                    cl.cmd_run(self.repo, self.runs, "full", self.state, fetch=False, targets_dir=self.targets)


class ForkScopeTest(RepoFixture):
    """since_upstream：只掃 fork 相對上游 merge-base 改過的檔案。"""

    REMOTE = "https://example.invalid/upstream/demo.git"

    def setUp(self):
        super().setUp()
        self.base = sh("git", "rev-parse", "HEAD", cwd=self.repo)
        sh("git", "update-ref", "refs/sweep/upstream/main", self.base, cwd=self.repo)  # 假裝上游停在 base
        (self.repo / "models" / "mine.py").write_text("m = 1\n")
        self.head = self.commit("fork change")
        (self.targets / "demo.yaml").write_text(f"since_upstream: {{remote: '{self.REMOTE}', branch: main}}\n")

    def test_only_fork_files_scanned(self):
        meta = cl.cmd_run(self.repo, self.runs, "full", self.state, fetch=False, targets_dir=self.targets)
        self.assertEqual(meta["scanned_files"], ["models/mine.py"])
        fork = meta["fork"]
        self.assertEqual((fork["merge_base"], fork["ahead"], fork["behind"]), (self.base, 1, 0))
        self.assertEqual(fork["files"], ["models/mine.py"])

    def test_never_fetched_aborts_instead_of_full_scan(self):
        sh("git", "update-ref", "-d", "refs/sweep/upstream/main", cwd=self.repo)
        with self.assertRaisesRegex(cl.CollectError, "從未 fetch 成功"):
            cl.cmd_run(self.repo, self.runs, "full", self.state, fetch=False, targets_dir=self.targets)

    def test_remote_must_be_https(self):
        for remote in ("file:///etc", "ext::sh -c id", "/tmp/x.git", "git@github.com:a/b.git",
                       "https://example.invalid/../x"):
            with self.subTest(remote=remote), self.assertRaises(cl.CollectError):
                cl.fork_scope(self.repo, self.head, {"remote": remote, "branch": "main"}, fetch=False)


class AdvisoryTest(unittest.TestCase):
    def test_version_range(self):
        cases = [("7.14.1", "<= 8.7.0", True), ("7.14.1", "<= 7.13", False), ("7.14.1", ">= 7.0, < 7.14.1", False),
                 ("9.0.0", "< 9.0.0.dev3159", False), ("9.0.0.dev3158", "< 9.0.0.dev3159", True),
                 ("1.5", ">= 1.0, < 2.0", True), ("1.2.3", "= 1.2.3", True), ("abc", "<= 1", None),
                 ("1.0", "", None), ("1.0", "~> 1.0", None)]
        for version, spec, want in cases:
            with self.subTest(version=version, spec=spec):
                self.assertEqual(cl.in_range(version, spec), want)

    def adv(self, ghsa, spec, package="ha-mcp", severity="medium"):
        return {"ghsa_id": ghsa, "summary": "s", "severity": severity, "published_at": "2026-10-24",
                "vulnerabilities": [{"package": {"name": package}, "vulnerable_version_range": spec,
                                     "patched_versions": "9.0.0"}]}

    def test_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "pyproject.toml").write_text('[project]\nname = "ha-mcp"\nversion = "7.14.1"\n')
            conf = {"github": "up/ha-mcp", "package": "ha-mcp", "version_file": "pyproject.toml"}
            advs = [self.adv("GHSA-aaaa-bbbb-cccc", "<= 8.7.0"), self.adv("GHSA-dddd-eeee-ffff", "<= 7.13"),
                    self.adv("GHSA-gggg-hhhh-jjjj", "<= 99", package="other"), {"ghsa_id": "../x"}]
            found, status = cl.advisory_findings(src, conf, advs)
            self.assertEqual([f["fingerprint"] for f in found], ["advisory:GHSA-aaaa-bbbb-cccc"])
            self.assertEqual((found[0]["path"], found[0]["line"], status["version"]), ("pyproject.toml", 3, "7.14.1"))
            self.assertTrue(found[0]["advisory"]["affected"])
            # 讀不到版號 ⇒ 範圍內外都無法判斷，一律列出交人
            (src / "pyproject.toml").write_text("[project]\n")
            found, status = cl.advisory_findings(src, conf, advs[:2])
            self.assertEqual(len(found), 2)
            self.assertIn("無法比對版本", found[0]["message"])
            self.assertIn("讀不到", status["detail"])

    def test_gh_missing_is_unavailable(self):
        with mock.patch.object(cl.shutil, "which", return_value=None):
            found, status, paths = cl.collect_advisories(Path("."), {"github": "a/b"})
        self.assertEqual((found, status["status"], paths), ([], "unavailable", []))



ROUTES_A = """from odoo import http
from odoo.http import route

class A(http.Controller):
    @http.route(['/ha/a', '/ha/b'], type='json', auth='none', csrf=False)
    def a(self):
        pass

    @route('/api/ws/<int:wid>/x', auth='user')
    def b(self):
        pass

    @route(ROUTE_CONST, auth='public')
    def c(self):
        pass

    @http.route('/legacy/old', auth='user')
    def d(self):
        pass

    @route()
    def web_login(self):
        pass
"""

ROUTES_OP = """from fastapi import APIRouter
router = APIRouter(prefix="/api")

@router.get("/releases/{name}")
async def get_release(name: str):
    raise SystemExit("ast 不會執行這裡")

@router.post(path="/releases")
def create():
    pass
"""

UNITS_TARGET = """review_units:
  per_run: 1
  incremental_max_files: 1
  cover: [controllers/, models/]
  units:
    - name: unauth
      focus: 不需登入的入口
      paths: [controllers/a.py]
      entry_points: {extract: odoo-route, auth: [none, public]}
    - name: ws-api
      paths: [controllers/a.py, models/x.py]
      entry_points: {extract: odoo-route, auth: [user], routes: ["/api/ws/*"]}
      review_classes: ["ATTACK-CLASSES.md#Business logic"]
    - name: operator
      paths: [op/*.py, gone/, controllers/]
      exclude_paths: [controllers/]
      entry_points: {extract: fastapi-route, extra: [{route: "kubectl exec", note: "手動列的入口"}]}
"""


class UnitsConfigTest(unittest.TestCase):
    """#2064 階段 C：review_units 格式錯一律中止，不能靜默退回全量。"""

    def test_defaults_and_clamp(self):
        conf = cl.validate_units({"per_run": 9, "units": [{"name": "a", "paths": ["x/"]}]})
        self.assertEqual((conf["per_run"], conf["incremental_max_files"]), (1, cl.DEFAULT_INCREMENTAL_MAX))
        self.assertIsNone(cl.validate_units(None))

    def test_rejects_bad_config(self):
        bad = [
            {"units": []},
            {"units": [{"name": "a", "paths": ["x"]}], "extra": 1},
            {"units": [{"name": "A b", "paths": ["x"]}]},
            {"units": [{"name": "a", "paths": ["x"]}, {"name": "a", "paths": ["y"]}]},
            {"units": [{"name": "a"}]},
            {"units": [{"name": "a", "paths": ["x"], "entry_points": {"extract": "django"}}]},
            {"units": [{"name": "a", "paths": ["x"], "entry_points": {"extra": [{"note": "沒有 route"}]}}]},
            {"per_run": 0, "units": [{"name": "a", "paths": ["x"]}]},
            {"per_run": True, "units": [{"name": "a", "paths": ["x"]}]},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(cl.CollectError):
                cl.validate_units(raw)


class TargetsUnitsTest(unittest.TestCase):
    def test_every_target_review_units_valid(self):
        import yaml
        for path in sorted((SWEEP / "targets").glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if "review_units" in data:
                with self.subTest(target=path.name):
                    conf = cl.validate_units(data["review_units"])
                    self.assertGreaterEqual(len(conf["units"]), conf["per_run"])


class ExtractRoutesTest(unittest.TestCase):
    def test_odoo_and_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "a.py").write_text(ROUTES_A)
            (src / "op.py").write_text(ROUTES_OP)
            (src / "broken.py").write_text("def x(:\n")
            rows, errors = cl.extract_routes(src, ["a.py", "broken.py", "README.md"], "odoo-route")
            by = {r["route"]: r for r in rows}
            self.assertEqual(by["/ha/a"], {"route": "/ha/a", "file": "a.py", "line": 6, "func": "a", "auth": "none",
                                           "type": "json", "csrf": False})
            self.assertIn("/ha/b", by)
            self.assertEqual(by["/api/ws/<int:wid>/x"]["auth"], "user")
            self.assertIn("<ROUTE_CONST>", by)  # 不是字面值：照原樣標示
            # 裸 @route() 覆寫父類：auth 看不出來，不能當成 Odoo 預設的 user
            self.assertEqual({k: by["<繼承父類 route：web_login>"][k] for k in ("auth", "type")},
                             {"auth": "inherited", "type": "inherited"})
            self.assertEqual(errors, ["broken.py：SyntaxError"])
            rows, _ = cl.extract_routes(src, ["op.py"], "fastapi-route")
            self.assertEqual([(r["route"], r["methods"], r["router"]) for r in rows],
                             [("/releases/{name}", ["GET"], "router"), ("/releases", ["POST"], "router")])


class UnitsRunTest(RepoFixture):
    def setUp(self):
        super().setUp()
        (self.repo / "controllers").mkdir()
        (self.repo / "controllers" / "a.py").write_text(ROUTES_A)
        (self.repo / "models" / "y.py").write_text("z = 1\n")  # cover 內但不屬於任何塊
        (self.repo / "op").mkdir()
        (self.repo / "op" / "api.py").write_text(ROUTES_OP)
        (self.repo / "README.md").write_text("doc\n")
        self.commit("add units fixture")
        (self.targets / "demo.yaml").write_text(UNITS_TARGET)

    def run_once(self, mode="full", units=None):
        return cl.cmd_run(self.repo, self.runs, mode, self.state, fetch=False, targets_dir=self.targets, units=units)

    def report(self, run_dir: Path, rows: list[dict]) -> Path:
        path = run_dir.parent / f"report-{len(list(run_dir.parent.glob('report-*')))}.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        return path

    def test_rotation_follows_imports(self):
        meta = self.run_once()
        review = meta["review"]
        self.assertEqual(review["mode"], "units")
        self.assertEqual([u["name"] for u in review["units"]], ["unauth"])
        unit = review["units"][0]
        self.assertEqual(unit["files"], ["controllers/a.py"])
        self.assertEqual(sorted(e["route"] for e in unit["entry_points"]), ["/ha/a", "/ha/b", "<ROUTE_CONST>"])
        self.assertEqual(review["next_due"], ["ws-api"])
        self.assertEqual(review["unassigned_files"], ["models/y.py"])
        self.assertEqual(sorted(r["route"] for r in review["unassigned_routes"]),
                         ["/legacy/old", "<繼承父類 route：web_login>"])
        run_dir = Path(meta["run_dir"])
        cov = self.report(run_dir, [{"type": "coverage", "entry": "/ha/a", "status": "reviewed"}])
        with self.assertRaisesRegex(cl.CollectError, "--unit"):
            cl.cmd_import(run_dir, cov)
        with self.assertRaisesRegex(cl.CollectError, "--unit"):
            cl.cmd_import(run_dir, cov, unit="ws-api")  # 不是本輪的塊
        out = cl.cmd_import(run_dir, cov, unit="unauth")
        self.assertEqual((out["coverage"], out["unit"]), (1, "unauth"))
        cov_rows = [json.loads(x) for x in (run_dir / "coverage.jsonl").read_text().splitlines()]
        self.assertEqual(cov_rows[0]["unit"], "unauth")
        state = cl.cmd_commit(run_dir, self.state)
        self.assertEqual(state["units_reviewed"], ["unauth"])
        self.assertIn("unauth", state["units"])
        # 第二輪輪到下一塊；last_sha 與分塊進度都保留
        meta = self.run_once()
        self.assertEqual([u["name"] for u in meta["review"]["units"]], ["ws-api"])
        ws = meta["review"]["units"][0]
        self.assertEqual([e["route"] for e in ws["entry_points"]], ["/api/ws/<int:wid>/x"])
        self.assertEqual(ws["review_classes"], ["ATTACK-CLASSES.md#Business logic"])
        cl.cmd_commit(Path(meta["run_dir"]), self.state)  # worker 沒回報：這塊不算審完
        saved = json.loads((self.state / "repos" / "demo.json").read_text())
        self.assertEqual(sorted(saved["units"]), ["unauth"])
        self.assertEqual(saved["last_sha"], meta["head_sha"])
        meta = self.run_once()
        self.assertEqual([u["name"] for u in meta["review"]["units"]], ["ws-api"])

    def test_forced_units_and_missing_paths(self):
        meta = self.run_once(units=["operator"])
        unit = meta["review"]["units"][0]
        self.assertTrue(meta["review"]["forced"])
        self.assertEqual(unit["files"], ["op/api.py"])
        self.assertEqual(unit["missing_paths"], ["gone/"])
        self.assertEqual([e["route"] for e in unit["entry_points"]], ["/releases/{name}", "/releases", "kubectl exec"])
        with self.assertRaisesRegex(cl.CollectError, "不存在的塊"):
            self.run_once(units=["nope"])
        with self.assertRaisesRegex(cl.CollectError, "--mode full"):
            self.run_once(mode="incremental", units=["unauth"])

    def test_incremental_reviews_only_unit_files_with_cap(self):
        cl.cmd_commit(Path(self.run_once()["run_dir"]), self.state)
        (self.repo / "controllers" / "a.py").write_text(ROUTES_A + "\n# touched\n")
        (self.repo / "op" / "api.py").write_text(ROUTES_OP + "\n# touched\n")
        (self.repo / "README.md").write_text("doc 2\n")
        self.commit("touch")
        meta = self.run_once(mode="incremental")
        review = meta["review"]
        self.assertEqual(review["mode"], "changed")
        self.assertEqual((review["files"], review["overflow"]), (["controllers/a.py"], ["op/api.py"]))
        self.assertEqual((review["units"], review["outside_count"]), (["unauth", "ws-api"], 1))
        self.assertEqual(sorted(e["route"] for e in review["entry_points"]),
                         ["/api/ws/<int:wid>/x", "/ha/a", "/ha/b", "<ROUTE_CONST>"])
        with self.assertRaisesRegex(cl.CollectError, "不用帶 --unit"):
            cl.cmd_import(Path(meta["run_dir"]), self.report(Path(meta["run_dir"]), [{"type": "coverage",
                          "entry": "x", "status": "reviewed"}]), unit="unauth")

    def test_target_without_units_has_no_review(self):
        (self.targets / "demo.yaml").write_text("context: {summary: x}\n")
        self.assertIsNone(self.run_once()["review"])
        with self.assertRaisesRegex(cl.CollectError, "沒有 review_units"):
            self.run_once(units=["unauth"])


if __name__ == "__main__":
    unittest.main()
