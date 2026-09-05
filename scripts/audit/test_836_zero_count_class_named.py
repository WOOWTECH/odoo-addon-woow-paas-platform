#!/usr/bin/env python3
"""守衛（issue #836）：靜態條數算成 0 的 class 不得從 C2 的輸出裡消失。

## 它出生時是紅的

本檔先以 RED 落地（commit `3d2c76ef`，修法前 exit 1、5 格全紅），修法之後才轉綠並
改名納入 `repo-lint.yml` 的「scripts/ 自我測試」glob（`scripts/audit/test_*.py`）。
一支只驗過一次、之後沒人跑的探針，正是那一步的註解自己在治的失效形態。

⚠ 它守的**不是** rc：C2 對「條數 0」那一段刻意不計分（多數是正常的 mixin／fixture，
   計分會把 C2 綁成常態紅）。它守的是**列名**——那批 class 必須出現在輸出裡。

## 這支是段一那支 RED 的重建，不是它的複製

段一的 `red_836_silent_class_drop.py` 用「`def test*` 被包在 class body 的 `if` /
`try` 裡」當觸發器。那個盲點已被 **#850**（PR #867，merge `91fb7df6`）修掉——
`_test_own_test_methods()` 現在會遞迴進 `if` / `try` / `for`。它自己的 docstring
`:34-35` 就寫著「這個盲點本身**不是** #836 要修的，它只是最省事的觸發器」。

⚠ 段一那支在新基底上**仍然 exit 1**，但**紅在別的分支上**：兩個 class 已經進了
   `expected`（條數 1），`check_baseline()` PASS 時本來就不列名 ⇒ 它的
   「輸出沒提到這個名字」成立，但它的失敗訊息（「條數被算成 0 之後…」）**是假的**。
   紅燈的顏色對、診斷錯——這比變綠更難察覺。所以本檔重建，段一那支同時退場。

## 觸發器換成 non-`def` 形態（#850 明文標為範圍外的那批）

條數為 0 的成因不只一個。`_test_own_test_methods()` 只認 `ast.FunctionDef`，
所以任何**不是 `def` 語法**的測試方法都會被算成 0，而 Odoo loader 看的是
**執行後的 class 物件**（`odoo/tests/loader.py:30` `test_case_class.__dict__.items()`
＋ `:43` `method_name.startswith('test')`），照樣收得到：

    test_x = lambda self: None      指派（ast.Assign，不是 FunctionDef）
    setattr(Cls, "test_x", fn)      class 定義結束後注入
    @decorator 動態塞方法            decorator 回傳前改寫 cls

三種都留在樹裡而不是只留一種：一個樣本會讓人以為是某種語法怪癖，三種不同的
statement 型別才看得出這是「只認頂層 `def`」這件事本身。

## 兩個靜默出口（#836 要治的就是這兩格）

`scan_expected()` 的同一個迴圈裡有兩處「算成 0 就兩個容器都不進」：

    格一 `if n:`                       選得中（tag 含 standard）但條數 0
    格二 `if _test_own_test_methods(node):`  選不中（非 tagged）且 own 為空

兩格都是「既不進 `expected`、也不進 `excluded`」——與本檔自宣告的
「排除一律**列名**，不靜默丟棄」（`scan_expected` docstring）相反。

## 這支檔的五格

    (A) 執行期對照   用 loader 篩子④／④' 的同一條判準，**執行**證明那些 class
                     真的有測試方法（本檔每一格的前提；用嘴巴宣告的話後面全部無效）
    (B) RED 格一     三個 non-`def` 形態的 tagged class ⇒ 必須出現在 C2 輸出裡
    (C) RED 格二     一個非 tagged、own 為空、但**真的**貢獻測試方法的 mixin ⇒ 同上
    (D) 陽性對照     同一支 check_baseline，餵它三種**該抓到**的差異，確認它會紅；
                     再對 P1 守恆檢查注入一次「讓一個 class 憑空消失」，確認它也會紅
    (E) 旁證（不計分）其餘判準對這個情境的反應

## 它與 `test_collection_audit.py --self-test` 分工，兩支都要留

同一組變異體實測（見 PR 說明）：`--self-test` 驗的是**容器歸屬**（那批 class 有沒有
進第三個容器、成因標籤分不分得開），本檔驗的是**輸出**（名字有沒有真的印出來）。
把 `_report_zero_count()` 整段短路掉時，容器完全正確 ⇒ `--self-test` 全綠，只有本檔會紅。
反過來把「條數 0」併進 `excluded`（＝#836 的甲案）時只有 `--self-test` 會紅。
⇒ 兩支的盲區互補，刪掉任何一支都會留下一整類無聲退化。

Refs #836 #546 #850
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent


def _load_audit():
    """載入同目錄的 `test_collection_audit.py`（檔名不含連字號，但統一走 spec）。"""
    path = _HERE / "test_collection_audit.py"
    if not path.is_file():
        raise SystemExit("FATAL: 找不到 %s —— 被測物不在，空跑不算通過" % path)
    spec = importlib.util.spec_from_file_location("_audit_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_audit_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 受測樹 ────────────────────────────────────────────────────────────────
# 三個 non-`def` 形態（格一）＋ 一個 own 為空的 mixin（格二）＋ 一個正常對照。
# 正常對照不可省：沒有它，「預期清單是空的」與「判準整個壞掉」會長得一樣。
_LEAK_BODY = """\
class TestDroppedByAssign(%(base)s):
    test_leaked_assign = lambda self: None

