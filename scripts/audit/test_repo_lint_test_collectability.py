#!/usr/bin/env python3
"""``test-collectability`` 兩條 check 的注入測試（#684＋#544）。

這條 gate 守的是「測試根本沒被執行，而輸出與『跑過而且全過』一模一樣」這個 bug
class。本 repo 已經實現過一次：``src/tests/test_reconcile_delete_gate.py`` 自己就是
一道 gate，而它因為基底錯而從未被 collect —— 沒有任何人發現。

**先注入紅燈，再相信綠燈**：一條新 gate 第一個要證明的不是「現在是綠的」，而是
「它會紅」，而且要紅在**該紅的那條判準**上。但只驗「該紅的有紅」不夠 —— 那不會發現
你把**該綠的也弄紅了**，而 gate 一上線就帶著假陽性，最自然的處置是把它塞進 baseline，
於是 repo 裡種下一筆永久的假宣稱（名單說「這些不會被跑」，實際上它們每次都在跑），
殭屍檢查還永遠叫不出來 —— 因為它「確實」符合那個寫錯的判準。所以陰性對照與注入紅燈
一樣重要，見 ``TestMixinIsNotAViolation``。

## 為什麼錨點是 ``BaseCase`` 而不是 ``odoo.tests.case.TestCase``

實查 Odoo 18（``/…/odoo-18.0``，``release.py`` ``version_info = (18, 0, 0, FINAL,
0, '')``）：

* ``odoo/tests/loader.py:8`` 是 ``from unittest import case`` ⇒ 篩子②
  （``loader.py:24`` ``issubclass(obj, case.TestCase)``）比的是 **unittest**。
  純 unittest 子類**通過**②③、會被 yield 成 test 實例。
* 真正丟掉它們的是 ``odoo/tests/tag_selector.py:69`` 的
  ``if not hasattr(test, 'test_tags')`` —— 該行原始註解自己寫著「does not inherit
  from BaseCase and has no test_tags」。
* ``test_tags`` 由 ``odoo/tests/common.py`` 的 ``MetaCase`` 賦予，而
  ``common.py:290`` 是 ``class BaseCase(case.TestCase, metaclass=MetaCase)``；
  ``common.py:71`` 的 ``from . import case`` 與 ``loader.py:8`` 的 ``case``
  **同名不同源**。

⇒ 「必須是 ``odoo.tests.case.TestCase`` 的傳遞子類」是**較寬**的判準、有盲區：直接
繼承它而沒經過 ``BaseCase`` 的 class 沒有 ``MetaCase``、拿不到 ``test_tags``、照樣
不會跑，卻會通過那個判準。``TestOdooCaseTestCaseIsNotEnough`` 就是釘住這一格 ——
🔴 **它是本檔唯一能分辨兩個錨點的測試**：本樹今天沒有任何 class 用那種寫法，所以拿
真實樹去驗，兩個錨點的輸出完全相同。

跑法：
    python3 scripts/audit/test_repo_lint_test_collectability.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_test_collectability.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo、git 執行檔，也不依賴 cwd。
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")
_spec = importlib.util.spec_from_file_location("repo_lint_collectability_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


# --------------------------------------------------------------------------- #
#  合成 fixture                                                                 #
# --------------------------------------------------------------------------- #

OK_CASE = '''
from odoo.tests.common import TransactionCase


class TestWidgetOk(TransactionCase):
    def test_widget_runs(self):
        self.assertTrue(True)
'''

# 注入：基底只到 unittest。刻意用 `from unittest import TestCase` —— 這個檔裡
# **沒有** `unittest.TestCase` 這個字串，字串 grep 型判準會整個放行。
PLAIN_UNITTEST = '''
from unittest import TestCase


class TestWidgetNeverRuns(TestCase):
    def test_a(self):
        self.assertTrue(True)

    def test_b(self):
        self.assertTrue(True)
'''

# 混合檔：同檔既有會跑的、也有不會跑的。module 粒度判準會整檔放行。
MIXED = '''
import unittest

from odoo.tests.common import TransactionCase


class TestMixedRuns(TransactionCase):
    def test_runs(self):
        self.assertTrue(True)


class TestMixedNeverRuns(unittest.TestCase):
    def test_never(self):
        self.assertTrue(True)
'''

# 🔴 盲區：直接繼承 odoo.tests.case.TestCase（沒經過 BaseCase ⇒ 沒有 MetaCase）。
ODOO_CASE_DIRECT = '''
from odoo.tests.case import TestCase


class TestNoMetaCase(TestCase):
    def test_never(self):
        self.assertTrue(True)
'''

# 同一個盲區的另一種載體：`from odoo.tests import case` 再走 `case.TestCase`。
# 解析時若把 dotted 的尾巴丟掉，模組會停在 `odoo.tests` 而被判成有血統。
ODOO_CASE_VIA_PACKAGE = '''
from odoo.tests import case


class TestNoMetaCaseViaPkg(case.TestCase):
    def test_never(self):
        self.assertTrue(True)
'''

# 🔴 陰性對照：帶 def test* 的 mixin ＋ 有開旗標的血統子類 ⇒ 必須放行。
#    mixin 自己到不了 BaseCase（跟「基底錯」的 class 一樣），但它的方法**每次都在跑**。
MIXIN_WITH_FLAG = '''
from odoo.tests.common import TransactionCase


class _WidgetAssertions:
    """底線開頭、bases 為空 —— 正是最容易被誤判成違規的形狀。"""

    allow_inherited_tests_method = True

    def test_shared_invariant(self):
        self.assertTrue(True)


class TestWidgetA(_WidgetAssertions, TransactionCase):
    pass


class TestWidgetB(_WidgetAssertions, TransactionCase):
    pass
'''

# 旗標寫在子類身上（也合法：getattr 只問拿不拿得到）。
MIXIN_FLAG_ON_SUBCLASS = MIXIN_WITH_FLAG.replace(
    "    allow_inherited_tests_method = True\n\n", ""
).replace(
    "class TestWidgetA(_WidgetAssertions, TransactionCase):\n    pass",
    "class TestWidgetA(_WidgetAssertions, TransactionCase):\n"
    "    allow_inherited_tests_method = True",
).replace(
    "class TestWidgetB(_WidgetAssertions, TransactionCase):\n    pass",
    "class TestWidgetB(_WidgetAssertions, TransactionCase):\n"
    "    allow_inherited_tests_method = True",
)

# 注入：旗標整個不見 ⇒ mixin 的方法一條都不會跑。
MIXIN_NO_FLAG = MIXIN_WITH_FLAG.replace(
    "    allow_inherited_tests_method = True\n\n", "")

# 注入：mixin 帶 test 方法卻連一個血統子類都沒有。
MIXIN_ORPHAN = '''
class _OrphanAssertions:
    def test_nobody_runs_me(self):
        self.assertTrue(True)
'''

STANDARD_OPT_OUT_OK = '''
from odoo.tests.common import TransactionCase, tagged


@tagged('-standard', '-at_install', 'post_install', 'widget_real')
class TestRealWidget(TransactionCase):
    def test_real(self):
        self.assertTrue(True)
'''

# 注入：-standard 但沒有任何非保留字 tag ⇒ 連精確指名都選不到，exit 0 + of 0 tests。
STANDARD_OPT_OUT_UNREACHABLE = '''
from odoo.tests.common import TransactionCase, tagged


@tagged('-standard', '-at_install', 'post_install')
class TestUnreachableWidget(TransactionCase):
    def test_real(self):
        self.assertTrue(True)
'''


# 🔴 第 3 態：基底名完全沒見過。gate 必須**點名**（無法判定），不是紅也不是綠。
UNKNOWN_BASE = '''
from some_vendor.testing import SomeUnknownBase


class TestOnUnknownBase(SomeUnknownBase):
    def test_something(self):
        self.assertTrue(True)
'''

# 認不得的基底、但不長得像測試 class ⇒ 不點名（否則乾淨的樹會被噪音弄紅）。
UNKNOWN_BASE_HELPER = '''
import requests


class _CapturingAdapter(requests.adapters.HTTPAdapter):
    def send(self, request, **kwargs):
        return None
'''

# (ii) 注入：@tagged 掛在拿不到 MetaCase 的 class 上 ⇒ make_suite 拋 AttributeError。
TAGGED_ON_PLAIN_UNITTEST = '''
import unittest

from odoo.tests.common import tagged


@tagged('-at_install', 'post_install', 'widget_real')
class TestExplodesAtLoad(unittest.TestCase):
    def test_boom(self):
        self.assertTrue(True)
'''

# 同一格的另一個載體：手寫 test_tags（沒有 @tagged 也一樣爆）。
HANDWRITTEN_TAGS_ON_PLAIN_UNITTEST = '''
import unittest


class TestAlsoExplodes(unittest.TestCase):
    test_tags = {'standard', 'at_install'}

    def test_boom(self):
        self.assertTrue(True)
'''


class _FixtureRoot:
    """一棵最小的合成 addon 樹。`files` 是 {rel: 內容}，`init` 是 tests/__init__.py。"""

    def __init__(self, files, init_lines, extra_init=None):
        self.dir = tempfile.mkdtemp(prefix="test-collectability-gate-")
        self.write("src/__manifest__.py", "{'name': 'ut'}\n")
        self.write("src/tests/__init__.py",
                   "".join("from . import %s\n" % m for m in init_lines)
                   + (extra_init or ""))
        for rel, text in files.items():
            self.write(rel, text)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def collection(self):
        return rl.check_test_module_collection(self.dir)

    def execution(self):
        return rl.check_test_class_execution(self.dir)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


_DEFAULT = object()


@contextlib.contextmanager
def tree(files=_DEFAULT, init=None, extra_init=None):
    # 🔴 用哨兵而不是 `files or {...}`：`{}` 是 falsy，會被預設值悄悄換掉，於是
    #    「完全沒有 test 檔」那格測到的其實是另一個對照。
    files = dict({"src/tests/test_ok.py": OK_CASE} if files is _DEFAULT else files)
    if init is None:
        init = [os.path.basename(r)[:-3] for r in files
                if r.startswith("src/tests/") and "/integration/" not in r
                and os.path.basename(r).startswith("test_")]
    fx = _FixtureRoot(files, init, extra_init)
    try:
        yield fx
    finally:
        fx.close()


@contextlib.contextmanager
def patched_baseline(baseline):
    original = rl.TEST_EXECUTION_BASELINE
    rl.TEST_EXECUTION_BASELINE = baseline
    try:
        yield
    finally:
        rl.TEST_EXECUTION_BASELINE = original


def joined(violations):
    return "\n".join(violations)


# ------------------------------------------------------- 陽性對照（綠的前提）

class TestBaselineIsGreen(unittest.TestCase):
    """合法 repo root 與合成 addon layout 必須是綠的。一條恆紅的 gate 通不過這裡。"""

    def test_valid_addon_layout_is_green(self):
        """repo layout：addon source tree 位於 `<root>/src/`。"""
        with patched_baseline({}), tree() as fx:
            self.assertEqual(fx.collection(), [])
            self.assertEqual(fx.execution(), [])

    def test_valid_repo_root_is_green(self):
        """真 repo root（含既有 baseline）必須是綠的——否則 `--all` 永遠紅。"""
        root = str(pathlib.Path(__file__).resolve().parents[2])
        self.assertEqual(rl.check_test_module_collection(root), [])
        self.assertEqual(rl.check_test_class_execution(root), [])


# --------------------------------------------------- 性質 1：收集面的注入紅燈

class TestUnimportedModuleIsBlocked(unittest.TestCase):

    def test_test_file_not_imported_by_init(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_orphan.py": OK_CASE},
                init=["test_ok"]) as fx:
            out = joined(fx.collection())
            self.assertIn("src/tests/test_orphan.py", out)
            self.assertIn("沒有被", out)
            # 紅在收集面，不是紅在執行面（那個檔的基底是對的）。
            self.assertEqual(fx.execution(), [])

    def test_ghost_import_is_blocked(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE},
                init=["test_ok", "test_deleted_last_week"]) as fx:
            out = joined(fx.collection())
            self.assertIn("test_deleted_last_week", out)
            self.assertIn("幽靈", out)

    def test_subpackage_not_reexported_at_top_level(self):
        """子套件自己 import 了，但 `src/tests/__init__.py` 沒有 re-export。

        `_get_tests_modules()` 只讀 `<addon>.tests` **自己**的屬性 ⇒ 照樣不會跑，
        而逐層看的判準是綠的。
        """
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/integration/__init__.py":
                           "from . import test_deep\n",
                       "src/tests/integration/test_deep.py": OK_CASE},
                init=["test_ok"]) as fx:
            out = joined(fx.collection())
            self.assertIn("src/tests/integration/test_deep.py", out)
            self.assertIn("re-export", out)

    def test_subpackage_reexported_at_top_level_is_green(self):
        """判準 B 不加 `module is None` 那格 —— 加了就會誤報這一格。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/integration/__init__.py":
                           "from . import test_deep\n",
                       "src/tests/integration/test_deep.py": OK_CASE},
                init=["test_ok"],
                extra_init="from .integration import test_deep\n") as fx:
            self.assertEqual(fx.collection(), [])


