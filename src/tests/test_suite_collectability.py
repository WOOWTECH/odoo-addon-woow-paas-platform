"""測試收集完備性 gate —— 以 **Odoo 測試本身**為載體（#546 A 段）。

## 這條 gate 在整組守線裡的定位（⚠ 這一段是規格，不是背景說明）

同一條性質有**兩條觀測通道**，兩者**都要留著**，位階不同：

| 通道 | 何時跑 | 觀測的是什麼 | 位階 |
| --- | --- | --- | --- |
| `repo-lint.py` 的 `test-collectability`（純 AST） | PR-time | 原始碼的**靜態鏡像** | **早期預警鏡像** |
| 本檔（runtime introspection） | 全套測試 | Odoo loader **實際載進來的 class 物件** | **ground truth** |

這與 `scripts/audit/test_collection_audit.py` 裡 C2（baseline vs 原始碼）對 C3
（baseline vs 實際執行清單）是**同一種**關係，不是 PR #819 §2 反對的那個形狀——那裡反對
的是「兩份判準、**哪一份算數沒有答案**、而較弱的那份會先報綠」。這裡有答案：
**runtime 說了算。**

🔴 **兩條通道分歧時的處理：去查，不是選一邊。** 分歧本身就是訊號——要嘛靜態鏡像漏了一格
（`repo-lint` 的缺陷），要嘛 runtime 這側的假設錯了（本檔的缺陷），要嘛樹上真的有東西同時
滿足一邊、違反另一邊。⛔ 不要為了讓 CI 綠而把其中一邊改成配合另一邊；先查出**是哪一個**
class、經由哪一道篩子產生差異，再決定改哪一側。

## 🔴 為什麼**不**共用 `repo-lint.py` 的實作（這一格有實證，不是偏好）

`repo-lint.py` 的兩條判準把版面**寫死**成「`root` 底下才有 `src/`」
（`_ADDON_MANIFEST_REL = "src/__manifest__.py"`）。#828 修正前，缺這個 manifest 會直接
`return []`，形成 wrong-root fail-open；現在兩條 check 會各自回傳可歸因的
root/layout violation，不能再把錯誤版面解讀為零違規。

但 Odoo 測試執行時的 addon 版面**沒有 `src/` 這一層**：CI 的
`.gitea/workflows/odoo-tests.yml` 做的是 `docker cp src/. "odoo:${MODULE_DIR}/"`，
把 `src/` 的**內容**攤進 `MODULE_DIR`，所以 manifest 與 `tests/` 直接在 addon 根底下。
同一份 `tests/test_probe.py` 放進兩種版面時，repo layout 才能得到 class-level verdict；
container layout 現在會正確 fail-loud，但那是「輸入版面不適用」，不是 Odoo runtime
實際載入結果。

本檔因此**不 import 也不移植** `repo-lint.py`：靜態 check 的 repo-layout 前置條件與
runtime addon layout 不同；本檔改問 Odoo 已經載好的 class 物件——
`issubclass()` / `hasattr()` / `getattr()` 就是 loader 與 `TagsSelector` 自己在做的事，
不需要任何路徑推導，也不需要維護一份會漂移的基底名清單。

## 本檔的判準逐條對應到 Odoo 的哪一行

`odoo/tests/loader.py:19-45` `get_module_test_cases()` 四道篩子，之後才輪到
`odoo/tests/tag_selector.py:69` 的 `if not hasattr(test, 'test_tags')`：

- 篩子① `isinstance(obj, type)` ／② `issubclass(obj, unittest.TestCase)` ／
  ③ `obj.__module__ != module.__name__` → `execution_violations()`
- 篩子④ `cls.__dict__`（**不含繼承**）／④' `allow_inherited_tests_method`（走 `getattr`
  ⇒ **可繼承**） → `orphan_test_mixins()`
- `tag_selector.py:41-44`（空 tag 補成 `standard`） → `unreachable_opt_outs()`
- `_get_tests_modules()`（只讀 `<addon>.tests` **自己**的 `test_*` 屬性）
  → `collection_violations()`

🔴 **判準逐 class，不是逐 module**：本樹有混合檔（同檔既有會跑的、也有不會跑的），
module 級判準會整檔放行。

🔴 **判準寫「性質」不寫名字**：這裡沒有任何「不得出現 `unittest.TestCase`」這種字面判準
（`from unittest import TestCase` 一行就繞過，而且會誤傷只是提到它的 helper）。

🔴 **本檔刻意不帶任何基底白名單。** runtime 沒有「認不得的基底」這個狀態——`issubclass`
給的是確定的答案。⛔ 不要把 `repo-lint.py` 的 `_KNOWN_NON_TESTCASE_BASES` 抄過來：抄了
就等於把它的漂移一併繼承，而「不需要那份清單」正是這條通道的價值之一。

🔴 **每一條都是否定性判準**（「找不到違規」），掃描器一壞輸出就與「全部都對」一模一樣
⇒ `existence_controls()` 是它們共用的反向對照，而且每一格都在下面配了注入式陽性對照。

## 對真實套件的斷言用**集合等值**不用計數

判準對現樹回空清單（零違規）。⛔ 不寫成 `assertGreaterEqual` 或任何計數門檻——違規數不是
monotonic 的量，`>=` 會把守衛裝反（[[seed-topology-invariants]]）。空集合斷言同時讓失敗
訊息直接印出**是哪一個 class**，而不是「差幾個」。
"""
import inspect
import os
import sys
import tempfile
import types
import unittest

