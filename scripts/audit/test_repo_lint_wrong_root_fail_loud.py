#!/usr/bin/env python3
"""wrong root 一律 fail-loud——`_addon_source_tree_present()` 的**全部**呼叫點（#936）。

## 守的是什麼

repo-lint 有一批**否定性**判準：「全 repo 都不准出現 X」「allowlist 有沒有養殭屍
條目」「兩端形狀一不一致」「這些測試有沒有被收集」。它們的綠燈本來就長成**空清
單** ⇒ 一旦在錯的 root 上早退回空清單，輸出與「這棵樹很乾淨」**完全不可區分**：
gate 寫好了、跑了、回綠了，而它一格都沒走到。

#828 拿 test-collectability 的兩條做過實證（同一份逐位元相同的 probe 檔，放進有／
沒有 `src/` 的兩棵樹，得到 1 條違規 vs 0 條違規），並把那兩條改成 fail-loud。
覆核 PR #931 時量到：同一個形狀還住在另外四條裡，**6 個呼叫點只修了 2 個**（#936）。

## 🔴 為什麼這支測試不寫死清單

「#828 只修了 6 個裡的 2 個」正是手寫清單的失效方式——漏掉時輸出與「全部都修好
了」一模一樣。所以判準改成**資源側枚舉**：AST 掃出 `repo-lint.py` 裡每一個呼叫
`_addon_source_tree_present()` 的 check 函式，逐一驗它在 rootless 樹上 fail-loud。
第 7 條 check 只要沿用這個守衛，這裡自動接得到；沿用了卻 fail-open，這裡自動紅。

契約（同 CI 那步的宣告）：無外部相依——不碰 ~/.claude、網路、Odoo、特定 cwd。
"""
import ast
import importlib.util
import os
import shutil
import tempfile
import unittest

# repo-lint.py 的檔名帶連字號，import 不進來 ⇒ 走 spec_from_file_location
# （與 test_repo_lint_docstring_registry.py 同一個慣例）。
_GATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint_wrong_root", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

_GUARD_FN = "_addon_source_tree_present"