# --------------------------------------------------- 性質 2：執行面的注入紅燈

class TestUntaggedClassIsBlocked(unittest.TestCase):

    def test_plain_unittest_class(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_plain.py": PLAIN_UNITTEST}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestWidgetNeverRuns", out)
            # 訊息要指對層：TagsSelector，不是 loader 篩子②。
            self.assertIn("tag_selector", out)
            self.assertIn("BaseCase", out)
            # 收集面是綠的（這個檔有被 import）——紅在該紅的那條判準上。
            self.assertEqual(fx.collection(), [])

    def test_string_grep_would_have_missed_it(self):
        """判準寫「性質」不寫「名字」的可執行證據。"""
        self.assertNotIn("unittest.TestCase", PLAIN_UNITTEST)
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_plain.py": PLAIN_UNITTEST}) as fx:
            self.assertTrue(fx.execution())

    def test_mixed_file_is_partially_red(self):
        """🔴 module 粒度會整檔放行的那一格。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_mixed.py": MIXED}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestMixedNeverRuns", out)
            self.assertNotIn("TestMixedRuns`", out)
            self.assertEqual(len(fx.execution()), 1)


class TestOdooCaseTestCaseIsNotEnough(unittest.TestCase):
    """🔴 錨點盲區：`odoo.tests.case.TestCase` 沒有 MetaCase ⇒ 沒有 test_tags。

    本檔唯一能分辨「BaseCase 錨點」與「odoo.tests.case.TestCase 錨點」的測試 ——
    真實樹上沒有任何 class 用這種寫法，拿真樹去驗兩個錨點輸出完全相同。
    """

    def test_direct_inheritance_is_blocked(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_nometa.py": ODOO_CASE_DIRECT}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestNoMetaCase", out)
            self.assertIn("BaseCase", out)

    def test_via_package_attribute_is_blocked(self):
        """`from odoo.tests import case` ＋ `case.TestCase`。

        解析時把 dotted 的尾巴丟掉，模組會停在 `odoo.tests` 而被判成有血統。
        """
        with patched_baseline({}), tree(
                files={"src/tests/test_nometa2.py": ODOO_CASE_VIA_PACKAGE}) as fx:
            self.assertIn("TestNoMetaCaseViaPkg", joined(fx.execution()))


# ------------------------------------- 🔴 陰性對照：mixin **不是**違規

class TestMixinIsNotAViolation(unittest.TestCase):
    """帶 `def test*` 的 mixin ＋ 有旗標的血統子類 ⇒ 必須放行。

    這一格與注入紅燈一樣重要：只驗「該紅的有紅」不會發現把該綠的也弄紅了，而 gate
    帶著假陽性上線時最自然的處置是塞 baseline —— 那是一筆**永久的假宣稱**（名單說
    「這些不會被跑」，實際上它們每次都在跑），殭屍檢查還永遠叫不出來，因為它「確實」
    符合那個寫錯的判準。
    """

    def test_mixin_with_flag_on_itself_is_green(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_mixin.py": MIXIN_WITH_FLAG}) as fx:
            self.assertEqual(fx.execution(), [])

    def test_mixin_with_flag_on_subclass_is_green(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_mixin.py": MIXIN_FLAG_ON_SUBCLASS}) as fx:
            self.assertEqual(fx.execution(), [])

    def test_underscore_mixin_never_lands_in_the_violation_set(self):
        """假陽性的可辨識指紋：底線開頭、bases 為空的 class 不得出現在違規裡。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_mixin.py": MIXIN_WITH_FLAG}) as fx:
            self.assertNotIn("_WidgetAssertions", joined(fx.execution()))

    def test_missing_flag_is_blocked(self):
        """反向：旗標不見時必須紅——否則上面三格的綠只是恆綠。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_mixin.py": MIXIN_NO_FLAG}) as fx:
            out = joined(fx.execution())
            self.assertIn("allow_inherited_tests_method", out)
            self.assertIn("TestWidgetA", out)

    def test_orphan_mixin_is_blocked(self):
        """連一個血統子類都沒有的 mixin：那些方法確實一條都不會跑。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_orphan_mixin.py": MIXIN_ORPHAN}) as fx:
            self.assertIn("_OrphanAssertions", joined(fx.execution()))