from odoo.tests.common import TransactionCase, tagged

#: `@tagged` 的**結構性**保留字。判準來源是 `odoo/tests/tag_selector.py:41-44`——它把空
#: tag 補成 `standard`，所以這三個字不能當成「這個 class 還有別的路可以被選到」的證據。
#: ⚠ 這是從 Odoo 源碼推導的**性質**，不是一份可以增刪的白名單。
_RESERVED_TAGS = frozenset({"standard", "at_install", "post_install"})

#: `odoo/tests/loader.py:31` 的旗標。走 `getattr` ⇒ **可繼承**（可以寫在 mixin 身上、
#: 由子類繼承），所以只查 class 自己那一行會漏掉本樹實際在用的寫法。
_INHERITED_TESTS_FLAG = "allow_inherited_tests_method"


def _iter_modules(pkg):
    """`pkg` 自己 ＋ 它所有子套件（遞迴），回 `(dotted 名, module)`。

    只走**名字掛在 `pkg` 底下**的模組，避免把 import 進來的第三方模組一起掃。
    """
    yield pkg.__name__, pkg
    for _name, obj in inspect.getmembers(pkg, inspect.ismodule):
        if not getattr(obj, "__name__", "").startswith(pkg.__name__ + "."):
            continue
        if hasattr(obj, "__path__"):
            yield from _iter_modules(obj)
        else:
            yield obj.__name__, obj


def _visible_test_modules(pkg):
    """Odoo 的 `_get_tests_modules()` **實際看得到**的模組名。

    逐字重演它的做法：只讀 `<addon>.tests` **自己**的屬性
    （`inspect.getmembers(..., ismodule)` ＋ 名字以 `test_` 開頭）。子套件自己的
    `__init__.py` **不在它的視野裡**。
    """
    return {name for name, obj in inspect.getmembers(pkg, inspect.ismodule)
            if name.startswith("test_")}


def _own_test_methods(cls):
    """篩子④：只算 `cls.__dict__` 裡的 `def test*`，**不含繼承**。"""
    return sorted(k for k, v in vars(cls).items()
                  if k.startswith("test") and callable(v))


def _defined_here(obj, mod):
    """篩子①＋③：是 class，而且是**在這個 module 裡定義**的（不是 import 進來的）。"""
    return isinstance(obj, type) and obj.__module__ == mod.__name__


