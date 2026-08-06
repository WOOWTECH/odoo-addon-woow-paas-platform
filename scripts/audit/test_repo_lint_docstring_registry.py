#!/usr/bin/env python3
"""repo-lint 檔頭對帳 gate 的 A/B 注入測試（#598）。

這支測試的存在理由跟被測的 check 一樣：**「現在是綠的」不是驗收**。#596 那條
check 當初通過了 CI，卻在 hook 模式整條休眠——綠色與「根本沒跑到」長得一模一樣。
所以下面每一條注入測試都在問同一個問題：**改壞它，會不會紅、紅得對不對、有沒有
點名是哪一個**。

注入的素材是**真實的 `scripts/audit/repo-lint.py` 內容**（複製到 tmp 目錄後再改），
不是手寫的假 fixture：假 fixture 只證明 parser 在自己捏的形狀上能動，真檔案才會在
別人重排檔頭時把測試一起弄紅。注入手法一律用被測模組自己的 regex 定位，不寫死
「十九」「17」這種當下值——寫死會讓第 20 條 check 進來的那天測試變成假紅。

跑法：
    python3 scripts/audit/test_repo_lint_docstring_registry.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_docstring_registry.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。實測過
`env -i`、空 HOME、`cwd=/` 皆綠。
"""
import ast
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_SELF_REL = "scripts/audit/repo-lint.py"
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

# 檔名有連字號，只能走 importlib。先登記進 sys.modules 再 exec（與
# test_memory_integrity.py 同）。
_spec = importlib.util.spec_from_file_location("repo_lint", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)

_REAL_SRC = _GATE_PATH.read_text(encoding="utf-8")
_DOC_END = '\n"""\n'

# 「在尾端追加一條新 check」這個注入手法需要兩個錨點：run_all() 的最後一行呼叫，以及
# REPO_LINT_RULES 的最後一項。兩者**從被測模組推導**，不寫死當下最後一條 check 的名字。
#
# 🔴 為什麼：寫死的版本在「有人真的在尾端新增一條 check」的當天會靜默不匹配，
#    `str.replace()` 找不到就原樣回傳 ⇒ 注入沒發生 ⇒ 那幾支測試改為驗證一份**未突變**
#    的原始碼。下游的 assertNotEqual 絆線會擋住（設計正確），但它只說「注入工具失效」，
#    不會說是哪個錨點、為什麼——2026-08-06 新增 kubectl-ns-whitelist-sync 時實際踩到，
#    三處錨點同時失效。推導版讓這個維護動作直接消失。
# ⚠ 假設：最後一條規則是一對一（單一實作函式），且它就是 run_all() 的最後一個呼叫。
#    下面兩行 assert 把「假設不成立」變成 import 期就大聲失敗，而不是靜默失去鑑別力。
_LAST_RULE, _LAST_RULE_FNS = list(rl.REPO_LINT_RULES.items())[-1]
_LAST_FN = _LAST_RULE_FNS[-1]
_RUN_ALL_TAIL = "                  + %s(root))" % _LAST_FN
_RULES_TAIL = '    "%s": ("%s",),\n}' % (_LAST_RULE, _LAST_FN)
assert _RUN_ALL_TAIL in _REAL_SRC, (
    "推導不出 run_all() 的尾行（%r）——注入工具失效，不是測試通過。"
    "若最後一條規則改成多函式實作，這裡的推導假設要一起改" % _RUN_ALL_TAIL)
assert _RULES_TAIL in _REAL_SRC, (
    "推導不出 REPO_LINT_RULES 的尾項（%r）——注入工具失效，不是測試通過" % _RULES_TAIL)


# ------------------------------------------------------------ 注入工具

def _split_doc(src):
    """把原始碼切成 (含 module docstring 的開頭, 其餘)。"""
    head, sep, tail = src.partition(_DOC_END)
    assert sep, "找不到 module docstring 的結尾——注入工具失效，不是測試通過"
    return head, tail


