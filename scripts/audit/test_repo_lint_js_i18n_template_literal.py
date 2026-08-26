#!/usr/bin/env python3
"""JS template literal i18n gate 的 injected-red 測試（#904）。

第一個測試刻意先注入 ``${_t("x")}`` 並要求紅燈。這是一條否定性 gate；若 scanner
壞掉而永遠回空，真 repo 的綠燈與故障輸出完全相同，所以不能先用「現樹是綠的」當
驗收。其餘測試守住 nested template、escaped backtick、comments、regex literal 與
「先取翻譯值再插值」的不誤報對照。

跑法：
    python3 scripts/audit/test_repo_lint_js_i18n_template_literal.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_js_i18n_template_literal.py

不得依賴 Odoo、網路、特定 cwd 或 HOME；CI 由 repo-lint workflow 的 scripts/audit
``test_*.py`` glob 自動收集。
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

_spec = importlib.util.spec_from_file_location("repo_lint_js_i18n_template_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


class _FixtureRoot:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="js-i18n-template-gate-")

    def write(self, source, rel="src/static/src/paas/probe.js"):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return path

    def run(self, only_file=None):
        return rl.check_js_i18n_template_literal(self.dir, only_file=only_file)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _scan(source):
    with _FixtureRoot() as fx:
        fx.write(source)
        return fx.run()


class TestInjectedRedControl(unittest.TestCase):
    """第一個驗收必須先證明 scanner 真的會紅。"""

    def test_00_injected_template_expression_call_is_blocked(self):
        violations = _scan('const title = `Diagram ${_t("Zoom in")}`;\n')
        self.assertEqual(len(violations), 1)
        self.assertIn("probe.js:1", violations[0])
        self.assertIn("`${}`", violations[0])
        self.assertIn("#904", violations[0])

    def test_diagnostic_names_the_scope_boundary(self):
        violation = _scan('const title = `${_t("Zoom")}`;')[0]
        self.assertIn("只抓這個形態", violation)
        self.assertIn("regex-vs-division", violation)
        self.assertIn("真 Babel 當第二把尺", violation)


class TestTemplateStructure(unittest.TestCase):
    def test_nested_template_expression_is_blocked(self):
        source = 'const value = `outer ${`inner ${_t("Nested")}`}`;\n'
        violations = _scan(source)
        self.assertEqual(len(violations), 1)
        self.assertIn("probe.js:1", violations[0])

    def test_escaped_backtick_does_not_end_template(self):
        source = 'const value = `escaped \\` tick ${_t("Escaped")}`;\n'
        self.assertEqual(len(_scan(source)), 1)

    def test_nested_braces_do_not_end_expression(self):
        source = 'const value = `${({label: _t("Object")}).label}`;\n'
        self.assertEqual(len(_scan(source)), 1)

    def test_regex_literal_with_backtick_does_not_confuse_template_state(self):
        source = 'const value = `${/`/.test(input) ? _t("Tick") : fallback}`;\n'
        self.assertEqual(len(_scan(source)), 1)


class TestNonViolations(unittest.TestCase):
    def test_translate_before_interpolation_is_allowed(self):
        source = '''
const title = _t("Zoom in");
const markup = `<button title="${title}">${title}</button>`;
'''
        self.assertEqual(_scan(source), [])

    def test_comments_and_quoted_examples_are_ignored(self):
        source = '''
// bad example: const x = `${_t("comment")}`;
/* another example: `${_t("block")}` */
const x = `plain ${/* _t("inside-comment") */ value}`;
const y = `${"_t('inside-string')"}`;
'''
        self.assertEqual(_scan(source), [])

    def test_member_named_t_is_not_the_translation_function(self):
        self.assertEqual(_scan('const x = `${service._t("technical")}`;\n'), [])

    def test_third_party_lib_is_excluded(self):
        with _FixtureRoot() as fx:
            path = fx.write('const x = `${_t("vendor")}`;\n',
                            rel="src/static/src/paas/lib/vendor.js")
            self.assertEqual(fx.run(), [])
            self.assertEqual(fx.run(only_file=path), [])


class TestCurrentTree(unittest.TestCase):
    def test_real_repo_is_green(self):
        self.assertEqual(rl.check_js_i18n_template_literal(str(_REPO_ROOT)), [])


if __name__ == "__main__":
    unittest.main()