class TestDroppedBySetattr(%(base)s):
    pass
setattr(TestDroppedBySetattr, "test_leaked_setattr", lambda self: None)

def _inject(cls):
    cls.test_leaked_deco = lambda self: None
    return cls

@_inject
class TestDroppedByDecorator(%(base)s):
    pass

class _MixinAssign:
    allow_inherited_tests_method = True
    test_from_mixin_assign = lambda self: None

class TestUsesMixin(_MixinAssign, %(base)s):
    def test_own(self): pass
"""

#: 格一：選得中（有效 tag 含 `standard`）但靜態條數＝0
LEAKED_GATE1 = ("test_leak.TestDroppedByAssign",
                "test_leak.TestDroppedBySetattr",
                "test_leak.TestDroppedByDecorator")
#: 格二：非 tagged（mixin）且 class body 頂層沒有 `def test*`
LEAKED_GATE2 = ("test_leak._MixinAssign",)

TREE = {
    # 合成 root 明示最小 workflow selector；真 repo 缺檔仍由被測物 fail-loud。
    ".gitea/workflows/odoo-tests.yml": (
        "run: odoo --test-tags /probe\\\\\n"
    ),
    # `_addon_source_tree_present()` 靠這個檔判斷「這是本 addon 的 root」；
    # #828 修正前少了它會 fail-open；現在會回明確 wrong-root violation，但仍只測到
    # layout 前置條件、測不到 zero-count 歸因，所以這棵樹必須保留 manifest。
    "src/__manifest__.py": "{'name': 'probe-836'}\n",
    "src/tests/__init__.py": "from . import test_leak\n",
    "src/tests/test_leak.py": (
        "from odoo.tests.common import TransactionCase\n"
        "class TestVisible(TransactionCase):\n"
        "    def test_ok(self): pass\n"
        "    def test_ok2(self): pass\n"
        + _LEAK_BODY % {"base": "TransactionCase"}
    ),
}


def _run(fn, *args) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


def _scan(audit, root):
    """呼叫真的 `scan_expected()`，對回傳的**元數**保持中立。

    🔴 本檔要在修法**前後**各跑一次同一份原始碼：修法前是 2-tuple、修法後是
       3-tuple。寫死元數的話「修法前跑不起來」會被讀成「RED 成立」——那是
       ValueError，不是判準的結論。所以這裡按位置取，不解包。
    """
    res = audit.scan_expected(root)
    expected, excluded = res[0], res[1]
    zero = list(res[2]) if len(res) > 2 else []
    return expected, list(excluded), zero


def _universe(root: pathlib.Path, suite: str) -> set[str]:
    """P1 的分母：**自己**用 plain `ast` 走一遍，不經過 `_test_tree_facts`。

    用被測物自己的枚舉當分母，守恆那條就恆真——那不是檢查，是重言式。
    """
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root / suite):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            fp = pathlib.Path(dirpath) / fn
            rel = str(fp.relative_to(root)).replace(os.sep, "/")
            mod = rel[len(suite) + 1:-len(".py")].replace("/", ".")
            tree = ast.parse(fp.read_text(encoding="utf-8"))
            out |= {"%s.%s" % (mod, n.name)
                    for n in tree.body if isinstance(n, ast.ClassDef)}
    return out


def _conservation(audit, root, suite):
    """回 (分母, 三個容器的聯集, 缺了誰, 重複出現在多個容器的是誰)。"""
    expected, excluded, zero = _scan(audit, root)
    u = _universe(root, suite)
    buckets = [set(expected), {i for i, _ in excluded}, {i for i, _ in zero}]
    union: set[str] = set()
    dup: set[str] = set()
    for b in buckets:
        dup |= union & b
        union |= b
    return u, union, u - union, dup


def main() -> int:  # noqa: C901 —— 五格是刻意攤平的，拆函式會讓「哪一格紅」變難讀
    audit = _load_audit()
    lint = audit.repo_lint()
    suite = lint.TEST_SUITE_DIR_REL
    failures: list[str] = []
    print("=" * 74)
    print("RED #836：靜態條數 0 的 class 從 C2 輸出裡消失（non-def 觸發器版）")
    print("=" * 74)

    # ── (A) 執行期對照 ───────────────────────────────────────────────────
    print("\n(A) 執行期對照 —— Odoo loader 會從這些 class 產生哪些測試？")
    ns: dict = {"TransactionCase": unittest.TestCase}
    exec(compile(_LEAK_BODY % {"base": "TransactionCase"}, "<red836>", "exec"), ns)
    # 篩子④：`cls.__dict__` 的 `test*`
    for cname in ("TestDroppedByAssign", "TestDroppedBySetattr", "TestDroppedByDecorator"):
        produced = sorted(n for n, v in ns[cname].__dict__.items()
                          if callable(v) and n.startswith("test"))
        print("    篩子④  %-24s → %s" % (cname, produced))
        if not produced:
            failures.append(
                "(A) %s 在執行期沒有產生任何測試方法 —— 本檔的前提不成立，後面每一格"
                "都不算數（樣本寫壞了，不是被測物有事）" % cname)
    # 篩子④'：帶 `allow_inherited_tests_method` 的子類走整條 MRO
    #   這一格證明的是「mixin 裡那個 lambda 真的會跑」——格二的 RED 若少了它，
    #   那個 mixin 消失就只是「少列一個沒人在意的名字」，強度完全不同。
    import inspect
    mixin_owner = sorted(n for n, _ in inspect.getmembers(ns["TestUsesMixin"], callable)
                         if n.startswith("test"))
    print("    篩子④' %-24s → %s" % ("TestUsesMixin", mixin_owner))
    if "test_from_mixin_assign" not in mixin_owner:
        failures.append(
            "(A) mixin 注入的 `test_from_mixin_assign` 沒有出現在子類的篩子④' 結果裡"
            " —— 格二那個 mixin 就不是「真的有東西會跑」，本檔格二的前提不成立")
    if failures:
        for f in failures:
            print("\n✗ " + f)
        return 2

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        audit._write_tree(root, TREE)

        expected, excluded, zero = _scan(audit, root)
        baseline = root / "baseline.txt"
        audit.write_baseline(baseline, expected)
        rc, out = _run(audit.check_baseline, root, baseline)

        print("\n(B)(C) C2 對這棵樹說什麼？")
        print("    scan_expected() 回傳元數     : %d" % len(audit.scan_expected(root)))
        print("    scan_expected() 預期清單     : %s" % sorted(expected))
        print("    scan_expected() 排除清單     : %s" % sorted(i for i, _ in excluded))
        print("    scan_expected() 條數 0 清單  : %s" % sorted(i for i, _ in zero))
        print("    check_baseline() 回傳碼      : %d（0 ＝ PASS）" % rc)
        print("    ---- C2 stdout 逐字 ----")
        for line in out.rstrip("\n").splitlines():
            print("    | " + line)
        print("    ------------------------")

        for grid, idents, desc in (
            ("(B) 格一", LEAKED_GATE1, "選得中（有效 tag 含 standard）但靜態條數＝0"),
            ("(C) 格二", LEAKED_GATE2, "非 tagged 且 class body 頂層無 `def test*`"),
        ):
            print("\n%s —— %s" % (grid, desc))
            for ident in idents:
                short = ident.split(".")[-1]
                named = short in out
                print("    輸出有沒有提到 %-24s : %s" % (short, "有" if named else "**沒有**"))
                if not named:
                    failures.append(
                        "%s %s 的靜態條數被算成 0（(A) 已執行證明它真的有測試方法），"
                        "之後**沒有出現在 C2 輸出的任何一行**——差集沒有它、註記也沒有它。"
                        "這正是 #836：輸出與「這棵樹很乾淨」不可區分" % (grid, ident))

        # ── P1 守恆 ─────────────────────────────────────────────────────
        print("\n(B/C) P1 守恆 —— 分母（自己 walk 的）vs 三個容器的聯集")
        u, union, missing, dup = _conservation(audit, root, suite)
        print("    分母（plain ast 自走）: %d  %s" % (len(u), sorted(u)))
        print("    三容器聯集            : %d" % len(union))
        print("    落在容器外（靜默丟棄）: %d  %s" % (len(missing), sorted(missing)))
        print("    重複出現在多個容器    : %d  %s" % (len(dup), sorted(dup)))
        if missing:
            failures.append(
                "P1 守恆不成立：%d 個 class 落在三個容器之外 ⇒ %s"
                % (len(missing), sorted(missing)))
        if dup:
            failures.append("P1 守恆不成立：%d 個 class 同時在多個容器裡 ⇒ %s"
                            % (len(dup), sorted(dup)))

        # ── (D) 陽性對照 ────────────────────────────────────────────────
        # 否定性結論（「輸出裡找不到它」「守恆成立」）必須配陽性對照：判準整個壞掉時，
        # 「找不到」與「這一格有縫」的輸出一模一樣，「守恆成立」與「分母算錯」也是。
        print("\n(D) 陽性對照 —— 明知該紅的輸入，判準會不會紅？")
        cases = [
            ("baseline 多一項（該報 gone）",
             dict(expected, **{"test_leak.TestGhost": 1}), "  - ", "-"),
            ("baseline 少一項（該報 added）", {}, "  + ", "+"),
            ("條數變少（該報 shrunk）", {"test_leak.TestVisible": 3}, "  ! ", "!"),
        ]
        for i, (label, content, marker, sym) in enumerate(cases):
            bp = root / ("pc%d.txt" % i)
            audit.write_baseline(bp, content)
            prc, pout = _run(audit.check_baseline, root, bp)
            hits = sum(1 for ln in pout.splitlines() if ln.startswith(marker))
            print("    %-30s rc=%d  '%s' 行=%d" % (label, prc, sym, hits))
            if prc == 0 or hits == 0:
                failures.append(
                    "(D) 陽性對照失敗：%s 卻回 rc=%d／%d 行 —— 判準對「明知該紅的輸入」"
                    "也不紅，那麼上面證的就不是 #836 那一格，是判準整個壞了" % (label, prc, hits))

        # 守恆的陽性對照：故意讓一個 class 從分母裡憑空消失，守恆檢查必須紅。
        #   ⚠ 這一格治的是「守恆恆真」那種假綠——分母若也來自被測物，
        #     少算一個時兩邊會**一起**變小，差額照樣是 0。
        u2 = set(u) - {"test_leak.TestVisible"}
        inj_missing = u2 - union
        inj_extra = union - u2
        print("    %-30s 分母少一項後：容器外=%d 容器多出=%d"
              % ("守恆注入（拿掉 TestVisible）", len(inj_missing), len(inj_extra)))
        if not inj_extra:
            failures.append(
                "(D) 守恆的陽性對照失敗：把 TestVisible 從分母拿掉之後，守恆比對"
                "**沒有**任何反應 ⇒ 上面那格 P1 綠燈不具鑑別力")

        # ── (E) 旁證（不計分）──────────────────────────────────────────
        print("\n(E) 旁證（不計分）—— 餵一份「洩漏的測試真的跑了」的 log 給其餘判準")
        log = root / "test.log"
        ran = [("test_leak.TestVisible", ("test_ok", "test_ok2")),
               ("test_leak.TestDroppedByAssign", ("test_leaked_assign",)),
               ("test_leak.TestDroppedBySetattr", ("test_leaked_setattr",)),
               ("test_leak.TestDroppedByDecorator", ("test_leaked_deco",)),
               ("test_leak.TestUsesMixin", ("test_own", "test_from_mixin_assign"))]
        lines = ["\tDetailed Tests Report:", "\tprobe: 9.99s 100 queries"]
        for cls_id, methods in ran:
            lines.append("\tprobe.%s: 0.10s 1 queries" % cls_id)
            lines += ["\tprobe.%s.%s: 0.10s 1 queries" % (cls_id, m) for m in methods]
        lines.append("2026-08-23 00:00:00,000 1 INFO test odoo.tests.result: "
                     "0 failed, 0 error(s) of %d tests" % sum(len(m) for _, m in ran))
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sent = root / "sentinels.txt"
        sent.write_text("test_leak.TestVisible.test_ok\n", encoding="utf-8")
        known = root / "known-failures.txt"
        known.write_text("", encoding="utf-8")
        orig = (audit.SENTINELS, audit.KNOWN_FAILURES)
        audit.SENTINELS, audit.KNOWN_FAILURES = sent, known
        try:
            for tag, fn, args in (
                ("C0 哨兵", audit.check_sentinels, (log,)),
                ("C3 執行清單", audit.check_executed, (baseline, log)),
                ("C4 skip", audit.check_skips, (log, None)),
                ("C5 失敗集合", audit.check_failure_set, (log,)),
            ):
                drc, dout = _run(fn, *args)
                head = next((l for l in dout.splitlines() if l.strip()), "")
                print("    %-12s rc=%d  %s" % (tag, drc, head.strip()[:90]))
        finally:
            audit.SENTINELS, audit.KNOWN_FAILURES = orig

    print("\n" + "=" * 74)
    if failures:
        print("🔴 FAIL（%d 格）—— #836 的靜默出口回來了（或本檔的前提壞了）：" % len(failures))
        for f in failures:
            print("  ✗ " + f)
        print("=" * 74)
        return 1
    print("🟢 GREEN —— 兩個靜默格都關上了：靜態條數 0 的 class 一律列名，且守恆成立。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
