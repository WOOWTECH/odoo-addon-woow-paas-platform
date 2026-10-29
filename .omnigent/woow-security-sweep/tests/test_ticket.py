"""ticket.py 的單元測試（不打網路、不派 worker）。

執行：python3 -m unittest discover -s .omnigent/woow-security-sweep/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml

SWEEP = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ticket", SWEEP / "skills/sec-dispatch/scripts/ticket.py")
tk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tk)

REPO = "WOOWTECH/demo"


class SourceBlocksTest(unittest.TestCase):
    """vendored 原文與 companion 的段落都要找得到（換版或改名時在這裡紅）。"""

    def test_every_target_review_class_resolves(self):
        for path in sorted((SWEEP / "targets").glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            groups = {"": data.get("review_classes") or []}
            # 分塊各自的 review_classes（#2064 階段 C）也要找得到原文
            groups |= {f"#{u.get('name')}": u["review_classes"]
                       for u in (data.get("review_units") or {}).get("units") or [] if u.get("review_classes")}
            for where, refs in groups.items():
                if not refs and where:
                    continue
                with self.subTest(target=path.name + where):
                    blocks, missing = tk.resolve_classes(refs)
                    self.assertEqual(missing, [])
                    self.assertTrue(blocks or not data.get("review_classes"))

    def test_every_stack_review_class_resolves(self):
        for path in sorted((SWEEP / "stacks").glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            with self.subTest(stack=path.name):
                self.assertEqual(data.get("name"), path.stem)
                blocks, missing = tk.resolve_classes(data.get("review_classes") or [])
                self.assertEqual(missing, [])
                self.assertTrue(blocks)

    def test_default_classes_and_method_blocks(self):
        blocks, missing = tk.resolve_classes(list(tk.DEFAULT_CLASSES))
        self.assertEqual((missing, len(blocks)), ([], len(tk.DEFAULT_CLASSES)))
        mb = tk.method_blocks()
        self.assertIn("Defensive vulnerability-finding method", mb["method"])
        self.assertIn("Candidate gate", mb["gate"])
        self.assertIn("**critical**", mb["severity"])

    def test_companion_gets_shared_sections_once(self):
        refs = ["odoo.md#sudo() scope creep", "odoo.md#Public method RPC exposure"]
        titles = [t for t, _ in tk.resolve_classes(refs)[0]]
        self.assertEqual(titles.count("odoo.md#Core discipline"), 1)
        self.assertEqual(titles.count("odoo.md#Validation rules"), 1)
        self.assertIn("odoo.md#sudo() scope creep", titles)

    def test_unknown_ref_is_reported(self):
        _, missing = tk.resolve_classes(["ATTACK-CLASSES.md#Nope", "NOPE.md#X"])
        self.assertEqual(len(missing), 2)

    def test_verifier_sections_exist(self):
        self.assertIn("You did not write this candidate",
                      tk.fenced_after("VALIDATION-AND-REPORTING.md", "#### Candidate-verifier prompt"))
        self.assertIn("For `needs_validation`", tk.section_between(
            "VALIDATION-AND-REPORTING.md", "For `confirmed`, require it to check:",
            "Each verifier returns exactly one JSON object"))


class TicketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.run = self.state / "runs" / "20261028-0030" / "demo"
        (self.run / "src" / "controllers").mkdir(parents=True)
        (self.run / "src" / "controllers" / "a.py").write_text("x = 1\n")
        cfg = {"repo": {"full_name": REPO},
               "context": {"summary": "demo", "entry_points": [
                   {"route": "/demo/login", "file": "controllers/a.py", "auth": "none", "csrf": False, "note": "登入"}],
                   "upstream": ["WOOWTECH/base"]},
               "review_focus": ["sudo 之前先驗身分"],
               "review_classes": ["ATTACK-CLASSES.md#Access control", "odoo.md#sudo() scope creep"]}
        (self.run / "config.json").write_text(json.dumps(cfg, ensure_ascii=False))
        meta = {"repo": REPO, "scope": {"mode": "incremental", "changed": ["controllers/a.py", "controllers/gone.py"],
                                         "reason": "aaa..bbb"},
                "deleted_files": ["controllers/gone.py"],
                "upstream": [{"repo": "WOOWTECH/base", "status": "ok", "path": "upstream/base", "head_sha": "1" * 40},
                             {"repo": "WOOWTECH/other", "status": "missing", "detail": "找不到 clone"}]}
        (self.run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        (self.run / "findings.jsonl").write_text(json.dumps(
            {"id": "t1", "tool": "semgrep", "rule": "python.x", "path": "controllers/a.py", "line": 1}) + "\n")
        # 前一輪：一筆舊制 review finding（人判追蹤）、一筆被驗證者推翻的說法
        prev = self.state / "runs" / "20261027-0030" / "demo"
        prev.mkdir(parents=True)
        legacy = "review:sudo-without-check:controllers/a.py:abc"
        (prev / "decisions.jsonl").write_text(json.dumps({"id": "d1", "finding": {
            "fingerprint": legacy, "path": "controllers/a.py", "line": 3, "message": "sudo 前沒檢查\n細節"}}) + "\n")
        (prev / "rejected.jsonl").write_text(json.dumps({"finding": {
            "fingerprint": "review:csp-too-wide", "title": "CSP 太寬"}, "reason": "防禦縱深，不是邊界違反"}) + "\n")
        shadow = [{"type": "decision", "gate": "triage", "id": "d1", "repo": REPO, "fingerprint": legacy,
                   "ts": "2026-10-27T00:31:00+00:00"},
                  {"type": "human", "ref": "d1", "label": "true_positive", "action": "track"},
                  {"type": "decision", "gate": "triage", "id": "d2", "repo": "WOOWTECH/elsewhere",
                   "fingerprint": "review:x", "ts": "2026-10-27T00:31:00+00:00"}]
        (self.state / "shadow.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in shadow))

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_ticket(self):
        out = tk.review_ticket(self.run, self.state)
        text = Path(out["ticket"]).read_text(encoding="utf-8")
        self.assertEqual((out["missing_blocks"], out["prior"], out["rejected"]), ([], 1, 1))
        scope = text.split("## repo 上下文", 1)[0]
        changed, deleted = scope.split("已刪除", 1)
        self.assertIn("`controllers/a.py`", changed)
        self.assertNotIn("gone.py", changed)  # 已刪除的檔案不能叫 worker 去審
        self.assertIn("`controllers/gone.py`", deleted)
        self.assertIn("upstream/base", scope)
        self.assertIn("WOOWTECH/other：找不到 clone", scope)
        self.assertIn("review:sudo-without-check:controllers/a.py:abc", text)
        self.assertIn("true_positive／track", text)
        self.assertNotIn("review:x", text)  # 別的 repo 的 finding 不混進來
        self.assertIn("`review:csp-too-wide`", text)
        self.assertIn("`/demo/login`", text)
        self.assertIn("semgrep `python.x` controllers/a.py:1", text)
        self.assertIn("### odoo.md#Validation rules", text)
        self.assertIn("**不給 `severity`**", text)
        prior = json.loads((self.run / "prior_findings.json").read_text(encoding="utf-8"))
        self.assertEqual([p["fingerprint"] for p in prior], ["review:sudo-without-check:controllers/a.py:abc"])

    def test_fork_scope_lists_only_fork_files(self):
        meta = json.loads((self.run / "meta.json").read_text(encoding="utf-8"))
        meta["scope"] = {"mode": "full", "changed": None, "reason": "full"}
        meta["deleted_files"] = []
        meta["fork"] = {"remote": "https://github.com/up/demo.git", "branch": "master", "merge_base": "4911d09d" * 5,
                        "fork_point": "2026-07-19T00:00:00+00:00", "ahead": 15, "behind": 1185,
                        "files": ["controllers/a.py", "brand/logo.png"]}
        (self.run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        text = Path(tk.review_ticket(self.run, self.state)["ticket"]).read_text(encoding="utf-8")
        scope = text.split("## repo 上下文", 1)[0]
        self.assertIn("只審 fork 改過的部分", scope)
        self.assertIn("落後 1185", scope)
        self.assertIn("限 fork 改過、仍存在的 2 個檔案", scope)
        self.assertIn("`controllers/a.py`", scope)
        self.assertNotIn("logo.png", scope)  # 二進位檔不列給 worker
        self.assertIn("另有 1 個圖片", scope)
        self.assertIn("上游問題", scope)

    def test_verify_ticket(self):
        row = {"id": "f9", "tool": "review", "fingerprint": "review:email-link", "verdict": "needs_validation",
               "title": "以 email 綁定", "path": "controllers/a.py", "line": 1, "root_cause": "rc",
               "trace": [{"kind": "sink", "file": "controllers/a.py", "line": 1, "description": "d"}],
               "blockers": ["b"], "validation_plan": {"local": "l"}}
        with open(self.run / "findings.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        out = tk.verify_ticket(self.run, "f9")
        text = Path(out["ticket"]).read_text(encoding="utf-8")
        self.assertIn("You did not write this candidate", text)
        self.assertIn('"fingerprint": "review:email-link"', text)
        self.assertIn("### odoo.md#Validation rules", text)
        with self.assertRaises(tk.TicketError):
            tk.verify_ticket(self.run, "t1")  # 工具 finding 不走驗證者
        with self.assertRaises(tk.TicketError):
            tk.verify_ticket(self.run, "missing")


    def use_units(self) -> None:
        """#2064 階段 C：分塊輪審的 meta.review 與 config.review_units。"""
        cfg = json.loads((self.run / "config.json").read_text(encoding="utf-8"))
        cfg["review_units"] = {"units": [{"name": "unauth", "review_classes": []},
                                         {"name": "front", "review_classes": ["CLIENT-SIDE.md#DOM-based XSS"]}]}
        (self.run / "config.json").write_text(json.dumps(cfg, ensure_ascii=False))
        meta = json.loads((self.run / "meta.json").read_text(encoding="utf-8"))
        meta["scope"] = {"mode": "full", "changed": None, "reason": "full"}
        meta["deleted_files"] = []
        meta["review"] = {"mode": "units", "total": 3, "units": [
            {"name": "unauth", "order": 1, "focus": "不需登入的入口", "files": ["controllers/a.py"], "bytes": 4096,
             "entry_points": [{"route": "/ha/a", "file": "controllers/a.py", "line": 6, "func": "a", "auth": "none",
                               "type": "json", "csrf": False}],
             "missing_paths": ["gone/"], "review_classes": [], "last_reviewed": None},
            {"name": "front", "order": 3, "focus": "", "files": ["static/x.js"], "bytes": 10, "entry_points": [],
             "missing_paths": [], "review_classes": ["CLIENT-SIDE.md#DOM-based XSS"],
             "last_reviewed": {"at": "2026-10-20T00:00:00+00:00"}}]}
        (self.run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    def test_units_one_ticket_per_unit(self):
        self.use_units()
        out = tk.review_ticket(self.run, self.state)
        self.assertEqual([t["unit"] for t in out["tickets"]], ["unauth", "front"])
        self.assertNotIn("ticket", out)  # 多張工單：只看 tickets
        unauth = Path(out["tickets"][0]["ticket"]).read_text(encoding="utf-8")
        front = Path(out["tickets"][1]["ticket"]).read_text(encoding="utf-8")
        self.assertTrue(out["tickets"][0]["ticket"].endswith("tickets/review-unauth.md"))
        self.assertIn("**分塊輪審**，本塊 `unauth`（targets 第 1 塊，共 3 塊；上次審：從未）", unauth)
        self.assertIn("本塊重點：不需登入的入口", unauth)
        self.assertIn("`gone/`", unauth)
        self.assertIn("- `/ha/a`（controllers/a.py:6 `a`，auth=none，type=json，csrf=False）", unauth)
        self.assertIn("review:sudo-without-check:controllers/a.py:abc", unauth)
        self.assertIn("semgrep `python.x` controllers/a.py:1", unauth)
        self.assertIn("### odoo.md#Validation rules", unauth)  # 沒寫 review_classes 的塊用 target 的
        self.assertIn("上次審：2026-10-20", front)
        self.assertNotIn("review:sudo-without-check", front)  # 別塊的 finding 不列
        self.assertIn("另有 1 筆在本輪範圍以外的檔案", front)
        self.assertNotIn("python.x", front)
        self.assertIn("### CLIENT-SIDE.md#DOM-based XSS", front)
        self.assertNotIn("odoo.md#", front)
        prior = json.loads((self.run / "prior_findings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(prior), 1)  # import 驗 fingerprint 用：存全部

    def test_changed_without_unit_files_skips(self):
        self.use_units()
        meta = json.loads((self.run / "meta.json").read_text(encoding="utf-8"))
        meta["review"] = {"mode": "changed", "files": [], "overflow": [], "units": [], "outside_count": 4,
                          "entry_points": []}
        (self.run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        out = tk.review_ticket(self.run, self.state)
        self.assertEqual(out["tickets"], [])
        self.assertIn("不派 deep review", out["skip"])
        meta["review"] = {"mode": "changed", "files": ["controllers/a.py"], "overflow": ["static/x.js"],
                          "max_files": 1, "units": ["front"], "outside_count": 4, "entry_points": []}
        (self.run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        out = tk.review_ticket(self.run, self.state)
        text = Path(out["ticket"]).read_text(encoding="utf-8")
        self.assertIn("只審落在分塊裡的變動檔案（涉及的塊：front）", text)
        self.assertIn("另有 1 個變動檔案超過本輪上限 1 個", text)
        self.assertIn("另有 4 個變動檔案不屬於任何分塊", text)
        self.assertIn("`/demo/login`", text)  # targets 手寫的 entry point 落在本輪檔案裡也列
        self.assertIn("### CLIENT-SIDE.md#DOM-based XSS", text)

    def test_verify_ticket_units_brief_scope(self):
        self.use_units()
        row = {"id": "f9", "tool": "review", "fingerprint": "review:email-link", "verdict": "needs_validation",
               "title": "t", "path": "controllers/a.py", "line": 1, "unit": "unauth",
               "trace": [{"kind": "sink", "file": "controllers/a.py", "line": 1, "description": "d"}],
               "blockers": ["b"], "validation_plan": {"local": "l"}}
        with open(self.run / "findings.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        text = Path(tk.verify_ticket(self.run, "f9")["ticket"]).read_text(encoding="utf-8")
        scope = text.split("## repo 上下文", 1)[0]
        self.assertIn("本輪是分塊輪審", scope)
        self.assertNotIn("static/x.js", scope)


if __name__ == "__main__":
    unittest.main()
