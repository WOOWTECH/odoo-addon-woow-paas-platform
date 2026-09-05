#!/usr/bin/env python3
"""巢狀 `def test*` 要不要算進條數（#850）。

## 這條測試守的性質

``repo-lint.py`` 的 ``_test_own_test_methods()`` 的 docstring **自宣告**它是
``odoo/tests/loader.py`` :: ``get_module_test_cases`` 篩子④的鏡像。篩子④看的是
**執行後**的 class 物件（``test_case_class.__dict__.items()`` ＋
``method_name.startswith('test')``），而舊版判準只看 class body **最頂層**的
``FunctionDef`` ⇒ ``def test*`` 只要包在 ``if`` / ``try`` / ``for`` / ``while`` /
``with`` / ``match`` 裡，**靜態算 0、執行期照跑**。

算成 0 之後那個 class 既不進 baseline 也不進註記（#836 的靜默出口）⇒ C2 兩邊一致
報 PASS。所以這不是「兩個東西剛好不一樣」，是**宣告要對齊、實際沒對齊**。

## 每個 assert 的基準是「執行出來的」，不是抄來的

``TestRuntimePrecondition`` 先把 fixture **執行**起來、直接讀 ``cls.__dict__``
（＝篩子④的那一手），再由其餘測試拿那個結果當比對基準。前提若只用嘴巴宣告，
後面每一格都不算數 —— 前提那格失敗時訊息會明說「樣本壞了，不是被測物有事」。

⚠ 這裡刻意**不** import Odoo：CI 容器沒有 Odoo，而篩子④的判準只需要
``cls.__dict__`` ＋ ``startswith('test')``。上游那支 ``get_module_test_cases``
本身已在 #850 的證據裡被真的 exec 起來對過（宿主 checkout
``odoo-18.0``，``version_info = (18, 0, 0, FINAL, 0, '')``，
``loader.py`` sha256 ``d5981574…``），本檔守的是那個結論的**回歸**。

## 邊界（測試把它釘住，不是假裝涵蓋）

``TestStaticUnionBoundary`` 明寫「執行期才決定會不會定義」那一格：靜態取**聯集**
⇒ 可能比實際**多算**，但不會算成 0。方向是刻意的，理由見被測函式的 docstring。

跑法：
    python3 scripts/audit/test_repo_lint_nested_test_defs.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_nested_test_defs.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部依賴
——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo、git 執行檔，也不依賴 cwd。

Refs #850 #836 #546
"""
import ast
import importlib.util
import pathlib
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent

