#!/usr/bin/env python3
"""測試收集完備性稽核（Refs issue #546 #684 #544）。

## 🔴 靜態判準的單一事實來源是 `repo-lint.py`，不是本檔

本檔曾自帶兩條**靜態**判準（C1 匯入完備性／C1b 純 unittest 模組）。#684 的
PR #811 之後，同一組性質已經落在 `repo-lint.py` 的 `test-collectability`
（`check_test_module_collection` ＋ `check_test_class_execution`），而且**更強**：

| 性質 | 本檔舊版 | repo-lint（現行） |
|---|---|---|
| 「檔沒被 `__init__.py` import」 | 有 | 有 |
| 「import 了不存在的檔」 | 有 | 有 |
| 「子套件檔沒被**頂層** re-export」 | ❌ 看不到 | ✅ 有（`_get_tests_modules()` 只讀 `<addon>.tests` 自己的屬性）|
| 「基底不對 ⇒ 拿不到 `test_tags`」 | module 級（**整檔**皆純 unittest 才算）| **逐 class** |
| 混合檔（同檔既有會跑的、也有不會跑的）| ❌ 整檔放行 | ✅ 點名到 class |

⇒ 兩份判準**已刪掉本檔這一份**，只留 repo-lint 那一份。留兩份的代價不是多跑一次，
是「哪一份才算數」沒有答案——而較弱的那份會先報綠。

本檔現在只做 **repo-lint 做不到的事**：它是純靜態的，看不到「這次跑到底執行了什麼」。
以下每一項的輸入都是**測試 log**，或需要一份可被 diff 覆核的 baseline 檔。

## 為什麼這支 gate 存在

Odoo 全套跑完印的是
`odoo/tests/result.py:198` → ``f'{failures_count} failed, {errors_count} error(s) of {testsRun} tests'``
—— **這一行對「測試根本沒被收集」與「測試被 skip」都是綠的**：

| 失效形態 | 在執行清單裡？ | 算進 `of N tests`？ | summary 看得出來？ | 誰在守 |
|---|---|---|---|---|
| 檔案沒被 `tests/__init__.py` 匯入（機制 A，#467） | ❌ | ❌ | ❌ 完全看不出來 | repo-lint |
| 基底拿不到 `test_tags`（機制 B，#544／#684） | ❌ | ❌ | ❌ | repo-lint |
| 整批 deselect（tag 沒匹配／module 沒裝） | ❌ | ❌ | ❌ | **C0／C3** |
| 測試 class／方法被靜默刪掉 | ❌ | ❌ | ❌ | **C2** |
| **skip** | ✅ | ✅ | ❌（`skipped` 不出現在 summary） | **C4** |
| 新增紅燈 | ✅ | ✅ | ✅（但只有數字）| **C5** |

## 設計原則：輸出「是哪一個」，不是「差幾個」

「差幾個」沒有可證偽性——拿到一個差額只能猜，任何聽起來合理的解釋都會被接受。
「是哪一個」拿到具體名字，兩秒就能核對。**所以本檔所有失敗輸出都列名。**

## 檢查

    C0 --check-sentinels 完整性哨兵：必然存在的那幾條沒出現 ⇒ 本次差集結論一律不成立
    C2 --check-baseline  靜態預測的預期 class 清單 vs baseline 檔（雙向差集）
    C3 --check-executed  baseline vs 測試 log 的實際執行清單（雙向差集）
    C4 --check-skips     測試 log 的 skip 盤點（列名＋理由，配白名單）
    C5 --check-failures  失敗／錯誤**集合**差（驗收標準：集合相同、不得新增）

    --upgrade-check      本 gate 何時該升級（狀態條件，由 gate 自己判定並提醒）
    --update-baseline    重新產生 baselines/test-collection.txt
    --self-test          把已知邊界樣本餵給本檔自己的 matcher（**預設動作**）

C2 的「預期」不是本檔自己算的：它呼叫 `repo-lint.py` 的 `_test_class_kind()` ／
`_test_effective_tags()` ／`_test_has_inherited_flag()`，也就是上面那張表裡的同一份
判準。本檔只負責把它落成一份**可被 diff 覆核的清單**，並拿去跟真實 log 對帳。

`--self-test` 不是裝飾。它抓過兩次本檔自己的缺陷，兩次都是「錯得少到看起來像資料
異常而不是工具異常」的形態：

- 舊版只 glob `*.py`，漏掉 `src/tests/integration/` 這個**子套件目錄** ——
  188 個名字裡只錯 1 個。（那段程式已隨 C1 一起刪除，判準交還 repo-lint。）
- `extract_executed()` 用**點數**判粒度，對子套件底下的測試（識別字多一段套件名）
  會把 class 行判成 method、method 行整個丟掉 ⇒ C3 永久假紅，而輸出與「那些 class
  真的沒跑」一模一樣。本樹目前不會引爆（`integration/` 三個 class 全帶 `-standard`）
  ⇒ **這是一個沒有任何現場症狀的缺陷，只有 self-test 的邊界樣本抓得到。**

任何改動本檔 matcher 的人請先跑它，並**先確認它對你要改的那一格是紅的**。

⚠ **不帶參數＝跑 `--self-test`**（不是印 help 然後 exit 2）。理由不是美觀：
`repo-lint.yml` 的「scripts/ 自我測試」步驟會 glob `scripts/audit/test_*.py` 並逐一
`python3 <檔>` 執行，本檔的檔名命中那個 glob。回非零就是把 repo-lint 整條弄紅，
而且症狀（「repo-lint 紅了」）跟真正的違規不可區分。
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HERE = pathlib.Path(__file__).resolve().parent
REPO_LINT = _HERE / "repo-lint.py"
BASELINE = _HERE / "baselines" / "test-collection.txt"
GREEN_STREAK = _HERE / "baselines" / "test-collection-green-streak.txt"
SENTINELS = _HERE / "baselines" / "test-collection-sentinels.txt"
KNOWN_FAILURES = _HERE / "baselines" / "test-collection-known-failures.txt"

# 升級條件之一：「連續 N 次 develop push 全套全綠」。N 取 3。
GREEN_STREAK_REQUIRED = 3


# ── 靜態判準：一律向 repo-lint 借，不在本檔重寫 ────────────────────────────
_repo_lint_cache: list = []   # repo-lint.py 有 6900+ 行，同一次執行不重複 exec


def repo_lint():
    """載入 `repo-lint.py`（檔名有連字號，不能直接 import）。

    🔴 這個函式存在的唯一理由是**不要有第二份判準**。本檔需要的「這個 class 跑不跑
    得到／跑幾條」全部出自 `repo-lint.py` 的 `test-collectability`；在這裡自己重寫一份
    ——哪怕只是「看起來一樣」——就會出現「哪一份才算數」沒有答案的狀態，而較弱的那一份
    會先報綠（#546 段 B 的實證：module 級判準對混合檔整檔放行，逐 class 版點名到 class）。
    """
    if _repo_lint_cache:
        return _repo_lint_cache[0]
    if not REPO_LINT.is_file():
        raise SystemExit("FATAL: 找不到 %s —— 靜態判準的單一事實來源不在，"
                         "不可用本檔自己的推測補上" % REPO_LINT)
    spec = importlib.util.spec_from_file_location("_repo_lint", REPO_LINT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_repo_lint"] = mod
    spec.loader.exec_module(mod)
    _repo_lint_cache.append(mod)
    return mod

# ── log matcher ───────────────────────────────────────────────────────────
# Odoo 18 `odoo/tests/result.py:263` log_stats() 在 stats logger 開到 DEBUG 時輸出
# "Detailed Tests Report"，每行形如：
#     \t<addon>.<模組路徑>.<Class>[.<method>]: 0.12s 34 queries
# 三種粒度是同一棵 `stats_tree` 的三層（`result.py:256-260`）：addon / addon+class /
# addon+class+method，全部混在同一批行裡。
#
# 🔴 **粒度不能用點數判**。`_TEST_ID`（`result.py:45`）的 `class` 群組是
# `(?P<class>.+)`——貪婪吃到最後一個點之前，所以子套件底下的測試那一段是
# `integration.test_x.TestY`（多一段套件名）。用「3 個點＝class 粒度」會把
# 子套件的 class 行判成 method 粒度、method 行整個丟掉 ⇒ C3 的 baseline 側有、
# 執行側沒有 ⇒ 永久假紅，而輸出與「那些 class 真的沒跑」一模一樣。
# 改用**樹的形狀**判：method 行是葉節點（不是任何其他 id 的前綴），class 行是
# 內部節點，addon 行是唯一沒有點的那個根。
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

# Odoo 18 `odoo/tests/result.py:300` logError() →
#     self.log(logging.ERROR, "%s: %s\n%s", flavour, self.getDescription(test), err, ...)
# flavour ∈ {FAIL, ERROR}，logger 名 = test.__module__。
# ⚠ 此樣式由源碼推導，尚未對真實紅燈觀察過——develop 目前有一條必然的 ERROR
#   （TestPRuleMigration，#543 的注入式驗證），第一次跑全套就會成為它的陽性對照。
FAILURE_LINE = re.compile(
    r"(?P<mod>odoo\.addons\.[\w.]+):\s+(?P<flavour>FAIL|ERROR):\s+(?P<test>[\w.]+)"
)

# logger 名（`test.__module__`）→ 與 detail report 同一個識別字空間的模組路徑。
# `odoo.addons.woow_paas_platform.tests.integration.test_x` → `integration.test_x`。
# ⚠ 不可用 `rsplit('.', 1)[-1]`：那對頂層檔剛好對，對子套件會把套件名吃掉，
#   於是 C0／C5 的識別字（`test_x.…`）與 C3 的（`integration.test_x.…`）指的是
#   同一條測試卻長得不一樣，差集永遠對不上。
_TESTS_PKG_SEP = ".tests."


def _module_ident(dotted: str) -> str:
    return dotted.split(_TESTS_PKG_SEP, 1)[-1]


# ── 靜態預測：這棵樹「應該」跑到哪些 class、各幾條 ─────────────────────────
class CollectionNotClean(Exception):
    """收集面有違規 ⇒ 「哪些 class 會跑」的預測不成立，不可先算一份再拿去對帳。"""


def _tests_module_path(rel: str, suite_dir: str) -> str:
    """`'src/tests/integration/test_x.py'` → `'integration.test_x'`。

    這是 **Odoo 側**的識別字空間：`odoo/tests/result.py:45` 的 `_TEST_ID` 把
    `odoo.addons.<addon>.tests.(?P<class>.+)\\.(?P<method>[^.]+)` 切開，中間那段就是
    「`src/tests/` 底下的模組路徑 ＋ class 名」。子套件底下的檔**帶著套件名**
    （`integration.test_x.TestY`），不是只有 stem —— 只取 stem 會讓子套件的 class
    在 C3 的差集裡永遠對不上，而那個輸出跟「那些 class 真的沒跑」一模一樣。
    """
    return rel[len(suite_dir) + 1:-len(".py")].replace("/", ".")


def _expected_methods(lint, facts, rel: str, name: str, node) -> set[str]:
    """靜態重演 `odoo/tests/loader.py` 篩子④／④' —— 這個 class 會產生哪些 test 實例。

    ④  `test_cases = test_case_class.__dict__.items()` —— **只收自己身上的**，
        繼承來的測試方法不會重跑（父類自己會跑那幾條）。
    ④' `if getattr(test_case_class, 'allow_inherited_tests_method', False):`
        才改走 `inspect.getmembers()`（整條 MRO）。
        🔴 走的是 `getattr` ⇒ **旗標可繼承**（本樹有 class 把它寫在 mixin 上），
        所以判「有沒有旗標」要沿繼承鏈找，不是只看 class 自己那一行。

    ⚠ MRO 只走**本樹解得出來的**基底：Odoo 自己的 `BaseCase` / `TransactionCase` /
      `HttpCase` 家族身上沒有任何 `def test*`（實查 odoo-18.0 的 `common.py`／
      `case.py`：只有 `test_xsd` 這個 module 級函式與 `testPartExecutor` 這個
      非 `test` 前綴的方法），所以樹外那段不會貢獻條數。
    """
    names = set(lint._test_own_test_methods(node))
    if lint._test_has_inherited_flag(facts, rel, name):
        for base_rel, base_name in lint._test_tree_bases(facts, rel, name):
            base_node = facts["classes"].get(base_rel, {}).get(base_name)
            if base_node is not None:
                names |= set(lint._test_own_test_methods(base_node))
    return names


def scan_expected(root: pathlib.Path) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """靜態預測「標準跑會執行哪些 class、各幾條」。

    判準**全部借自** `repo-lint.py`（理由見 `repo_lint()`），逐 class：

    1. `_test_class_kind() == tagged` —— 傳遞繼承到 `odoo.tests.common.BaseCase`，
       也就是 `tag_selector.py:69` 的 `hasattr(test, 'test_tags')` 為真。
    2. `'standard' in _test_effective_tags()` —— `--test-tags /woow_paas_platform`
       的空 tag 被 `tag_selector.py:41-44` 補成 `standard`；沒有它的 class 連**精確
       指名**都選不到（實測 `EXIT=0` ＋ `of 0 tests`，與「跑過而且全過」不可區分）。
    3. 條數＝`_expected_methods()`（loader 篩子④／④'）。

    🔴 **前提：收集面必須乾淨。** 沒被 `__init__.py` import 的檔對 loader 而言不存在，
    「它裡面的 class 會跑」這個預測就是假的。所以收集面有違規時**拒絕產生預測**
    （丟 `CollectionNotClean`），而不是先算一份不成立的清單再拿去跟 log 對帳——
    後者會把「收集壞了」偽裝成「有幾個 class 沒跑」，指錯修法。

    回 (`{<模組路徑>.<Class>: 條數}`, `[(被排除者, 原因)]`)；排除一律**列名**，不靜默丟棄。
    """
    lint = repo_lint()
    root_s = str(root)
    blockers = lint.check_test_module_collection(root_s)
    if blockers:
        raise CollectionNotClean(blockers)

    facts = lint._test_tree_facts(root_s)
    suite = lint.TEST_SUITE_DIR_REL
    expected: dict[str, int] = {}
    excluded: list[tuple[str, str]] = []

    why = {
        lint._TEST_KIND_UNTAGGED:
            "基底拿不到 `test_tags`（傳遞繼承不到 odoo.tests.common.BaseCase）"
            " ⇒ 不會執行。這是 repo-lint 的 test-collectability 在守的那一格",
        lint._TEST_KIND_UNKNOWN:
            "基底解析不到任何已知家族 ⇒ 無法判定跑不跑得到（repo-lint 會點名要求"
            "補進名單，這裡不猜、也不放進 baseline）",
        lint._TEST_KIND_OTHER:
            "不是 TestCase 子類（mixin／fixture）⇒ 它的 `def test*` 由子類經 "
            "`allow_inherited_tests_method` 展開後**計進子類**，不自成一項",
    }

    for rel in sorted(facts["classes"]):
        if not rel.startswith(suite + "/") or rel.endswith("/__init__.py"):
            continue
        mod = _tests_module_path(rel, suite)
        for name, node in sorted(facts["classes"][rel].items()):
            ident = "%s.%s" % (mod, name)
            kind = lint._test_class_kind(facts, rel, name)
            if kind != lint._TEST_KIND_TAGGED:
                if lint._test_own_test_methods(node):
                    excluded.append((ident, why[kind]))
                continue
            tags = lint._test_effective_tags(facts, rel, name)
            if "standard" not in tags:
                excluded.append((ident, "有效 tag ＝ %s，不含 `standard` ⇒ 標準跑選不到"
                                        "（合法 opt-out，見 repo-lint 的 (b) 那一格）"
                                        % ("／".join(sorted(tags)) or "（空）")))
                continue
            n = len(_expected_methods(lint, facts, rel, name, node))
            if n:
                expected[ident] = n

    return expected, excluded


# ── baseline I/O ──────────────────────────────────────────────────────────
BASELINE_HEADER = """\
# 預期會被 Odoo 收集並執行的測試 class 清單
# 產生指令：scripts/audit/test_collection_audit.py --update-baseline
# ⚠ **不是** repo-lint.py --update-baseline —— 本檔不歸 repo-lint 的 baseline 機制管。
# 調整前必讀 issue #546（設計與判準）：
#   https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/546
#
# 格式：<預期執行的 test 方法數>\\t<src/tests 底下的模組路徑>.<Class>
#   子套件底下的檔帶著套件名（integration.test_x.TestY）——那是 Odoo 側
#   `odoo/tests/result.py:45` `_TEST_ID` 的識別字空間，不是檔名 stem。
#
# 判準**不在本檔**：class 選不選得到、條數怎麼算，全部呼叫 repo-lint.py 的
# test-collectability 實作（_test_class_kind / _test_effective_tags /
# _test_has_inherited_flag）。這裡只是把那份判準的結果落成可被 diff 覆核的清單。
#   選中：傳遞繼承到 odoo.tests.common.BaseCase，且有效 tag 含 standard
#   條數：loader 篩子④ cls.__dict__ 的 def test*；帶 allow_inherited_tests_method
#         （可繼承！）者改走整條 MRO 去重
#
# 這份清單目前是**靜態預測**，尚未與真實 CI 執行清單對帳過
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
    """從 Detailed Tests Report 抽實際執行清單。

    回傳的識別字**已剝掉 addon 名**：`<模組路徑>.<Class>`（class 粒度）或
    `<模組路徑>.<Class>.<method>`（method 粒度），與 `scan_expected()` 同一個空間。

    粒度用**樹的形狀**判，不是點數（理由見 `DETAIL_LINE` 的註解）：
      root   ＝ 沒有點的那個 id（addon 名）
      class  ＝ 有別的 id 以「自己＋`.`」開頭（＝內部節點）
      method ＝ 葉節點
    `stats_tree` 一定同時放了 class 與 method 兩層（`result.py:259-260`），所以每個
    class 至少有一個 method 子節點，這個判準沒有邊界洞。
    """
    ids = set()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = DETAIL_LINE.match(line)
        if m:
            ids.add(m.group("id"))
    roots = {i for i in ids if "." not in i}
    internal = {i for i in ids if any(o.startswith(i + ".") for o in ids)}
    want = {"module": roots,
            "class": internal - roots,
            "method": ids - internal - roots}[granularity]
    if granularity == "module":
        return set(want)
    return {i.split(".", 1)[1] for i in want}