def _root_guarded_checks():
    """回傳所有呼叫 `_addon_source_tree_present()` 的模組層 `check_*` 函式名。

    只收 `check_` 開頭的：`_addon_source_tree_present` 自己、以及日後可能出現的
    非 check helper，都不是本條的管轄對象。
    """
    with open(_GATE_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("check_"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == _GUARD_FN):
                names.add(node.name)
                break
    return sorted(names)


class TestTheProbeItselfWorks(unittest.TestCase):
    """🔴 陽性對照：枚舉器壞掉時，下面整個測試類別會安靜地跑零個 case。

    「for name in []: ...」是綠的，而且與「全部都通過」不可區分——這正是本檔要
    根治的形態，不能在本檔內部復刻一次。
    """

    def test_enumeration_is_not_empty(self):
        found = _root_guarded_checks()
        self.assertTrue(
            found,
            "AST 枚舉不到任何呼叫 %s() 的 check ⇒ 守衛被改名／被換成別的形式，"
            "本檔的每一條斷言都會靜默跑零次" % _GUARD_FN)

    def test_enumeration_covers_the_two_known_from_828(self):
        """#828 修過的那兩條必須在枚舉結果裡——枚舉器的已知答案對照。"""
        found = set(_root_guarded_checks())
        for known in ("check_test_module_collection", "check_test_class_execution"):
            self.assertIn(
                known, found,
                "%s 是 #828 的當事人，枚舉不到它代表 AST 判準抓錯東西" % known)

    def test_a_valid_root_is_not_flagged(self):
        """🔴 反向對照：真 repo root 上這些 check **不得**回 wrong-root violation。

        沒有這一條，一個「無論如何都回 wrong-root」的壞守衛也能讓上面全綠。
        """
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        for name in _root_guarded_checks():
            with self.subTest(check=name):
                out = getattr(rl, name)(repo_root)
                self.assertNotIn(
                    "root/layout 前置條件不成立", "\n".join(out),
                    "%s 在真 repo root 上仍報 wrong root" % name)


class TestEveryRootGuardedCheckFailsLoud(unittest.TestCase):
    """rootless 樹上，每一條 check 各回**一筆可歸因**的 violation。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-wrong-root-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_each_check_returns_one_attributable_violation(self):
        for name in _root_guarded_checks():
            with self.subTest(check=name):
                out = getattr(rl, name)(self.root)
                self.assertEqual(
                    len(out), 1,
                    "%s 在缺 %s 的 root 上回了 %d 筆（期望恰好 1 筆）：%r"
                    % (name, rl._ADDON_MANIFEST_REL, len(out), out))
                msg = out[0]
                self.assertIn("root/layout 前置條件不成立", msg)
                self.assertIn(rl._ADDON_MANIFEST_REL, msg)
                self.assertIn("不能解讀為零違規", msg)
                # 🔴 歸因要指到**這一條**。兩條 check 同時撞 wrong root 時訊息
                # 一樣，就會被讀成「同一件事報了兩次」而被去重掉（#828 的教訓）。
                self.assertIn(
                    name, msg,
                    "%s 的 wrong-root 訊息沒點名自己 ⇒ 多條同時觸發時無法歸因"
                    % name)

    def test_messages_are_pairwise_distinct(self):
        """任兩條的訊息不得相同——去重機制不會把它們併成一筆。"""
        msgs = {}
        for name in _root_guarded_checks():
            out = getattr(rl, name)(self.root)
            if out:
                msgs.setdefault(out[0], []).append(name)
        dupes = {m: n for m, n in msgs.items() if len(n) > 1}
        self.assertEqual(
            dupes, {},
            "以下 check 的 wrong-root 訊息逐字相同，聚合時會被併成一筆：%r"
            % {tuple(v): k[:60] for k, v in dupes.items()})

    def test_hook_mode_is_the_only_exemption_and_it_is_explicit(self):
        """🔴 `run_all` fail-loud ／ hook 回空清單——這個不對稱本身要被鎖住。

        兩個方向都驗：hook 模式下確實安靜（否則 hook 會擋住任何非 addon 樹上的
        正當編輯，含 repo-lint 自己的端對端測試），而且離開 hook 模式**立刻**恢復
        fail-loud（否則旗標忘了還原時，`--all` 會靜默退回 #828 的形態）。
        """
        names = _root_guarded_checks()
        original = rl._HOOK_MODE
        try:
            rl._HOOK_MODE = True
            for name in names:
                with self.subTest(check=name, mode="hook"):
                    self.assertEqual(
                        getattr(rl, name)(self.root), [],
                        "%s 在 hook 模式的 wrong root 上仍回違規 ⇒ hook 會擋住"
                        "非 addon 樹上的正當編輯" % name)
        finally:
            rl._HOOK_MODE = original
        self.assertFalse(
            rl._HOOK_MODE, "旗標沒還原，本檔後續的斷言會全部跑在 hook 語意下")
        for name in names:
            with self.subTest(check=name, mode="run_all"):
                self.assertEqual(
                    len(getattr(rl, name)(self.root)), 1,
                    "%s 離開 hook 模式後沒有恢復 fail-loud" % name)

    def test_no_check_silently_returns_empty(self):
        """明寫的反面：**沒有任何一條**可以在 wrong root 上回空清單。

        與上面那條的差別是失敗訊息——這一條直接把「還沒掃到的那幾條」點名列出，
        而不是報一個「期望 1 筆、得到 0 筆」的 subTest。#936 的緣起就是「還剩
        哪幾條」這個問題沒有人在問。
        """
        silent = [name for name in _root_guarded_checks()
                  if not getattr(rl, name)(self.root)]
        self.assertEqual(
            silent, [],
            "下列 check 在 wrong root 上仍靜默回零違規（#936 未掃完）：%r" % silent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
