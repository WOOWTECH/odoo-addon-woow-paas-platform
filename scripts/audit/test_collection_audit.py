#!/usr/bin/env python3
"""測試收集完備性稽核（Refs issue #546）。

**刻意獨立於 `repo-lint.py`**：那支檔案改動頻繁（`045c15aa`、`ab692d6c`、`b491682d`
都動過它），多條線同時加 gate 會互相踩。本檔自成一支，只共用 `baselines/` 目錄慣例。

## 為什麼這支 gate 存在

Odoo 全套跑完印的是
`odoo/tests/result.py:198` → ``f'{failures_count} failed, {errors_count} error(s) of {testsRun} tests'``
—— **這一行對「測試根本沒被收集」與「測試被 skip」都是綠的**：

| 失效形態 | 在執行清單裡？ | 算進 `of N tests`？ | summary 看得出來？ |
|---|---|---|---|
| 檔案沒被 `tests/__init__.py` 匯入（機制 A，#467） | ❌ | ❌ | ❌ 完全看不出來 |
| **整檔只有純 `unittest.TestCase`（機制 B，#544）** | ❌ | ❌ | ❌ **C1 的匯入比對看不到它——import 都在** |
| 整批 deselect（tag 沒匹配／module 沒裝） | ❌ | ❌ | ❌ |
| **skip** | ✅ | ✅ | ❌（`skipped` 不出現在 summary） |

## 設計原則：輸出「是哪一個」，不是「差幾個」

「差幾個」沒有可證偽性——拿到一個差額只能猜，任何聽起來合理的解釋都會被接受。
「是哪一個」拿到具體名字，兩秒就能核對。**所以本檔所有失敗輸出都列名。**

## 檢查

    C1  --check-imports  `src/tests/` 可匯入名字 vs `__init__.py` 實際匯入（含子套件遞迴，雙向）
    C1b --check-imports  整檔只有純 unittest.TestCase 的模組（會被 --test-tags 整批過濾）
    C2 --check-baseline  靜態推導的預期 class 清單 vs baseline 檔（雙向差集）
    C3 --check-executed  baseline vs 測試 log 的實際執行清單（雙向差集）
    C4 --check-skips     測試 log 的 skip 盤點（列名＋理由，配白名單）

    --update-baseline    重新產生 baselines/test-collection.txt
    --self-test          把已知邊界樣本餵給本檔自己的 matcher

`--self-test` 不是裝飾：實作過程中它抓到本檔自己的缺陷（只 glob `*.py` 會漏掉
`src/tests/integration/` 這個子套件目錄）——**188 個名字裡只錯 1 個，少到看起來
像資料異常而不是工具異常**。任何改動本檔 matcher 的人請先跑它。
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "src" / "tests"
BASELINE = pathlib.Path(__file__).resolve().parent / "baselines" / "test-collection.txt"
KNOWN_ORPHANS = pathlib.Path(__file__).resolve().parent / "baselines" / "test-collection-known-unexecuted.txt"
GREEN_STREAK = pathlib.Path(__file__).resolve().parent / "baselines" / "test-collection-green-streak.txt"

# 升級為 blocking 需要「連續 N 次 develop push 全綠」。N 取 3。
GREEN_STREAK_REQUIRED = 3

# ── log matcher ───────────────────────────────────────────────────────────
# Odoo 18 `odoo/tests/result.py:263` log_stats() 在 stats logger 開到 DEBUG 時輸出
# "Detailed Tests Report"，每行形如：
#     \t<addon>.<測試檔>.<Class>.<method>: 0.12s 34 queries
# 粒度由點數決定（1=addon / 3=addon.檔.類 / 4=addon.檔.類.方法）。
DETAIL_LINE = re.compile(
    r"^\s*(?P<id>[A-Za-z_][\w.]*)\s*:\s*[\d.]+s\s+\d+\s+queries\s*$"
)

# Odoo 18 `odoo/tests/result.py:157` addSkip() →
#     self.log(logging.INFO, 'skipped %s : %s', self.getDescription(test), reason, test=test)
# logger 名 = test.__module__，所以同一行同時有「哪個檔」「哪個 Class.method」「什麼理由」。
# ⚠ 理由字串本身可能含冒號（repo 實例：
#   "A.2 prerequisite: cloud_service_ids missing — see A.1"），
#   所以 reason 必須吃到行尾。用 `[^:]*` 之類的天真寫法**不是抓錯理由，是整行不匹配**，
#   那筆 skip 會直接從清單消失 ⇒ skip 數被低估 ⇒ gate 照樣通過。
SKIP_LINE = re.compile(
    r"(?P<mod>odoo\.addons\.[\w.]+):\s+skipped\s+(?P<test>\S+)\s+:\s+(?P<reason>.*?)\s*$"
)

# `@tagged` 內出現這些負向 tag 的 class 不會進標準跑，屬預期排除。
DESELECT_TAGS = ("-standard",)


# ── 靜態掃描 ──────────────────────────────────────────────────────────────
def imported_submodules(init_path: pathlib.Path) -> set[str]:
    """`__init__.py` 匯入的同層 submodule 名。

    刻意用 AST 不用 regex：`from . import a`、`from . import a, b`、
    `from .a import X`、括號跨行、`if` 內巢狀 —— 字面 regex 任一形態都會漏。
    （實測：換成 `^from \\. import (\\w+)$` 會漏抓 7 種形態、對真實 repo 多報 14 個假陽性。）
    """
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module is None:            # from . import a, b
            found.update(a.name for a in node.names)
        else:                              # from .a import X / from .a.b import X
            found.add(node.module.split(".")[0])
    return found


def on_disk_submodules(pkg_dir: pathlib.Path) -> set[str]:
    """可被 `from . import X` 匯入的名字集合。

    ⚠ 只 glob `*.py` 會漏掉**子套件目錄**（本 repo 的 `src/tests/integration/`）。
    """
    names = {p.stem for p in pkg_dir.glob("*.py") if p.stem != "__init__"}
    names |= {
        d.name for d in pkg_dir.iterdir()
        if d.is_dir() and (d / "__init__.py").is_file()
    }
    return names


def _decorator_tags(node: ast.ClassDef) -> list[str]:
    tags: list[str] = []
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        if call is None or not isinstance(call.func, ast.Name) or call.func.id != "tagged":
            continue
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                tags.append(arg.value)
    return tags


def _pure_unittest_module(tree: ast.Module) -> bool:
    """整個 module 是否「只有純 unittest.TestCase」⇒ 會被 --test-tags 整批過濾掉。

    這是「從未執行」的**機制 B**（#544 實證，62 條中的 47 條）：檔案有被
    `tests/__init__.py` 匯入、import 也都在，所以 C1 的匯入比對**結構上看不到它**。
    Odoo 的 `--test-tags /module` 過濾依賴 Odoo tags 機制，純 `unittest.TestCase`
    沒有該屬性 —— 但只要**同檔有任一個 Odoo TestCase**，整個 module 就會被載入
    （對照組：`test_reconciler_diff.py` 同樣含 unittest.TestCase 卻有被執行）。

    ⇒ 判準是 module 級的「有沒有至少一個非 unittest 基底」，不是 class 級的
      「有沒有用 unittest.TestCase」。後者會把 test_reconciler_diff 誤殺。
    """
    unittest_aliases = {"unittest.TestCase"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "unittest":
            unittest_aliases |= {a.asname or a.name for a in node.names if a.name == "TestCase"}

    saw_class = False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        saw_class = True
        for b in node.bases:
            if isinstance(b, ast.Name):
                name = b.id
            elif isinstance(b, ast.Attribute):
                name = f"{getattr(b.value, 'id', '')}.{b.attr}"
            else:
                return False          # 無法靜態判定 → 保守視為可收集
            if name not in unittest_aliases:
                return False          # 有非 unittest 基底 ⇒ 整個 module 會被載入
    return saw_class


def scan_expected(tests_dir: pathlib.Path) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """回傳 (預期會被收集的 {`<檔>.<Class>`: test 方法數}, [(被排除者, 原因)])。

    判準＝**class body 內直接定義** `def test_*`。Odoo 18 的收集不吃「只從 base／mixin
    繼承來的 test_*」（#477 實證：4 個只繼承的 class 各跑 0 test、靜默不報錯），
    所以「直接定義」才是對的判準，不是「繼承鏈上有 test_*」。

    排除一律**列名回傳**，不靜默丟棄。
    """
    expected: dict[str, int] = {}
    excluded: list[tuple[str, str]] = []

    init = tests_dir / "__init__.py"
    imported = imported_submodules(init) if init.is_file() else set()

    for path in sorted(tests_dir.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(tests_dir)
        top = rel.parts[0][: -len(".py")] if len(rel.parts) == 1 else rel.parts[0]
        stem = path.stem

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            excluded.append((str(rel), f"語法錯誤無法解析：{exc}"))
            continue

        pure_unittest = _pure_unittest_module(tree)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            n = sum(
                1 for b in node.body
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                and b.name.startswith("test_")
            )
            if not n:
                continue
            ident = f"{stem}.{node.name}"

            if top not in imported:
                excluded.append((ident, f"所在檔 `{top}` 沒被 tests/__init__.py 匯入 → C1 會擋"))
                continue
            deselect = [t for t in _decorator_tags(node) if t in DESELECT_TAGS]
            if deselect:
                excluded.append((ident, f"@tagged{tuple(deselect)} 不進標準跑"))
                continue
            if pure_unittest:
                excluded.append((ident, f"所在檔 `{stem}` 整檔只有純 unittest.TestCase"
                                        " → 被 --test-tags 整批過濾（機制 B，#544）"))
                continue
            expected[ident] = n

    return expected, excluded


# ── baseline I/O ──────────────────────────────────────────────────────────
BASELINE_HEADER = """\
# 預期會被 Odoo 收集的測試 class 清單（scripts/audit/test_collection_audit.py --update-baseline 產生）
# ⚠ 產生指令**不是** repo-lint.py --update-baseline —— 本 gate 是獨立腳本。
# 調整前必讀 issue #546（設計與判準）：
#   https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/546
#
# 格式：<class 內直接定義的 test 方法數>\\t<測試檔>.<Class>
# 判準：class body 內**直接定義** def test_*（Odoo 18 不收只從 base/mixin 繼承來的）。
# 排除：檔案沒被 tests/__init__.py 匯入者（C1 會擋）、@tagged('-standard') 者。
#
# 這份清單目前是**靜態推導**，尚未與真實 CI 執行清單對帳過
# （對帳排程：B+C 觸發策略落地後第一趟 develop push 全套，見 issue #546）。
"""


def load_baseline(path: pathlib.Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        count, _, ident = line.partition("\t")
        if ident:
            out[ident.strip()] = int(count.strip())
    return out


def write_baseline(path: pathlib.Path, expected: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{n}\t{ident}\n" for ident, n in sorted(expected.items()))
    path.write_text(BASELINE_HEADER + body, encoding="utf-8")


# ── log 解析 ──────────────────────────────────────────────────────────────
def extract_executed(log_path: pathlib.Path, granularity: str = "class") -> set[str]:
    """從 Detailed Tests Report 抽實際執行清單，回傳 `<檔>.<Class>` 集合。"""
    want = {"module": 1, "class": 3, "method": 4}[granularity]
    out: set[str] = set()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = DETAIL_LINE.match(line)
        if not m:
            continue
        parts = m.group("id").split(".")
        if len(parts) == want:
            out.add(".".join(parts[1:]) if want >= 3 else parts[0])
    return out


def parse_skips(log_path: pathlib.Path) -> list[tuple[str, str, str]]:
    """回傳 [(測試檔, `Class.method`, 理由)]。"""
    out = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SKIP_LINE.search(line)
        if m:
            out.append((m.group("mod").rsplit(".", 1)[-1],
                        m.group("test"), m.group("reason")))
    return out


# ── 檢查 ──────────────────────────────────────────────────────────────────
def check_imports(tests_dir: pathlib.Path, _prefix: str = "") -> int:
    init = tests_dir / "__init__.py"
    if not init.is_file():
        print(f"C1 FATAL: {init} 不存在", file=sys.stderr)
        return 2
    if _prefix:
        label = _prefix
    else:
        # self-test 會餵 REPO_ROOT 之外的 temp dir，relative_to 會 raise
        try:
            label = tests_dir.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            label = tests_dir.as_posix()
    on_disk, imported = on_disk_submodules(tests_dir), imported_submodules(init)
    orphan, phantom = sorted(on_disk - imported), sorted(imported - on_disk)

    known = _load_known_unexecuted()["A"]
    fresh = [o for o in orphan if f"{label}/{o}" not in known]
    stale = [k for k in sorted(known) if k.startswith(f"{label}/")
             and k.rsplit("/", 1)[-1] not in orphan]

    rc = 0
    if fresh:
        rc = 1
        print(f"\nC1 FAIL [{label}] — 以下 {len(fresh)} 個測試檔存在但沒被 __init__.py 匯入，")
        print("        它們的測試從未被收集（`of N tests` 看起來正常，少的是這幾個）：")
        for name in fresh:
            f = tests_dir / f"{name}.py"
            n = count_test_defs(f) if f.is_file() else -1
            print(f"  - {label}/{name}   " + (f"（{n} 條 def test_*）" if n >= 0 else "（子套件目錄）"))
    known_hit = [o for o in orphan if f"{label}/{o}" in known]
    if known_hit:
        print(f"\nC1 註記 [{label}]：{len(known_hit)} 個已在待修清單（issue #467），不重複擋：")
        for name in known_hit:
            f = tests_dir / f"{name}.py"
            n = count_test_defs(f) if f.is_file() else -1
            print(f"  ~ {label}/{name}   " + (f"（{n} 條 def test_*，仍未執行）" if n >= 0 else ""))
    if stale:
        # 待修清單不是允許清單：修好了就該刪行，否則它會慢慢腐爛成一張沒人看的白名單
        rc = 1
        print(f"\nC1 FAIL [{label}] — 以下 {len(stale)} 項已不再是 orphan，"
              "請從 test-collection-known-orphans.txt 刪掉（清單腐爛防線）：")
        for name in stale:
            print(f"  ✓ {name}")
    if phantom:
        rc = 1
        print(f"\nC1 FAIL [{label}] — 以下 {len(phantom)} 個名字被匯入但東西不存在（會 ImportError）：")
        for name in phantom:
            print(f"  - {label}/{name}")

    for d in sorted(tests_dir.iterdir()):
        if d.is_dir() and (d / "__init__.py").is_file():
            rc |= check_imports(d, _prefix=f"{label}/{d.name}")

    if rc == 0 and not _prefix:
        print("C1 PASS：每一個測試檔（含子套件）都被匯入，沒有多餘匯入")
    return rc


def _load_known_unexecuted() -> dict[str, set[str]]:
    """回傳 {"A": {路徑…}, "B": {模組名…}}。"""
    out: dict[str, set[str]] = {"A": set(), "B": set()}
    if not KNOWN_ORPHANS.is_file():
        return out
    for ln in KNOWN_ORPHANS.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        mech, _, name = ln.partition("\t")
        if mech in out and name:
            out[mech].add(name.strip())
    return out


def check_upgrade_readiness() -> int:
    """本 gate 何時該從 warning-only 升級成 blocking —— **由 gate 自己判定並提醒**。

    刻意**不用時間條件**（「跑一週再改」）：時間到了不保證問題解決，
    而且沒有任何機制會在一週後提醒任何人回來改 ⇒ 它會永遠停在 warning-only。
    **常態 warning 的 gate 等於沒有 gate**，與 known-unexecuted 檔頭那句
    「常態帶紅的 gate 等於沒有 gate」是同一個孿生形態。

    改用兩個**可判定的狀態**，兩者都要滿足：
      1. `test-collection-known-unexecuted.txt` 清空（機制 A／B 都不剩）
      2. 連續 GREEN_STREAK_REQUIRED 次 develop push 全套全綠

    條件 2 的資料來源是 `baselines/test-collection-green-streak.txt`，
    由跑全套的那支 workflow 每次成功後 append 一行 `<run_id>\t<UTC>`；
    失敗時清空。⚠ 該 workflow 尚未落地（觸發策略待拍板）⇒ 檔案不存在時
    **明確報「無法累計」而不是報 0**——那兩者意思完全不同。
    """
    known = _load_known_unexecuted()
    remaining = sum(len(v) for v in known.values())

    if GREEN_STREAK.is_file():
        streak = len([ln for ln in GREEN_STREAK.read_text(encoding="utf-8").splitlines()
                      if ln.strip() and not ln.startswith("#")])
        streak_note = f"{streak}/{GREEN_STREAK_REQUIRED}"
    else:
        streak = -1
        streak_note = f"無法累計（跑全套的 workflow 尚未落地，非 0）"

    print(f"\n升級條件：待修清單剩 {remaining} 項；develop push 連續全綠 {streak_note}")
    if remaining or streak < GREEN_STREAK_REQUIRED:
        return 0

    print("=" * 72)
    print("★ 本 gate 已滿足升級條件，請把 .gitea/workflows/test-collection-audit.yml")
    print("  的 `continue-on-error: true` 移除，讓它真正擋 merge。")
    print(f"  依據：待修清單已清空、develop push 連續 {streak} 次全綠。")
    print("  （本步驟以非零退出碼提醒你——這是唯一會讓它變紅的情況，")
    print("    不是缺陷，是「該做的事」自己找上門。）")
    print("=" * 72)
    return 3


def check_pure_unittest_modules(tests_dir: pathlib.Path) -> int:
    """C1b：抓「機制 B」——整檔只有純 unittest.TestCase ⇒ 被 --test-tags 整批過濾。

    C1 的匯入比對**結構上看不到這種**（import 都在），所以必須是獨立一道。
    #544 實證：全 repo 62 條從未執行的測試裡，47 條是機制 B。
    """
    known = _load_known_unexecuted()["B"]
    found: dict[str, int] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if not _pure_unittest_module(tree):
            continue
        n = sum(1 for node in tree.body if isinstance(node, ast.ClassDef)
                for b in node.body
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                and b.name.startswith("test_"))
        if n:
            found[path.stem] = n

    fresh = {k: v for k, v in found.items() if k not in known}
    stale = sorted(known - set(found))

    rc = 0
    if fresh:
        rc = 1
        print(f"\nC1b FAIL — 以下 {len(fresh)} 個測試檔整檔只有純 unittest.TestCase，")
        print("        會被 --test-tags 整批過濾 ⇒ 從未執行（`of N tests` 看起來正常）：")
        for name, n in sorted(fresh.items()):
            print(f"  - src/tests/{name}.py   （{n} 條 def test_*）"
                  f"\n      修法：把基底改成 Odoo TestCase（TransactionCase / HttpCase 等）")
    known_hit = sorted(set(found) & known)
    if known_hit:
        print(f"\nC1b 註記：{len(known_hit)} 個已在待修清單（issue #544），不重複擋：")
        for name in known_hit:
            print(f"  ~ src/tests/{name}.py   （{found[name]} 條 def test_*，仍未執行）")
    if stale:
        rc = 1
        print(f"\nC1b FAIL — 以下 {len(stale)} 項已不再是純 unittest 模組，"
              "請從 test-collection-known-unexecuted.txt 刪掉（清單腐爛防線）：")
        for name in stale:
            print(f"  ✓ B\t{name}")
    if rc == 0 and not known_hit:
        print("C1b PASS：沒有整檔純 unittest.TestCase 的測試模組")
    elif rc == 0:
        print("C1b PASS：除待修清單外，沒有新的純 unittest.TestCase 模組")
    return rc


def count_test_defs(path: pathlib.Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return -1
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name.startswith("test_"))


def _diff_report(title: str, left_name: str, right_name: str,
                 left: dict[str, int] | set[str], right: dict[str, int] | set[str]) -> int:
    """雙向差集並列名。

    ⚠ 為什麼一定要雙向：單向（只檢查 baseline 每一項是否還在）有假綠洞——
    **把項目從 baseline 拿掉就通過了**。雙向才會讓「baseline 被偷偷刪一行」以
    「actual 多出一項」的形式浮上來。
    """
    ls, rs = set(left), set(right)
    gone, added = sorted(ls - rs), sorted(rs - ls)
    shrunk = []
    if isinstance(left, dict) and isinstance(right, dict):
        shrunk = sorted(
            (k, left[k], right[k]) for k in ls & rs if right[k] < left[k]
        )
    if not gone and not added and not shrunk:
        print(f"{title} PASS：{left_name} 與 {right_name} 完全一致（{len(ls)} 項）")
        return 0
    if gone:
        print(f"\n{title} FAIL — 以下 {len(gone)} 項在 {left_name} 有、{right_name} 沒有：")
        for x in gone:
            print(f"  - {x}")
    if added:
        print(f"\n{title} FAIL — 以下 {len(added)} 項在 {right_name} 有、{left_name} 沒有"
              "（新增測試？還是 baseline 被刪行？）：")
        for x in added:
            print(f"  + {x}")
    if shrunk:
        print(f"\n{title} FAIL — 以下 {len(shrunk)} 個 class 的 test 方法數變少了"
              "（class 還在，但裡面的測試消失了）：")
        for ident, before, after in shrunk:
            print(f"  ! {ident}   {before} → {after}")
    return 1


def check_baseline(tests_dir: pathlib.Path, baseline: pathlib.Path) -> int:
    expected, excluded = scan_expected(tests_dir)
    if not baseline.is_file():
        print(f"C2 FATAL: baseline 不存在，先跑 --update-baseline（{baseline}）", file=sys.stderr)
        return 2
    rc = _diff_report("C2", "baseline", "目前原始碼", load_baseline(baseline), expected)
    if excluded:
        print(f"\nC2 註記：以下 {len(excluded)} 個 class 被排除在 baseline 之外（列名，不靜默丟棄）：")
        for ident, why in sorted(excluded):
            print(f"  ~ {ident}\n      因為：{why}")
    return rc


def check_executed(baseline: pathlib.Path, log_path: pathlib.Path) -> int:
    actual = extract_executed(log_path, "class")
    if not actual:
        print("C3 FATAL: log 裡找不到 Detailed Tests Report —— "
              "跑測試時要帶 --log-handler=odoo.tests.stats:DEBUG。"
              "（空集合不算通過：這正是探針自毀的形狀）", file=sys.stderr)
        return 2
    return _diff_report("C3", "baseline", "實際執行清單", load_baseline(baseline), actual)


def check_skips(log_path: pathlib.Path, allow_path: pathlib.Path | None) -> int:
    skips = parse_skips(log_path)
    print(f"skipped 總數={len(skips)}")
    if not skips:
        print("C4 PASS：本次沒有任何測試被 skip")
        return 0

    print("\n--- 依理由彙總（CI 與本機 DB 的差異通常集中在少數幾個理由）---")
    by_reason: dict[str, int] = {}
    for _, _, reason in skips:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {reason}")

    allow: set[str] = set()
    if allow_path and allow_path.is_file():
        allow = {ln.strip() for ln in allow_path.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")}

    unexpected = [s for s in skips if f"{s[0]}.{s[1]}" not in allow]
    if not unexpected:
        print(f"\nC4 PASS：{len(skips)} 筆 skip 全在白名單內")
        return 0
    print(f"\nC4 FAIL — 以下 {len(unexpected)} 筆 skip 不在白名單，"
          "它們算進了 `of N tests` 卻什麼都沒驗：")
    for mod, test, reason in sorted(unexpected):
        print(f"  - {mod}.{test}\n      理由：{reason}")
    return 1


# ── self-test：把邊界樣本餵給自己的 matcher ────────────────────────────────
SELF_TEST_INIT = '''
from . import test_plain
from . import test_two_a, test_two_b
from .test_submodule_form import SomeClass
from . import (
    test_paren_multiline,
    test_paren_second,
)
from .test_deep.nested import Thing
import os
from odoo.tests import TransactionCase
if True:
    from . import test_conditional
'''

SELF_TEST_SKIP_LINES = [
    ("2026-08-03 00:01:10,040 265 INFO test "
     "odoo.addons.woow_paas_platform.tests.test_probe_sentinel: "
     "skipped TestProbeSentinel.test_probe_sentinel_skips : probe sentinel: deliberately skipped ",
     ("test_probe_sentinel", "TestProbeSentinel.test_probe_sentinel_skips",
      "probe sentinel: deliberately skipped")),
    ("... INFO test odoo.addons.woow_paas_platform.tests.test_a: "
     "skipped TestA.test_y : A.2 prerequisite: cloud_service_ids missing",
     ("test_a", "TestA.test_y", "A.2 prerequisite: cloud_service_ids missing")),
    ("... INFO test odoo.addons.woow_paas_platform.tests.test_removal_477: "
     "skipped _AddonRemovalCase.test_z : no template",
     ("test_removal_477", "_AddonRemovalCase.test_z", "no template")),
]

SELF_TEST_DETAIL_LINES = [
    ("\twoow_paas_platform: 259.47s 282962 queries", 1),
    ("\twoow_paas_platform.test_acl_p9.TestAclP9: 1.20s 34 queries", 3),
    ("\twoow_paas_platform.test_removal_477._AddonRemovalCase.test_x: 0.10s 2 queries", 4),
    ("2026-08-02 22:15:42,894 260 INFO test odoo.tests.result: 0 failed, 0 error(s) of 2237 tests", 0),
    ("\tDetailed Tests Report:", 0),
]


def self_test() -> int:
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    # (1) import matcher：五種匯入形態全要抓到，絕對匯入不得誤抓
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "__init__.py"
        f.write_text(SELF_TEST_INIT, encoding="utf-8")
        got = imported_submodules(f)
        want = {"test_plain", "test_two_a", "test_two_b", "test_submodule_form",
                "test_paren_multiline", "test_paren_second", "test_deep", "test_conditional"}
        if got != want:
            failures.append(f"import matcher：漏抓={sorted(want - got)} 誤抓={sorted(got - want)}")
        if "os" in got:
            failures.append("import matcher 把絕對匯入 `import os` 誤算成同層 submodule")

    # (2) on_disk：子套件目錄要算、無 __init__.py 的純目錄不算；遞迴要抓到子套件內漏匯入
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "__init__.py").write_text("from . import test_a\nfrom . import subpkg\n")
        (root / "test_a.py").write_text("def test_x(): pass\n")
        (root / "subpkg").mkdir()
        (root / "subpkg" / "__init__.py").write_text("")           # 子套件自己漏匯入
        (root / "subpkg" / "test_inner.py").write_text("def test_y(): pass\n")
        (root / "not_a_pkg").mkdir()
        (root / "not_a_pkg" / "test_z.py").write_text("")
        if on_disk_submodules(root) != {"test_a", "subpkg"}:
            failures.append(f"on_disk matcher 錯：{sorted(on_disk_submodules(root))}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check_imports(root)
        if rc == 0:
            failures.append("遞迴檢查沒抓到子套件內漏匯入的檔案（假綠）")
        if "test_inner" not in buf.getvalue():
            failures.append("遞迴檢查沒把漏掉的名字印出來（只給數字＝不可證偽）")

    # (3) SKIP_LINE：理由含冒號 / 底線開頭 class / 行尾空白；非 skip 行不得誤抓
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "log.txt"
        p.write_text("\n".join(s for s, _ in SELF_TEST_SKIP_LINES), encoding="utf-8")
        got_s = parse_skips(p)
        want_s = [w for _, w in SELF_TEST_SKIP_LINES]
        if got_s != want_s:
            failures.append(f"SKIP_LINE：\n    實得={got_s}\n    期望={want_s}")
        neg = pathlib.Path(td) / "neg.txt"
        neg.write_text(
            "... INFO test odoo.tests.result: 0 failed, 0 error(s) of 2237 tests\n"
            "FAIL: TestA.test_b\n"
            "... INFO test odoo.addons.x.tests.test_c: Starting TestC.test_d ...\n",
            encoding="utf-8")
        if parse_skips(neg):
            failures.append(f"SKIP_LINE 誤抓非 skip 行：{parse_skips(neg)}")

    # (4) DETAIL_LINE 粒度切分
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "log.txt"
        p.write_text("\n".join(s for s, _ in SELF_TEST_DETAIL_LINES), encoding="utf-8")
        if extract_executed(p, "class") != {"test_acl_p9.TestAclP9"}:
            failures.append(f"class 粒度切分錯：{extract_executed(p, 'class')}")
        if extract_executed(p, "method") != {"test_removal_477._AddonRemovalCase.test_x"}:
            failures.append(f"method 粒度切分錯：{extract_executed(p, 'method')}")

    # (5) scan_expected：只繼承不算、@tagged('-standard') 排除且**列名**
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "__init__.py").write_text("from . import test_s\n")
        (root / "test_s.py").write_text(
            "from odoo.tests import tagged\n"
            "class _Mixin:\n"
            "    def test_from_mixin(self): pass\n"
            "class TestConsumer(_Mixin, object):\n"
            "    pass\n"                                    # 只繼承 → Odoo 18 不收
            "class TestReal(object):\n"
            "    def test_a(self): pass\n"
            "    def test_b(self): pass\n"
            "@tagged('-standard', 'post_install')\n"
            "class TestDeselected(object):\n"
            "    def test_c(self): pass\n",
            encoding="utf-8")
        exp, exc = scan_expected(root)
        if "test_s.TestConsumer" in exp:
            failures.append("scan_expected 把「只繼承 test_*」的 class 算進預期（Odoo 18 不收）")
        if exp.get("test_s.TestReal") != 2:
            failures.append(f"scan_expected 對直接定義的 class 計數錯：{exp}")
        if "test_s.TestDeselected" in exp:
            failures.append("scan_expected 沒排除 @tagged('-standard')")
        if not any(i == "test_s.TestDeselected" for i, _ in exc):
            failures.append("@tagged('-standard') 被靜默排除而非列名")

    # (5b) 機制 B：整檔純 unittest.TestCase ⇒ 排除；但**混用**時不得誤殺
    #      對照組來自 #544：test_reconciler_diff.py 同樣含 unittest.TestCase，
    #      卻因同檔有 TransactionCase 而確實被執行。判準必須是 module 級不是 class 級。
    for label, src, want_pure in [
        ("全純 unittest",
         "import unittest\nclass TestA(unittest.TestCase):\n    def test_x(self): pass\n", True),
        ("from unittest import TestCase 別名形",
         "from unittest import TestCase\nclass TestA(TestCase):\n    def test_x(self): pass\n", True),
        ("混用（對照組，不得誤殺）",
         "import unittest\nclass TestA(unittest.TestCase):\n    def test_x(self): pass\n"
         "class TestB(TransactionCase):\n    def test_y(self): pass\n", False),
        ("基底是複雜運算式 → 保守視為可收集",
         "class TestA(mod.get_base()):\n    def test_x(self): pass\n", False),
        ("沒有任何 class",
         "def helper(): pass\n", False),
    ]:
        if _pure_unittest_module(ast.parse(src)) is not want_pure:
            failures.append(f"_pure_unittest_module 對「{label}」判錯（期望 pure={want_pure}）")

    # 端到端：混用檔的純 unittest class 也必須留在 expected（module 會整個載入）
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "__init__.py").write_text("from . import test_mix\nfrom . import test_pure\n")
        (root / "test_mix.py").write_text(
            "import unittest\nclass TestPureOne(unittest.TestCase):\n    def test_x(self): pass\n"
            "class TestOdoo(TransactionCase):\n    def test_y(self): pass\n")
        (root / "test_pure.py").write_text(
            "import unittest\nclass TestOnlyPure(unittest.TestCase):\n    def test_z(self): pass\n")
        exp, exc = scan_expected(root)
        if "test_mix.TestPureOne" not in exp:
            failures.append("混用檔的純 unittest class 被誤殺（#544 的對照組會被誤判）")
        if "test_pure.TestOnlyPure" in exp:
            failures.append("全純 unittest 模組沒被排除（機制 B 漏抓）")
        if not any(i == "test_pure.TestOnlyPure" for i, _ in exc):
            failures.append("機制 B 的排除被靜默丟棄而非列名")

    # (6) 雙向差集：單向會有「把項目從 baseline 拿掉就通過」的假綠洞
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_del = _diff_report("X", "baseline", "actual", {"a": 1}, {"a": 1, "b": 1})
        rc_shrink = _diff_report("Y", "baseline", "actual", {"a": 5}, {"a": 3})
    if rc_del == 0:
        failures.append("差集不是雙向：baseline 少一行（actual 多出來）卻通過了")
    if rc_shrink == 0:
        failures.append("沒偵測到 class 還在但 test 方法數變少")

    if failures:
        print("SELF-TEST FAIL：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("SELF-TEST PASS：import 五形態／子套件遞迴／SKIP_LINE 三形態＋陰性對照／"
          "粒度切分／繼承與 -standard 排除／機制 B（含混用對照組）／雙向差集 —— 全部命中")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-imports", action="store_true")
    ap.add_argument("--check-baseline", action="store_true")
    ap.add_argument("--check-executed", metavar="ODOO_TEST_LOG")
    ap.add_argument("--check-skips", metavar="ODOO_TEST_LOG")
    ap.add_argument("--allow-skips", metavar="FILE")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--upgrade-check", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--all", action="store_true", help="跑所有不需要 log 的檢查（C1+C2）")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.update_baseline:
        expected, excluded = scan_expected(TESTS_DIR)
        write_baseline(BASELINE, expected)
        print(f"已寫入 {BASELINE.relative_to(REPO_ROOT)}：{len(expected)} 個 class")
        if excluded:
            print(f"排除 {len(excluded)} 個（列名）：")
            for ident, why in sorted(excluded):
                print(f"  ~ {ident}  —  {why}")
        return 0

    rc = 0
    ran = False
    if args.all or args.check_imports:
        rc |= check_imports(TESTS_DIR); ran = True
    if args.all or args.check_imports:
        rc |= check_pure_unittest_modules(TESTS_DIR)
    if args.all:
        rc |= check_upgrade_readiness()
    if args.all or args.check_baseline:
        rc |= check_baseline(TESTS_DIR, BASELINE); ran = True
    if args.upgrade_check:
        rc |= check_upgrade_readiness(); ran = True
    if args.check_executed:
        rc |= check_executed(BASELINE, pathlib.Path(args.check_executed)); ran = True
    if args.check_skips:
        rc |= check_skips(pathlib.Path(args.check_skips),
                          pathlib.Path(args.allow_skips) if args.allow_skips else None)
        ran = True
    if not ran:
        ap.print_help()
        return 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
