#!/usr/bin/env python3
"""#524 ③ gate 的 A/B 注入測試。

這支測試存在的理由跟被測的 gate 一樣：**「現在是綠的」不是驗收**。
一道從沒被看過會失敗的 gate，與一道什麼都沒檢查的 gate，輸出長得一模一樣。
所以下面每一條都在問：**改壞它，會不會紅、紅得對不對、有沒有點名是哪一個**。

注入素材是**真實的 `src/security/*` 與 `src/models/*`**（複製到 tmp 目錄後再改），
不是手寫的假 fixture——假 fixture 只證明 parser 在自己捏的形狀上能動。

特別覆蓋兩個**假綠**來源（issue #524 留言 4640 點名的那兩個）：

1. **只綁 `base.group_system` 的 rule** 對 `base.group_user` 完全不生效
   ⇒ 「至少有一條 ir.rule」這個判準會綠，但洞是開的。
2. **`primary_workspace_id` 的子字串誤命中**（`agent_session`）
   ⇒ 用 grep 寫的 gate 會把它拉進範圍，判準會在錯的集合上執行。

跑法：
    python3 scripts/audit/test_workspace_scoped_rule_gate.py
    # 或 python3 -m pytest scripts/audit/test_workspace_scoped_rule_gate.py
"""
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workspace_scoped_rule_gate as gate  # noqa: E402

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


class _Repo:
    """把真實 repo 的相關子樹複製到 tmp，讓每個測試各自突變自己那份。"""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix='ws-rule-gate-')
        src = os.path.join(self.tmp, 'src')
        os.makedirs(src)
        for sub in ('models', 'wizard', 'controllers', 'services', 'security'):
            real = os.path.join(REPO_ROOT, 'src', sub)
            if os.path.isdir(real):
                shutil.copytree(real, os.path.join(src, sub))
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 讀寫 helper ---------------------------------------------------- #
    def path(self, rel):
        return os.path.join(self.tmp, rel)

    def read(self, rel):
        with open(self.path(rel), encoding='utf-8') as fh:
            return fh.read()

    def write(self, rel, text):
        with open(self.path(rel), 'w', encoding='utf-8') as fh:
            fh.write(text)

    def run(self, strict=False):
        buf = io.StringIO()
        code = gate.run(self.tmp, strict=strict, out=buf)
        return code, buf.getvalue()


RULES = 'src/security/ir_rules.xml'
ACL = 'src/security/ir.model.access.csv'
SMART_HOME = 'woow_paas_platform.smart_home'


def _drop_rule(xml, xmlid):
    m = re.search(r'        <record id="%s" model="ir\.rule">.*?</record>\n\n?'
                  % re.escape(xmlid), xml, re.S)
    assert m, 'fixture 前提：找不到 rule %s' % xmlid
    return xml[:m.start()] + xml[m.end():]


class TestBaselineIsGreen(unittest.TestCase):
    """陽性對照：未突變的 repo 必須通過。

    沒有這一條，下面每一條紅燈都可能只是「gate 對任何輸入都紅」。
    """

    def test_unmutated_repo_passes(self):
        with _Repo() as repo:
            code, out = repo.run()
        self.assertEqual(code, 0, out)
        self.assertIn('✅ 通過', out)

    def test_smart_home_is_actually_in_scope(self):
        """鑑別力前提：受測 model 真的落在檢查範圍內。

        沒有這一條，「移除 rule 卻沒紅」與「這個 model 根本沒被檢查」分不出來。
        """
        with _Repo() as repo:
            _code, out = repo.run()
        self.assertIn(SMART_HOME, out.split('已知盲區')[0])


class TestMissingRuleIsRed(unittest.TestCase):
    """注入①：把 smart_home 的兩條 rule 整組移除 —— 這就是 #524 的原始狀態。"""

    def test_removing_all_rules_fails_and_names_the_model(self):
        with _Repo() as repo:
            xml = repo.read(RULES)
            for xmlid in ('smart_home_rule_member_read',
                          'smart_home_rule_owner_admin_manage'):
                xml = _drop_rule(xml, xmlid)
            repo.write(RULES, xml)
            code, out = repo.run()
        self.assertEqual(code, 1, out)
        self.assertIn('❌ 違規', out)
        self.assertIn(SMART_HOME, out.split('❌ 違規')[1])
        self.assertIn('一條 rule 都沒有', out)

    def test_other_models_are_not_collaterally_flagged(self):
        """紅得對不對：只該點名 smart_home，不該連坐 cloud_service。"""
        with _Repo() as repo:
            xml = repo.read(RULES)
            for xmlid in ('smart_home_rule_member_read',
                          'smart_home_rule_owner_admin_manage'):
                xml = _drop_rule(xml, xmlid)
            repo.write(RULES, xml)
            _code, out = repo.run()
        offending = out.split('❌ 違規')[1]
        self.assertNotIn('cloud_service', offending)
        self.assertNotIn('security_access', offending)