# --------------------------------------------------- `-standard` 的無聲假綠

class TestStandardOptOut(unittest.TestCase):

    def test_opt_out_with_selectable_tag_is_green(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_real.py": STANDARD_OPT_OUT_OK}) as fx:
            self.assertEqual(fx.execution(), [])

    def test_opt_out_without_any_selectable_tag_is_blocked(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_real.py":
                       STANDARD_OPT_OUT_UNREACHABLE}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestUnreachableWidget", out)
            self.assertIn("-standard", out)


# ------------------------------------------------------ baseline 與殭屍條目

class TestBaselineBehaviour(unittest.TestCase):

    def test_baseline_suppresses_a_known_violation(self):
        with tree(files={"src/tests/test_ok.py": OK_CASE,
                         "src/tests/test_plain.py": PLAIN_UNITTEST}) as fx:
            key = "src/tests/test_plain.py::TestWidgetNeverRuns"
            with patched_baseline({key: {"issue": "#544", "why": "ut"}}):
                self.assertEqual(fx.execution(), [])

    def test_zombie_entry_is_blocked(self):
        """修好了卻留在名單上 ⇒ 紅。這格讓本 PR 與 #806 的 merge 順序自動對帳。"""
        with tree() as fx:
            with patched_baseline({"src/tests/test_gone.py::TestGone":
                                   {"issue": "#544", "why": "ut"}}):
                out = joined(fx.execution())
                self.assertIn("TestGone", out)
                self.assertIn("殭屍", out)

    def test_entry_without_issue_or_why_is_blocked(self):
        with tree() as fx:
            with patched_baseline({"src/tests/test_x.py::TestX": {"why": "ut"}}):
                self.assertIn("issue", joined(fx.execution()))
            with patched_baseline({"src/tests/test_x.py::TestX": {"issue": "#1"}}):
                self.assertIn("why", joined(fx.execution()))

    def test_file_granular_key_is_blocked(self):
        """整檔豁免正是這條 gate 要修的形態，不能靠 baseline 偷渡回來。"""
        with tree() as fx:
            with patched_baseline({"src/tests/test_x.py":
                                   {"issue": "#1", "why": "ut"}}):
                self.assertIn("class 粒度", joined(fx.execution()))

    def test_the_real_baseline_holds_no_mixin(self):
        """底線開頭的 class 出現在 baseline ＝ 三分法寫錯了（假陽性被蓋掉）。"""
        bad = [k for k in rl.TEST_EXECUTION_BASELINE if "::_" in k]
        self.assertEqual(bad, [], "baseline 裡有 mixin 形狀的條目：%s" % bad)


# ------------------------------------------------------------ 探針自身的鑑別力

class TestProbeFailuresAreBlocked(unittest.TestCase):
    """兩條判準都是否定性的：掃描器壞掉時輸出與「全部都對」一模一樣。"""

    def test_no_test_files_at_all(self):
        with patched_baseline({}), tree(files={}, init=["placeholder"]) as fx:
            self.assertIn("掃不到任何", joined(fx.collection()))

    def test_no_level1_importfrom_at_all(self):
        """`__init__.py` 抽不到任何 level-1 ImportFrom ＝ 判準 B 的採集器壞了。

        涵蓋判準是「test_*.py 的集合 − 這個集合」，探針回空集合時它會全部報成違規
        或全部放行，兩種都不是真話。
        """
        with patched_baseline({}), tree(init=[]) as fx:
            self.assertIn("抽不到任何", joined(fx.collection()))

    def test_no_tests_package(self):
        d = tempfile.mkdtemp(prefix="test-collectability-nopkg-")
        try:
            os.makedirs(os.path.join(d, "src"))
            with open(os.path.join(d, "src/__manifest__.py"), "w") as f:
                f.write("{'name': 'ut'}\n")
            with patched_baseline({}):
                self.assertIn("掃不到任何",
                              joined(rl.check_test_module_collection(d)))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_basecase_class_anywhere(self):
        """一個 BaseCase 血統的 class 都掃不到 ＝ 繼承鏈追蹤壞了，不是乾淨。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_only_helper.py":
                       "class _Helper:\n    pass\n"},
                init=["test_only_helper"]) as fx:
            self.assertIn("找不到**任何**", joined(fx.execution()))

    def test_parse_error_is_a_violation(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_broken.py": "class ((:\n"}) as fx:
            self.assertIn("parse 失敗", joined(fx.collection()))
            self.assertIn("parse 失敗", joined(fx.execution()))

    def test_wrong_root_fails_loud(self):
        """缺 manifest 時兩條判準各自回一筆可歸因的 violation。"""
        d = tempfile.mkdtemp(prefix="test-collectability-noroot-")
        try:
            collection = rl.check_test_module_collection(d)
            execution = rl.check_test_class_execution(d)
            self.assertEqual(len(collection), 1)
            self.assertEqual(len(execution), 1)
            for out in (collection[0], execution[0]):
                self.assertIn("root/layout 前置條件不成立", out)
                self.assertIn("src/__manifest__.py", out)
                self.assertIn("不能解讀為零違規", out)
                self.assertNotIn("掃不到任何", out)
            self.assertIn("check_test_module_collection", collection[0])
            self.assertIn("check_test_class_execution", execution[0])
            self.assertNotEqual(collection[0], execution[0])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_run_all_keeps_both_attributed_root_violations(self):
        """aggregation 不去重：兩條 check 保留各自 wrong-root 訊息並 exit 1。"""
        target_checks = {
            "check_test_module_collection",
            "check_test_class_execution",
        }
        check_names = {
            name
            for names in rl.REPO_LINT_RULES.values()
            for name in names
        }
        with tempfile.TemporaryDirectory(
                prefix="test-collectability-run-all-") as d:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    rl, "repo_lint_registry_floor", return_value=[]))
                for name in sorted(check_names - target_checks):
                    stack.enter_context(mock.patch.object(
                        rl, name, return_value=[]))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = rl.run_all(d)

        text = output.getvalue()
        self.assertEqual(exit_code, 1, text)
        self.assertIn("repo-lint：2 個違規", text)
        self.assertEqual(text.count("check_test_module_collection"), 1)
        self.assertEqual(text.count("check_test_class_execution"), 1)


# ------------------------------- 🔴 第 3 態：認不得的基底要點名，不猜

class TestUnknownBaseIsNamedNotGuessed(unittest.TestCase):
    """名單漏一個實際在用的框架基底 ⇒ 合法 class 被判違規 ⇒ 假紅進 baseline 變永久
    假宣稱。死條目只是永遠不命中，漏條目才是風險 ⇒ 認不得就點名。"""

    def test_unknown_base_is_reported_as_undecidable(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_unknown.py": UNKNOWN_BASE}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestOnUnknownBase", out)
            self.assertIn("SomeUnknownBase", out)
            self.assertIn("無法判定", out)
            # 🔴 不得被講成「這個 class 不會跑」——那是另一類違規，動作也不同。
            self.assertNotIn("從來沒有執行過", out)
            self.assertNotIn("載入爆掉", out)

    def test_unknown_base_must_not_be_shelved_in_baseline(self):
        """把無法判定塞 baseline 不會讓它變綠——baseline 只認 UNTAGGED 那一類。"""
        key = "src/tests/test_unknown.py::TestOnUnknownBase"
        with tree(files={"src/tests/test_unknown.py": UNKNOWN_BASE}) as fx:
            with patched_baseline({key: {"issue": "#1", "why": "ut"}}):
                out = joined(fx.execution())
                self.assertIn("無法判定", out)
                # 而且那筆 baseline 會被殭屍檢查點名——兩個方向都推向「不要這樣做」。
                self.assertIn("殭屍", out)

    def test_unknown_base_on_a_plain_helper_is_quiet(self):
        """認不得但不長得像測試 class ⇒ 不點名，否則乾淨的樹會被噪音弄紅。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_ok.py": OK_CASE,
                       "src/tests/test_helper.py": UNKNOWN_BASE_HELPER}) as fx:
            self.assertEqual(fx.execution(), [])


