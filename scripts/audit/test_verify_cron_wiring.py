#!/usr/bin/env python3
"""第 35 條 `cron-wiring` 的 injected-red 測試（#960；判準來源 #803 F-803-1）。

## 為什麼第一支測試是「先注入壞樣本、要求紅燈」

這是一條**否定性** gate：綠燈長成「空清單」。實作壞掉（掃不到檔、glob 打錯、
`root` 沒接進去）時的輸出與「全部接得上」**逐字相同**，所以不能拿「現樹是綠的」
當驗收——先證明它會紅，綠燈才有意義。

## 涵蓋

| 格 | 注入 | 期望 |
| -- | ---- | ---- |
| A | `code` 裡的 method 名拼錯 | 違規，訊息指名是**哪一筆** cron |
| B | `model_id` 的 `ref` 指到**另一個存在的** model | 違規，指名那一筆 |
| C | `ref` 指到不存在的 model | 違規，指名那一筆 |
| D | 正確接線 | 零違規 |
| E | 跨模組 ref（帶模組前綴） | 略過並列名，**不算**違規 |

外加四類「什麼都沒查到」的對照：零筆 ir.cron／實作載入失敗／實作執行時炸開／
wrong root——四者都必須紅，因為它們的「空清單」與真綠不可區分。

以及**唯一實作**的釘子：repo-lint 第 35 條不得自己複製一份 AST／XML 邏輯，
判準是它載入的檔就是 sibling 的 `verify-cron-wiring.py`。

跑法：
    python3 scripts/audit/test_verify_cron_wiring.py
    # 或 python3 -m pytest scripts/audit/test_verify_cron_wiring.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。fixture 一律
`tempfile.mkdtemp()` 現造。
"""
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = str(_HERE.parents[1])
_IMPL_PATH = _HERE / "verify-cron-wiring.py"
_GATE_PATH = _HERE / "repo-lint.py"


def _load(path, name):
    """檔名帶連字號 import 不進來 ⇒ 走 spec_from_file_location（同 repo 慣例）。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vc = _load(_IMPL_PATH, "verify_cron_wiring_ut")
rl = _load(_GATE_PATH, "repo_lint_cron_wiring_ut")


# ------------------------------------------------------------ fixture

_MODEL_MAIN = """from odoo import models


class Probe(models.Model):
    _name = 'woow_probe.thing'

    def _cron_do_the_thing(self):
        return True
"""

_MODEL_OTHER = """from odoo import models


class Other(models.Model):
    _name = 'woow_probe.other'

    def _cron_something_else(self):
        return True