class TestGroupSystemOnlyRuleIsRed(unittest.TestCase):
    """注入②：rule 仍在，但只綁 `base.group_system`。

    🔴 這一條是本檔最重要的：它是「至少有一條 ir.rule」那個假綠判準的鑑別項。
    Odoo core `ir_rule.py::_get_rules` 只選 global 或使用者所屬 group 的 rule
    ⇒ 只綁 group_system 的 rule 對 `base.group_user` 零作用，model 上卻「有 rule」。
    """

    def _mutate(self, repo, group_from, group_to):
        out_lines, inside, changed = [], False, 0
        for ln in repo.read(RULES).split('\n'):
            if '<record id="smart_home_rule_' in ln:
                inside = True
            if inside and ("ref('%s')" % group_from) in ln:
                ln = ln.replace(group_from, group_to)
                changed += 1
            if inside and '</record>' in ln:
                inside = False
            out_lines.append(ln)
        assert changed == 2, 'fixture 前提：應該改到兩條，實際 %d' % changed
        repo.write(RULES, '\n'.join(out_lines))

    def test_rules_bound_only_to_group_system_do_not_count(self):
        with _Repo() as repo:
            self._mutate(repo, 'base.group_user', 'base.group_system')
            code, out = repo.run()
        self.assertEqual(code, 1, out)
        self.assertIn('沒有一條對 Internal User 生效', out)
        # 點名到底是哪兩條，否則修的人要自己找
        self.assertIn('smart_home_rule_member_read', out)
        self.assertIn('smart_home_rule_owner_admin_manage', out)

    def test_inactive_rules_do_not_count(self):
        """注入③：rule 綁對群組，但 `active="False"`。"""
        with _Repo() as repo:
            xml = repo.read(RULES)
            xml = xml.replace(
                '<record id="smart_home_rule_member_read" model="ir.rule">\n'
                '            <field name="name">SmartHome: workspace member read'
                '</field>',
                '<record id="smart_home_rule_member_read" model="ir.rule">\n'
                '            <field name="active" eval="False"/>\n'
                '            <field name="name">SmartHome: workspace member read'
                '</field>')
            xml = _drop_rule(xml, 'smart_home_rule_owner_admin_manage')
            repo.write(RULES, xml)
            code, out = repo.run()
        self.assertEqual(code, 1, out)
        self.assertIn('沒有一條對 Internal User 生效', out)


class TestExactFieldNameMatching(unittest.TestCase):
    """`primary_workspace_id` 不得被當成 `workspace_id`（grep 會，AST 不會）。"""

    def test_agent_session_is_not_in_scope(self):
        with _Repo() as repo:
            _code, out = repo.run()
        scope_line = [ln for ln in out.split('\n')
                      if ln.startswith('涵蓋範圍內的 model')][0]
        self.assertNotIn('agent_session', scope_line)

    def test_the_repo_really_declares_the_decoy(self):
        """鑑別力前提：誘餌必須真的存在，否則上一條零資訊。"""
        with _Repo() as repo:
            models = gate.collect_models([
                os.path.join(dp, f)
                for dp, _dn, fns in os.walk(repo.path('src/models'))
                for f in fns if f.endswith('.py')])
        fields = gate.resolve_inherited_fields(models)
        agent = fields.get('woow_paas_platform.agent_session', set())
        self.assertIn('primary_workspace_id', agent,
                      '誘餌欄位不在了 —— 上一條測試已失去鑑別力，請改用新誘餌')
        self.assertNotIn('workspace_id', agent)


class TestAclGateInteraction(unittest.TestCase):
    """ACL 沒對 `base.group_user` 開的 model 不該被判違規。"""

    def test_model_without_internal_user_acl_is_listed_not_flagged(self):
        with _Repo() as repo:
            rows = repo.read(ACL).split('\n')
            rows = [r for r in rows
                    if not r.startswith('access_smart_home_user,')]
            repo.write(ACL, '\n'.join(rows))
            xml = repo.read(RULES)
            for xmlid in ('smart_home_rule_member_read',
                          'smart_home_rule_owner_admin_manage'):
                xml = _drop_rule(xml, xmlid)
            repo.write(RULES, xml)
            code, out = repo.run()
        self.assertEqual(code, 0, out)
        self.assertIn('有 workspace_id 但本 repo 的 ACL 未對 base.group_user 開放',
                      out)
        self.assertIn(SMART_HOME, out.split('未對 base.group_user 開放')[1])