# ------------------------- 🔴 (ii) @tagged 掛在非 BaseCase 上 ＝ 載入爆炸

class TestTaggedOnNonBaseCaseExplodes(unittest.TestCase):
    """本樹目前 0 違規 ⇒ 這格**上線即休眠**，注入紅燈是它唯一能證明自己活著的方式。

    機制（實查 Odoo 18）：`tagged()`（common.py:2412-2418）只設 `test_tags`，
    `test_module` / `test_class` / `test_sequence` 是 `MetaCase`（common.py:260-265）
    設的 ⇒ `tag_selector.py:69` 的 `hasattr` 會**通過**，然後 `:73` 讀
    `test.test_module` 拋 AttributeError；`loader.py:108` 的
    `sorted(..., key=lambda t: t.test_sequence)` 同樣會炸，而 `make_suite()`
    沒有任何 try/except。
    """

    def test_tagged_decorator_is_blocked_as_a_load_time_explosion(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_boom.py":
                       TAGGED_ON_PLAIN_UNITTEST}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestExplodesAtLoad", out)
            self.assertIn("載入爆掉", out)
            self.assertIn("AttributeError", out)
            # 訊息必須與 (i) 分得開：(i) 是靜默不跑，這是整批爆炸。
            self.assertNotIn("從來沒有執行過", out)

    def test_handwritten_test_tags_is_blocked_the_same_way(self):
        with patched_baseline({}), tree(
                files={"src/tests/test_boom2.py":
                       HANDWRITTEN_TAGS_ON_PLAIN_UNITTEST}) as fx:
            out = joined(fx.execution())
            self.assertIn("TestAlsoExplodes", out)
            self.assertIn("載入爆掉", out)

    def test_case_i_and_case_ii_are_distinguishable(self):
        """兩種違規同時出現時，訊息各自指對層。"""
        with patched_baseline({}), tree(
                files={"src/tests/test_silent.py": PLAIN_UNITTEST,
                       "src/tests/test_boom.py": TAGGED_ON_PLAIN_UNITTEST}) as fx:
            out = fx.execution()
            silent = [v for v in out if "TestWidgetNeverRuns" in v]
            boom = [v for v in out if "TestExplodesAtLoad" in v]
            self.assertEqual(len(silent), 1)
            self.assertEqual(len(boom), 1)
            self.assertIn("tag_selector.py:69", silent[0])
            self.assertIn("載入爆掉", boom[0])
            self.assertNotIn("載入爆掉", silent[0])


# ------------------------------------------------------------ 四處＋floor 登記

class TestRegistryWiring(unittest.TestCase):

    def test_rule_is_registered(self):
        self.assertIn("test-collectability", rl.REPO_LINT_RULES)
        self.assertEqual(
            tuple(rl.REPO_LINT_RULES["test-collectability"]),
            ("check_test_module_collection", "check_test_class_execution"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