def collection_violations(pkg):
    """性質 1（收集面）：磁碟上的每個 `test_*.py`，Odoo 的 loader 都要看得到。

    沒有被 `__init__.py` import 的檔案對 loader 而言不存在——不會有 collection error、
    不會有 warning，`of N tests` 少了幾條也沒有人看得出來。

    子套件多一格：`_get_tests_modules()` 只讀頂層屬性 ⇒ 子套件的檔**必須被頂層
    `__init__.py` re-export**，只被子套件自己的 `__init__.py` import 是不夠的。
    """
    root = os.path.dirname(pkg.__file__)
    visible = _visible_test_modules(pkg)
    violations = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if not (filename.startswith("test_") and filename.endswith(".py")):
                continue
            if filename[:-3] in visible:
                continue
            rel = os.path.relpath(os.path.join(dirpath, filename), root)
            if os.path.dirname(rel):
                violations.append(
                    "%s：Odoo 的 `_get_tests_modules()` 只讀 `<addon>.tests` 自己的 "
                    "`test_*` 屬性，子套件的 `__init__.py` 不在它的視野裡 ⇒ 這個檔一條"
                    "都不會跑。請在頂層 `tests/__init__.py` re-export 它" % rel)
            else:
                violations.append(
                    "%s：沒有被 `tests/__init__.py` import ⇒ loader 根本看不到這個模組，"
                    "裡面的測試一條都不會執行，而且不會有任何 collection error" % rel)
    return sorted(violations)


def execution_violations(pkg):
    """性質 2（執行面）：通過 loader 篩子②③、卻拿不到 `test_tags` 的 class。

    這種 class 會被 yield 成 test 實例，然後倒在 `tag_selector.py:69` 的
    `hasattr(test, 'test_tags')` ⇒ **靜默不跑**。

    🔴 `test_tags` 由 `MetaCase`（`odoo/tests/common.py:253`）賦予，而 `MetaCase` 只掛在
    `BaseCase` 上。所以問 `hasattr(cls, 'test_tags')` 就等於問「它跑不跑得到」——不需要走
    繼承鏈、不需要認得基底叫什麼名字。

    ⚠ **精確一點**：`MetaCase.__init__` 還有第二個條件——`common.py:260` 的
    `if cls.__module__.startswith('odoo.addons.')`。所以 `hasattr(cls, 'test_tags')`
    嚴格說是「有 `BaseCase` 血統 **且** 定義在 `odoo.addons.*` 底下」。在本判準的掃描範圍
    內兩者等價（loader 只走 `odoo.addons.<addon>.tests`，範圍外的 class 本來就不會被
    collect），但**造假 fixture 時這一格會咬人**：假 module 若命名成 `fake.tests.x`，
    連 `TransactionCase` 子類都拿不到 `test_tags`，整組對照跟著失真。
    （這不是推論——本檔 fixture 的第一版就是那樣寫的，實跑當場紅，見 `_fake_module()`。）
    """
    violations = []
    for dotted, mod in _iter_modules(pkg):
        for obj in vars(mod).values():
            if not _defined_here(obj, mod):
                continue
            if not issubclass(obj, unittest.TestCase):
                continue
            if hasattr(obj, "test_tags"):
                continue
            violations.append(
                "%s.%s：是 `unittest.TestCase` 子類卻拿不到 `test_tags`（＝沒有經過 "
                "`MetaCase`，最常見的原因是基底沒有傳遞繼承到 `odoo.tests.common.BaseCase`）"
                "，會通過 loader 篩子②③被 yield 成 test 實例，然後倒在 "
                "`tag_selector.py:69` ⇒ 這 %d 條測試**靜默不跑**，`of N tests` "
                "不會顯示少了它們" % (dotted, obj.__name__, len(_own_test_methods(obj))))
    return sorted(violations)