class TestGlobalAccessRowIsInScope(unittest.TestCase):
    """`group_id` 為空 ＝ global access，**比 base.group_user 更寬**，必須被檢查。

    PR #738 覆核 round1 MEDIUM：這類列原本被 `continue` 整個跳過 ⇒ 有人把某個
    workspace-scoped model 的 ACL 列 group 留空、又沒配 rule，這道專門防 #524
    重演的 gate 會回綠燈，而洞比 #524 更大。
    """

    def _make_global_row(self, repo, drop_rules):
        rows = repo.read(ACL).split('\n')
        out = []
        replaced = 0
        for r in rows:
            if r.startswith('access_smart_home_user,'):
                parts = r.split(',')
                self.assertEqual(parts[3], 'base.group_user',
                                 'fixture 前提：第 4 欄應該是 group_id')
                parts[3] = ''            # ← group 留空 = global access
                r = ','.join(parts)
                replaced += 1
            out.append(r)
        self.assertEqual(replaced, 1, 'fixture 前提：應該只改到那一列')
        repo.write(ACL, '\n'.join(out))
        if drop_rules:
            xml = repo.read(RULES)
            for xmlid in ('smart_home_rule_member_read',
                          'smart_home_rule_owner_admin_manage'):
                xml = _drop_rule(xml, xmlid)
            repo.write(RULES, xml)

    def test_global_row_without_any_rule_is_flagged(self):
        """🔴 這一格就是修復前會漏掉的那個：更寬的授權 + 零 rule ⇒ 必須紅。"""
        with _Repo() as repo:
            self._make_global_row(repo, drop_rules=True)
            code, out = repo.run()
        self.assertEqual(code, 1, out)
        self.assertIn(SMART_HOME, out.split('❌ 違規')[1])

    def test_global_row_is_named_in_the_output(self):
        """有 rule 時仍要**點名**它是 global access——判準只到 Internal User。"""
        with _Repo() as repo:
            self._make_global_row(repo, drop_rules=False)
            code, out = repo.run()
        self.assertEqual(code, 0, out)
        self.assertIn('group_id 為空', out)
        self.assertIn(SMART_HOME, out.split('group_id 為空')[1])

    def test_baseline_has_no_global_row(self):
        """鑑別力前提：未突變的 repo 不該有 global 列，否則上兩條零資訊。"""
        with _Repo() as repo:
            _code, out = repo.run()
        self.assertNotIn('group_id 為空', out)


class TestBlindSpotsAreAlwaysPrinted(unittest.TestCase):
    """盲區必須出現在**gate 自己的輸出**裡，紅綠皆然。

    只寫在文件裡等於沒聲明——看到綠燈的人不會去翻文件。
    """

    def test_blind_spots_printed_on_green(self):
        with _Repo() as repo:
            code, out = repo.run()
        self.assertEqual(code, 0)
        self.assertIn('已知盲區', out)
        self.assertIn('change_plan_wizard', out)

    def test_blind_spots_printed_on_red(self):
        with _Repo() as repo:
            xml = repo.read(RULES)
            for xmlid in ('smart_home_rule_member_read',
                          'smart_home_rule_owner_admin_manage'):
                xml = _drop_rule(xml, xmlid)
            repo.write(RULES, xml)
            code, out = repo.run()
        self.assertEqual(code, 1)
        self.assertIn('已知盲區', out)
        self.assertIn('change_plan_wizard', out)


class TestSecondRuleSourceFailsLoud(unittest.TestCase):
    """盲區③不得靜默擴大：多一個 ir.rule 來源檔就 exit 2。"""

    def test_extra_ir_rule_file_returns_2(self):
        with _Repo() as repo:
            repo.write('src/security/extra_rules.xml',
                       '<odoo><data><record id="x" model="ir.rule"/>'
                       '</data></odoo>')
            code, out = repo.run()
        self.assertEqual(code, 2, out)
        self.assertIn('GATE 前提被打破', out)


class TestDirectionAdvisory(unittest.TestCase):
    """逐方向覆蓋：預設 advisory、`--strict` 阻擋。"""

    def test_direction_gap_is_advisory_by_default(self):
        with _Repo() as repo:
            code, out = repo.run()
        self.assertEqual(code, 0, out)
        self.assertIn('advisory', out)

    def test_strict_promotes_direction_gap_to_failure(self):
        with _Repo() as repo:
            code, out = repo.run(strict=True)
        self.assertEqual(code, 1, out)
        self.assertIn('--strict', out)

    def test_direction_gap_detects_a_freshly_injected_one(self):
        """把 owner/admin 那條的 `perm_write` 關掉 ⇒ write 方向失去列過濾。"""
        with _Repo() as repo:
            xml = repo.read(RULES)
            head, sep, tail = xml.partition(
                '<record id="smart_home_rule_owner_admin_manage"')
            tail = tail.replace('<field name="perm_write" eval="True"/>',
                                '<field name="perm_write" eval="False"/>', 1)
            repo.write(RULES, head + sep + tail)
            code, out = repo.run()
        self.assertEqual(code, 0, out)          # 主判準仍過（member_read 還在）
        self.assertIn('%s：ACL 給了 perm_write' % SMART_HOME, out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