def _edit_doc(src, mutate):
    head, tail = _split_doc(src)
    return mutate(head) + _DOC_END + tail


def _entries(text):
    return list(rl._REPO_LINT_ENTRY_RE.finditer(text))


def _replace_span(text, start, end, new):
    return text[:start] + new + text[end:]


def _bump_count(src, regex):
    """把檔頭某個計數宣告 +1（改寫成阿拉伯數字，parser 兩種都吃）。"""
    def mutate(head):
        m = regex.search(head)
        assert m, "檔頭找不到計數宣告——注入工具失效"
        claimed = rl._cn_int(m.group(1))
        assert claimed is not None, "計數宣告看不懂——注入工具失效"
        return _replace_span(head, m.start(1), m.end(1), str(claimed + 1))
    return _edit_doc(src, mutate)


def _drop_entry(src, index):
    """整條刪掉第 index（0-based）個編號條目，含它的續行。"""
    def mutate(head):
        ms = _entries(head)
        assert len(ms) > index + 1, "條目不足以做「刪中間一條」注入"
        return _replace_span(head, ms[index].start(), ms[index + 1].start(), "")
    return _edit_doc(src, mutate)


def _swap_numbers(src, i, j):
    """把第 i 與第 j 個條目的編號對調（#596 的 11／12 顛倒）。"""
    def mutate(head):
        ms = _entries(head)
        a, b = ms[i], ms[j]
        assert a.start(1) < b.start(1)
        # 由後往前改，前面的 span 才不會位移。
        head = _replace_span(head, b.start(1), b.end(1), a.group(1))
        return _replace_span(head, a.start(1), a.end(1), b.group(1))
    return _edit_doc(src, mutate)


def _rename_entry(src, index, new_name):
    def mutate(head):
        m = _entries(head)[index]
        return _replace_span(head, m.start(2), m.end(2), new_name)
    return _edit_doc(src, mutate)


def _fn_line_range(src, name):
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "找不到 %s()——注入工具失效" % name
    return fn.lineno - 1, fn.end_lineno


def _drop_call_line(src, inside, call):
    """刪掉 inside() 內含有 `call(` 的那一行（模擬「忘了加進 run_all」）。"""
    lo, hi = _fn_line_range(src, inside)
    lines = src.splitlines(True)
    for i in range(lo, hi):
        if call + "(" in lines[i]:
            del lines[i]
            return "".join(lines)
    raise AssertionError("%s() 裡找不到 %s(——注入工具失效" % (inside, call))


def _blank_call_lines(src, inside, call):
    """把 inside() 內**每一處** `… = call(root…)` 換成 `… = []`（模擬「hook 分派忘了
    掛」，語法仍合法）。

    🔴 一定要換掉全部出現處。像 `check_gateway_copy_gate_anchor` 在 run_hook 裡有
    兩個分支各叫一次，只換一處的話它仍留在呼叫集合裡 ⇒ 注入等於沒發生，而測試會
    照樣綠（注入無效與「gate 正確」在輸出上完全相同）。所以換完之後再 parse 一次
    自檢：它必須真的不在 inside() 的呼叫集合裡。
    """
    lo, hi = _fn_line_range(src, inside)
    lines = src.splitlines(True)
    hits = 0
    for i in range(lo, hi):
        if call + "(" in lines[i]:
            new = re.sub(r"=\s*%s\(root[^)]*\)" % re.escape(call), "= []", lines[i])
            assert new != lines[i], (
                "第 %d 行不是 `… = %s(root…)` 的形態，注入工具改不動它：%r"
                % (i + 1, call, lines[i]))
            lines[i] = new
            hits += 1
    assert hits, "%s() 裡找不到 %s(——注入工具失效" % (inside, call)
    out = "".join(lines)
    fn = next(n for n in ast.parse(out).body
              if isinstance(n, ast.FunctionDef) and n.name == inside)
    assert call not in rl._repo_lint_check_calls(fn), (
        "%s 仍出現在 %s() 裡（只 blank 了 %d 處？）——注入沒生效，測試會假綠"
        % (call, inside, hits))
    return out