def orphan_test_mixins(pkg):
    """性質 3：帶 `def test*` 的 mixin，必須至少有一個子類真的會展開它。

    篩子④只收 `cls.__dict__`，繼承來的測試方法**不會**被收——除非該子類的
    `allow_inherited_tests_method` 為真。

    🔴 **mixin 本身不是違規**：它與「是 TestCase 但基底錯」一樣到不了 `BaseCase`，但它的
    `def test*` 由開了旗標的子類展開後**每次都真的在跑**。把 mixin 判成違規等於在 repo 裡
    種一筆假宣稱（名單說「這些不會被跑」，而它們在跑）。所以這裡查的是**它的子類拿不拿得
    到旗標**，不是它自己。
    """
    lineage = [obj for _dotted, mod in _iter_modules(pkg)
               for obj in vars(mod).values()
               if _defined_here(obj, mod) and hasattr(obj, "test_tags")]
    violations = []
    for dotted, mod in _iter_modules(pkg):
        for obj in vars(mod).values():
            if not _defined_here(obj, mod) or issubclass(obj, unittest.TestCase):
                continue
            methods = _own_test_methods(obj)
            if not methods:
                continue
            heirs = [cls for cls in lineage if cls is not obj and issubclass(cls, obj)]
            if any(getattr(heir, _INHERITED_TESTS_FLAG, False) for heir in heirs):
                continue
            violations.append(
                "%s.%s：帶 %d 條 `def test*` 的 mixin，但沒有任何一個有 `BaseCase` 血統的"
                "子類拿得到 `%s`（旗標走 `getattr` ⇒ 可繼承，寫在 mixin 自己身上也算）"
                " ⇒ 那些方法一條都不會跑"
                % (dotted, obj.__name__, len(methods), _INHERITED_TESTS_FLAG))
    return sorted(violations)


def unreachable_opt_outs(pkg):
    """性質 4：`-standard` 是合法 opt-out，但必須另帶至少一個可選 tag 當入口。

    `tag_selector.py:41-44` 把空 tag 補成 `standard`，所以只剩保留字的 class **連精確
    指名都選不到**，而輸出是 `EXIT=0` ＋ WARNING 級的 `of 0 tests`——與「跑過而且全過」
    不可區分。
    """
    violations = []
    for dotted, mod in _iter_modules(pkg):
        for obj in vars(mod).values():
            if not _defined_here(obj, mod):
                continue
            tags = getattr(obj, "test_tags", None)
            if tags is None:
                continue                      # 那是 execution_violations() 那一格
            if "standard" in tags:
                continue                      # 沒有 opt-out
            if tags - _RESERVED_TAGS:
                continue                      # 另帶了可選 tag ⇒ 合法 opt-out
            violations.append(
                "%s.%s：帶 `-standard` 卻沒有任何非保留字 tag（有效 tag=%s）⇒ 任何常規"
                "選法都選不到它，連精確指名 `--test-tags /woow_paas_platform:%s` 都不行，"
                "而輸出是 `EXIT=0` ＋ `of 0 tests`。請另帶一個可選 tag 當入口"
                % (dotted, obj.__name__, sorted(tags), obj.__name__))
    return sorted(violations)


def existence_controls(pkg):
    """🔴 上面四條全是否定性判準，探針一壞輸出就與「全部都對」一模一樣。

    掃不到任何 test 模組／找不到**任何**拿得到 `test_tags` 的 class，一律當違規而不是
    判乾淨。這是本檔唯一一條**肯定性**判準，也是它們共用的反向對照。
    """
    violations = []
    if not _visible_test_modules(pkg):
        violations.append(
            "掃不到任何 `test_*` 模組 ⇒ 掃描範圍壞了，不是這棵樹沒有測試")
    lineage = [obj for _dotted, mod in _iter_modules(pkg)
               for obj in vars(mod).values()
               if _defined_here(obj, mod) and hasattr(obj, "test_tags")]
    if not lineage:
        violations.append(
            "找不到**任何**拿得到 `test_tags` 的 class ⇒ 探針壞了，不是全樹都不會跑")
    return violations


