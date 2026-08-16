#!/usr/bin/env python3
"""kubectl namespace 白名單三份副本對帳 gate 的 A/B 注入測試。

這支測試的存在理由跟被測的 check 一樣：**「現在是綠的」不是驗收**。集合相等這種
判準最容易在探針壞掉時恆真——兩邊都抽不到東西，`set() == set()` 就是綠的，而它與
「兩份清單確實一致」的輸出**完全相同**。所以下面每一條注入測試都在問：**改壞它，
會不會紅、紅得對不對、有沒有點名是哪一個**。

注入的素材是**真實的 CLAUDE.md、guard doc 與 kubectl-ns-guard hook**（複製到 tmp
目錄後再改），不是手寫
的假 fixture：假 fixture 只證明 parser 在自己捏的形狀上能動。定位一律用被測模組
自己的 regex，**不寫死 `fleet-local` 這種當下值**——寫死會讓白名單下次增刪的那天
測試變成假紅（或更糟：突變錨點消失而測試靜默失去鑑別力）。

不涵蓋：這條 check 有沒有被掛進 run_all() 與 run_hook() 分派。那是
check_repo_lint_docstring_registry 的職責（它會在漏掛時點名），重複驗只會多一份
會漂的清單。

跑法：
    python3 scripts/audit/test_kubectl_ns_whitelist_sync.py
    # 或 python3 -m pytest scripts/audit/test_kubectl_ns_whitelist_sync.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

# 檔名有連字號，只能走 importlib。先登記進 sys.modules 再 exec，否則被測模組內部
# 的相對 import 或 traceback 會長得像「腳本自己有 bug」。
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
repo_lint = importlib.util.module_from_spec(_spec)
sys.modules["repo_lint"] = repo_lint
_spec.loader.exec_module(repo_lint)

CLAUDE_REL = repo_lint.KUBECTL_NS_CLAUDE_MD
GUARD_REL = repo_lint.KUBECTL_NS_GUARD_DOC
HOOK_REL = repo_lint.KUBECTL_NS_HOOK_FILE
ALL_RELS = (CLAUDE_REL, GUARD_REL, HOOK_REL)


def _read(rel):
    with open(_REPO_ROOT / rel, encoding="utf-8") as f:
        return f.read()


class _Fixture:
    """把三份真實檔案複製到 tmp root，可選擇性突變其中一份或整個拿掉。"""

    def __init__(self, mutations=None, drop=None):
        self.root = tempfile.mkdtemp(prefix="ns-whitelist-gate-")
        for rel in ALL_RELS:
            if rel == drop:
                continue
            dirname = os.path.dirname(rel)
            if dirname:
                os.makedirs(os.path.join(self.root, dirname), exist_ok=True)
            text = (mutations or {}).get(rel, _read(rel))
            with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
                f.write(text)

    def run(self):
        return repo_lint.check_kubectl_ns_whitelist_sync(self.root)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)


def _drop_last_guard_item(text):
    """從 guard doc 那行砍掉最後一個項目，回傳 (新文字, 被砍掉的 namespace)。"""
    match = repo_lint.KUBECTL_NS_GUARD_LINE_RE.search(text)
    assert match, "guard doc 的錨點行不存在 ⇒ 突變沒發生，測試會假綠"
    line = match.group(0)
    segments = line.split("｜")
    assert len(segments) >= 2, "guard doc 只有一個項目，這個突變沒有意義"
    victim = repo_lint.KUBECTL_NS_TOKEN_RE.search(segments[-1])
    assert victim, "最後一個項目抽不到 namespace ⇒ 突變沒發生"
    mutated = "｜".join(segments[:-1]) + "。"
    return text[:match.start()] + mutated + text[match.end():], victim.group(1)


def _drop_last_hook_item(text):
    """從 hook 的 ALLOWED_NAMESPACES tuple 砍掉最後一個項目，回傳 (新文字, 被砍的
    namespace)。定位用被測模組自己的 regex，不寫死當下值（同上兩個突變器）。"""
    match = repo_lint.KUBECTL_NS_HOOK_TUPLE_RE.search(text)
    assert match, "hook 的 ALLOWED_NAMESPACES 錨點不存在 ⇒ 突變沒發生，測試會假綠"
    body = match.group(1)
    items = list(repo_lint.KUBECTL_NS_HOOK_ITEM_RE.finditer(body))
    assert len(items) >= 2, "hook tuple 只有一個項目，這個突變沒有意義"
    last = items[-1]
    victim = last.group(1) or last.group(2)
    line_start = body.rfind("\n", 0, last.start()) + 1
    line_end = body.find("\n", last.end())
    if line_end == -1:
        line_end = len(body)
    new_body = body[:line_start] + body[line_end + 1:]
    abs_start = match.start(1)
    return text[:abs_start] + new_body + text[match.end(1):], victim


def _drop_last_claude_row(text):
    """從 CLAUDE.md 白名單表格砍掉最後一列，回傳 (新文字, 被砍掉的 namespace)。"""
    heading = repo_lint.KUBECTL_NS_HEADING_RE.search(text)
    assert heading, "CLAUDE.md 的錨點標題不存在 ⇒ 突變沒發生，測試會假綠"
    rest = text[heading.end():]
    nxt = repo_lint.KUBECTL_NS_NEXT_HEADING_RE.search(rest)
    section = rest[:nxt.start()] if nxt else rest
    rows = list(repo_lint.KUBECTL_NS_ROW_RE.finditer(section))
    assert rows, "白名單表格抽不到任何列 ⇒ 突變沒發生"
    last = rows[-1]
    line_end = section.find("\n", last.start())
    if line_end == -1:
        line_end = len(section)
    new_section = section[:last.start()] + section[line_end + 1:]
    return text[:heading.end()] + new_section + rest[len(section):], last.group(1)


class TestBaselineIsGreen(unittest.TestCase):
    """陽性對照：未突變時必須是綠的，否則後面每一條紅都可能只是 harness 壞了。"""

    def test_unmutated_repo_files_pass(self):
        with _Fixture() as fx:
            self.assertEqual(fx.run(), [])

    def test_real_repo_root_passes(self):
        # 直接對真實 repo root 跑一次：tmp 複製過程本身也可能失真。
        self.assertEqual(
            repo_lint.check_kubectl_ns_whitelist_sync(str(_REPO_ROOT)), [])

    def test_all_sides_extract_a_nonempty_set(self):
        # 集合相等在「各邊都空」時恆真 ⇒ 先證明三邊都真的抽得到東西。
        claude, err_a = repo_lint._kubectl_ns_from_claude_md(_read(CLAUDE_REL))
        guard, err_b = repo_lint._kubectl_ns_from_guard_doc(_read(GUARD_REL))
        hook, err_c = repo_lint._kubectl_ns_from_hook(_read(HOOK_REL))
        self.assertIsNone(err_a)
        self.assertIsNone(err_b)
        self.assertIsNone(err_c)
        self.assertTrue(claude, "CLAUDE.md 側抽到空集合")
        self.assertTrue(guard, "guard doc 側抽到空集合")
        self.assertTrue(hook, "hook 側抽到空集合")
        self.assertEqual(claude, guard)
        self.assertEqual(claude, hook)


class TestDivergenceGoesRed(unittest.TestCase):
    """兩份副本分歧時要紅，而且要點名是哪一個 namespace、在哪一邊。"""

    def test_guard_doc_missing_one_namespace(self):
        text, victim = _drop_last_guard_item(_read(GUARD_REL))
        with _Fixture(mutations={GUARD_REL: text}) as fx:
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn(victim, violations[0])
        self.assertIn(CLAUDE_REL, violations[0])

    def test_claude_md_missing_one_namespace(self):
        text, victim = _drop_last_claude_row(_read(CLAUDE_REL))
        with _Fixture(mutations={CLAUDE_REL: text}) as fx:
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn(victim, violations[0])
        self.assertIn(GUARD_REL, violations[0])

    def test_hook_missing_one_namespace(self):
        # 第三份副本（hook 常數）漏一項也要紅——這是把 hook 納入對帳的驗收本體。
        text, victim = _drop_last_hook_item(_read(HOOK_REL))
        with _Fixture(mutations={HOOK_REL: text}) as fx:
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn(victim, violations[0])
        self.assertIn(HOOK_REL, violations[0])

    def test_renamed_namespace_is_caught_on_both_sides(self):
        # 只改一邊的拼字＝兩個方向各多一項，訊息必須兩邊都點名。
        claude = _read(CLAUDE_REL)
        heading = repo_lint.KUBECTL_NS_HEADING_RE.search(claude)
        rest = claude[heading.end():]
        nxt = repo_lint.KUBECTL_NS_NEXT_HEADING_RE.search(rest)
        section = rest[:nxt.start()] if nxt else rest
        row = list(repo_lint.KUBECTL_NS_ROW_RE.finditer(section))[-1]
        original = row.group(1)
        typo = original + "-typo"
        mutated = section[:row.start(1)] + typo + section[row.end(1):]
        with _Fixture(mutations={CLAUDE_REL: claude[:heading.end()] + mutated
                                 + rest[len(section):]}) as fx:
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn(typo, violations[0])
        self.assertIn(original, violations[0])


class TestBrokenProbeIsNotGreen(unittest.TestCase):
    """探針壞掉必須紅——這是本 gate 最容易靜默失效的方向。"""

    def test_claude_md_heading_renamed(self):
        text = _read(CLAUDE_REL)
        match = repo_lint.KUBECTL_NS_HEADING_RE.search(text)
        text = text[:match.start()] + "### 標題被改寫了" + text[match.end():]
        with _Fixture(mutations={CLAUDE_REL: text}) as fx:
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn(CLAUDE_REL, violations[0])

    def test_guard_doc_line_renamed(self):
        text = _read(GUARD_REL)
        match = repo_lint.KUBECTL_NS_GUARD_LINE_RE.search(text)
        text = text[:match.start()] + "本節不再列出清單。" + text[match.end():]
        with _Fixture(mutations={GUARD_REL: text}) as fx:
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn(GUARD_REL, violations[0])

    def test_claude_md_table_emptied(self):
        # 表格整個被搬走：兩邊都空會讓集合比較恆真，所以這裡必須紅。
        text = _read(CLAUDE_REL)
        heading = repo_lint.KUBECTL_NS_HEADING_RE.search(text)
        rest = text[heading.end():]
        nxt = repo_lint.KUBECTL_NS_NEXT_HEADING_RE.search(rest)
        section = rest[:nxt.start()] if nxt else rest
        emptied = repo_lint.KUBECTL_NS_ROW_RE.sub("| 已搬走", section)
        with _Fixture(mutations={CLAUDE_REL: text[:heading.end()] + emptied
                                 + rest[len(section):]}) as fx:
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("恆真", violations[0])

    def test_hook_tuple_anchor_renamed(self):
        # 用被測模組自己的 regex 定位賦值行再改名——naive replace 第一次出現會打中
        # docstring 裡的字樣、突變沒發生，測試假紅（初版就是這樣被自己抓到的）。
        text = _read(HOOK_REL)
        match = repo_lint.KUBECTL_NS_HOOK_TUPLE_RE.search(text)
        assert match, "hook tuple 錨點不存在 ⇒ 突變沒發生"
        mutated = (text[:match.start()]
                   + match.group(0).replace("ALLOWED_NAMESPACES",
                                            "RENAMED_TUPLE", 1)
                   + text[match.end():])
        with _Fixture(mutations={HOOK_REL: mutated}) as fx:
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn(HOOK_REL, violations[0])

    def test_missing_file_is_not_fail_open(self):
        for dropped in ALL_RELS:
            with self.subTest(dropped=dropped):
                with _Fixture(drop=dropped) as fx:
                    violations = fx.run()
                self.assertTrue(violations)
                self.assertIn(dropped, violations[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