# ------------------------------------------------------------ 共用 TestCase

class InjectionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "scripts", "audit"))
        self.addCleanup(self._tmp.cleanup)

    def write(self, src):
        path = os.path.join(self.root, _SELF_REL)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        return path

    def audit(self, src):
        self.write(src)
        return rl.check_repo_lint_docstring_registry(self.root)

    def assertMentions(self, violations, *needles):
        """至少一條違規同時提到所有 needle（＝有點名「是哪一個」）。"""
        for v in violations:
            if all(n in v for n in needles):
                return v
        self.fail("沒有任何違規同時提到 %s。實際違規：\n%s"
                  % (list(needles), "\n".join("  ✗ " + v for v in violations) or "  （空）"))

    def assertNoMention(self, violations, needle):
        hit = [v for v in violations if needle in v]
        self.assertEqual(hit, [], "不該出現提到 %r 的違規：%s" % (needle, hit))


# ------------------------------------------------------------ 正對照

class TestRealFileIsClean(unittest.TestCase):
    """未注入時是綠的——而且綠得有前提（探針真的解析到東西）。"""

    def test_real_repo_is_green(self):
        self.assertEqual(rl.check_repo_lint_docstring_registry(str(_REPO_ROOT)), [])

    def test_probe_actually_parsed_something(self):
        # 「零違規」若來自空集合就是假綠。這裡把前提釘住：檔頭條目數 ≥ 10、
        # run_all 呼叫數 ≥ 10、且兩者透過 REPO_LINT_RULES 對得上。
        doc = ast.get_docstring(ast.parse(_REAL_SRC))
        entries = rl._REPO_LINT_ENTRY_RE.findall(doc)
        self.assertGreaterEqual(len(entries), 10)
        self.assertEqual(len(entries), len(rl.REPO_LINT_RULES))
        mapped = [fn for fns in rl.REPO_LINT_RULES.values() for fn in fns]
        tree = ast.parse(_REAL_SRC)
        top = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        called = set(rl._repo_lint_check_calls(top["run_all"]))
        self.assertGreaterEqual(len(called), 10)
        self.assertEqual(set(mapped), called)

    def test_this_gate_is_registered_everywhere(self):
        # 本 check 自己也必須四處到齊——尤其 run_hook：漏掛就是 #596 的形態本身。
        tree = ast.parse(_REAL_SRC)
        top = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        me = "check_repo_lint_docstring_registry"
        self.assertIn(me, rl._repo_lint_check_calls(top["run_all"]))
        self.assertIn(me, rl._repo_lint_check_calls(top["run_hook"]))
        self.assertIn("repo-lint-docstring-registry", rl.REPO_LINT_RULES)

    def test_unmutated_copy_is_green(self):
        # 注入測試的基準：**同一份內容複製到別的 root 也必須是綠的**。少了這條，
        # 下面每個「注入後變紅」都可能是複製本身弄紅的（比對實驗要固定 recipe）。
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "scripts", "audit"))
            shutil.copy(str(_GATE_PATH), os.path.join(tmp, _SELF_REL))
            self.assertEqual(rl.check_repo_lint_docstring_registry(tmp), [])


# ------------------------------------------------------------ 三種指定注入