# --------------------------------------------------------------------------- #
#  陽性對照的注入機制                                                          #
#                                                                              #
#  🔴 對照物件一律是 **runtime 造出來的假 class／假 module**，⛔ 不落地           #
#     `src/tests/`——落地會被真的收集，污染本樹，而且會撞 #684 的 gate。         #
#     需要磁碟的只有收集面（它要 `os.walk`），走 `tempfile` 臨時目錄。          #
# --------------------------------------------------------------------------- #

#: 假 fixture 的模組名前綴。🔴 **必須以 `odoo.addons.` 開頭**：`MetaCase.__init__`
#: （`odoo/tests/common.py:260`）只對這個前綴底下的 class 賦予 `test_tags`。命名成
#: `fake.tests.x` 的話，連 `TransactionCase` 子類都拿不到 `test_tags` ⇒ 陽性對照會多抓、
#: 陰性對照會誤紅，整組失真。本檔第一版就是這樣寫的，實跑當場兩條紅。
#: ⚠ 這個 addon 名不存在也不會被註冊——fixture 從不進 `sys.modules`。
_FAKE_PKG = "odoo.addons._collectability_probe.tests"


def _fake_module(short_name):
    """造一個假 module（只在記憶體裡，**不進 `sys.modules`**）。"""
    return types.ModuleType("%s.%s" % (_FAKE_PKG, short_name))


def _fake_class(mod, name, bases, **attrs):
    """在假 module 裡造一個**真的** class 物件。

    `__module__` 必須明寫成該假 module 的名字，否則 loader 篩子③
    （`obj.__module__ != module.__name__`）會把它濾掉，對照就不成立。
    """
    namespace = dict(attrs)
    namespace["__module__"] = mod.__name__
    cls = type(name, bases, namespace)
    setattr(mod, name, cls)
    return cls


def _fake_package(modules=(), file_path=None):
    """造一個假的 tests 套件；`modules` 是一串 `(短名, module)`。"""
    pkg = types.ModuleType(_FAKE_PKG)
    pkg.__path__ = []
    pkg.__file__ = file_path or os.path.join(os.sep, "nonexistent", "__init__.py")
    for short_name, mod in modules:
        setattr(pkg, short_name, mod)
    return pkg


def _noop_test(self):
    """給假 class 用的測試方法本體（本體不重要，**存在與否**才重要）。"""