def parse_skips(log_path: pathlib.Path) -> list[tuple[str, str, str]]:
    """回傳 [(模組路徑, `Class.method`, 理由)]。"""
    out = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SKIP_LINE.search(line)
        if m:
            out.append((_module_ident(m.group("mod")),
                        m.group("test"), m.group("reason")))
    return out


def parse_failures(log_path: pathlib.Path) -> set[str]:
    """回傳 {`<模組路徑>.<Class>.<method>`} 的失敗／錯誤集合。"""
    out: set[str] = set()
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = FAILURE_LINE.search(line)
        if m:
            out.add(f"{_module_ident(m.group('mod'))}.{m.group('test')}")
    return out


def _load_list(path: pathlib.Path) -> set[str]:
    if not path.is_file():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def check_sentinels(log_path: pathlib.Path) -> int:
    """★ 完整性哨兵：某些測試**必然**會出現在每一次完整的全套結果裡（紅綠皆可）。

    **哨兵沒出現 ⇒ 這次跑的範圍不完整** ⇒ 本次所有差集結論一律不成立，
    必須大聲失敗而不是報綠。這比自造的邊界樣本強：它不是造出來的樣本，
    是真實環境裡必然存在的一條，且會隨對應缺陷修好而消失（屆時要更新清單）。
    """
    sentinels = _load_list(SENTINELS)
    if not sentinels:
        print("C0 FATAL: 哨兵清單為空 —— 沒有哨兵就無法判斷收集是否完整", file=sys.stderr)
        return 2
    seen = extract_executed(log_path, "method") | parse_failures(log_path)
    missing = sorted(s for s in sentinels if s not in seen)
    if not missing:
        print(f"C0 PASS：{len(sentinels)} 個完整性哨兵全部出現 ⇒ 收集範圍完整")
        return 0
    print(f"\nC0 FATAL — 以下 {len(missing)} 個哨兵沒有出現在結果中："
          "\n        ⇒ **這次跑的範圍不完整**，本次所有差集結論一律不成立。",
          file=sys.stderr)
    for s in missing:
        print(f"  ✗ {s}", file=sys.stderr)
    print("        （若該缺陷已修好、測試已移除，請同步更新 "
          "baselines/test-collection-sentinels.txt）", file=sys.stderr)
    return 2