class TestInjectionCountDrift(InjectionCase):
    """注入 1：檔頭數字改錯。"""

    def test_function_count_wrong(self):
        v = self.audit(_bump_count(_REAL_SRC, rl._REPO_LINT_FUNC_COUNT_RE))
        self.assertMentions(v, "N 個 check 函式", "run_all()")

    def test_rule_count_wrong(self):
        v = self.audit(_bump_count(_REAL_SRC, rl._REPO_LINT_RULE_COUNT_RE))
        self.assertMentions(v, "M 條規則", "編號條目數")

    def test_count_declaration_deleted(self):
        # 把宣告整句拿掉不能變成「沒有這個訊號就沒事」——零命中同樣算紅。
        def mutate(head):
            m = rl._REPO_LINT_FUNC_COUNT_RE.search(head)
            return _replace_span(head, m.start(), m.end(), "若干 check")
        v = self.audit(_edit_doc(_REAL_SRC, mutate))
        self.assertMentions(v, "找不到「N 個 check 函式」的宣告")

    def test_count_unreadable(self):
        def mutate(head):
            m = rl._REPO_LINT_FUNC_COUNT_RE.search(head)
            return _replace_span(head, m.start(1), m.end(1), "十十十")
        v = self.audit(_edit_doc(_REAL_SRC, mutate))
        self.assertMentions(v, "看不懂")


class TestInjectionEntryDeleted(InjectionCase):
    """注入 2：刪掉一條條目。"""

    def setUp(self):
        super().setUp()
        self.dropped = _entries(_split_doc(_REAL_SRC)[0])[4].group(2)
        self.violations = self.audit(_drop_entry(_REAL_SRC, 4))

    def test_names_the_missing_rule(self):
        # 🔴 關鍵：要說出**是哪一條**不見了，不是「差一條」。
        self.assertMentions(self.violations, self.dropped, "檔頭卻沒有這條編號條目")

    def test_rule_count_also_red(self):
        self.assertMentions(self.violations, "M 條規則")

    def test_numbering_gap_reported(self):
        self.assertMentions(self.violations, "超出總數")


class TestInjectionNumbersSwapped(InjectionCase):
    """注入 3：兩個編號對調（#596 的第三處問題）。"""

    def setUp(self):
        super().setUp()
        ms = _entries(_split_doc(_REAL_SRC)[0])
        self.a, self.b = ms[10].group(2), ms[11].group(2)
        self.violations = self.audit(_swap_numbers(_REAL_SRC, 10, 11))

    def test_both_sides_named(self):
        self.assertMentions(self.violations, self.a, "編號必須嚴格遞增")
        self.assertMentions(self.violations, self.b, "編號必須嚴格遞增")

    def test_counts_stay_green(self):
        # 顛倒不改變任何數量——這正是「只比數字抓不到」的那一類。
        self.assertNoMention(self.violations, "N 個 check 函式")
        self.assertNoMention(self.violations, "M 條規則")


# ------------------------------------------- 數量相等但名字錯配 / 實作側漂移

class TestInjectionNameMismatch(InjectionCase):
    def test_renamed_entry_is_caught_both_ways(self):
        old = _entries(_split_doc(_REAL_SRC)[0])[3].group(2)
        v = self.audit(_rename_entry(_REAL_SRC, 3, "totally-different-name"))
        self.assertMentions(v, "totally-different-name", "沒有對應項")
        self.assertMentions(v, old, "檔頭卻沒有這條編號條目")

    def test_entry_order_must_match_mapping_order(self):
        # 對調檔頭兩條條目的**順序**（連編號一起搬，所以編號仍是 1..N 遞增、數量
        # 也沒變）——只有順序對帳會紅。映射表的順序就是通過訊息的順序。
        def mutate(head):
            ms = _entries(head)
            i, j = 2, 3
            a = head[ms[i].start():ms[j].start()]
            b = head[ms[j].start():ms[j + 1].start()]
            swapped = (re.sub(r"^\d+\.", "%d." % (i + 1), b, count=1)
                       + re.sub(r"^\d+\.", "%d." % (j + 1), a, count=1))
            return head[:ms[i].start()] + swapped + head[ms[j + 1].start():]
        v = self.audit(_edit_doc(_REAL_SRC, mutate))
        self.assertMentions(v, "順序不一致")
        self.assertNoMention(v, "M 條規則")
        self.assertNoMention(v, "編號必須嚴格遞增")

    def test_renamed_entry_keeps_counts_green(self):
        v = self.audit(_rename_entry(_REAL_SRC, 3, "totally-different-name"))
        self.assertNoMention(v, "N 個 check 函式")
        self.assertNoMention(v, "M 條規則")


