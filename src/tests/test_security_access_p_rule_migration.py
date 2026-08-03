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

    def _make_legacy_policy(self, decision, rule_type, rule_value, prefix):
        """Create a policy, then force it into the pre-.39 shape: flat columns
        set + its rule rows removed (raw SQL — an ORM unlink of the last include
        would trip the new constraint)."""
        sa = self.SA.create(
            {'name': 'PRM GW ' + prefix, 'workspace_id': self.workspace.id})
        route = self.Route.create({
            'security_access_id': sa.id, 'subdomain_prefix': prefix})
        pol = self.Policy.create({
            'route_id': route.id, 'name': 'flat-' + prefix,
            'decision': decision, 'rule_ids': [(0, 0, {
                'group': 'include', 'rule_type': rule_type,
                'rule_value': rule_value})]})
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
        """migration 用 cursor flag 跳過 email 配額；finally 必須清掉，否則洩漏
        到後續 transaction 靜默關閉配額。

        ⚠️ 截至 2026-08-03，這條是紅的——**紅的原因不是測試壞了，是它撞到一個
        真缺陷**（#543）。fixture 建 decision=allow + email_domain 的 legacy
        policy 時，撞上 2026-08-02 才加入的 `_check_allow_policy_no_bulk_grant`
        （`security_access_access_policy_rule.py:158` 主動呼叫，非
        `@api.constrains`），而 `migrations/18.0.1.0.39/post-migrate.py` 的
        `Rule.create` 沒有繞過該守衛 ⇒ 任何仍停在 < 18.0.1.0.39 且帶
        allow × (email_domain|everyone) flat policy 的 DB（主要是從舊備份還原
        的情境）升版會中斷。

        把 fixture 改成 deny 一行就能讓它轉綠，**但那是用假綠掩蓋真缺陷**，
        刻意不做。修好 #543 之後它會自然轉綠。

        本測試原本要驗的安全斷言（flag 不洩漏）另經獨立探針驗證為通過，
        migration 的 try/finally 是對的——見 #543 內文。
        """
        self._add_legacy_flat_cols()
        try:
            self._make_legacy_policy(
                'allow', 'email_domain', 'a.com', 'prm-c')
            self.migrate(self.env.cr, '18.0.1.0.39')
            self.assertFalse(
                getattr(self.env.cr, 'sa_skip_policy_quota', False),
                "cursor bypass flag must be cleared after migrate() returns")
        finally:
            self._drop_legacy_flat_cols()
