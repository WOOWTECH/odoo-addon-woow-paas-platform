"""P-rule migration (18.0.1.0.39): flat policy rule → include sub-rule.

Simulates the pre-.39 DB shape (a policy carrying the orphan flat rule_type /
rule_value columns and NO rule rows) and drives migrate(), asserting each flat
policy gains one group='include' rule with the migrated selector, the decision
is preserved, and re-runs are idempotent.
"""
from odoo.tests.common import TransactionCase


def _migrate_fn():
    import importlib.util
    import os
    here = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(here, 'migrations', '18.0.1.0.39', 'post-migrate.py')
    spec = importlib.util.spec_from_file_location('p_rule_post_migrate', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class TestPRuleMigration(TransactionCase):
    _POL_TABLE = 'woow_paas_platform_security_access_access_policy'
    _RULE_TABLE = 'woow_paas_platform_security_access_access_policy_rule'

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'PRM WS'})
        self.migrate = _migrate_fn()

    def _add_legacy_flat_cols(self):
        """Re-create the orphan flat columns an upgraded DB still carries."""
        self.env.cr.execute(
            "ALTER TABLE %s "
            "ADD COLUMN IF NOT EXISTS rule_type varchar, "
            "ADD COLUMN IF NOT EXISTS rule_value varchar" % self._POL_TABLE)

    def _drop_legacy_flat_cols(self):
        self.env.cr.execute(
            "ALTER TABLE %s "
            "DROP COLUMN IF EXISTS rule_type, "
            "DROP COLUMN IF EXISTS rule_value" % self._POL_TABLE)

    #: 建 policy 時用來滿足 at-least-one-include 的**鷹架** rule，之後整批被
    #: raw SQL 刪掉。刻意寫死成安全型別（不吃 rule_type 參數）——見
    #: _make_legacy_policy 的說明。
    _SCAFFOLD_RULE = {'group': 'include', 'rule_type': 'ip_range',
                      'rule_value': '10.0.0.0/8'}

    def _make_legacy_policy(self, decision, rule_type, rule_value, prefix):
        """Create a policy, then force it into the pre-.39 shape: flat columns
        set + its rule rows removed (raw SQL — an ORM unlink of the last include
        would trip the new constraint).

        鷹架 rule 固定用 ip_range，**不**用參數的 rule_type（#543）：policy 一定
        要帶至少一條 include 才建得起來，但那條 rule 建完立刻被下面的 DELETE 清
        掉，migrate() 只讀 flat 欄位（post-migrate.py 的 SELECT 走 raw SQL，完全
        不看 rule 表），所以鷹架的型別對受測程式碼**零影響**。

        反過來，若鷹架照抄參數，decision='allow' × rule_type='email_domain' 這組
        會在 ORM create 當場撞上 _check_allow_policy_no_bulk_grant（#352），測試
        直接死在 fixture、`migrate()` 那行**永遠不會被執行**——探針壞在抵達受測
        程式碼之前，而失敗輸出跟「migration 真的有缺陷」長得一模一樣。
        """
        sa = self.SA.create(
            {'name': 'PRM GW ' + prefix, 'workspace_id': self.workspace.id})
        route = self.Route.create({
            'security_access_id': sa.id, 'subdomain_prefix': prefix})
        pol = self.Policy.create({
            'route_id': route.id, 'name': 'flat-' + prefix,
            'decision': decision,
            'rule_ids': [(0, 0, dict(self._SCAFFOLD_RULE))]})
        self.env.cr.execute(
            "DELETE FROM %s WHERE policy_id = %%s" % self._RULE_TABLE,
            (pol.id,))
        self.env.cr.execute(
            "UPDATE %s SET rule_type = %%s, rule_value = %%s WHERE id = %%s"
            % self._POL_TABLE, (rule_type, rule_value, pol.id))
        self.Policy.invalidate_model()
        self.env['woow_paas_platform.security_access_access_policy_rule'
                 ].invalidate_model()
        return pol

    def test_flat_policy_becomes_include_rule(self):
        self._add_legacy_flat_cols()
        try:
            pol = self._make_legacy_policy(
                'deny', 'email_domain', 'acme.com', 'prm-a')
            # pre-state: no rules
            self.assertFalse(pol.rule_ids)

            self.migrate(self.env.cr, '18.0.1.0.39')

            pol.invalidate_recordset()
            self.assertEqual(len(pol.rule_ids), 1,
                             "flat policy must gain exactly one rule")
            rule = pol.rule_ids
            self.assertEqual(rule.group, 'include')
            self.assertEqual(rule.rule_type, 'email_domain')
            self.assertEqual(rule.rule_value, 'acme.com')
            # decision is unchanged (it stays on the policy row)
            self.assertEqual(pol.decision, 'deny')
        finally:
            self._drop_legacy_flat_cols()

    def test_migration_idempotent(self):
        self._add_legacy_flat_cols()
        try:
            pol = self._make_legacy_policy(
                'allow', 'ip_range', '10.0.0.0/8', 'prm-b')
            self.migrate(self.env.cr, '18.0.1.0.39')
            pol.invalidate_recordset()
            self.assertEqual(len(pol.rule_ids), 1)
            # second pass: the policy already has a rule → skipped, no dup
            self.migrate(self.env.cr, '18.0.1.0.39')
            pol.invalidate_recordset()
            self.assertEqual(len(pol.rule_ids), 1,
                             "idempotent: no duplicate rule on re-run")
        finally:
            self._drop_legacy_flat_cols()

    def test_cursor_bypass_flag_cleared_after_migrate(self):
        """migration 的兩個 cursor bypass flag 在 finally 必須清掉，否則洩漏到
        後續 transaction——sa_skip_policy_quota 會靜默關閉 email 配額，
        sa_skip_bulk_grant_check 會靜默關閉 #352 的整批放行守衛。

        fixture 落在 allow × email_domain 這個交集上，所以本測試同時是 #543 的
        迴歸測試：migration 若沒繞過 `_check_allow_policy_no_bulk_grant`
        （`security_access_access_policy_rule.py` 的 `create()` 主動呼叫，非
        `@api.constrains`），`migrate()` 會拋 ValidationError——那正是任何仍停在
        < 18.0.1.0.39 且帶 allow × (email_domain|everyone) flat policy 的 DB
        （主要是從舊備份還原的情境）升版中斷的形狀。
        """
        self._add_legacy_flat_cols()
        try:
            self._make_legacy_policy(
                'allow', 'email_domain', 'a.com', 'prm-c')
            self.migrate(self.env.cr, '18.0.1.0.39')
            self.assertFalse(
                getattr(self.env.cr, 'sa_skip_policy_quota', False),
                "cursor bypass flag must be cleared after migrate() returns")
            self.assertFalse(
                getattr(self.env.cr, 'sa_skip_bulk_grant_check', False),
                "sa_skip_bulk_grant_check must be cleared after migrate() "
                "returns — a leak silently disables the #352 bulk-grant guard "
                "for the rest of the transaction")
        finally:
            self._drop_legacy_flat_cols()