def check_failure_set(log_path: pathlib.Path) -> int:
    """C5：失敗／錯誤**集合**比對——取代「0F/0E」這種絕對值標準。

    絕對值標準本質上是「差幾個」：develop 基準一旦不是 0 就整個失效。
    集合差不受基準值影響，且直接回答「是哪一條」。
    驗收語意＝**集合必須相同；本次不得新增任何一條**。
    """
    actual = parse_failures(log_path)
    expected = _load_list(KNOWN_FAILURES)
    if expected and not actual:
        print(f"\nC5 FATAL — 預期有 {len(expected)} 條已知紅燈，本次卻解析到 0 條。",
              file=sys.stderr)
        print("        兩種可能且輸出一模一樣：(a) 缺陷被修好了（請刪行），"
              "(b) FAIL/ERROR 解析器壞了。零結果不當作通過。", file=sys.stderr)
        return 2
    new = sorted(actual - expected)
    fixed = sorted(expected - actual)
    if not new and not fixed:
        print(f"C5 PASS：失敗集合與 baseline 相同（{len(actual)} 條已知紅燈，零新增）")
        return 0
    rc = 0
    if new:
        rc = 1
        print(f"\nC5 FAIL — 本次**新增** {len(new)} 條失敗（驗收標準：不得新增任何一條）：")
        for x in new:
            print(f"  + {x}")
    if fixed:
        print(f"\nC5 註記：以下 {len(fixed)} 條已知紅燈本次沒紅，"
              "請從 test-collection-known-failures.txt 刪行：")
        for x in fixed:
            print(f"  ✓ {x}")
    return rc