"""


def _cron_xml(ref, code, xmlid="ir_cron_probe"):
    return (
        '<odoo>\n'
        '  <record id="%s" model="ir.cron">\n'
        '    <field name="name">probe</field>\n'
        '    <field name="model_id" ref="%s"/>\n'
        '    <field name="state">code</field>\n'
        '    <field name="code">%s</field>\n'
        '  </record>\n'
        '</odoo>\n' % (xmlid, ref, code))


class _FixtureRoot(object):
    """一棵最小的合成 addon 樹：`src/__manifest__.py` ＋ models ＋ data。"""

    def __init__(self, with_models=True):
        self.dir = tempfile.mkdtemp(prefix="cron-wiring-gate-")
        self.write("src/__manifest__.py", "{'name': 'probe-960'}\n")
        if with_models:
            self.write("src/models/probe.py", _MODEL_MAIN)
            self.write("src/models/other.py", _MODEL_OTHER)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def cron(self, ref, code, name="probe_cron.xml", xmlid="ir_cron_probe"):
        self.write("src/data/" + name, _cron_xml(ref, code, xmlid))
        return self

    def gate(self):
        """走 repo-lint 第 35 條（＝ CI 與 hook 真正呼叫的那條路）。"""
        return rl.check_cron_wiring(self.dir)

    def impl(self, verbose=False):
        """走實作本身，回 `(掃到幾筆, 問題清單)`。"""
        return vc.check(verbose=verbose, root=self.dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


# ------------------------------------------------------------ A–E

class TestInjectedRedControl(unittest.TestCase):
    """🔴 先證明它會紅——否則後面每一句「綠」都沒有鑑別力。"""

    def test_00_a_typo_in_the_method_name_is_blocked(self):
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_thing", "model._cron_do_the_thng()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        message = violations[0]
        # 列名，不是數量：是哪一個檔、哪一個 xmlid、哪一個 method。
        self.assertIn("probe_cron.xml :: ir_cron_probe", message)
        self.assertIn("_cron_do_the_thng", message)
        self.assertIn("woow_probe.thing", message)
        # 訊息要能把人帶到判準的出處。
        self.assertIn(rl.CRON_WIRING_DOC, message)

    def test_00_the_gate_is_wired_into_run_all_and_the_registry(self):
        """紅得再漂亮，沒掛進 run_all／登記表也等於沒有這條 gate。"""
        self.assertIn("cron-wiring", rl.REPO_LINT_RULES)
        self.assertEqual(rl.REPO_LINT_RULES["cron-wiring"],
                         ("check_cron_wiring",))


class TestModelIdRef(unittest.TestCase):
    def test_ref_to_another_existing_model_is_blocked(self):
        """B 格：ref 有效、XML 載得起來，只有 method 不在那個 model 上會露餡。"""
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_other", "model._cron_do_the_thing()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        message = violations[0]
        self.assertIn("probe_cron.xml :: ir_cron_probe", message)
        self.assertIn("woow_probe.other", message)
        self.assertIn("_cron_do_the_thing", message)

    def test_ref_to_a_missing_model_is_blocked(self):
        """C 格。"""
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_ghost", "model._cron_do_the_thing()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("probe_cron.xml :: ir_cron_probe", violations[0])
        self.assertIn("model_woow_probe_ghost", violations[0])

    def test_code_that_is_not_a_method_call_is_named_not_skipped(self):
        """不是 `model.<method>()` 形狀 ⇒ 列為「無法驗」，不是靜默放過。"""
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_thing", "env['woow_probe.thing'].go()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("probe_cron.xml :: ir_cron_probe", violations[0])
        self.assertIn("無法驗", violations[0])


_MODEL_EXTENSION = """from odoo import models


class ThingExtension(models.Model):
    _inherit = 'woow_probe.thing'

    def _cron_from_the_extension(self):
        return True


class ForeignExtension(models.Model):
    _inherit = 'project.project'

    def _cron_on_a_foreign_model(self):
        return True
