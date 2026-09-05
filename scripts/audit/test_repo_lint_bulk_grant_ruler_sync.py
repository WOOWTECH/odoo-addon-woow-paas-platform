#!/usr/bin/env python3
"""第 37 條 bulk-grant-ruler-sync 的 injected-red 測試（#521 P-J）。

第一個測試刻意把前後端兩份清單注入成不一致並要求**紅燈**。這是一條否定性 gate
（「找不到不一致」），若抽取器壞掉而永遠回空集合／永遠回相等，真 repo 的綠燈與
故障輸出完全相同 —— 所以不能拿「現樹是綠的」當驗收。

實撞過的那顆探針故障就在 `test_js_values_are_actually_parsed`：第一版實作用了既有
的 `_strip_js_noise`，那支連 quoted string 一起遮掉 ⇒ JS 側恆為空集合。它當時**看
起來是一條抓到東西的紅燈**（空集合 ≠ 後端那份），而實際上 gate 已經瞎了：任何前端
內容都會紅，包括正確的那一份。

跑法：
    python3 scripts/audit/test_repo_lint_bulk_grant_ruler_sync.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_bulk_grant_ruler_sync.py

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

_spec = importlib.util.spec_from_file_location("repo_lint_bulk_grant_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)

#: rule 子 model 的合法值域（fixture 用；與真檔同形，不抄真檔內容）
_RULE_MODEL_SRC = '''
class AccessPolicyRule(models.Model):
    _name = 'woow_paas_platform.security_access_access_policy_rule'
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
            ('everyone', 'Everyone'),
            ('country', 'Country'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
'''


def _py_src(types_literal):
    return (
        "class AccessPolicy(models.Model):\n"
        "    _BULK_GRANT_RULE_TYPES = %s\n" % types_literal
    )


def _js_src(body, extra=""):
    return (
        'import { Component } from "@odoo/owl";\n'
        "const BULK_GRANT_RULE_TYPES = [%s];\n%s"
        'export class AddPolicyModal extends Component {}\n' % (body, extra)
    )


class _FixtureRoot:
    """合成 addon 樹：只放這條 gate 讀的三個檔 ＋ manifest（root 判準）。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="bulk-grant-ruler-sync-")
        self.write("src/__manifest__.py", "{'name': 'probe'}\n")

    def write(self, rel, source):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        return path

    def seed(self, py_types="('email_domain', 'everyone', 'ip_range', 'country')",
             js_body='"email_domain", "everyone", "ip_range", "country"',
             js_extra="", rule_model=_RULE_MODEL_SRC):
        self.write(rl._BULK_GRANT_PY_REL, _py_src(py_types))
        self.write(rl._BULK_GRANT_JS_REL, _js_src(js_body, js_extra))
        self.write(rl._BULK_GRANT_RULE_MODEL_REL, rule_model)
        return self

    def run(self):
        return rl.check_bulk_grant_ruler_sync(self.dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class TestInjectedRed(unittest.TestCase):
    """先證明它紅得起來，再談綠燈代表什麼。"""

    def test_drift_is_caught_and_names_both_sides(self):
        with _FixtureRoot() as fx:
            fx.seed(js_body='"email_domain", "everyone"')
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        msg = violations[0]
        self.assertIn("py-only=['country', 'ip_range']", msg, msg)
        self.assertIn("js-only=[]", msg, msg)

    def test_frontend_only_drift_is_caught(self):
        """反方向同樣要紅：只在前端擋 = 守衛沒擋，打 API 就進得去。"""
        with _FixtureRoot() as fx:
            fx.seed(py_types="('email_domain', 'everyone')")
            violations = fx.run()
        self.assertEqual(len(violations), 1, violations)
        self.assertIn("js-only=['country', 'ip_range']", violations[0])


class TestProbeHealth(unittest.TestCase):
    """探針自身的故障形態——每一格都實際發生過或差一點發生。"""

    def test_js_values_are_actually_parsed(self):
        """🔴 實撞過的假紅：用 `_strip_js_noise` 會把字串一起遮掉 ⇒ 恆空集合。

        直接對抽取器斷言，不透過 `check_*` —— 因為「恆空」在 check 的輸出上長得
        像一條抓到東西的紅燈。
        """
        with _FixtureRoot() as fx:
            fx.seed()
            got, err = rl._bulk_grant_js_types(fx.dir)
        self.assertIsNone(err, err)
        self.assertEqual(
            got, {"email_domain", "everyone", "ip_range", "country"},
            "JS 側抽取器必須讀得到字串內容；空集合＝探針瞎了，不是清單真的空")

    def test_matched_lists_are_green(self):
        with _FixtureRoot() as fx:
            fx.seed()
            self.assertEqual(fx.run(), [])

    def test_missing_js_anchor_is_a_violation_not_a_pass(self):
        with _FixtureRoot() as fx:
            fx.seed()
            fx.write(rl._BULK_GRANT_JS_REL,
                     'const SOMETHING_ELSE = ["email_domain"];\n')
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("找不到 `const BULK_GRANT_RULE_TYPES", violations[0])

    def test_missing_py_anchor_is_a_violation_not_a_pass(self):
        with _FixtureRoot() as fx:
            fx.seed()
            fx.write(rl._BULK_GRANT_PY_REL, "class AccessPolicy:\n    pass\n")
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("找不到 `_BULK_GRANT_RULE_TYPES` 的賦值", violations[0])

    def test_py_rewritten_as_computed_is_a_violation(self):
        """改成 computed 之後判準抓不到字面量 ⇒ 必須紅，不能靜默失去鑑別力。"""
        with _FixtureRoot() as fx:
            fx.seed(py_types="tuple(_load_bulk_types())")
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("不再是字面 tuple", violations[0])

    def test_second_js_copy_is_a_violation(self):
        with _FixtureRoot() as fx:
            fx.seed(js_extra='const BULK_GRANT_RULE_TYPES = ["everyone"];\n')
            violations = fx.run()
        self.assertTrue(violations)
        self.assertIn("有 2 份", violations[0])

    def test_commented_out_copy_is_not_counted(self):
        """註解掉的舊清單不算第二份——否則這條 gate 會擋住正當的 code 註解。"""
        with _FixtureRoot() as fx:
            fx.seed(js_extra='// const BULK_GRANT_RULE_TYPES = ["everyone"];\n'
                             '/* const BULK_GRANT_RULE_TYPES = ["country"]; */\n')
            self.assertEqual(fx.run(), [])

    def test_typo_type_outside_selection_domain_is_caught(self):
        """打錯字的型別在 `in` 比對下永遠不命中，兩側同時靜默失效。"""
        with _FixtureRoot() as fx:
            fx.seed(py_types="('email_domain', 'everyone', 'ip_ranges')",
                    js_body='"email_domain", "everyone", "ip_ranges"')
            violations = fx.run()
        self.assertTrue(violations)
        self.assertTrue(any("不存在的 rule_type ['ip_ranges']" in v
                            for v in violations), violations)

    def test_wrong_root_fails_loud_in_all_mode(self):
        """非 addon 樹回空清單會與「兩份一致」不可區分（#828／#936）。"""
        tmp = tempfile.mkdtemp(prefix="bulk-grant-wrong-root-")
        try:
            self.assertTrue(rl.check_bulk_grant_ruler_sync(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRealTree(unittest.TestCase):
    """真 repo 必須綠——放在最後，因為它的意義完全建立在上面那些紅燈之上。"""

    def test_real_repo_is_in_sync(self):
        self.assertEqual(rl.check_bulk_grant_ruler_sync(str(_REPO_ROOT)), [])

    def test_hook_watches_both_ends(self):
        for rel in (rl._BULK_GRANT_PY_REL, rl._BULK_GRANT_JS_REL,
                    rl._BULK_GRANT_RULE_MODEL_REL):
            self.assertTrue(rl._bulk_grant_ruler_watches(rel), rel)
        self.assertFalse(rl._bulk_grant_ruler_watches("src/models/other.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
