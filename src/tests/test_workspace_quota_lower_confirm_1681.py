"""#1681 管理員安全降低或清除 Workspace 最低保障：檢查、風險確認、重檢與恢復。

外部可觀察行為：管理員入口（``preview_quota_minimums``／``set_quota_minimums``／對話框）→
是否保存、operator 收到的 PUT 與叢集最後的值、後台同步狀態與 chatter。用量與故障只在
operator 邊界注入（``ns_quota_fixture``）。

拒絕後的 DB 斷言不用 ``assertRaises``：它把區塊包進 savepoint，例外後回捲成區塊之前的值，
「沒有保存」會假綠（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged
from odoo.tools import html2plaintext

from ..services.paas_operator import PaaSOperatorConnectionError
from ..services.workspace_quota import workspace_namespace
from .ns_quota_fixture import FakeQuotaOperator, build_hard
from .test_workspace_quota_floor_1679 import _CLIENT
from .test_workspace_quota_sync_1680 import _SyncFixture


class _LowerFixture(_SyncFixture):
    """公式 12／12Gi／100Gi；先把最低保障設成 18／40Gi／300Gi 並同步到叢集。"""

    def setUp(self):
        super().setUp()
        self._set(cpu=18, memory_gi=40, storage_gi=300)
        self.assertEqual(self.operator.live[self.ns],
                         build_hard('18', '40Gi', '300Gi', '4Gi', '80Gi'))

    def _preview(self, user=None, workspace=None, **values):
        with patch(_CLIENT, return_value=self.client):
            return (workspace or self.workspace).with_user(user or self.admin).preview_quota_minimums(
                **values)

    def _set_as(self, user=None, workspace=None, **values):
        with patch(_CLIENT, return_value=self.client):
            return (workspace or self.workspace).with_user(user or self.admin).set_quota_minimums(
                **values)

    def _rejected(self, call, *args, **kwargs):
        """呼叫必須以 UserError 拒絕；回傳訊息（不用 assertRaises，見檔頭）。"""
        try:
            call(*args, **kwargs)
        except UserError as exc:
            return str(exc)
        self.fail('expected UserError')

    def _snapshot(self):
        ws = self._ws()
        ws.invalidate_recordset()
        return ((ws.quota_floor_cpu, ws.quota_floor_memory_gi, ws.quota_floor_storage_gi,
                 ws.quota_target_version, ws.quota_lower_authorization),
                len(self.operator.puts), self._audit_count())

    def _live(self, key):
        return self.operator.live[self.ns][key]

    def _monitor_text(self):
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        return html2plaintext(monitor.message_ids[:1].body)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerConfirm1681(_LowerFixture):

    def test_clear_with_confirmation_lowers_cluster_and_is_logged(self):
        self.operator.used[self.ns] = {'requests.cpu': '4', 'limits.cpu': '10'}
        preview = self._preview(cpu=0)
        self.assertEqual(preview['state'], 'confirmation_required')
        self.assertEqual(
            sorted((d['key'], d['cluster'], d['target'], d['used']) for d in preview['decreases']),
            [('limits.cpu', '18', '12', '10'), ('requests.cpu', '18', '12', '4')])
        self.assertIn('not guarantee room for future', preview['warning'])
        self.assertIn('not atomic with Kubernetes admission', preview['warning'])
        result = self._set_as(cpu=0, confirmation=preview['confirmation'])
        self.assertEqual(result['state'], 'synced')
        self.assertEqual(self.operator.puts[-1], (self.ns, '12', '40Gi', '300Gi', '4Gi', '80Gi'))
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_target_version), (0, 2))
        self.assertFalse(ws.quota_lower_authorization, '已落地：確認用完即清')
        text = self._monitor_text()
        self.assertIn('cpu=18, memory=40Gi, storage=300Gi -> cpu=-, memory=40Gi, storage=300Gi',
                      text)
        self.assertIn('Confirmed quota decrease', text)
        self.assertIn('%s limits.cpu: 18 -> 12 (used 10)' % self.ns, text)
        self.assertIn(self.admin.login, text)

    def test_lowering_several_resources_at_once(self):
        preview = self._preview(cpu=14, memory_gi=20, storage_gi=0)
        self.assertEqual({d['key'] for d in preview['decreases']},
                         {'requests.cpu', 'limits.cpu', 'requests.memory', 'limits.memory',
                          'requests.storage'})
        self._set_as(cpu=14, memory_gi=20, storage_gi=0, confirmation=preview['confirmation'])
        self.assertEqual(self.operator.live[self.ns],
                         build_hard('14', '20Gi', '100Gi', '4Gi', '80Gi'))
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_target_uses_formula_when_it_is_above_the_new_minimum(self):
        self._service('svc-lower-b', vcpu=5, ram=1.0, storage=1)  # 公式 cpu 27
        self._set(cpu=30)
        preview = self._preview(cpu=20)  # 目標 max(27, 20)=27
        self._set_as(cpu=20, confirmation=preview['confirmation'])
        self.assertEqual(self._live('limits.cpu'), '27')
        self.assertEqual(self._ws().quota_target_cpu, '27')

    def test_lowering_without_confirmation_is_rejected_and_not_saved(self):
        before = self._snapshot()
        message = self._rejected(self._set_as, cpu=0)
        self.assertIn('needs confirmation', message)
        self.assertEqual(self._snapshot(), before)

    def test_usage_above_target_is_rejected_on_preview_and_on_save(self):
        self.operator.used[self.ns] = {'limits.cpu': '16'}
        preview = self._preview(cpu=0)
        self.assertEqual(preview['state'], 'rejected')
        self.assertIn('limits.cpu used_exceeds', '; '.join(preview['reasons']))
        self.assertNotIn('confirmation', preview)
        before = self._snapshot()
        self._rejected(self._set_as, cpu=0)
        self.assertEqual(self._snapshot(), before)

    def test_requests_and_limits_usage_are_checked_separately(self):
        self.operator.used[self.ns] = {'requests.memory': '6Gi', 'limits.memory': '30Gi'}
        preview = self._preview(memory_gi=20)
        self.assertEqual(preview['state'], 'rejected')
        self.assertIn('limits.memory used_exceeds', '; '.join(preview['reasons']))
        self.operator.used[self.ns] = {'requests.memory': '30Gi', 'limits.memory': '6Gi'}
        preview = self._preview(memory_gi=20)
        self.assertIn('requests.memory used_exceeds', '; '.join(preview['reasons']))

    def test_retained_pvc_storage_blocks_storage_clear(self):
        self.operator.used[self.ns] = {'requests.storage': '250Gi'}
        preview = self._preview(storage_gi=0)
        self.assertEqual(preview['state'], 'rejected')
        self.assertIn('requests.storage used_exceeds', '; '.join(preview['reasons']))

    def test_unreadable_missing_or_legacy_quota_is_rejected(self):
        cases = []
        self.operator.get_error = PaaSOperatorConnectionError('Connection failed')
        cases.append(('quota_unreadable', self._preview(cpu=0)))
        self.operator.get_error = None
        self.operator.drop_used[self.ns] = {'requests.storage'}
        cases.append(('requests.storage used_missing', self._preview(cpu=0)))
        self.operator.drop_used = {}
        legacy = FakeQuotaOperator({self.ns: dict(self.operator.live[self.ns])}, guarded=False)
        self.client = legacy.bind()
        cases.append(('operator_unsupported', self._preview(cpu=0)))
        for reason, preview in cases:
            with self.subTest(reason=reason):
                self.assertEqual(preview['state'], 'rejected')
                self.assertIn(reason, '; '.join(preview['reasons']))
        before = self._snapshot()
        self._rejected(self._set_as, cpu=0)
        self.assertEqual(self._snapshot(), before)

    def test_change_that_does_not_lower_the_cluster_needs_no_confirmation(self):
        # 叢集已被（例如另一個工具）調回 12：清除 cpu 最低保障不會降低叢集任何一項。
        self.operator.live[self.ns]['requests.cpu'] = '12'
        self.operator.live[self.ns]['limits.cpu'] = '12'
        self.assertEqual(self._preview(cpu=0)['state'], 'ok')
        self._set_as(cpu=0)
        self.assertEqual(self._ws().quota_floor_cpu, 0)

    def test_namespace_not_created_yet_saves_without_confirmation(self):
        del self.operator.live[self.ns]
        self.operator.namespace_exists[self.ns] = False
        self.s1.sudo().write({'state': 'pending'})
        self._set_as(cpu=0)
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_sync_state), (0, 'pending_first_deploy'))

    def test_raising_never_needs_confirmation(self):
        self._set_as(cpu=24)
        self.assertEqual(self._live('limits.cpu'), '24')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerConfirmBinding1681(_LowerFixture):
    """確認只適用於當次目標：過期、改目標、跨 Workspace、換人、版本前進、偽造都不能用。"""

    def _expect_invalid(self, call, **values):
        before = self._snapshot()
        message = self._rejected(call, **values)
        self.assertIn('confirmation is invalid, expired or for a different change', message)
        self.assertEqual(self._snapshot(), before)

    def test_expired_confirmation_is_rejected(self):
        preview = self._preview(cpu=0)
        with freeze_time(fields.Datetime.now() + timedelta(minutes=11)):
            self._expect_invalid(self._set_as, cpu=0, confirmation=preview['confirmation'])

    def test_confirmation_for_another_target_is_rejected(self):
        preview = self._preview(cpu=14)
        self._expect_invalid(self._set_as, cpu=13, confirmation=preview['confirmation'])
        self._expect_invalid(self._set_as, cpu=14, memory_gi=20,
                             confirmation=preview['confirmation'])

    def test_confirmation_for_another_workspace_is_rejected(self):
        other = self.env['woow_paas_platform.workspace'].create({
            'name': 'Other 1681', 'owner_id': self.workspace.owner_id.id})
        other_ns = workspace_namespace(self.env, other)
        self.operator.live[other_ns] = build_hard('8', '8Gi', '100Gi')
        self.operator.rv[other_ns] = 100
        self._set_as(workspace=other, cpu=18)
        self.assertEqual(self.operator.live[other_ns]['limits.cpu'], '18')
        token = self._preview(cpu=0)['confirmation']  # 發給本工作區的確認碼
        message = self._rejected(self._set_as, workspace=other, cpu=0, confirmation=token)
        self.assertIn('confirmation is invalid', message)
        other.invalidate_recordset()
        self.assertEqual(other.sudo().quota_floor_cpu, 18)
        self.assertEqual(self.operator.live[other_ns]['limits.cpu'], '18')

    def test_confirmation_from_another_administrator_is_rejected(self):
        preview = self._preview(cpu=0)
        second = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Second admin 1681', 'login': 'second-admin-1681',
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id])]})
        self._expect_invalid(self._set_as, user=second, cpu=0,
                             confirmation=preview['confirmation'])

    def test_newer_policy_version_invalidates_confirmation(self):
        preview = self._preview(cpu=0)
        self._set(storage_gi=400)  # 另一次保存：政策版本前進
        self._expect_invalid(self._set_as, cpu=0, confirmation=preview['confirmation'])

    def test_formula_change_invalidates_confirmation(self):
        preview = self._preview(cpu=0)
        self._service('svc-lower-c', vcpu=1, ram=1.0, storage=1)  # 目標跟著變
        self._expect_invalid(self._set_as, cpu=0, confirmation=preview['confirmation'])

    def test_tampered_or_forged_confirmation_is_rejected(self):
        import base64
        import json
        preview = self._preview(cpu=14)
        encoded, signature = preview['confirmation'].rsplit('.', 1)
        claims = json.loads(base64.urlsafe_b64decode(encoded))
        claims['floors']['quota_floor_cpu'] = 0
        claims['target']['cpu_limit'] = '12'
        forged = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).decode()
        for token in ('%s.%s' % (forged, signature), 'garbage', 'x.y', preview['confirmation'][:-1]):
            with self.subTest(token=token[:20]):
                self._expect_invalid(self._set_as, cpu=0, confirmation=token)

    def test_usage_growth_after_confirmation_is_caught_by_the_recheck(self):
        preview = self._preview(cpu=0)
        self.operator.used[self.ns] = {'limits.cpu': '17'}
        before = self._snapshot()
        message = self._rejected(self._set_as, cpu=0, confirmation=preview['confirmation'])
        self.assertIn('limits.cpu used_exceeds', message)
        self.assertEqual(self._snapshot(), before)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerRecovery1681(_LowerFixture):
    """確認後推送失敗：保留並有限重試；每次重試都重檢用量。"""

    def test_timeout_keeps_confirmed_setting_and_retry_rechecks_usage(self):
        preview = self._preview(cpu=0)
        self.operator.hold_writes = True
        result = self._set_as(cpu=0, confirmation=preview['confirmation'])
        self.assertEqual(result['state'], 'unconfirmed')
        ws = self._ws()
        self.assertEqual(ws.quota_floor_cpu, 0, '合法且已確認：保留')
        self.assertTrue(ws.quota_lower_authorization)
        self.assertTrue(ws.quota_next_retry_at)
        self.assertEqual(self._live('limits.cpu'), '18')
        # 在途請求一直沒落地；用量在重試前長到 16：重試重檢，不降，標示原因。
        self.operator.hold_writes = False
        self.operator.held = []
        self.operator.used[self.ns] = {'limits.cpu': '16'}
        self._sync_retry()
        self.assertEqual(self._live('limits.cpu'), '18')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('unsafe_decrease: limits.cpu used_exceeds', ws.quota_sync_reason)
        self.assertTrue(ws.quota_next_retry_at)
        # 用量釋放後的下一次重試收回。
        self.operator.used[self.ns] = {}
        self._sync_retry()
        self.assertEqual(self._live('limits.cpu'), '12')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_late_old_request_cannot_overwrite_the_confirmed_decrease(self):
        preview = self._preview(cpu=0)
        self.operator.hold_writes = True
        self._set_as(cpu=0, confirmation=preview['confirmation'])
        self.operator.hold_writes = False
        self._sync_retry()  # 以新 token、新 resourceVersion 落地
        self.assertEqual(self._live('limits.cpu'), '12')
        self.assertEqual(self.operator.land_held(), ['conflict'])
        self.assertEqual(self._live('limits.cpu'), '12')

    def _sync_retry(self):
        with patch(_CLIENT, return_value=self.client):
            self.workspace.sudo()._quota_sync('retry', nowait=True)

    def test_lifecycle_deletion_is_never_forced_through_confirmation(self):
        sibling = self._service('svc-lower-d', vcpu=10, ram=10.0, storage=10)
        self._sync_retry()
        with patch(_CLIENT, return_value=self.client):
            sibling.unlink()  # 公式下降：不要求確認、不擋刪除
        self.assertFalse(sibling.exists())


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerWizard1681(_LowerFixture):
    """後台對話框：先顯示降額與風險，再確認。"""

    def _wizard(self, **values):
        Wizard = self.env['woow_paas_platform.workspace_quota_minimum_wizard'].with_user(self.admin)
        return Wizard.create(dict({'workspace_id': self.workspace.id, 'cpu': 18,
                                   'memory_gi': 40, 'storage_gi': 300}, **values))

    def test_two_step_confirmation(self):
        wizard = self._wizard(cpu=0)
        with patch(_CLIENT, return_value=self.client):
            action = wizard.action_apply()
        self.assertEqual((action['res_model'], action['res_id']), (wizard._name, wizard.id))
        self.assertEqual(wizard.step, 'confirm')
        self.assertIn('limits.cpu: 18 -> 12', wizard.decrease_summary)
        self.assertIn('not atomic with Kubernetes admission', wizard.decrease_warning)
        self.assertEqual(self._ws().quota_floor_cpu, 18, '確認前不保存')
        with patch(_CLIENT, return_value=self.client):
            done = wizard.action_confirm_decrease()
        self.assertEqual(done['params']['type'], 'success')
        self.assertEqual(self._live('limits.cpu'), '12')

    def test_editing_values_after_review_invalidates_confirmation(self):
        wizard = self._wizard(cpu=0)
        with patch(_CLIENT, return_value=self.client):
            wizard.action_apply()
        wizard.write({'cpu': 1})
        before = self._snapshot()
        with patch(_CLIENT, return_value=self.client):
            self._rejected(wizard.action_confirm_decrease)
        self.assertEqual(self._snapshot(), before)

    def test_unsafe_decrease_is_rejected_by_the_dialog(self):
        self.operator.used[self.ns] = {'limits.cpu': '16'}
        wizard = self._wizard(cpu=0)
        before = self._snapshot()
        with patch(_CLIENT, return_value=self.client):
            message = self._rejected(wizard.action_apply)
        self.assertIn('cannot be lowered safely', message)
        self.assertEqual(self._snapshot(), before)
