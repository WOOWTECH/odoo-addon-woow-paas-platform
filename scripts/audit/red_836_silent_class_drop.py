#!/usr/bin/env python3
"""🔴 RED（issue #836）：條數算成 0 的 TestCase 子類會從 C2 的輸出裡**整個消失**。

## 這支不是通過型測試——它現在**故意是紅的**

`exit 1` 是預期狀態，直到 #836 的修法落地為止。它守的性質只有一條，
刻意寫得比任何一個候選修法都窄（四個候選方案都必須讓它轉綠，所以它不預判裁決）：

    範圍內的一個 class，**真的有** `def test*`（Odoo loader 會產生測試實例），
    但條數被算成 0 ⇒ 它必須出現在 C2 的輸出裡某處（差集或註記皆可）。

⚠ 檔名刻意**不是** `test_*.py`：`repo-lint.yml` 的「scripts/ 自我測試」步驟會 glob
   `scripts/audit/test_*.py` 並逐一 `python3 <檔>`，回非零就把 repo-lint 整條弄紅。
   一支預期為紅的檔放進那個 glob 等於把 CI 綁成常態紅。修法落地、本檔轉綠之後，
   要不要改名納入那個 glob 是另一個決定。

## 情境對應的是哪一種真實 bug（不是憑空注入的假設）

`repo-lint.py:6685 _test_own_test_methods()` 只看 **class body 的頂層** statement：

    return [n.name for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")]

而 Odoo 的 loader 看的是**執行後的 class 物件**（`odoo/tests/loader.py:30`
`test_case_class.__dict__.items()` ＋ `:43` `method_name.startswith('test')`）。
兩者在「`def test*` 被包在 class body 的 `if` / `try` / `for` 裡」時**結論相反**：
AST 那邊看到的是一個 `ast.If`／`ast.Try` 節點（不是 FunctionDef）⇒ 條數 0；
執行期那邊該方法已經是 class 屬性 ⇒ 照跑。

所以本檔用的不是「注入一個假的判準 bug」，是**現存判準本來就有的盲點**。
`(A)` 那一格會用 loader 的同一條判準把「它真的會跑」執行出來，不是用嘴巴說的。

⚠ 這個盲點本身**不是** #836 要修的東西（#836 修的是「算成 0 之後靜默丟棄」那一格），
   它在這裡只是最省事的觸發器：任何讓條數變 0 的判準 bug 都會走到同一個出口。

## 這支檔的四格

    (A) 執行期對照   用 loader 篩子④的同一條判準，證明那個 class 真的有測試方法
    (B) RED          真的 scan_expected → write_baseline → check_baseline ⇒ 看它報 PASS
    (C) 陽性對照     同一支 check_baseline，餵它三種**該抓到**的差異，確認它會紅
    (D) 旁證（不計分）其餘判準對這個情境的反應

Refs #836 #546
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
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
# 兩個洩漏形狀（`if` 與 `try`）而不是一個：一個樣本會讓人以為是某種特殊語法的怪癖，
# 兩個不同的 statement 型別才看得出這是「只認頂層 FunctionDef」這件事本身。
_LEAK_BODY = (
    "class TestDroppedByIf(%(base)s):\n"
    "    if True:\n"
    "        def test_leaked_if(self): pass\n"
    "class TestDroppedByTry(%(base)s):\n"
    "    try:\n"
    "        def test_leaked_try(self): pass\n"
    "    except Exception:\n"
    "        pass\n"
)

LEAKED = ("test_leak.TestDroppedByIf", "test_leak.TestDroppedByTry")

TREE = {
    # `_addon_source_tree_present()` 靠這個檔判斷「這是本 addon 的 root」；
    # 少了它，收集面檢查會 fail-open（#828 那一格）⇒ 這棵樹就不是有效的對照環境。
    "src/__manifest__.py": "{'name': 'probe-836'}\n",
    "src/tests/__init__.py": "from . import test_leak\n",
    "src/tests/test_leak.py": (
        "from odoo.tests.common import TransactionCase\n"
        # 對照用的正常 class：沒有它，(C) 的三個陽性對照就沒有東西可比，
        # 而且「預期清單是空的」與「判準壞掉」會長得一樣。
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


def main() -> int:
    audit = _load_audit()
    failures: list[str] = []
    print("=" * 74)
    print("RED #836：條數 0 的 class 從 C2 輸出裡消失")
    print("=" * 74)

    # ── (A) 執行期對照：它真的有測試方法 ────────────────────────────────
    # 用 loader.py:30 `cls.__dict__` ＋ :43 `startswith('test')` 的同一條判準，
    # 對**執行過的 class 物件**求值。這一格存在的理由：整支 RED 的前提是
    # 「這個 class 有測試方法」，那個前提若只是用嘴巴宣告，後面全部無效。
    print("\n(A) 執行期對照 —— Odoo loader 篩子④ 會從這些 class 產生哪些測試？")
    ns: dict = {"TransactionCase": unittest.TestCase}
    exec(compile(_LEAK_BODY % {"base": "TransactionCase"}, "<red836>", "exec"), ns)
    for cname in ("TestDroppedByIf", "TestDroppedByTry"):
        cls = ns[cname]
        produced = sorted(n for n, v in cls.__dict__.items()
                          if callable(v) and n.startswith("test"))
        print("    %-18s → %s" % (cname, produced))
        if not produced:
            failures.append(
                "(A) %s 在執行期沒有產生任何測試方法 —— 本 RED 的前提不成立，"
                "後面每一格都不算數（樣本寫壞了，不是被測物有事）" % cname)
    if failures:
        for f in failures:
            print("\n✗ " + f)
        return 2

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        audit._write_tree(root, TREE)

        # ── (B) RED ─────────────────────────────────────────────────────
        # baseline 由**同一份判準**產生（＝ `--update-baseline` 的行為）。這正是
        # issue 描述的觸發路徑：判準把 class 算成 0 ⇒ 它不進 baseline，也不進註記
        # ⇒ 雙向差集兩邊都沒有它 ⇒ PASS。新增一個 class 時也走同一條路。
        expected, excluded = audit.scan_expected(root)
        baseline = root / "baseline.txt"
        audit.write_baseline(baseline, expected)
        rc, out = _run(audit.check_baseline, root, baseline)

        print("\n(B) RED —— C2 對這棵樹說什麼？")
        print("    scan_expected() 預期清單 : %s" % sorted(expected))
        print("    scan_expected() 排除清單 : %s" % sorted(i for i, _ in excluded))
        print("    check_baseline() 回傳碼  : %d（0 ＝ PASS）" % rc)
        print("    ---- C2 stdout 逐字 ----")
        for line in out.rstrip("\n").splitlines():
            print("    | " + line)
        print("    ------------------------")

        for ident in LEAKED:
            short = ident.split(".")[-1]
            named = short in out
            print("    輸出有沒有提到 %-18s : %s" % (short, "有" if named else "**沒有**"))
            if not named:
                failures.append(
                    "%s 有 `def test*`（(A) 已執行證明），條數被算成 0 之後"
                    "**沒有出現在 C2 輸出的任何一行**——差集沒有它、註記也沒有它。"
                    "這正是 #836：輸出與「這棵樹很乾淨」不可區分" % ident)
        if rc == 0:
            print("    ⇒ C2 報 PASS，而且一個字都沒提那兩個 class。**這個 PASS 就是紅燈。**")

        # ── (C) 陽性對照 ────────────────────────────────────────────────
        # 否定性結論（「輸出裡找不到它」）必須配陽性對照：判準整個壞掉時，
        # 「找不到」與「這一格有縫」的輸出一模一樣。
        print("\n(C) 陽性對照 —— 同一支 check_baseline，餵它三種該抓到的差異")
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
            print("    %-28s rc=%d  '%s' 行=%d" % (label, prc, sym, hits))
            if prc == 0 or hits == 0:
                failures.append(
                    "(C) 陽性對照失敗：%s 卻回 rc=%d／%d 行 —— 判準對「明知該紅的輸入」"
                    "也不紅，那麼 (B) 證的就不是 #836 那一格，是判準整個壞了" % (label, prc, hits))

        # ── (D) 旁證（不計分）──────────────────────────────────────────
        # C0/C3/C4/C5 的輸入是**測試 log**。這裡餵一份「洩漏的測試真的跑了」的 log，
        # 看其餘判準對同一個情境的反應。⚠ 這一格的結果不進 failures：
        # 它記錄的是現況，不是本檔主張的性質。
        print("\n(D) 旁證（不計分）—— 餵一份「洩漏的測試真的跑了」的 log 給其餘判準")
        log = root / "test.log"
        log.write_text("\n".join([
            "\tDetailed Tests Report:",
            "\tprobe: 9.99s 100 queries",
            "\tprobe.test_leak.TestVisible: 1.00s 10 queries",
            "\tprobe.test_leak.TestVisible.test_ok: 0.50s 5 queries",
            "\tprobe.test_leak.TestVisible.test_ok2: 0.50s 5 queries",
            "\tprobe.test_leak.TestDroppedByIf: 0.10s 1 queries",
            "\tprobe.test_leak.TestDroppedByIf.test_leaked_if: 0.10s 1 queries",
            "\tprobe.test_leak.TestDroppedByTry: 0.10s 1 queries",
            "\tprobe.test_leak.TestDroppedByTry.test_leaked_try: 0.10s 1 queries",
            "2026-08-22 00:00:00,000 1 INFO test odoo.tests.result: "
            "0 failed, 0 error(s) of 4 tests",
        ]) + "\n", encoding="utf-8")
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
        print("🔴 RED（%d 格）—— 這是 #836 落地前的預期狀態：" % len(failures))
        for f in failures:
            print("  ✗ " + f)
        print("=" * 74)
        return 1
    print("🟢 GREEN —— 條數 0 的 class 已經會被列名，#836 那一格關上了。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