@tagged("post_install", "-at_install")
class TestSuiteCollectability(TransactionCase):
    """本 gate 自己的基底對、tag 對 ⇒ **它一定會被 collect**（#684 留言 8233 §6 建議 A）。

    活教材：`src/tests/test_reconcile_delete_gate.py` 本身就是一道 gate，而它曾經是
    `unittest.TestCase` ⇒ 那幾條從來沒跑過，而且沒有任何人發現（已由 #816 修好）。
    """

    def _suite_package(self):
        """本 addon 的 tests 套件本身。

        測試檔自己就住在這個 package 裡 ⇒ `sys.modules[__package__]` 必然存在，
        不需要任何路徑推導，三種執行環境（CI 容器／本機 checkout／k8s 沙盒）都一樣。
        """
        pkg = sys.modules.get(__package__)
        self.assertIsNotNone(
            pkg, "拿不到 `%s` ⇒ 探針壞了，不是這棵樹沒有測試" % __package__)
        return pkg

    # ---- 對真實測試套件的斷言 ---------------------------------------------- #

    def test_probe_itself_is_healthy(self):
        """🔴 先證明探針活著，再讓它去否定。

        下面四條都是否定性判準，這一條不成立時它們的綠燈**沒有任何意義**。
        """
        self.assertEqual(
            existence_controls(self._suite_package()), [],
            "存在性反向對照不通過 ⇒ 本檔其餘四條的綠燈一律不成立")

    def test_every_test_file_is_collected(self):
        """磁碟上的每個 `test_*.py`，Odoo 的 loader 都看得到。"""
        self.assertEqual(
            collection_violations(self._suite_package()), [],
            "有測試檔沒有被 collect")

    def test_every_test_class_can_run(self):
        """每個 TestCase 子類都拿得到 `test_tags`（＝有 `BaseCase` 血統）。"""
        self.assertEqual(
            execution_violations(self._suite_package()), [],
            "有 TestCase 子類拿不到 test_tags ⇒ 靜默不跑")

    def test_every_test_mixin_is_expanded_by_someone(self):
        """帶 `def test*` 的 mixin 都至少有一個子類會展開它。"""
        self.assertEqual(
            orphan_test_mixins(self._suite_package()), [],
            "有 mixin 的測試方法一條都不會跑")

    def test_every_opt_out_stays_selectable(self):
        """帶 `-standard` 的 class 都另帶了可選 tag 當入口。"""
        self.assertEqual(
            unreachable_opt_outs(self._suite_package()), [],
            "有 class 帶 -standard 卻沒有任何入口 ⇒ 靜默假綠")

    # ---- 陽性對照：注入違規必須翻紅 ---------------------------------------- #
    #
    # 這一段的計數斷言（`len(...) == 1`）鎖的是**本檔自己構造的 fixture**，不是活的樹，
    # 所以它是刻意凍結的契約、不是 schema 快照——與上面對真實套件的空集合斷言分屬兩類。

    def test_positive_control_untagged_class_in_a_mixed_module(self):
        """混合檔那一格——**這正是 module 級判準會漏掉的那一格**。

        同一個假 module 裡同時放「有血統的」與「沒血統的」，判準必須只點名後者。
        """
        mod = _fake_module("test_mixed")
        _fake_class(mod, "TestRuns", (TransactionCase,), test_runs=_noop_test)
        _fake_class(mod, "TestNeverRuns", (unittest.TestCase,), test_never=_noop_test)
        pkg = _fake_package([("test_mixed", mod)])

        violations = execution_violations(pkg)
        self.assertEqual(len(violations), 1,
                         "混合檔應該只點名沒血統的那一個：%s" % violations)
        self.assertIn("TestNeverRuns", violations[0])
        # 陰性對照：同檔有血統的那個不得被點名
        self.assertNotIn("TestRuns：", violations[0])

    def test_positive_control_subclass_of_odoo_case_without_metacase(self):
        """`odoo.tests.case.TestCase` 那一格——判準寫寬時最容易漏掉的形狀。

        它掛在 `odoo.tests` 命名空間下、也是 `unittest.TestCase` 子類，但**沒有**
        `MetaCase` ⇒ 拿不到 `test_tags`。以「名字／模組路徑」為判準會放它過去。
        """
        from odoo.tests import case as odoo_case

        mod = _fake_module("test_odoo_case")
        _fake_class(mod, "TestDirectOdooCase", (odoo_case.TestCase,),
                    test_never=_noop_test)
        pkg = _fake_package([("test_odoo_case", mod)])

        violations = execution_violations(pkg)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("TestDirectOdooCase", violations[0])

    def test_positive_control_mixin_without_any_flagged_heir(self):
        """mixin 沒有任何開旗標的子類 ⇒ 它的測試方法一條都不會跑。"""
        mod = _fake_module("test_mixin")
        mixin = _fake_class(mod, "_OrphanMixin", (object,), test_shared=_noop_test)
        _fake_class(mod, "TestUsesMixin", (mixin, TransactionCase), test_own=_noop_test)
        pkg = _fake_package([("test_mixin", mod)])

        violations = orphan_test_mixins(pkg)
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("_OrphanMixin", violations[0])

    def test_negative_control_mixin_with_inherited_flag_is_green(self):
        """🔴 陰性對照，而且挑的是**有鑑別力**的形狀：旗標寫在 mixin 自己身上。

        旗標走 `getattr` ⇒ 可繼承。只查子類自己那一行的實作會把這一格誤判成違規，
        所以判準一退化這條就翻紅。
        """
        mod = _fake_module("test_mixin_ok")
        mixin = _fake_class(mod, "_SharedAssertions", (object,),
                            test_shared=_noop_test,
                            **{_INHERITED_TESTS_FLAG: True})
        _fake_class(mod, "TestUsesMixin", (mixin, TransactionCase), test_own=_noop_test)
        pkg = _fake_package([("test_mixin_ok", mod)])

        self.assertEqual(orphan_test_mixins(pkg), [],
                         "旗標繼承自 mixin 的情形不得被判成違規")

    def test_positive_control_opt_out_without_any_entry_tag(self):
        """`-standard` 且只剩保留字 ⇒ 任何常規選法都選不到。

        同一個假 module 裡並放合法 opt-out 當陰性對照——判準若退化成「只要不含
        `standard` 就報」，那一個會一起被點名而讓這條紅。
        """
        mod = _fake_module("test_optout")
        # 用**真的** `tagged()` 疊 tag，不自己寫 `test_tags`——否則對照驗的是我對
        # `tagged()` 的理解，不是 `tagged()` 本身。
        tagged("-standard", "post_install", "-at_install")(
            _fake_class(mod, "TestUnreachable", (TransactionCase,),
                        test_unreachable=_noop_test))
        tagged("-standard", "post_install", "-at_install", "ecpay_real")(
            _fake_class(mod, "TestLegalOptOut", (TransactionCase,),
                        test_legal=_noop_test))
        pkg = _fake_package([("test_optout", mod)])

        violations = unreachable_opt_outs(pkg)
        self.assertEqual(len(violations), 1,
                         "合法 opt-out（另帶可選 tag）不得被點名：%s" % violations)
        self.assertIn("TestUnreachable", violations[0])

    def test_positive_control_uncollected_files_on_disk(self):
        """收集面兩格：完全沒被 import ／ 子套件沒被**頂層** re-export。

        這一格需要磁碟（判準要 `os.walk`）⇒ 走 `tempfile` 臨時目錄，
        ⛔ 不落地 `src/tests/`。
        """
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "integration"))
            for rel in ("__init__.py",
                        "test_seen.py",
                        "test_orphan.py",
                        os.path.join("integration", "__init__.py"),
                        os.path.join("integration", "test_sub_only.py")):
                open(os.path.join(tmp, rel), "w").close()

            seen = _fake_module("test_seen")
            _fake_class(seen, "TestSeen", (TransactionCase,), test_x=_noop_test)
            pkg = _fake_package([("test_seen", seen)],
                                file_path=os.path.join(tmp, "__init__.py"))

            violations = collection_violations(pkg)

        self.assertEqual(len(violations), 2, violations)
        joined = "\n".join(violations)
        self.assertIn("test_orphan.py", joined)
        self.assertIn("test_sub_only.py", joined)
        # 陰性對照：被頂層看得到的那個不得被點名
        self.assertNotIn("test_seen.py", joined)

    def test_positive_control_existence_probe_catches_an_empty_scan(self):
        """🔴 反向對照自己也要有陽性對照，否則它回空等於零資訊。"""
        self.assertEqual(len(existence_controls(_fake_package())), 2,
                         "空套件必須兩格都報")

        # 只有 mixin、零血統：模組看得到，但沒有任何 class 拿得到 test_tags
        mod = _fake_module("test_only_mixin")
        _fake_class(mod, "_OnlyMixin", (object,), test_x=_noop_test)
        violations = existence_controls(_fake_package([("test_only_mixin", mod)]))
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("拿得到 `test_tags`", violations[0])