# ── 檢查 ──────────────────────────────────────────────────────────────────
def check_upgrade_readiness() -> int:
    """本 gate 何時該升級成「真的擋得住」—— **由 gate 自己判定並提醒**。

    刻意**不用時間條件**（「跑一週再回來改」）：時間到了不保證問題解決，而且沒有任何
    機制會在一週後提醒任何人回來改 ⇒ 它會永遠停在現狀。改用兩個**可判定的狀態**，
    兩者都要滿足：

      1. `repo-lint.py` 的 `TEST_EXECUTION_BASELINE` 清空（樹上沒有任何「已知拿不到
         `test_tags`」的豁免條目）——這是 #684／#544 那條線的完成狀態。
         🔴 刻意讀 repo-lint 的那份、不在本檔另存一份：本檔已經把靜態判準整個交給
         repo-lint，升級條件當然也要看它，否則又是兩份會分岔的狀態。
      2. 連續 `GREEN_STREAK_REQUIRED` 次 develop push 全套全綠。

    滿足時以退出碼 3 ＋橫幅提醒：可以回 issue #546 重新裁決觸發策略（2026-08-03 拍板的
    B+C 終點是「全套綠」、**不 block merge**；要不要往前走一步是 Eugene 的裁量，不是
    本腳本的）。**那是唯一會讓這一步變紅的情況**——不是缺陷，是該做的事自己找上門。

    條件 2 的資料來源是 `baselines/test-collection-green-streak.txt`，由 `odoo-tests`
    workflow 每次全綠後 append 一行 `<run_id>\\t<UTC>`；失敗時清空。
    ⚠ **那個 append 目前是人工的**（CI 自動 commit 回 develop 會與本 workflow 的
    `push: develop` 觸發形成迴圈，需要 loop guard；那是會動到 develop 的自動寫入，
    未經 review 不自行加）。所以檔案不存在時**明確報「無法累計」而不是報 0**
    ——那兩者意思完全不同，報 0 會讀成「跑過但沒綠過」。
    """
    lint = repo_lint()
    exemptions = sorted(lint.TEST_EXECUTION_BASELINE)

    if GREEN_STREAK.is_file():
        streak = len([ln for ln in GREEN_STREAK.read_text(encoding="utf-8").splitlines()
                      if ln.strip() and not ln.startswith("#")])
        streak_note = f"{streak}/{GREEN_STREAK_REQUIRED}"
    else:
        streak = -1
        streak_note = "無法累計（streak 檔尚未建立，非 0）"

    print(f"\n升級條件：repo-lint 的 test-collectability 豁免條目剩 {len(exemptions)} 項；"
          f"develop push 連續全綠 {streak_note}")
    for key in exemptions:
        print(f"  ~ {key}")
    if exemptions or streak < GREEN_STREAK_REQUIRED:
        return 0

    print("=" * 72)
    print("★ 兩個升級條件都已滿足：")
    print("    1. repo-lint 的 TEST_EXECUTION_BASELINE 已清空（沒有任何 class 拿不到 test_tags）")
    print(f"    2. develop push 全套連續 {streak} 次全綠")
    print("  ⇒ 可以回 issue #546 重新裁決觸發策略：2026-08-03 拍板的 B+C 終點停在")
    print("    「全套綠」、刻意不 block merge；上面兩件事是當時列出的前置條件。")
    print("  （本步驟以非零退出碼提醒——這是唯一會讓它變紅的情況，")
    print("    不是缺陷，是「該做的事」自己找上門。要不要往前走是 Eugene 的裁量。）")
    print("=" * 72)
    return 3


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