class TestInjectionImplementationDrift(InjectionCase):
    def test_check_missing_from_run_all(self):
        target = "check_availability_reason_labels"
        v = self.audit(_drop_call_line(_REAL_SRC, "run_all", target))
        self.assertMentions(v, target, "run_all() 沒有呼叫它")
        self.assertMentions(v, target, "沒有出現在 run_all()")

    def test_check_missing_from_run_hook(self):
        # #596 本體：CI 會抓、hook 完全沉默。
        target = "check_pattern_doc_registry"
        v = self.audit(_blank_call_lines(_REAL_SRC, "run_hook", target))
        self.assertMentions(v, target, "沒有出現在 run_hook() 的分派")

    def test_new_check_not_registered(self):
        src = _REAL_SRC.replace(
            _RUN_ALL_TAIL,
            _RUN_ALL_TAIL[:-1] + "\n                  + check_brand_new_thing(root))")
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——run_all 的尾行變了")
        src += "\n\ndef check_brand_new_thing(root):\n    return []\n"
        v = self.audit(src)
        self.assertMentions(v, "check_brand_new_thing", "沒有任何一條規則登記它")

    def test_stale_hook_exemption(self):
        src = _REAL_SRC.replace(
            "REPO_LINT_HOOK_EXEMPT = {}",
            "REPO_LINT_HOOK_EXEMPT = {'check_long_gone': '理由'}")
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——豁免清單的宣告變了")
        v = self.audit(src)
        self.assertMentions(v, "check_long_gone", "豁免比它豁免的東西活得久")

    def test_exemption_silences_only_the_named_check(self):
        """豁免必須是**逐項扣除**，不是「EXEMPT 非空就跳過整個 4b」的全域開關。

        🔴 這支的前一版只 blank 一個 check 再豁免它，唯一的斷言又是否定性的
        （`assertNoMention`）。那樣的構造裡根本不存在「未豁免、且不在 run_hook」的
        第二個 check ⇒ 測試名承諾的 "only" 那一半驗不到，而且 check 整個 fail-open
        時它必然通過（實測：把 check 函式體插 `return []`，全套 22 支變紅、這支仍綠）。
        改成 blank 兩個、只豁免其中一個，並加上一句**肯定性**斷言。四格實測：

            實作\\注入          舊注入(blank 1)   新注入(blank 2, 豁免 1)
            逐項扣除（正確）        綠                綠   ← 陰性對照成立
            全域開關（錯誤）        綠                紅
        """
        exempted = "check_pattern_doc_registry"
        # run_hook 裡有兩個分支各叫一次，`_blank_call_lines` 會兩處都換掉並自檢。
        still_red = "check_gateway_copy_gate_anchor"
        src = _blank_call_lines(_REAL_SRC, "run_hook", exempted)
        src = _blank_call_lines(src, "run_hook", still_red)
        patched = src.replace(
            "REPO_LINT_HOOK_EXEMPT = {}",
            "REPO_LINT_HOOK_EXEMPT = {'%s': '理由'}" % exempted)
        self.assertNotEqual(patched, src, "注入工具失效——豁免清單的宣告變了")
        v = self.audit(patched)
        # 具名的那個被消音……
        self.assertNoMention(v, exempted)
        # ……未豁免的那個仍被點名。這是唯一的肯定性斷言，也是 fail-open 時會紅的那句。
        self.assertMentions(v, still_red, "沒有出現在 run_hook() 的分派")


# ------------------------------------------------------------ 特異性（不得誤擋）