_GATE_PATH = _HERE / "repo-lint.py"
_spec = importlib.util.spec_from_file_location("repo_lint_nested_defs_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


# --------------------------------------------------------------------------- #
#  fixture：一種形態一個 class，全部只差「包 `def test*` 的那層語句」            #
# --------------------------------------------------------------------------- #

FIXTURE = '''
import contextlib
from unittest import TestCase

FLAG = True


class TestTopLevel(TestCase):
    """對照組：頂層定義。舊判準也數得到 —— 沒有它，「新判準把該數的數到了」與
    「新判準什麼都回」不可區分。"""
    def test_top(self):
        pass


class TestNestedIf(TestCase):
    if FLAG:
        def test_in_if(self):
            pass


class TestNestedTry(TestCase):
    try:
        def test_in_try(self):
            pass
    except Exception:
        pass
    finally:
        def test_in_finally(self):
            pass


class TestNestedFor(TestCase):
    for _i in (0,):
        def test_in_for(self):
            pass


class TestNestedWhile(TestCase):
    _n = 1
    while _n:
        def test_in_while(self):
            pass
        _n = 0


class TestNestedWith(TestCase):
    with contextlib.suppress(Exception):
        def test_in_with(self):
            pass


class TestDeeplyNested(TestCase):
    if FLAG:
        for _j in (0,):
            try:
                def test_deep(self):
                    pass
            except Exception:
                pass


class TestNegativeControls(TestCase):
    """三個**不該**被算進來的：走進去就換了 namespace，或名字根本不是 test*。"""
    def test_outer(self):
        def test_inside_a_function(self):     # 函式自己的 local scope
            pass

    class Inner:
        def test_inside_a_nested_class(self):  # 落在 Inner.__dict__
            pass

    if FLAG:
        def helper_not_prefixed(self):         # 不以 test 開頭
            pass


class TestNoTestsAtAll(TestCase):
    """鑑別力對照：真的沒有測試方法時就該回空 —— 不是「有 def 就算」。"""
    if FLAG:
        def setUp(self):
            pass
'''

# 執行期才決定會不會定義的形態：`else` 那一支永遠不會執行。
FIXTURE_UNREACHABLE = '''
from unittest import TestCase

FLAG = True


class TestBranch(TestCase):
    if FLAG:
        def test_taken(self):
            pass
    else:
        def test_not_taken(self):
            pass
'''

# 同名分支：兩支各定義一次，執行後 __dict__ 只會有一個。
FIXTURE_DUPLICATE = '''
from unittest import TestCase

FLAG = True


class TestSameName(TestCase):
    if FLAG:
        def test_same(self):
            pass
    else:
        def test_same(self):
            pass
'''

# `match` / `except*` 只在夠新的 Python 上才是合法語法，各自獨立成一份。
FIXTURE_MATCH = '''
from unittest import TestCase


class TestNestedMatch(TestCase):
    _v = 1
    match _v:
        case 1:
            def test_in_match(self):
                pass
'''

FIXTURE_TRY_STAR = '''
from unittest import TestCase


class TestNestedTryStar(TestCase):
    try:
        def test_in_try_star(self):
            pass
    except* Exception:
        pass
'''


def runtime_test_methods(cls):
    """篩子④的那一手：**執行後**的 class 物件身上有哪些 `test*`。"""
    return sorted(name for name, value in cls.__dict__.items()
                  if callable(value) and name.startswith("test"))


def static_test_methods(source, class_name):
    """被測物：對同一份原始碼的 AST 跑 `_test_own_test_methods()`。"""
    tree = ast.parse(source)
    node = next(n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == class_name)
    return sorted(rl._test_own_test_methods(node))


def exec_fixture(source):
    ns = {}
    exec(compile(source, "<fixture>", "exec"), ns)   # noqa: S102 —— 前提就是要執行
    return ns


# --------------------------------------------------------------------------- #
#  (A) 前提：這些 class 執行後**真的**有那些測試方法                             #
# --------------------------------------------------------------------------- #

class TestRuntimePrecondition(unittest.TestCase):
    """整份測試的前提。這格紅＝樣本寫壞了，不是被測物有事。"""

    def test_nested_defs_really_land_in_class_dict(self):
        ns = exec_fixture(FIXTURE)
        expected = {
            "TestTopLevel": ["test_top"],
            "TestNestedIf": ["test_in_if"],
            "TestNestedTry": ["test_in_finally", "test_in_try"],
            "TestNestedFor": ["test_in_for"],
            "TestNestedWhile": ["test_in_while"],
            "TestNestedWith": ["test_in_with"],
            "TestDeeplyNested": ["test_deep"],
            "TestNegativeControls": ["test_outer"],
            "TestNoTestsAtAll": [],
        }
        for name, methods in expected.items():
            self.assertEqual(
                runtime_test_methods(ns[name]), methods,
                "樣本壞了（不是被測物有事）：%s 執行後的 __dict__ 與預期不符" % name)

    def test_nested_class_helper_is_not_on_the_outer_class(self):
        ns = exec_fixture(FIXTURE)
        self.assertEqual(runtime_test_methods(ns["TestNegativeControls"].Inner),
                         ["test_inside_a_nested_class"],
                         "樣本壞了：巢狀 class 的方法應該落在**它自己**身上")


# --------------------------------------------------------------------------- #
#  (B) 判準必須與執行期一致                                                     #
# --------------------------------------------------------------------------- #

class TestNestedDefsAreCounted(unittest.TestCase):
    """一種語句一格 —— 只放一種會讓人以為是某個語法的怪癖。"""

    def _assert_mirrors_runtime(self, class_name):
        ns = exec_fixture(FIXTURE)
        self.assertEqual(static_test_methods(FIXTURE, class_name),
                         runtime_test_methods(ns[class_name]),
                         "%s：靜態判準與 loader 篩子④ 的執行期結果分歧" % class_name)

    def test_top_level(self):
        self._assert_mirrors_runtime("TestTopLevel")

    def test_nested_in_if(self):
        self._assert_mirrors_runtime("TestNestedIf")

    def test_nested_in_try_and_finally(self):
        self._assert_mirrors_runtime("TestNestedTry")

    def test_nested_in_for(self):
        self._assert_mirrors_runtime("TestNestedFor")

    def test_nested_in_while(self):
        self._assert_mirrors_runtime("TestNestedWhile")

    def test_nested_in_with(self):
        self._assert_mirrors_runtime("TestNestedWith")

    def test_nested_three_levels_deep(self):
        self._assert_mirrors_runtime("TestDeeplyNested")

    @unittest.skipIf(sys.version_info < (3, 10), "match 需要 Python 3.10+")
    def test_nested_in_match(self):
        ns = exec_fixture(FIXTURE_MATCH)
        self.assertEqual(static_test_methods(FIXTURE_MATCH, "TestNestedMatch"),
                         runtime_test_methods(ns["TestNestedMatch"]))

    @unittest.skipIf(sys.version_info < (3, 11), "except* 需要 Python 3.11+")
    def test_nested_in_try_star(self):
        ns = exec_fixture(FIXTURE_TRY_STAR)
        self.assertEqual(static_test_methods(FIXTURE_TRY_STAR, "TestNestedTryStar"),
                         runtime_test_methods(ns["TestNestedTryStar"]))


# --------------------------------------------------------------------------- #
#  (C) 陰性對照：換了 namespace 的、名字不對的，一個都不准算進來                 #
# --------------------------------------------------------------------------- #

class TestScopeBoundaries(unittest.TestCase):

    def test_negative_controls_mirror_runtime(self):
        ns = exec_fixture(FIXTURE)
        self.assertEqual(static_test_methods(FIXTURE, "TestNegativeControls"),
                         runtime_test_methods(ns["TestNegativeControls"]))

    def test_def_inside_a_method_is_not_counted(self):
        self.assertNotIn("test_inside_a_function",
                         static_test_methods(FIXTURE, "TestNegativeControls"),
                         "函式 body 裡的 def 不會出現在 class 的 __dict__ 裡")

    def test_def_inside_a_nested_class_is_not_counted(self):
        self.assertNotIn("test_inside_a_nested_class",
                         static_test_methods(FIXTURE, "TestNegativeControls"),
                         "巢狀 class 的方法落在**那個** class 的 __dict__ 裡")

    def test_non_test_prefix_is_not_counted(self):
        self.assertEqual(static_test_methods(FIXTURE, "TestNoTestsAtAll"), [],
                         "真的沒有測試方法時要回空 —— 否則本判準沒有鑑別力")


# --------------------------------------------------------------------------- #
#  (D) 邊界：靜態永遠對不齊 loader 的那一格，明寫並釘住                          #
# --------------------------------------------------------------------------- #

class TestStaticUnionBoundary(unittest.TestCase):
    """定義與否在**執行期**才決定的形態：靜態取聯集 ⇒ 可能多算，但不會算成 0。"""

    def test_unreachable_branch_is_counted_statically(self):
        ns = exec_fixture(FIXTURE_UNREACHABLE)
        runtime = runtime_test_methods(ns["TestBranch"])
        static = static_test_methods(FIXTURE_UNREACHABLE, "TestBranch")
        self.assertEqual(runtime, ["test_taken"],
                         "樣本壞了：else 那一支本來就不該執行")
        self.assertEqual(static, ["test_not_taken", "test_taken"],
                         "這是**刻意**的多算（取兩支的聯集）。要改成只認某一支，"
                         "先讀被測函式 docstring 裡「靜態永遠對不齊」那段的理由")
        self.assertGreaterEqual(len(static), len(runtime),
                                "失效方向必須是多算，不是算成 0")

    def test_same_name_in_both_branches_counts_once(self):
        ns = exec_fixture(FIXTURE_DUPLICATE)
        self.assertEqual(runtime_test_methods(ns["TestSameName"]), ["test_same"])
        self.assertEqual(static_test_methods(FIXTURE_DUPLICATE, "TestSameName"),
                         ["test_same"],
                         "同名只算一條 —— 執行後 __dict__ 只會有一個。"
                         "（`check_duplicate_class_methods` 刻意不管巢狀 def，"
                         "所以這個形態是合法的、會真的出現）")


# --------------------------------------------------------------------------- #
#  (E) 系統面：這個修正真的傳到 C2 的預期清單                                    #
# --------------------------------------------------------------------------- #

def _load_audit():
    path = _HERE / "test_collection_audit.py"
    if not path.is_file():
        raise SystemExit("FATAL: 找不到 %s —— 下游判準不在，空跑不算通過" % path)
    spec = importlib.util.spec_from_file_location("_audit_for_nested_defs_ut", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestReachesTheBaselinePredictor(unittest.TestCase):
    """判準只是中間值 —— 要證明它走到 C2 的預期清單，不是停在單元測試裡。

    ⛔ 本格**不碰** #836 在裁的「條數 0 的 class 怎麼列」：修好之後這個 class 的
       條數不是 0，它走的是一般的 `expected` 路徑。

    ⚠ #836 落地後 `scan_expected()` 改回 3-tuple（多了「條數 0」那個容器）。
      本格因此多一條斷言：這個 class 也**不得**落進那個新容器。原本只斷言
      「不在 `excluded`」的話，#850 的判準若哪天退化成算 0 條，這個 class 會
      安靜地換到新容器裡，而 `assertNotIn(..., excluded)` 照樣通過。
      ⇒ 解包從 2 改 3 是機械性的，斷言是**變強**，不是被改弱。
    """

    TREE = {
        # 合成 root 明示最小 workflow selector；真 repo 缺檔仍由被測物 fail-loud。
        ".gitea/workflows/odoo-tests.yml": (
            "run: odoo --test-tags /probe\\\\\n"
        ),
        # `_addon_source_tree_present()` 靠這個檔認出「這是本 addon 的 root」；
        # #828 修正前少了它會 fail-open；現在會回明確 wrong-root violation，但仍只測到
        # layout 前置條件、測不到 nested def 判準，所以這棵樹必須保留 manifest。
        "src/__manifest__.py": "{'name': 'probe-850'}\n",
        "src/tests/__init__.py": "from . import test_nested\n",
        "src/tests/test_nested.py": (
            "from odoo.tests.common import TransactionCase\n"
            "FLAG = True\n"
            "class TestOnlyNested(TransactionCase):\n"
            "    if FLAG:\n"
            "        def test_a(self): pass\n"
            "    try:\n"
            "        def test_b(self): pass\n"
            "    except Exception:\n"
            "        pass\n"
            "class TestPlain(TransactionCase):\n"
            "    def test_c(self): pass\n"
        ),
    }

    def test_class_with_only_nested_defs_reaches_expected(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            audit._write_tree(root, self.TREE)
            expected, excluded, zero_count = audit.scan_expected(root)
        self.assertEqual(expected.get("test_nested.TestPlain"), 1,
                         "對照組不見了 ⇒ 這棵樹本身有問題，不是本條的結論")
        self.assertEqual(
            expected.get("test_nested.TestOnlyNested"), 2,
            "只有巢狀 `def test*` 的 class 必須以正確條數進預期清單；"
            "拿到 None ＝ 它被算成 0 條、從清單裡整個消失（#850 → #836 的靜默出口）")
        self.assertNotIn("test_nested.TestOnlyNested", dict(excluded),
                         "條數不是 0 的 class 不該落到 excluded")
        self.assertNotIn(
            "test_nested.TestOnlyNested", dict(zero_count),
            "條數不是 0 的 class 不該落到 #836 的『條數 0』容器——落進去代表 "
            "#850 的判準退化回只認頂層 `def`，而那個退化在 `excluded` 那條斷言下是無聲的")


if __name__ == "__main__":
    unittest.main(verbosity=1)