def _report_collection_not_clean(exc: CollectionNotClean, title: str) -> int:
    print(f"\n{title} FATAL — 收集面有違規，「哪些 class 會跑」的預測不成立："
          "\n        先修好收集面再回來（repo-lint 的 test-collectability 是同一組判準，"
          "\n        它會給出逐項修法）。這裡刻意不先算一份不成立的清單再拿去對帳——"
          "\n        那會把「收集壞了」偽裝成「有幾個 class 沒跑」，指錯修法。",
          file=sys.stderr)
    for v in exc.args[0]:
        print(f"  ✗ {v.split('（Refs')[0].split('（#')[0].strip()}", file=sys.stderr)
    return 2


def check_baseline(root: pathlib.Path, baseline: pathlib.Path) -> int:
    try:
        expected, excluded = scan_expected(root)
    except CollectionNotClean as exc:
        return _report_collection_not_clean(exc, "C2")
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
    # 子套件：logger 名多一段套件名，識別字必須跟著帶（不可 rsplit 成 stem）
    ("... INFO test odoo.addons.woow_paas_platform.tests.integration.test_i: "
     "skipped TestI.test_w : needs real cluster",
     ("integration.test_i", "TestI.test_w", "needs real cluster")),
]

SELF_TEST_DETAIL_LINES = [
    "\twoow_paas_platform: 259.47s 282962 queries",
    "\twoow_paas_platform.test_acl_p9.TestAclP9: 1.20s 34 queries",
    "\twoow_paas_platform.test_acl_p9.TestAclP9.test_one: 1.20s 34 queries",
    "\twoow_paas_platform.test_removal_477._AddonRemovalCase: 0.10s 2 queries",
    "\twoow_paas_platform.test_removal_477._AddonRemovalCase.test_x: 0.10s 2 queries",
    # 🔴 子套件：class 行 4 段、method 行 5 段。舊版用點數判粒度會把這兩行分別
    #    誤判成 method／丟棄 ⇒ 這一格就是那個假紅的陽性對照。
    "\twoow_paas_platform.integration.test_i.TestI: 0.30s 5 queries",
    "\twoow_paas_platform.integration.test_i.TestI.test_w: 0.30s 5 queries",
    "2026-08-02 22:15:42,894 260 INFO test odoo.tests.result: 0 failed, 0 error(s) of 2237 tests",
    "\tDetailed Tests Report:",
]