def _append_entry(src, name, num_hint=None):
    """在檔頭最後一條條目之後補一條（編號接續）。"""
    def mutate(head):
        ms = _entries(head)
        num = num_hint or int(ms[-1].group(1)) + 1
        idx = head.index("\n用法：")
        return head[:idx] + "%d. %s  測試用的新條目。\n" % (num, name) + head[idx:]
    return _edit_doc(src, mutate)


class TestSpecificity(InjectionCase):
    """合法改動不得被誤擋。

    🔴 這一類測試在「只驗改壞→紅」的測試集裡是隱形的：一條恆紅的 gate 每次注入都
    變紅，敏感度測試全綠，看起來完美——直到它擋住正當工作，然後被人整條拿掉。所以
    敏感度與特異性要成對，缺一半的測試集不知道自己缺了什麼。
    """

    def test_new_check_registered_in_all_four_places_is_green(self):
        rule, fn = "brand-new-thing", "check_brand_new_thing"
        src = _REAL_SRC
        # (1) run_all：接在最後一項後面
        src = src.replace(
            _RUN_ALL_TAIL, _RUN_ALL_TAIL[:-1] + "\n                  + %s(root))" % fn)
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——run_all 的尾行變了")
        # (2) REPO_LINT_RULES：順序要與檔頭條目一致，所以也接在最後
        before = src
        src = src.replace(
            _RULES_TAIL, _RULES_TAIL[:-1] + '    "%s": ("%s",),\n}' % (rule, fn))
        self.assertNotEqual(src, before, "注入工具失效——映射表的尾項變了")
        # (3) run_hook 分派
        before = src
        src = src.replace(
            "        elif rel == REPO_LINT_SELF_REL:\n",
            '        elif rel == "src/brand_new.py":\n'
            "            violations = %s(root)\n"
            "        elif rel == REPO_LINT_SELF_REL:\n" % fn)
        self.assertNotEqual(src, before, "注入工具失效——run_hook 的分支變了")
        # (4) 函式定義
        src += "\n\ndef %s(root):\n    return []\n" % fn
        # (5) 檔頭：補條目 ＋ 兩個計數各 +1
        src = _append_entry(src, rule)
        src = _bump_count(src, rl._REPO_LINT_FUNC_COUNT_RE)
        src = _bump_count(src, rl._REPO_LINT_RULE_COUNT_RE)

        self.assertEqual(self.audit(src), [],
                         "四處都正確登記的新 check 被誤擋——gate 過度敏感，"
                         "正當工作會被擋住")

    def test_same_change_missing_only_the_docstring_entry_is_red(self):
        # 上一支的陰性對照：同一組改動只少做第 (5) 步 ⇒ 必須紅。否則「綠」可能來自
        # gate 根本沒看這些東西，而不是來自它們真的對得上。
        rule, fn = "brand-new-thing", "check_brand_new_thing"
        src = _REAL_SRC.replace(
            _RUN_ALL_TAIL, _RUN_ALL_TAIL[:-1] + "\n                  + %s(root))" % fn)
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——run_all 的尾行變了")
        src = src.replace(
            _RULES_TAIL, _RULES_TAIL[:-1] + '    "%s": ("%s",),\n}' % (rule, fn))
        src = src.replace(
            "        elif rel == REPO_LINT_SELF_REL:\n",
            '        elif rel == "src/brand_new.py":\n'
            "            violations = %s(root)\n"
            "        elif rel == REPO_LINT_SELF_REL:\n" % fn)
        src += "\n\ndef %s(root):\n    return []\n" % fn
        v = self.audit(src)
        self.assertMentions(v, rule, "檔頭卻沒有這條編號條目")


# ------------------------------------------------------------ fail-loud

