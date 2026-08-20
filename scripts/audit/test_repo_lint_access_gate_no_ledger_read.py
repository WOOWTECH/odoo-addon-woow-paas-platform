#!/usr/bin/env python3
"""`access-gate-no-ledger-read`（repo-lint 第 29 條）的自檢。

守的是**這條規則自己**的鑑別力，不是被守的那棵樹乾不乾淨。分兩半：

* **敏感度**：三種「gate 讀帳本」的寫法都要被抓到（#784 F6 的實際形狀是其中之
  一——lambda 呼叫的方法本體讀帳本，那一格最容易漏）；
* **特異性**：不讀帳本的 filter、以及帳本查詢出現在**非** filter 判準的位置，
  都不得被誤標。缺這一半的測試集不知道自己缺了什麼——一條恆紅的 gate 在只驗
  「改壞→紅」的測試集裡是隱形的，直到它擋住正當工作然後被整條拿掉。

🔴 第三組（同名碰撞）是 #792 第四輪回驗挖出來的：`filtered()` 的 lambda 參數是
**另一個 model** 的記錄，所以「`r.foo` 指哪個 class 的 `foo`」靜態上無法可靠決定。
第一版把同檔定義攤平成 dict、同名後者覆蓋前者 ⇒ 兩個方向都會**靜默**出錯，而且
挑到哪一個取決於 `ast.walk()` 的走訪順序。現在的契約是「唯一解才追進去，多解報
無法判定」，這一組就是鎖那個契約。

契約（同 CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

# repo-lint.py 的檔名帶連字號，import 不進來 ⇒ 走 spec_from_file_location
# （與 test_repo_lint_docstring_registry.py 同一個慣例）。
_GATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


class _Case(unittest.TestCase):
    """把一份合成的 model 檔丟進暫時 root，跑 check，回傳違規清單。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-agnlr-")
        os.makedirs(os.path.join(self.root, "src", "models"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_check(self, source):
        path = os.path.join(self.root, "src", "models", "m.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return rl.check_access_gate_no_ledger_read(self.root)


class TestSensitivity(_Case):
    """gate 讀帳本的三種寫法都要紅。"""

    def test_lambda_body_reads_the_ledger_directly(self):
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(\n"
            "            lambda r: bool(r.env['reap']._pending_for(r)))\n")
        self.assertEqual(len(v), 1, v)
        self.assertIn("直接讀待回收帳本", v[0])

    def test_lambda_calls_a_method_that_reads_the_ledger(self):
        """#784 F6 的實際形狀：判準本身乾淨，它呼叫的方法才讀帳本。"""
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(lambda r: r.gate())\n"
            "class B:\n"
            "    def gate(self):\n"
            "        return bool(self.env['reap']._pending_ids('m', self.id))\n")
        self.assertEqual(len(v), 1, v)
        self.assertIn("而它讀待回收帳本", v[0])

    def test_payload_helper_used_as_the_predicate(self):
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(\n"
            "            lambda r: any(r._access_teardown_payload_ids()))\n")
        self.assertTrue(v)
        self.assertIn("直接讀待回收帳本", v[0])


class TestSpecificity(_Case):
    """不讀帳本的東西不得被誤標。"""

    def test_clean_predicate_is_green(self):
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(lambda r: r.gate())\n"
            "class B:\n"
            "    def gate(self):\n"
            "        return bool(self.app_id or self.pol_ids)\n")
        self.assertEqual(v, [])

    def test_ledger_read_outside_a_filter_predicate_is_green(self):
        """帳本查詢本身不是罪——只有出現在 filter 判準才是。"""
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        ids = self.env['reap']._pending_ids('m', self.id)\n"
            "        return self.x.filtered(lambda r: r.gate()) and ids\n"
            "class B:\n"
            "    def gate(self):\n"
            "        return bool(self.app_id)\n")
        self.assertEqual(v, [])

    def test_filtered_with_a_string_arg_is_green(self):
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered('app_id')\n")
        self.assertEqual(v, [])


class TestSameNameCollision(_Case):
    """🔴 同名碰撞：不猜，報「無法判定」。

    兩個方向都要鎖——第一版只有假陰性那半被想到，而假陽性那半（無辜的 filter
    被冤枉）同樣是靜默的。
    """

    _DUP_GATE = (
        "class A:\n"
        "    def f(self):\n"
        "        return self.x.filtered(lambda r: r.gate())\n"
        "class B:\n"
        "    def gate(self):\n"
        "        return bool(self.env['reap']._pending_ids('m', self.id))\n"
        "class C:\n"
        "    def gate(self):\n"
        "        return bool(self.app_id)\n")

    def test_dirty_definition_must_not_be_masked_by_a_clean_namesake(self):
        """假陰性方向：乾淨那版覆蓋髒的 ⇒ F6 形狀漏網。"""
        v = self.run_check(self._DUP_GATE)
        self.assertTrue(
            v, "同名碰撞時靜默挑一個 ⇒ F6 的實際形狀會漏網（這正是本規則的理由）")
        self.assertIn("無法判定", v[0])
        # 訊息要看得出是哪一種：不可以講成「確定讀了帳本」。
        self.assertNotIn("而它讀待回收帳本", v[0])

    def test_innocent_filter_is_not_accused_of_reading_the_ledger(self):
        """假陽性方向：兩個不相關 class 的同名 to_dict，一個讀帳本。"""
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(lambda r: r.to_dict())\n"
            "class B:\n"
            "    def to_dict(self):\n"
            "        return {'a': 1}\n"
            "class C:\n"
            "    def to_dict(self):\n"
            "        return self.env['reap']._pending_for(self)\n")
        self.assertEqual(len(v), 1, v)
        self.assertIn("無法判定", v[0],
                      "不得直接指控它讀帳本——那條 filter 可能完全無辜")
        self.assertIn("同名定義", v[0])

    def test_collision_on_an_unused_name_stays_green(self):
        """同名本身不是罪：沒被當 filter 判準用就不該有任何訊號。"""
        v = self.run_check(
            "class A:\n"
            "    def f(self):\n"
            "        return self.x.filtered(lambda r: r.gate())\n"
            "class B:\n"
            "    def gate(self):\n"
            "        return bool(self.app_id)\n"
            "class C:\n"
            "    def to_dict(self):\n"
            "        return {}\n"
            "class D:\n"
            "    def to_dict(self):\n"
            "        return self.env['reap']._pending_for(self)\n")
        self.assertEqual(v, [], "重名的是 to_dict，而判準用的是 gate ⇒ 不該有訊號")


if __name__ == "__main__":
    unittest.main(verbosity=2)