#: `scan_expected()` 的樣本樹。**混合檔是本組樣本的核心**：#684 留言 8233 §5.1 的
#: `test_reconciler_diff.py` 就是這個形狀，而 module 級判準對它整檔放行。
SELF_TEST_TREE = {
    "src/__manifest__.py": "{'name': 'probe'}\n",
    "src/tests/__init__.py": (
        "from . import test_mix\n"
        "from . import test_inherit\n"
        "from . import test_deselected\n"
        "from .sub import test_in_sub\n"
    ),
    # 混合檔：同檔一個會跑、一個不會跑
    "src/tests/test_mix.py": (
        "from odoo.tests.common import TransactionCase\n"
        "import unittest\n"
        "class TestOdooSide(TransactionCase):\n"
        "    def test_a(self): pass\n"
        "    def test_b(self): pass\n"
        "class TestPureUnittest(unittest.TestCase):\n"
        "    def test_c(self): pass\n"
    ),
    # 旗標寫在 mixin 上、由子類繼承（loader 篩子④'，走 getattr ⇒ 可繼承）
    "src/tests/test_inherit.py": (
        "from odoo.tests.common import TransactionCase\n"
        "class _Fixture:\n"
        "    allow_inherited_tests_method = True\n"
        "    def test_from_mixin(self): pass\n"
        "class TestWithFlag(_Fixture, TransactionCase):\n"
        "    def test_own(self): pass\n"
        "class _PlainFixture:\n"
        "    def test_not_collected(self): pass\n"
        "class TestNoFlag(_PlainFixture, TransactionCase):\n"
        "    def test_own(self): pass\n"
    ),
    "src/tests/test_deselected.py": (
        "from odoo.tests.common import TransactionCase\n"
        "from odoo.tests import tagged\n"
        "@tagged('-standard', 'post_install', 'probe_only')\n"
        "class TestDeselected(TransactionCase):\n"
        "    def test_d(self): pass\n"
    ),
    "src/tests/sub/__init__.py": "from . import test_in_sub\n",
    "src/tests/sub/test_in_sub.py": (
        "from odoo.tests.common import TransactionCase\n"
        "class TestInSub(TransactionCase):\n"
        "    def test_e(self): pass\n"
    ),
}