class TestFailLoud(InjectionCase):
    """解析失敗必須是紅的。自檢寫成 fail-open ＝永久沉默且看起來永遠是綠的。"""

    def test_missing_file(self):
        v = rl.check_repo_lint_docstring_registry(self.root)
        self.assertMentions(v, "讀不到或 parse 失敗")

    def test_syntax_error(self):
        v = self.audit("def broken(:\n    pass\n")
        self.assertMentions(v, "讀不到或 parse 失敗")

    def test_docstring_deleted(self):
        _, tail = _split_doc(_REAL_SRC)
        v = self.audit(tail)
        self.assertMentions(v, "module docstring 不見了")

    def test_no_numbered_entries(self):
        # 條目格式被改成不認得的樣子（例如全部縮排）——雙射在空集合上恆真，
        # 所以「抽不到條目」必須自己就是違規。
        def mutate(head):
            return re.sub(r"^\d+\.", "  -", head, flags=re.M)
        v = self.audit(_edit_doc(_REAL_SRC, mutate))
        self.assertMentions(v, "抽不到任何")

    def test_mapping_table_deleted(self):
        src = _REAL_SRC.replace("REPO_LINT_RULES = {", "REPO_LINT_RULES_OLD = {", 1)
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——映射表的宣告變了")
        v = self.audit(src)
        self.assertMentions(v, "讀不到 `REPO_LINT_RULES` 映射表")

    def test_run_hook_deleted(self):
        lo, hi = _fn_line_range(_REAL_SRC, "run_hook")
        lines = _REAL_SRC.splitlines(True)
        src = "".join(lines[:lo] + ["def run_hook_renamed():\n", "    return 0\n"]
                      + lines[hi:])
        v = self.audit(src)
        self.assertMentions(v, "找不到", "run_hook")

    def test_mapping_value_written_as_string(self):
        # tuple 少一個逗號就變成字串，逐字元拆開後對帳會爆出一堆單字母假違規。
        src = _REAL_SRC.replace(
            '"controller-sudo": ("check_controller_sudo",),',
            '"controller-sudo": ("check_controller_sudo"),')
        self.assertNotEqual(src, _REAL_SRC, "注入工具失效——映射表第一列變了")
        v = self.audit(src)
        self.assertMentions(v, "必須是非空的函式名 tuple")


# ------------------------------------------------------------ hook 模式實走

class TestHookMode(InjectionCase):
    """不 grep 原始碼，直接餵 hook payload 給複製過去的那份腳本跑。

    這是 #596 唯一該有而當時沒有的驗收：**hook 分派真的接得到這個檔嗎**。
    """

    def _hook(self, src):
        path = self.write(src)
        env = {"CLAUDE_PROJECT_DIR": self.root, "PYTHONIOENCODING": "utf-8"}
        if os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        proc = subprocess.run(
            [sys.executable, path, "--hook"],
            input=json.dumps({"tool_input": {"file_path": path}}),
            capture_output=True, text=True, env=env, cwd=self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)  # hook 永遠 exit 0
        return proc.stdout.strip()

    def test_clean_file_does_not_block(self):
        self.assertEqual(self._hook(_REAL_SRC), "")

    def test_drifted_docstring_blocks(self):
        out = self._hook(_bump_count(_REAL_SRC, rl._REPO_LINT_FUNC_COUNT_RE))
        self.assertTrue(out, "hook 對檔頭漂移完全沉默——分派沒接到 %s" % _SELF_REL)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("N 個 check 函式", payload["reason"])


# ------------------------------------------------------------ 中文數字

class TestCnInt(unittest.TestCase):
    def test_arabic(self):
        self.assertEqual(rl._cn_int("19"), 19)
        self.assertEqual(rl._cn_int(" 7 "), 7)

    def test_chinese(self):
        for text, want in (("十", 10), ("十七", 17), ("十九", 19), ("二十", 20),
                           ("二十一", 21), ("九十九", 99), ("八", 8), ("〇", 0)):
            self.assertEqual(rl._cn_int(text), want, text)

    def test_unparseable(self):
        for text in ("", "十十", "abc", "一二三", "十七八", "五百"):
            self.assertIsNone(rl._cn_int(text), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
