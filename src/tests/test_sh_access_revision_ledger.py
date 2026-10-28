"""自動撤權 v1（2026-10-14 跨 repo 契約）：SH 撤權帳本。

守的是平台側的四件事：

1. **什麼時候記一版**：operator 成功回條（含受保護時有 app_id）之後，指紋與這條
   route 上一筆**已提交**的列不同（或沒有列）才記；關保護／被 gate 擋掉／operator
   失敗／回條不完整都不記。另加「這一輪之前 CF 上沒有生效中的 app」強制記一版
   （關掉保護後重開同一份設定時，指紋與很久以前那筆相同，但中間那段任何人都可能
   連上）。
2. **記在獨立交易**：列要活過主交易的回捲（CF 那半回捲不掉）。
3. **寫不進去就 fail-loud**，而且 cron 重試不會原地空轉。
4. **讀取面**：``smart_home.to_dict()['access_revision']``，租戶對帳本零存取。

## 🔴 本檔的 cursor 事實（讀斷言前先看）

Odoo 18 的 ``TransactionCase`` **不進** test mode（只有 ``HttpCase`` 會），所以被測
碼的 ``registry.cursor()`` 回的是**真的第二條連線**、真的 commit（見
``test_access_failopen_compensation_794.py`` 檔頭的實測）。兩個後果：

* 帳本列由別的連線 commit，而本測試的主交易是 REPEATABLE READ、快照早在
  ``setUpClass`` 就定了 ⇒ **主 cursor 讀不到**。一律用 ``_committed_rows()``（新開
  一條連線）讀。
* 那些列**真的落地**、不會隨測試的 savepoint 回捲 ⇒ 每個 fixture 在建立前就先
  ``addCleanup`` 一支以新連線 raw SQL 刪除自己那幾列的清理（帳本 ORM 層不准刪，
  見 model 的 docstring）。

真正的「整筆交易 rollback」由 ``integration/test_sh_access_revision_rollback.py``
（``BaseCase``＋三筆真交易）守；本檔的 E 系列用 savepoint 回捲做同一件事的單元版。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..controllers.ha_api import _with_live_tunnel_status
from ..models.smart_home_access_revision import (
    AccessRevisionLedgerError, access_security_fingerprint,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity, _sm_skip_access_sync,
)
from .publication_receipt_fixture import install_publication_receipts
from .smart_home_fixture import sh_model

MOCK_SH_CLIENT = ('odoo.addons.woow_paas_platform.models.smart_home.'
                  'get_paas_operator_client')
LOG_REV = 'odoo.addons.woow_paas_platform.models.smart_home_access_revision'
LOG_SH_ROUTE = 'odoo.addons.woow_paas_platform.models.smart_home_route'
LOG_CRON = 'odoo.addons.woow_paas_platform.models.access_resync_cron'
_REV = 'woow_paas_platform.smart_home_access_revision'
_REV_TABLE = 'woow_paas_platform_smart_home_access_revision'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'


def _base_payload(**extra):
    payload = {
        'enabled': True,
        'canonical_name': 'sh-canon',
        'app_name': 'sh-app',
        'primary_hostname': 'h.example.com',
        'protected_hostnames': ['h.example.com'],
        'session_duration': '24h',
        'login_idp_types': ['onetimepin'],
        'policies': [{
            'name': 'p1', 'decision': 'allow', 'precedence': 10,
            'include': [{'rule_type': 'email_list',
                         'rule_value': 'a@x.com, b@x.com'}],
            'require': [], 'exclude': [],
        }],
        'existing_app_id': 'app-1',
        'existing_policy_ids': ['pol-1'],
    }
    payload.update(extra)
    return payload


@tagged('post_install', '-at_install')
class TestAccessSecurityFingerprint(TransactionCase):
    """A 系列：指紋只看安全相關欄位（契約 §1）。"""

    def test_A1_identity_and_display_fields_do_not_change_it(self):
        base = access_security_fingerprint(_base_payload())
        for key, value in (('existing_app_id', 'app-2'),
                           ('existing_policy_ids', ['pol-9']),
                           ('canonical_name', 'other'),
                           ('app_name', 'other'),
                           ('primary_hostname', 'other.example.com')):
            self.assertEqual(
                access_security_fingerprint(_base_payload(**{key: value})),
                base, '%s 不是安全欄位，改它不得多斷一次線' % key)
        renamed = _base_payload()
        renamed['policies'][0]['name'] = 'renamed'
        self.assertEqual(access_security_fingerprint(renamed), base,
                         'policy 名稱不是安全欄位')

    def test_A2_every_security_field_changes_it(self):
        base = access_security_fingerprint(_base_payload())
        variants = {
            'enabled': _base_payload(enabled=False),
            'protected_hostnames': _base_payload(protected_hostnames=[]),
            'session_duration': _base_payload(session_duration='1h'),
            'login_idp_types': _base_payload(login_idp_types=['google']),
        }
        for field, (key, value) in {
                'decision': ('decision', 'block'),
                'precedence': ('precedence', 20),
                'include': ('include', [{'rule_type': 'email_list',
                                         'rule_value': 'a@x.com'}]),
                'require': ('require', [{'rule_type': 'country',
                                         'rule_value': 'TW'}]),
                'exclude': ('exclude', [{'rule_type': 'email_list',
                                         'rule_value': 'b@x.com'}]),
        }.items():
            payload = _base_payload()
            payload['policies'][0][key] = value
            variants['policies[].' + field] = payload
        variants['policies removed'] = _base_payload(policies=[])
        for label, payload in variants.items():
            self.assertNotEqual(
                access_security_fingerprint(payload), base,
                '%s 改變必須改變指紋（否則那一次收緊不會斷線）' % label)

    def test_A3_dict_key_order_is_irrelevant(self):
        payload = _base_payload()
        reordered = dict(reversed(list(payload.items())))
        reordered['policies'] = [dict(reversed(list(p.items())))
                                 for p in payload['policies']]
        self.assertEqual(access_security_fingerprint(reordered),
                         access_security_fingerprint(payload))


class _LedgerFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.home_ids = []
        # 清理在建立任何東西**之前**掛上（帳本列是別的連線真的 commit 的）。
        self.addCleanup(self._purge_committed_rows)
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS rev %s' % uuid.uuid4().hex[:6]})
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'rev-plan-%s' % uuid.uuid4().hex[:6], 'sh_duration': 1,
            'sh_unit': 'month', 'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1, 'ac_max_access_seats': 99})
        self.client = install_publication_receipts(MagicMock())
        patcher = patch(MOCK_SH_CLIENT, return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sh, self.route = self._home()

    def _purge_committed_rows(self):
        if not self.home_ids:
            return
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM %s WHERE smart_home_ref IN %%s' % _REV_TABLE,
                       (tuple(self.home_ids),))

    def _home(self, state='active'):
        sh = sh_model(self.env).create({
            'name': 'sh-rev-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id, 'state': state,
            'tunnel_id': 'tun-rev-%s' % uuid.uuid4().hex[:8]})
        self.home_ids.append(sh.id)
        sh.write({'subdomain': sh._generate_subdomain()})
        sh = sh.with_env(self.env)
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = self.plan.id
        sh.sudo().subscription_id = sub.id
        route = _sm_skip_access_sync(self.env[_SH_ROUTE]).create(
            {'smart_home_id': sh.id, 'is_primary': True})
        return sh, route.with_env(self.env)

    def _protect(self, route=None, **extra):
        vals = {'is_protected': True, 'login_email_otp': True}
        vals.update(extra)
        (route or self.route).write(vals)

    def _allow_policy(self, emails='a@x.com, b@x.com', route=None):
        return self.env['woow_paas_platform.security_access_access_policy'
                        ].create({
            'name': 'allow-%s' % uuid.uuid4().hex[:4], 'decision': 'allow',
            'smart_home_route_id': (route or self.route).id,
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': emails})]})

    def _committed_rows(self, route=None):
        """新開一條連線讀帳本（主交易的快照看不到別的連線 commit 的列）。"""
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id, smart_home_ref, route_ref, fingerprint FROM %s '
                'WHERE route_ref = %%s ORDER BY id' % _REV_TABLE,
                ((route or self.route).id,))
            return cr.fetchall()

    def _committed_revision(self, sh=None):
        """從新連線呼叫生產碼的讀取方法（＝HA 輪詢那筆新交易看到的值）。"""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            return env[_REV]._revision_for_home((sh or self.sh).id)

    def _last_payload(self):
        return self.client.sync_access_app.call_args.args[0]


@tagged('post_install', '-at_install')
class TestRevisionRecordedOnConfirmedSync(_LedgerFixture):
    """B 系列：成功的 sync 什麼時候記、什麼時候不記。"""

    def test_B1_first_successful_protect_records_a_revision(self):
        self.assertEqual(self._committed_rows(), [], '前提：還沒有任何列')
        self.assertEqual(self._committed_revision(), 0)
        self._protect()
        rows = self._committed_rows()
        self.assertEqual(len(rows), 1)
        rev_id, home_ref, route_ref, fingerprint = rows[0]
        self.assertGreater(rev_id, 0)
        self.assertEqual((home_ref, route_ref), (self.sh.id, self.route.id))
        payload = self._last_payload()
        self.assertTrue(payload['enabled'], '前提：推出去的是保護中的狀態')
        self.assertEqual(fingerprint, access_security_fingerprint(payload))
        self.assertEqual(self._committed_revision(), rev_id)

    def test_B2_identical_resync_does_not_bump(self):
        self._protect()
        before = self._committed_rows()
        self.assertEqual(len(before), 1)
        calls = self.client.sync_access_app.call_count
        self.assertTrue(self.route._sync_access_to_operator())
        self.assertTrue(self.route._sync_access_to_operator())
        self.assertEqual(self.client.sync_access_app.call_count, calls + 2,
                         '前提：operator 真的又被推了兩次')
        self.assertEqual(self._committed_rows(), before,
                         '同一份 desired state 重推（cron／重試）不得多斷線')

    def test_B3_removing_an_email_from_an_allow_list_bumps(self):
        self._protect()
        policy = self._allow_policy('a@x.com, b@x.com')
        rows = self._committed_rows()
        self.assertGreaterEqual(len(rows), 2, '加 policy 本身也改變了指紋')
        rule = policy.rule_ids
        rule.write({'rule_value': 'a@x.com'})
        payload = self._last_payload()
        self.assertEqual(payload['policies'][0]['include'][0]['rule_value'],
                         'a@x.com', '前提：推出去的是移除 b 之後的狀態')
        after = self._committed_rows()
        self.assertEqual(len(after), len(rows) + 1)
        self.assertGreater(after[-1][0], rows[-1][0])
        self.assertEqual(after[-1][3], access_security_fingerprint(payload))
        self.assertEqual(self._committed_revision(), after[-1][0])

    def test_B4_session_or_login_method_change_bumps(self):
        self._protect()
        self.route.write({'session_duration': '1h'})
        self.route.write({'login_google': True})
        self.assertEqual(len(self._committed_rows()), 3)

    def test_B5_unprotect_is_widening_and_does_not_bump(self):
        self._protect()
        before = self._committed_rows()
        self.route.write({'is_protected': False})
        self.assertFalse(self._last_payload()['enabled'],
                         '前提：teardown 那一趟真的推出去了')
        self.assertEqual(self._committed_rows(), before)

    def test_B6_reprotect_with_identical_config_still_bumps(self):
        """關保護期間任何人都連得上 ⇒ 重開保護即使指紋與舊列相同也要斷線。"""
        self._protect()
        first = self._committed_rows()
        self.route.write({'is_protected': False})
        self._protect()
        after = self._committed_rows()
        self.assertEqual(len(after), 2)
        self.assertEqual(after[-1][3], first[-1][3],
                         '前提：desired state 與第一次完全相同')

    def test_B7_protection_lost_rebuild_bumps(self):
        self._protect()
        before = self._committed_rows()
        self.route._access_mark_protection_lost()
        self.assertTrue(self.route._sync_access_to_operator())
        self.assertEqual(len(self._committed_rows()), len(before) + 1)

    def test_B8_other_home_does_not_share_the_revision(self):
        other_sh, other_route = self._home()
        self._protect(route=other_route)
        self.assertEqual(len(self._committed_rows(other_route)), 1)
        self.assertEqual(self._committed_revision(self.sh), 0,
                         '別台 home 的撤權不得讓這台斷線')


@tagged('post_install', '-at_install')
class TestRevisionNotRecordedWithoutConfirmation(_LedgerFixture):
    """C 系列：沒有確認的變更不記（記了等於叫 HA 為一個沒生效的政策斷線）。"""

    def setUp(self):
        super().setUp()
        self._protect()
        self.baseline = self._committed_rows()
        self.assertEqual(len(self.baseline), 1, '前提')

    @mute_logger(LOG_SH_ROUTE)
    def test_C1_operator_failure_does_not_bump(self):
        self.client.sync_access_app.side_effect = Exception('operator down')
        with self.assertRaises(Exception):
            self.route.write({'session_duration': '1h'})
        self.assertEqual(self._committed_rows(), self.baseline)

    def test_C2_incomplete_receipt_does_not_bump(self):
        self.client.sync_access_app.side_effect = None
        self.client.sync_access_app.return_value = {'app_id': 'app-1'}
        with self.assertRaises(UserError):
            self.route.write({'session_duration': '1h'})
        self.assertTrue(self.client.sync_access_app.called)
        self.assertEqual(self._committed_rows(), self.baseline)

    def test_C3_protected_without_app_id_does_not_bump(self):
        self.client.sync_access_app.side_effect = None
        self.client.sync_access_app.return_value = {
            'app_id': None, 'policy_ids': [], 'allowed_idps': [],
            'unresolved_idp_types': [], 'orphaned_policy_ids': []}
        with self.assertRaises(UserError):
            self.route.write({'session_duration': '1h'})
        self.assertEqual(self._committed_rows(), self.baseline)

    def test_C4_skipped_sync_does_not_bump(self):
        # 直接改 state（不走 _do_suspend 那條會打 operator 的生命週期路徑）。
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE woow_paas_platform_smart_home SET state = 'suspended' "
            "WHERE id = %s", (self.sh.id,))
        self.env.invalidate_all()
        calls = self.client.sync_access_app.call_count
        self.route.write({'session_duration': '1h'})
        self.assertEqual(self.client.sync_access_app.call_count, calls,
                         '前提：liveness gate 擋掉、一次外呼都沒有')
        self.assertFalse(self.route._sync_access_to_operator())
        self.assertEqual(self._committed_rows(), self.baseline)


@tagged('post_install', '-at_install')
class TestRevisionLedgerDurabilityAndFailure(_LedgerFixture):
    """E 系列：獨立交易與 fail-loud。"""

    def _route_session(self):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT session_duration FROM woow_paas_platform_smart_home_route '
            'WHERE id = %s', (self.route.id,))
        return self.env.cr.fetchone()[0]

    def test_E1_row_survives_rollback_of_the_request_changes(self):
        self._protect()
        baseline = self._committed_rows()

        class _Boom(Exception):
            pass

        with self.assertRaises(_Boom):
            with self.env.cr.savepoint():
                self.route.write({'session_duration': '1h'})
                raise _Boom()
        self.assertEqual(self._route_session(), '24h',
                         '前提：主交易這一段真的回捲了')
        rows = self._committed_rows()
        self.assertEqual(len(rows), len(baseline) + 1,
                         'CF 已套用 1h，帳必須活過主交易的回捲')
        self.assertEqual(self._committed_revision(), rows[-1][0])

    def test_E2_ledger_write_failure_fails_loud_and_rolls_back(self):
        self._protect()
        baseline = self._committed_rows()
        calls = self.client.sync_access_app.call_count
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('pool exhausted')):
            with self.assertLogs(LOG_REV, level='ERROR'):
                with self.assertRaises(AccessRevisionLedgerError):
                    self.route.write({'session_duration': '1h'})
        self.assertEqual(self.client.sync_access_app.call_count, calls + 1,
                         '前提：operator 真的被推了（失敗發生在提交點之後）')
        self.assertEqual(self._route_session(), '24h',
                         '操作回報失敗 ⇒ 主交易回捲，租戶看到要重試')
        self.assertEqual(self._committed_rows(), baseline)
        # 恢復之後同一個操作重試 ⇒ 記得到。
        self.route.write({'session_duration': '1h'})
        self.assertEqual(len(self._committed_rows()), len(baseline) + 1)

    def test_E3_cron_retry_is_bounded_when_the_ledger_keeps_failing(self):
        """access_resync_cron 重推遇到帳本失敗：一列一輪只推一次、旗標留著。"""
        self._protect()
        # Flag only: clearing the app id would send the retry down the create
        # path, whose dispatch fence also needs a registry cursor and would
        # fail before the operator is reached.
        self.route.access_protection_lost = True
        cron = self.env['woow_paas_platform.access_resync_cron']
        calls = self.client.sync_access_app.call_count
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('pool exhausted')):
            with self.assertLogs(LOG_CRON, level=logging.WARNING):
                for _round in range(2):
                    self.assertEqual(
                        cron._cron_resync_access_protection(), 0)
        self.assertEqual(self.client.sync_access_app.call_count, calls + 2,
                         '兩輪各推一次：失敗不會讓同一輪原地重試')
        self.assertTrue(self.route.access_protection_lost,
                        '旗標留著 ⇒ 下一輪（15 分鐘後）重試')
        rows_before = self._committed_rows()
        self.assertEqual(cron._cron_resync_access_protection(), 1)
        self.assertEqual(len(self._committed_rows()), len(rows_before) + 1,
                         '帳本恢復後的下一輪記得到（protection_lost 重建強制記）')


@tagged('post_install', '-at_install')
class TestRevisionReadSurface(_LedgerFixture):
    """F 系列：to_dict／HA status 帶出版本；租戶碰不到帳本。"""

    def test_F1_to_dict_exposes_max_ledger_id_for_this_home(self):
        self.assertEqual(self.sh.to_dict()['access_revision'], 0)
        Rev = self.env[_REV].sudo()
        other_sh, _other_route = self._home()
        first = Rev.create({'smart_home_ref': self.sh.id,
                           'route_ref': self.route.id, 'fingerprint': 'f1'})
        second = Rev.create({'smart_home_ref': self.sh.id,
                            'route_ref': self.route.id, 'fingerprint': 'f2'})
        third = Rev.create({'smart_home_ref': other_sh.id,
                           'route_ref': _other_route.id, 'fingerprint': 'f3'})
        self.assertGreater(second.id, first.id)
        self.assertEqual(self.sh.to_dict()['access_revision'], second.id)
        self.assertEqual(other_sh.to_dict()['access_revision'], third.id)

    def test_F2_ha_status_payload_carries_it(self):
        Rev = self.env[_REV].sudo()
        rev = Rev.create({'smart_home_ref': self.sh.id,
                          'route_ref': self.route.id, 'fingerprint': 'f'})
        with patch.object(type(self.sh), '_live_tunnel_status',
                          return_value={'tunnel_status': 'connected'}):
            data = _with_live_tunnel_status(self.sh)
        self.assertEqual(data['tunnel_status'], 'connected',
                         '前提：走的是 HA status 端點的真實組裝函式')
        self.assertEqual(data['access_revision'], rev.id)

    def test_F3_append_only_for_everyone_including_superuser(self):
        rev = self.env[_REV].sudo().create({
            'smart_home_ref': self.sh.id, 'route_ref': self.route.id,
            'fingerprint': 'f'})
        with self.assertRaises(AccessError):
            rev.write({'fingerprint': 'forged'})
        with self.assertRaises(AccessError):
            rev.write({'smart_home_ref': self.sh.id + 1})
        with self.assertRaises(AccessError):
            rev.unlink()

    def test_F4_tenants_cannot_read_or_write_the_ledger(self):
        rev = self.env[_REV].sudo().create({
            'smart_home_ref': self.sh.id, 'route_ref': self.route.id,
            'fingerprint': 'f'})
        internal = self.env['res.users'].create({
            'name': 'rev tenant', 'login': 'rev-%s' % uuid.uuid4().hex[:8],
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        portal = self.env['res.users'].create({
            'name': 'rev portal', 'login': 'revp-%s' % uuid.uuid4().hex[:8],
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])]})
        for user in (internal, portal):
            Rev = self.env[_REV].with_user(user)
            with self.assertRaises(AccessError):
                Rev.search([])
            with self.assertRaises(AccessError):
                rev.with_user(user).read(['fingerprint'])
            with self.assertRaises(AccessError):
                Rev.create({'smart_home_ref': self.sh.id,
                            'route_ref': self.route.id,
                            'fingerprint': 'forged'})
        # 陽性對照：系統管理員可讀（維運查帳），但同樣不能經 ORM 新增。
        admin = self.env.ref('base.user_admin')
        self.assertEqual(
            self.env[_REV].with_user(admin).search(
                [('id', '=', rev.id)]).fingerprint, 'f')
        with self.assertRaises(AccessError):
            self.env[_REV].with_user(admin).create({
                'smart_home_ref': self.sh.id, 'route_ref': self.route.id,
                'fingerprint': 'forged'})
