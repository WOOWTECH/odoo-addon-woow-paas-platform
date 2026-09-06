#!/usr/bin/env python3
"""`test-collectability`（repo-lint 第 31 條）收集面第 (4) 格——重複 import——的自檢。

`src/tests/__init__.py` 是 tail-append 的 import 清單，平行 PR 解 add/add 衝突時兩側各加
的同一行常被一起留下；那個結果由 `check_test_module_collection` 的第 (4) 格攔（#1052；
曾考慮的 `merge=union` 因會靜默復活被刪的 import 而否決）。本檔守的是**那一格自己的鑑別力**：

* **敏感度**：同一個 `__init__.py` 對同一模組出現第二個 level-1 `ImportFrom` 要紅，
  訊息要點名兩個行號；
* **特異性**：只 import 一次的乾淨樹，第 (4) 格不得產生任何違規（其他三格的訊息
  另有各自的測試，這裡只看「重複」關鍵字）。

契約（同 CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

# repo-lint.py 的檔名帶連字號，import 不進來 ⇒ 走 spec_from_file_location
# （與 test_repo_lint_access_gate_no_ledger_read.py 同一個慣例）。
_GATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

_DUP_MARKER = "重複 import"


class _Case(unittest.TestCase):
    """合成一棵最小 addon 樹（manifest ＋ src/tests），跑收集面 check。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-tidup-")
        os.makedirs(os.path.join(self.root, "src", "tests"))
        with open(os.path.join(self.root, "src", "__manifest__.py"), "w",
                  encoding="utf-8") as f:
            f.write("{'name': 'synthetic'}\n")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_tests(self, init_source, modules):
        tests = os.path.join(self.root, "src", "tests")
        with open(os.path.join(tests, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(init_source)
        for name in modules:
            with open(os.path.join(tests, name + ".py"), "w",
                      encoding="utf-8") as f:
                f.write("from odoo.tests.common import TransactionCase\n\n\n"
                        "class T(TransactionCase):\n"
                        "    def test_x(self):\n        pass\n")

    def dup_violations(self):
        return [v for v in rl.check_test_module_collection(self.root)
                if _DUP_MARKER in v]


class TestSensitivity(_Case):
    """同一模組 import 兩次要紅，且訊息點名兩個行號。"""

    def test_duplicate_import_is_reported_with_both_lines(self):
        self.write_tests(
            "from . import test_a\n"
            "from . import test_b\n"
            "from . import test_a\n",
            ["test_a", "test_b"])
        v = self.dup_violations()
        self.assertEqual(len(v), 1, v)
        self.assertIn("src/tests/__init__.py:3", v[0])
        self.assertIn("第一次在 :1", v[0])
        self.assertIn("test_a", v[0])
        self.assertIn("重複 import", v[0])

    def test_triplicate_reports_each_extra_occurrence(self):
        """三份 ⇒ 兩條違規（第 2、3 次各一條），不是一條。"""
        self.write_tests(
            "from . import test_a\n"
            "from . import test_a\n"
            "from . import test_a\n",
            ["test_a"])
        v = self.dup_violations()
        self.assertEqual(len(v), 2, v)
        self.assertIn(":2：", v[0])
        self.assertIn(":3：", v[1])


class TestSpecificity(_Case):
    """乾淨樹上第 (4) 格不得誤報。"""

    def test_single_imports_produce_no_duplicate_violation(self):
        self.write_tests(
            "from . import test_a\n"
            "from . import test_b\n",
            ["test_a", "test_b"])
        self.assertEqual(self.dup_violations(), [])

    def test_same_module_name_in_different_init_files_is_not_a_duplicate(self):
        """子套件的 `__init__.py` 與頂層 re-export 指的是同一模組——那是第 (3) 格
        要求的形狀，不是重複。判準是 (init 檔, 目標) 成對，不是只看目標。"""
        sub = os.path.join(self.root, "src", "tests", "integration")
        os.makedirs(sub)
        with open(os.path.join(sub, "__init__.py"), "w", encoding="utf-8") as f:
            f.write("from . import test_i\n")
        with open(os.path.join(sub, "test_i.py"), "w", encoding="utf-8") as f:
            f.write("from odoo.tests.common import TransactionCase\n\n\n"
                    "class T(TransactionCase):\n"
                    "    def test_x(self):\n        pass\n")
        self.write_tests(
            "from . import test_a\n"
            "from .integration import test_i\n",
            ["test_a"])
        self.assertEqual(self.dup_violations(), [])


if __name__ == "__main__":
    unittest.main()