def _write_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def self_test() -> int:
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    # (1) SKIP_LINE：理由含冒號 / 底線開頭 class / 行尾空白 / 子套件；非 skip 行不得誤抓
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

    # (2) DETAIL_LINE 粒度切分 —— **子套件是陽性對照**（點數判法在這裡會錯）
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "log.txt"
        p.write_text("\n".join(SELF_TEST_DETAIL_LINES), encoding="utf-8")
        want_cls = {"test_acl_p9.TestAclP9", "test_removal_477._AddonRemovalCase",
                    "integration.test_i.TestI"}
        want_mth = {"test_acl_p9.TestAclP9.test_one",
                    "test_removal_477._AddonRemovalCase.test_x",
                    "integration.test_i.TestI.test_w"}
        got_cls, got_mth = extract_executed(p, "class"), extract_executed(p, "method")
        if got_cls != want_cls:
            failures.append(f"class 粒度切分錯：漏={sorted(want_cls-got_cls)} "
                            f"誤={sorted(got_cls-want_cls)}")
        if got_mth != want_mth:
            failures.append(f"method 粒度切分錯：漏={sorted(want_mth-got_mth)} "
                            f"誤={sorted(got_mth-want_mth)}")
        if extract_executed(p, "module") != {"woow_paas_platform"}:
            failures.append(f"module 粒度切分錯：{extract_executed(p, 'module')}")

    # (3) scan_expected：判準借自 repo-lint，這裡驗的是「借對了」
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _write_tree(root, SELF_TEST_TREE)
        exp, exc = scan_expected(root)
        exc_map = dict(exc)

        # 🔴 混合檔：同檔的兩個 class 結論必須**不同**。這正是被刪掉的 module 級判準
        #    看不到的那一格（#684 8233 §5.1 的 test_reconciler_diff.py）。
        if exp.get("test_mix.TestOdooSide") != 2:
            failures.append(f"混合檔裡會跑的 class 沒被算進預期：{exp}")
        if "test_mix.TestPureUnittest" in exp:
            failures.append("🔴 混合檔裡拿不到 test_tags 的 class 被算進預期了"
                            "——這就是 module 級判準的那個假綠")
        if "test_mix.TestPureUnittest" not in exc_map:
            failures.append("混合檔那一格被靜默丟棄而非列名")

        # 篩子④'：旗標可繼承 ⇒ mixin 的方法算進子類；沒旗標的不算
        if exp.get("test_inherit.TestWithFlag") != 2:
            failures.append(f"allow_inherited_tests_method（寫在 mixin 上）沒被展開："
                            f"{exp.get('test_inherit.TestWithFlag')} != 2")
        if exp.get("test_inherit.TestNoFlag") != 1:
            failures.append(f"沒有旗標的 class 把繼承來的方法也算進去了："
                            f"{exp.get('test_inherit.TestNoFlag')} != 1")

        # -standard：合法 opt-out，排除且列名
        if "test_deselected.TestDeselected" in exp:
            failures.append("`-standard` 的 class 沒被排除")
        if "test_deselected.TestDeselected" not in exc_map:
            failures.append("`-standard` 被靜默排除而非列名")

        # 子套件：識別字要帶套件名（與 detail report 的 `_TEST_ID` 同一個空間）
        if "sub.test_in_sub.TestInSub" not in exp:
            failures.append(f"子套件的識別字沒帶套件名：{sorted(exp)}")

    # (3b) 收集面不乾淨 ⇒ 拒絕產生預測，不是先算一份不成立的
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        tree = dict(SELF_TEST_TREE)
        tree["src/tests/test_zz_orphan.py"] = (
            "from odoo.tests.common import TransactionCase\n"
            "class TestOrphan(TransactionCase):\n"
            "    def test_a(self): pass\n"
        )
        _write_tree(root, tree)
        try:
            scan_expected(root)
            failures.append("收集面有違規（有檔沒被 import）卻照樣產出預測清單")
        except CollectionNotClean:
            pass

    # (5c) FAILURE_LINE：FAIL/ERROR 兩種 flavour；skip／summary／detail 行不得誤抓
    with tempfile.TemporaryDirectory() as td:
        p_ = pathlib.Path(td) / "log.txt"
        p_.write_text(
            "2026-08-03 10:00:00,000 1 ERROR test odoo.addons.woow_paas_platform.tests."
            "test_security_access_p_rule_migration: ERROR: TestPRuleMigration."
            "test_cursor_bypass_flag_cleared_after_migrate\n"
            "2026-08-03 10:00:01,000 1 ERROR test odoo.addons.woow_paas_platform.tests."
            "test_a: FAIL: TestA.test_b\n"
            # 陰性對照：以下三種都不得被當成失敗
            "... INFO test odoo.addons.woow_paas_platform.tests.test_c: "
            "skipped TestC.test_d : some reason\n"
            "... INFO test odoo.tests.result: 0 failed, 0 error(s) of 2237 tests\n"
            "\twoow_paas_platform.test_e.TestE.test_f: 0.01s 3 queries\n",
            encoding="utf-8")
        got_f = parse_failures(p_)
        want_f = {"test_security_access_p_rule_migration.TestPRuleMigration."
                  "test_cursor_bypass_flag_cleared_after_migrate",
                  "test_a.TestA.test_b"}
        if got_f != want_f:
            failures.append(f"FAILURE_LINE：漏={sorted(want_f-got_f)} 誤抓={sorted(got_f-want_f)}")

    # (5d) 哨兵：出現→PASS；不出現→FATAL(2)。這是「本次收集是否完整」的判準。
    with tempfile.TemporaryDirectory() as td:
        import io, contextlib
        good = pathlib.Path(td) / "good.log"
        good.write_text("\twoow_paas_platform.test_s.TestS.test_ok: 0.01s 1 queries\n"
                        "... ERROR test odoo.addons.woow_paas_platform.tests.test_sent: "
                        "ERROR: TestSent.test_sentinel\n", encoding="utf-8")
        bad = pathlib.Path(td) / "bad.log"
        bad.write_text("\twoow_paas_platform.test_s.TestS.test_ok: 0.01s 1 queries\n",
                       encoding="utf-8")
        sent = pathlib.Path(td) / "sent.txt"
        sent.write_text("test_sent.TestSent.test_sentinel\n", encoding="utf-8")
        real_sent = globals()["SENTINELS"]
        try:
            globals()["SENTINELS"] = sent
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc_good = check_sentinels(good)
                rc_bad = check_sentinels(bad)
            if rc_good != 0:
                failures.append("哨兵出現在結果中卻沒 PASS")
            if rc_bad != 2:
                failures.append("哨兵消失時沒有 FATAL —— 收集不完整會被當成通過")
            if "test_sent.TestSent.test_sentinel" not in buf.getvalue():
                failures.append("哨兵失敗訊息沒有列名")
        finally:
            globals()["SENTINELS"] = real_sent

    # (5e) C5 失敗集合：相同→0；新增→1；預期非空但解析到 0→FATAL(2) 不可當通過
    with tempfile.TemporaryDirectory() as td:
        import io, contextlib
        kf = pathlib.Path(td) / "kf.txt"
        kf.write_text("test_a.TestA.test_b\n", encoding="utf-8")
        same = pathlib.Path(td) / "same.log"
        same.write_text("... ERROR test odoo.addons.x.tests.test_a: FAIL: TestA.test_b\n")
        more = pathlib.Path(td) / "more.log"
        more.write_text("... ERROR test odoo.addons.x.tests.test_a: FAIL: TestA.test_b\n"
                        "... ERROR test odoo.addons.x.tests.test_c: ERROR: TestC.test_d\n")
        none = pathlib.Path(td) / "none.log"
        none.write_text("... INFO test odoo.tests.result: 0 failed, 0 error(s) of 5 tests\n")
        real_kf = globals()["KNOWN_FAILURES"]
        try:
            globals()["KNOWN_FAILURES"] = kf
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                r_same, r_more, r_none = (check_failure_set(same), check_failure_set(more),
                                          check_failure_set(none))
            if r_same != 0: failures.append("C5 對相同集合沒 PASS")
            if r_more != 1: failures.append("C5 沒抓到新增的失敗")
            if "test_c.TestC.test_d" not in buf.getvalue():
                failures.append("C5 新增的失敗沒有列名")
            if r_none != 2:
                failures.append("C5 對「預期非空但解析到 0」當成通過了（零結果不可信）")
        finally:
            globals()["KNOWN_FAILURES"] = real_kf

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
    print("SELF-TEST PASS：SKIP_LINE 四形態＋陰性對照／粒度切分（含子套件，"
          "點數判法在那裡會錯）／scan_expected（混合檔逐 class、篩子④' 旗標可繼承、"
          "-standard 列名排除、子套件識別字）／收集面不乾淨時拒絕預測／FAILURE_LINE／"
          "完整性哨兵／C5 失敗集合差／雙向差集 —— 全部命中")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-baseline", action="store_true")
    ap.add_argument("--check-executed", metavar="ODOO_TEST_LOG")
    ap.add_argument("--check-skips", metavar="ODOO_TEST_LOG")
    ap.add_argument("--allow-skips", metavar="FILE")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--check-sentinels", metavar="ODOO_TEST_LOG")
    ap.add_argument("--check-failures", metavar="ODOO_TEST_LOG")
    ap.add_argument("--upgrade-check", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="跑所有不需要 log 的檢查（C2 ＋ 升級條件自檢）")
    args = ap.parse_args()
    actions = (args.check_baseline, args.check_executed, args.check_skips,
               args.update_baseline, args.check_sentinels, args.check_failures,
               args.upgrade_check, args.all)

    # 不帶任何動作＝ `--self-test`。理由見檔頭：repo-lint.yml 的「scripts/ 自我測試」
    # 會 glob `scripts/audit/test_*.py` 逐一裸執行，印 help 然後 exit 2 會把 repo-lint
    # 整條弄紅，而症狀跟真正的違規不可區分。
    if args.self_test or not any(actions):
        return self_test()
    if args.update_baseline:
        try:
            expected, excluded = scan_expected(REPO_ROOT)
        except CollectionNotClean as exc:
            return _report_collection_not_clean(exc, "--update-baseline")
        write_baseline(BASELINE, expected)
        total = sum(expected.values())
        print(f"已寫入 {BASELINE.relative_to(REPO_ROOT)}："
              f"{len(expected)} 個 class／{total} 條 test 方法")
        if excluded:
            print(f"排除 {len(excluded)} 個（列名）：")
            for ident, why in sorted(excluded):
                print(f"  ~ {ident}  —  {why}")
        return 0

    rc = 0
    if args.all or args.check_baseline:
        rc |= check_baseline(REPO_ROOT, BASELINE)
    if args.all or args.upgrade_check:
        rc |= check_upgrade_readiness()
    if args.check_sentinels:
        rc |= check_sentinels(pathlib.Path(args.check_sentinels))
    if args.check_failures:
        rc |= check_failure_set(pathlib.Path(args.check_failures))
    if args.check_executed:
        rc |= check_executed(BASELINE, pathlib.Path(args.check_executed))
    if args.check_skips:
        rc |= check_skips(pathlib.Path(args.check_skips),
                          pathlib.Path(args.allow_skips) if args.allow_skips else None)
    return rc


if __name__ == "__main__":
    sys.exit(main())
