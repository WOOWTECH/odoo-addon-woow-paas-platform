#!/usr/bin/env python3
"""repo-lint 檔頭條目區塊的先紅後綠回歸測試（#835）。

把真實檔頭的尾端條目搬到「用法：」之後，保留編號、名稱、順序與四處登記，
驗證位置本身會被 gate 擋下，而不是靠既有注入器間接發現。

跑法：python3 scripts/audit/test_repo_lint_header_835.py
自測入口使用 unittest.main，verbosity=2 會逐項列出實際執行的 fixture。
只使用 Python 標準庫，不依賴 Odoo、網路、HOME 或特定 cwd。
"""
import ast
import importlib.util
import pathlib
import sys
import tempfile
import unittest


_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")
_REPO_ROOT = _GATE_PATH.resolve().parents[2]
_SELF_REL = "scripts/audit/repo-lint.py"
_REAL_SRC = _GATE_PATH.read_text(encoding="utf-8")

_spec = importlib.util.spec_from_file_location("repo_lint_header_835", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


class TestHeaderEntryBlock835(unittest.TestCase):
    def setUp(self):
        # fixture 限於 checkout 內，結束後自動清除；不寫主要 checkout 或 HOME。
        tmp = tempfile.TemporaryDirectory(prefix=".fixture-header-835-", dir=_REPO_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.path = self.root / _SELF_REL
        self.path.parent.mkdir(parents=True)

    def audit(self, src):
        self.path.write_text(src, encoding="utf-8")
        return rl.check_repo_lint_docstring_registry(str(self.root))

    def test_legal_header_before_usage_is_green(self):
        self.assertEqual(self.audit(_REAL_SRC), [])

    def test_last_entry_after_usage_is_rejected(self):
        # 從 module docstring 的 AST 範圍取出真實檔頭，不碰函式內的說明文字。
        doc_node = ast.parse(_REAL_SRC).body[0]
        lines = _REAL_SRC.splitlines(keepends=True)
        head = "".join(lines[:doc_node.end_lineno])
        tail = "".join(lines[doc_node.end_lineno:])
        entries = list(rl._REPO_LINT_ENTRY_RE.finditer(head))
        self.assertTrue(entries, "找不到真實條目，注入工具失效")
        usage = head.index("\n用法：\n")
        last = entries[-1]
        self.assertLess(last.start(), usage, "原始檔頭已不合法，不能當陰性對照")
        block = head[last.start():usage]
        without_last = head[:last.start()] + head[usage:]
        anchor = "\n用法：\n"
        insertion = without_last.index(anchor) + len(anchor)
        moved_head = without_last[:insertion] + "\n" + block + without_last[insertion:]
        mutated = moved_head + tail

        self.assertNotEqual(mutated, _REAL_SRC, "條目搬移注入未生效")
        self.assertEqual(
            rl._REPO_LINT_ENTRY_RE.findall(moved_head),
            rl._REPO_LINT_ENTRY_RE.findall(head),
            "注入必須保留條目編號、名稱與順序，不能靠既有一致性檢查變紅")
        moved = list(rl._REPO_LINT_ENTRY_RE.finditer(moved_head))[-1]
        self.assertGreater(moved.start(), moved_head.index(anchor))

        violations = self.audit(mutated)
        print("#835 fixture：%s. %s 搬到「用法：」之後；gate 回傳 %r"
              % (last.group(1), last.group(2), violations), flush=True)
        self.assertEqual(len(violations), 1, "位置錯誤必須被獨立點名，不能全綠")
        self.assertIn(last.group(1), violations[0])
        self.assertIn(last.group(2), violations[0])
        self.assertIn("用法：", violations[0])
        self.assertIn("條目區塊", violations[0])

    def test_rule_suffix_after_usage_examples_is_rejected(self):
        # 重現 #832 的 route-contract-golden 形態，但連後續條目一起搬，維持清單順序。
        doc_node = ast.parse(_REAL_SRC).body[0]
        lines = _REAL_SRC.splitlines(keepends=True)
        head = "".join(lines[:doc_node.end_lineno])
        tail = "".join(lines[doc_node.end_lineno:])
        entries = list(rl._REPO_LINT_ENTRY_RE.finditer(head))
        first = next(m for m in entries if m.group(2) == "route-contract-golden")
        usage = head.index("\n用法：\n")
        self.assertLess(first.start(), usage)
        block = head[first.start():usage]
        moved_entries = rl._REPO_LINT_ENTRY_RE.findall(block)
        without_suffix = head[:first.start()] + head[usage:]
        insertion = without_suffix.rindex('\n"""')
        moved_head = without_suffix[:insertion] + "\n" + block + without_suffix[insertion:]
        mutated = moved_head + tail
        self.assertNotEqual(mutated, _REAL_SRC, "條目搬移注入未生效")
        self.assertEqual(rl._REPO_LINT_ENTRY_RE.findall(moved_head),
                         rl._REPO_LINT_ENTRY_RE.findall(head))
        self.assertGreater(moved_head.index(block), moved_head.index("--hook"),
                           "fixture 必須真的搬到用法範例之後")

        violations = self.audit(mutated)
        print("#835 fixture：route-contract-golden 起的 %d 條條目搬到用法範例之後；"
              "gate 回傳 %d 條位置違規" % (len(moved_entries), len(violations)), flush=True)
        self.assertEqual(len(violations), len(moved_entries))
        for (number, name), violation in zip(moved_entries, violations):
            with self.subTest(rule=name):
                self.assertIn("第 %s 條" % number, violation)
                self.assertIn(name, violation)
                self.assertIn("用法：", violation)
                self.assertIn("條目區塊", violation)

    def test_inline_usage_reference_before_entries_is_green(self):
        anchor = "每條都對應一個已翻車的 pitfall"
        mutated = _REAL_SRC.replace(
            anchor, "維護者會參照「用法：」，條目仍須留在原區塊。\n" + anchor, 1)
        self.assertNotEqual(mutated, _REAL_SRC, "散文引用注入未生效")
        self.assertEqual(self.audit(mutated), [], "散文提及用法不能冒充區塊標題")

    def test_missing_usage_boundary_is_rejected(self):
        mutated = _REAL_SRC.replace("\n用法：\n", "\n執行方式：\n", 1)
        self.assertNotEqual(mutated, _REAL_SRC, "區塊邊界刪除注入未生效")
        violations = self.audit(mutated)
        self.assertEqual(len(violations), 1)
        self.assertIn("用法：", violations[0])
        self.assertIn("區塊邊界", violations[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