"""


class TestInPlaceExtension(unittest.TestCase):
    """#1680：`_inherit` 單一字串、沒有 `_name` 的就地擴充，method 屬於被擴充的 model。"""

    def test_method_defined_on_an_in_place_extension_is_wired(self):
        with _FixtureRoot() as fx:
            # 檔名排在 probe.py 前面：併入不能依賴掃描順序。
            fx.write("src/models/a_extension.py", _MODEL_EXTENSION)
            fx.cron("model_woow_probe_thing", "model._cron_from_the_extension()")
            self.assertEqual(fx.gate(), [])

    def test_extension_methods_do_not_leak_to_other_models(self):
        with _FixtureRoot() as fx:
            fx.write("src/models/a_extension.py", _MODEL_EXTENSION)
            fx.cron("model_woow_probe_other", "model._cron_from_the_extension()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("woow_probe.other", violations[0])

    def test_foreign_extension_does_not_invent_a_model(self):
        with _FixtureRoot() as fx:
            fx.write("src/models/a_extension.py", _MODEL_EXTENSION)
            fx.cron("model_project_project", "model._cron_on_a_foreign_model()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("model_project_project", violations[0])


class TestCorrectWiring(unittest.TestCase):
    def test_correct_wiring_is_green(self):
        """D 格。"""
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_thing", "model._cron_do_the_thing()")
            self.assertEqual(fx.gate(), [])
            self.assertEqual(fx.impl(), (1, []))

    def test_cross_module_ref_is_skipped_and_named(self):
        """E 格：跨模組 ref 略過並列名——略過**不等於**沒掃到，checked 仍然計數。"""
        with _FixtureRoot() as fx:
            fx.cron("project.model_project_project",
                    "model._cron_reach_reconciler()")
            self.assertEqual(fx.gate(), [])
            checked, problems = fx.impl()
            self.assertEqual((checked, problems), (1, []))
            # 「列名」在獨立跑的那條路上是印出來的，這裡把它釘住。
            stdout, sys.stdout = sys.stdout, io.StringIO()
            try:
                vc.check(verbose=True, root=fx.dir)
                printed = sys.stdout.getvalue()
            finally:
                sys.stdout = stdout
        self.assertIn("probe_cron.xml :: ir_cron_probe", printed)
        self.assertIn("project.model_project_project", printed)


# ------------------------------------------------------------ 探針壞掉的四種形態

class TestNothingScannedIsNotGreen(unittest.TestCase):
    """「什麼都沒查到」的四種形態，全部必須紅。"""

    def test_zero_cron_records_is_a_violation(self):
        with _FixtureRoot() as fx:
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("一筆 `ir.cron` record 都沒掃到", violations[0])

    def test_missing_implementation_file_is_a_violation(self):
        """實作被搬走／改名 ⇒ 紅，不是靜默回空清單。"""
        self._with_impl_path(os.path.join(tempfile.gettempdir(),
                                          "no-such-verify-cron-wiring.py"))
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_thing", "model._cron_do_the_thing()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("scripts/audit/verify-cron-wiring.py", violations[0])

    def test_broken_implementation_file_is_a_violation(self):
        """實作 parse 不了 ⇒ 紅（例外被讀成零違規就是假綠）。"""
        broken = tempfile.mkdtemp(prefix="cron-wiring-broken-")
        self.addCleanup(shutil.rmtree, broken, ignore_errors=True)
        path = os.path.join(broken, "verify-cron-wiring.py")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("def check(  # 故意壞掉\n")
        self._with_impl_path(path)
        with _FixtureRoot() as fx:
            fx.cron("model_woow_probe_thing", "model._cron_do_the_thing()")
            violations = fx.gate()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("載入", violations[0])

    def test_wrong_root_fails_loud_and_names_itself(self):
        """`--all` 語意下 wrong root 要點名自己（hook 語意的安靜由 #936 那支守）。"""
        root = tempfile.mkdtemp(prefix="cron-wiring-wrong-root-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        original, rl._HOOK_MODE = rl._HOOK_MODE, False
        try:
            violations = rl.check_cron_wiring(root)
        finally:
            rl._HOOK_MODE = original
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("check_cron_wiring", violations[0])
        self.assertIn("不能解讀為零違規", violations[0])

    def _with_impl_path(self, path):
        """暫時把載入路徑換掉，並在 cleanup 時連快取一起還原。"""
        old_path, old_mod = rl._CRON_WIRING_IMPL_PATH, rl._CRON_WIRING_IMPL

        def restore():
            rl._CRON_WIRING_IMPL_PATH = old_path
            rl._CRON_WIRING_IMPL = old_mod

        self.addCleanup(restore)
        rl._CRON_WIRING_IMPL_PATH = path
        rl._CRON_WIRING_IMPL = None


# ------------------------------------------------------------ 單一真相源

class TestSingleImplementation(unittest.TestCase):
    """⛔ 兩份實作必然漂移，而漂移那天兩邊都是綠的。"""

    def test_gate_loads_the_sibling_script(self):
        self.assertEqual(os.path.realpath(rl._CRON_WIRING_IMPL_PATH),
                         os.path.realpath(str(_IMPL_PATH)))
        module = rl._cron_wiring_impl()
        for name in ("check", "python_models", "cron_records"):
            self.assertTrue(hasattr(module, name),
                            "實作缺 %s()——轉接層與實作已經分岔" % name)

    def test_repo_lint_does_not_re_implement_the_parsing(self):
        """repo-lint 本體不得出現實作側的判準片段（＝有人把實作複製了一份）。

        判準挑的是**只有複製才會帶過去**的兩段逐字文字（`CODE_CALL` 的 regex、
        record 過濾式），不是「有沒有提到 ir.cron」——後者一句文件散文就會誤報。
        """
        source = _GATE_PATH.read_text(encoding="utf-8")
        for fragment in ("(?P<meth>", "rec.get('model')"):
            self.assertNotIn(
                fragment, source,
                "repo-lint.py 裡出現實作側的 %r ⇒ 判準被複製成第二份，"
                "兩份實作漂移的那天兩邊都會是綠的" % fragment)
            self.assertIn(fragment, _IMPL_PATH.read_text(encoding="utf-8"),
                          "%r 不在實作裡 ⇒ 本條的「不存在」沒有鑑別力" % fragment)


# ------------------------------------------------------------ hook 觸發面

class TestHookWatchSurface(unittest.TestCase):
    """兩端都要接住：改 XML 與改 model .py 都會打破這條不變式。"""

    def test_every_file_the_implementation_scans_is_watched(self):
        """資源側枚舉：實作真的會讀的每一個檔，hook 都要接得住。

        寫死兩個檔名的版本會在那兩個檔改名的當天誤紅，而且照不到「掃描範圍長出
        第三個 glob、觸發面卻沒跟上」——後者才是會讓 hook 對整類編輯靜默的那一種。
        """
        scanned = []
        for sub, pattern in (("data", "*.xml"), ("models", "*.py")):
            for path in sorted((pathlib.Path(_REPO_ROOT) / "src" / sub)
                               .glob(pattern)):
                scanned.append("src/%s/%s" % (sub, path.name))
        self.assertTrue(scanned, "枚舉不到任何被掃描的檔 ⇒ 本條的綠是空集合上的恆真")
        for end in ("src/data/", "src/models/"):
            self.assertTrue(
                any(rel.startswith(end) for rel in scanned),
                "%s 這一端一個檔都沒枚舉到 ⇒ 「兩端都接住」只驗了其中一端" % end)
        missed = [rel for rel in scanned if not rl._cron_wiring_watches(rel)]
        self.assertEqual(
            missed, [],
            "以下檔在實作的掃描範圍內、卻不在 hook 觸發面上 ⇒ 改到它們時 CI 會抓、"
            "hook 完全沉默（#596 的形態）：%r" % missed)

    def test_surface_matches_the_implementation_globs(self):
        """實作的兩個 glob 都是**非遞迴**的；觸發面寬於它就是白跑。"""
        for rel in ("src/data/nested/x.xml", "src/models/nested/x.py",
                    "src/static/src/paas/app.js", "src/models/x.xml",
                    "docs/index.md", "src/data/x.csv"):
            self.assertFalse(rl._cron_wiring_watches(rel), rel)


# ------------------------------------------------------------ 向後相容

class TestStandaloneBehaviourUnchanged(unittest.TestCase):
    """`root=None` 必須維持「相對本檔往上兩層」的既有行為。"""

    def test_root_none_still_reads_the_real_repo(self):
        self.assertEqual(str(vc.REPO_ROOT), _REPO_ROOT)
        self.assertEqual(str(vc._src_dir()), os.path.join(_REPO_ROOT, "src"))
        self.assertTrue(vc.python_models())
        self.assertTrue(vc.cron_records())

    def test_explicit_root_wins(self):
        with _FixtureRoot() as fx:
            self.assertEqual(str(vc._src_dir(fx.dir)),
                             os.path.join(fx.dir, "src"))
            self.assertEqual(sorted(vc.python_models(fx.dir)),
                             ["woow_probe.other", "woow_probe.thing"])


class TestCurrentTree(unittest.TestCase):
    def test_real_repo_is_green_and_actually_scanned_something(self):
        self.assertEqual(rl.check_cron_wiring(_REPO_ROOT), [])
        checked, problems = vc.check(verbose=False, root=_REPO_ROOT)
        self.assertEqual(problems, [])
        self.assertGreater(checked, 0, "掃到零筆 ⇒ 綠燈沒有鑑別力")


if __name__ == "__main__":
    unittest.main()
