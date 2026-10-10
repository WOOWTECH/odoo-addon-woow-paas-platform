# -*- coding: utf-8 -*-
"""#1158：換方案要把新方案的規格套到已部署的服務（Closes #1158／#342）。

裁決（#1158 步驟 2，2026-10-06）：

- Q1：升降級都**立即**——帳務完成後同一次呼叫排背景 helm upgrade（釘現行 chart、
  ``reset_values=True``、不 refresh image），服務進 ``upgrading``。
- Q2（暫定，討論票 #1512）：儲存**只擴不縮**——降級保留目前的 ``persistence.size``，
  只調 CPU／RAM／workers；``allocated_storage_gb`` 記實際大小。規則集中在
  ``_plan_storage_size``。
- Q4：``allocated_*`` 同一交易更新、helm upgrade **之前** push ns quota；現值 ≠ 舊方案
  推得的值（營運手調過）就保留現值。
- Q5：能預判的失敗全部擋在第一個寫入（退點）之前。
- Q6：overlay 搬進 ``_assemble_deploy_values``（所有 template）；只有 values 真的變了
  才觸發 upgrade（月繳↔年繳同規格不重啟）。
- Q7：停權中的服務照 reconfigure：upgrade 後 scale 回 0、state 留在 suspended。

⚠ 背景執行緒用自己的 cursor，讀不到測試 transaction 尚未 commit 的 row ⇒ 背景對
DB 的寫入在本檔不可觀察（同 ``test_suspended_redeploy_state_482`` 的說明）；
upgrade 參數與呼叫順序靠 client mock 斷言。

mock namespace（見 docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）：
controller 頂部 import 的 ``get_paas_operator_client``、model ``_push_ns_quota`` 函式內
import 的同名 helper（來源模組）、背景執行緒函式內 import 的 ``PaaSOperatorClient``——三個
target 都要 patch。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SERVICES = 'odoo.addons.woow_paas_platform.services.paas_operator'

GIB = 1024 ** 3


class _SyncThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


@tagged('post_install', '-at_install')
class TestPlanStorageRule1158(TransactionCase):
    """Q2 暫定規則（#1512）：只擴不縮，集中在一處。"""

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']

    def test_grows_to_the_plan_size(self):
        self.assertEqual(self.Svc._plan_storage_size('20Gi', 30), '30Gi')

    def test_never_shrinks(self):
        self.assertEqual(self.Svc._plan_storage_size('30Gi', 20), '30Gi')

    def test_missing_current_size_takes_the_plan(self):
        self.assertEqual(self.Svc._plan_storage_size(None, 20), '20Gi')

    def test_other_units_are_compared_by_value(self):
        self.assertEqual(self.Svc._plan_storage_size('40960Mi', 30), '40960Mi')
        self.assertEqual(self.Svc._plan_storage_size('1Ti', 30), '1Ti')
        self.assertEqual(self.Svc._plan_storage_size('10240Mi', 30), '30Gi')

    def test_unparseable_current_size_is_kept(self):
        """看不懂現值時不能冒險送較小的值（PVC 縮小會讓整次 helm upgrade 失敗）。"""
        self.assertEqual(self.Svc._plan_storage_size('lots', 30), 'lots')


class _ChangePlanFixture:
    """woow-odoo template＋三個方案、照 create 路徑造服務、打換方案端點（#1158／#1513 共用）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        company = env.company.sudo()
        if not company.sh_journal_id:
            journal = env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        cls.tpl = env.ref('woow_paas_platform.cloud_app_odoo')
        cls.plan_a = cls._plan('1158 A', 100.0, vcpu=2, ram=2.0, storage=20, workers=2)
        cls.plan_a_yearly = cls._plan(
            '1158 A yearly', 1000.0, vcpu=2, ram=2.0, storage=20, workers=2)
        cls.plan_b = cls._plan('1158 B', 200.0, vcpu=2, ram=4.0, storage=30, workers=3)
        cls.tpl.sudo().write({'subscription_plan_ids': [
            (4, p.id) for p in (cls.plan_a, cls.plan_a_yearly, cls.plan_b)]})
        cls.controller = PaasController()

    @classmethod
    def _plan(cls, name, price, vcpu, ram, storage, workers):
        plan = cls.env['sh.subscription.plan'].sudo().create({
            'name': name, 'sh_plan_price': price,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
            'cs_vcpu': vcpu, 'cs_ram_gb': ram, 'cs_storage_gb': storage,
            'cs_odoo_workers': workers,
        })
        cls.env['product.template'].sudo().create({
            'name': name + ' P', 'type': 'service', 'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': price,
        })
        return plan

    def _service(self, tag, plan=None, state='running'):
        """照 create 路徑造一台：plan overlay 疊進 values、allocated 讀 plan、首購訂閱。"""
        plan = plan or self.plan_a
        owner = self.env['res.users'].sudo().create({
            'name': '1158 %s' % tag, 'login': '1158_%s' % tag,
            'email': '1158_%s@example.test' % tag,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        ws = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': '1158 WS %s' % tag, 'owner_id': owner.id})
        Svc = self.env['woow_paas_platform.cloud_service']
        values = Svc._apply_plan_spec_overlay(
            json.loads(self.tpl.helm_default_values), self.tpl, plan)
        svc = _internal_provision(Svc).create({
            'name': 'svc-1158-%s' % tag, 'workspace_id': ws.id,
            'template_id': self.tpl.id, 'state': state,
            'reference_id': 'ref-1158-%s' % tag, 'subdomain': 'svc-1158-%s' % tag,
            'helm_namespace': 'paas-ws-test-1158-%s' % tag,
            'helm_release_name': 'svc-1158%s' % tag,
            'helm_chart_version': '0.2.7',
            **Svc._allocated_vals_from(self.tpl, plan),
        })
        svc._apply_helm_values(values)
        svc._create_and_confirm_subscription(ws.owner_id.partner_id, plan)
        return owner, svc

    def _change(self, owner, svc, plan, client=None):
        client = client if client is not None else self._client()
        fake_request = MagicMock(env=self.env(user=owner))
        with patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_SERVICES}.get_paas_operator_client', return_value=client), \
                patch(f'{_SERVICES}.PaaSOperatorClient', return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            return self.controller.api_service_change_plan(
                service_id=svc.id, plan_id=plan.id)

    @staticmethod
    def _client():
        client = MagicMock()
        client.base_url = 'http://operator.test'
        client._session.headers = {'X-API-Key': 'k'}
        client.upgrade_release.return_value = {'revision': 2, 'chart_version': '0.2.7'}
        return client

    @staticmethod
    def _values(svc):
        return svc.sudo()._helm_values_dict()

    def _assert_untouched(self, svc, old_sub, values_before):
        self.assertEqual(svc.sudo().subscription_id, old_sub, '不得換訂閱')
        self.assertEqual(old_sub.state, 'in_progress', '不得關舊訂閱')
        self.assertEqual(self._values(svc), values_before)


@tagged('post_install', '-at_install')
class TestChangePlanReapply1158(_ChangePlanFixture, TransactionCase):

    # ---------------- 升級 ---------------- #

    def test_upgrade_reapplies_the_new_plan_spec(self):
        owner, svc = self._service('up')
        client = self._client()
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))

        values = self._values(svc)
        self.assertEqual(values['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})
        # #1637：cs_storage_gb 30 是整台總量＝資料 13＋PostgreSQL 4（固定）＋備份 13。
        self.assertEqual(values['persistence']['size'], '13Gi')
        self.assertEqual(values['backup']['persistence']['size'], '13Gi')
        self.assertEqual(values['postgres']['persistence']['size'], '4Gi')
        self.assertEqual(values['conf']['workers'], 3)
        self.assertEqual(values['conf']['limitMemoryHard'], int(4 * GIB * 0.85 / 3))
        self.assertEqual(values['conf']['limitMemorySoft'], int(4 * GIB * 0.70 / 3))
        self.assertEqual(
            (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb),
            (2, 4.0, 30))
        self.assertEqual(svc.state, 'upgrading')

        kwargs = client.upgrade_release.call_args.kwargs
        self.assertTrue(kwargs['reset_values'])
        self.assertEqual(kwargs['version'], '0.2.7', '換方案不升 chart：釘服務現行版本')
        self.assertEqual(kwargs['values']['conf']['workers'], 3)
        self.assertEqual(kwargs['values']['persistence']['size'], '13Gi')
        self.assertIn('sensitive_values', kwargs, 'woow-odoo 每次全量送 adminPassword')

    def test_quota_is_pushed_before_the_upgrade(self):
        """quota 先擴：新 pod 才不會被 ResourceQuota admission 擋下。"""
        owner, svc = self._service('quota')
        client = self._client()
        self._change(owner, svc, self.plan_b, client)
        names = [c[0] for c in client.method_calls]
        self.assertIn('update_namespace_quota', names)
        self.assertLess(names.index('update_namespace_quota'),
                        names.index('upgrade_release'))

    def test_upgrade_does_not_refresh_images(self):
        """#1160／#1184：換方案不動 image——values 的 image 與換之前一致。"""
        owner, svc = self._service('img')
        before = self._values(svc)['image']
        with patch.object(type(svc), '_refresh_template_images') as refresh:
            self._change(owner, svc, self.plan_b)
        refresh.assert_not_called()
        self.assertEqual(self._values(svc)['image'], before)

    # ---------------- 降級（Q2 B：只擴不縮） ---------------- #

    def test_downgrade_keeps_storage_and_resizes_the_rest(self):
        owner, svc = self._service('down', plan=self.plan_b)
        res = self._change(owner, svc, self.plan_a)
        self.assertTrue(res['success'], res.get('error'))

        values = self._values(svc)
        self.assertEqual(values['persistence']['size'], '13Gi', '儲存只擴不縮（#1512）')
        self.assertEqual(values['backup']['persistence']['size'], '13Gi')
        self.assertEqual(values['resources']['limits'], {'cpu': '2', 'memory': '2Gi'})
        self.assertEqual(values['conf']['workers'], 2)
        self.assertEqual(svc.allocated_storage_gb, 30, 'allocated 記實際大小')
        self.assertEqual(svc.allocated_ram_gb, 2.0)

    def test_regrow_after_downgrade_is_tracked(self):
        """B→A→B：保留下來的 30Gi 不被當成「營運手調」，再升級照常更新帳面。"""
        owner, svc = self._service('regrow', plan=self.plan_b)
        self._change(owner, svc, self.plan_a)
        svc.write({'state': 'running'})  # 背景 upgrade 的收斂在本檔不可觀察，手動推回
        self._change(owner, svc, self.plan_b)
        self.assertEqual(
            (svc.allocated_ram_gb, svc.allocated_storage_gb), (4.0, 30))

    # ---------------- 同規格 ---------------- #

    def test_same_spec_plan_does_not_restart(self):
        owner, svc = self._service('same')
        client = self._client()
        res = self._change(owner, svc, self.plan_a_yearly, client)
        self.assertTrue(res['success'], res.get('error'))
        client.upgrade_release.assert_not_called()
        self.assertEqual(svc.state, 'running')
        self.assertEqual(svc.sudo().subscription_id.sh_subscription_plan_id,
                         self.plan_a_yearly)

    def test_same_spec_plan_is_allowed_in_any_state(self):
        """不需要調資源的換方案不加新閘（帳務行為與修之前相同）。"""
        owner, svc = self._service('same-err', state='error')
        res = self._change(owner, svc, self.plan_a_yearly, client=False)
        self.assertTrue(res['success'], res.get('error'))

    # ---------------- 前置 gate（第一個寫入之前） ---------------- #

    def test_rejects_when_service_is_not_running(self):
        for state in ('error', 'deploying', 'upgrading', 'initializing', 'pending'):
            with self.subTest(state=state):
                owner, svc = self._service('st-%s' % state, state=state)
                old_sub, before = svc.sudo().subscription_id, self._values(svc)
                client = self._client()
                res = self._change(owner, svc, self.plan_b, client)
                self.assertFalse(res['success'])
                self._assert_untouched(svc, old_sub, before)
                client.upgrade_release.assert_not_called()

    def test_rejects_when_operator_is_not_configured(self):
        owner, svc = self._service('noop')
        old_sub, before = svc.sudo().subscription_id, self._values(svc)
        res = self._change(owner, svc, self.plan_b, client=False)
        self.assertFalse(res['success'])
        self._assert_untouched(svc, old_sub, before)

    def test_rejects_when_values_cannot_take_the_plan(self):
        """woow-odoo values 缺 conf ⇒ overlay fail-loud ⇒ 擋在帳務之前（不收錢不給資源）。"""
        owner, svc = self._service('noconf')
        broken = self._values(svc)
        broken.pop('conf')
        svc._apply_helm_values(broken)
        old_sub = svc.sudo().subscription_id
        with mute_logger(_CONTROLLER):  # 預期中的 fail-loud traceback
            res = self._change(owner, svc, self.plan_b)
        self.assertFalse(res['success'])
        self._assert_untouched(svc, old_sub, broken)

    def test_rejects_while_init_lease_is_held(self):
        owner, svc = self._service('lease')
        old_sub, before = svc.sudo().subscription_id, self._values(svc)
        with patch.object(type(svc), '_init_lease_active', return_value=True):
            res = self._change(owner, svc, self.plan_b)
        self.assertFalse(res['success'])
        self._assert_untouched(svc, old_sub, before)

    # ---------------- allocated 手調 ---------------- #

    def test_hand_tuned_allocation_is_kept(self):
        """營運刻意調大的 allocated（例 prod service 174）不被租戶換方案洗掉。"""
        owner, svc = self._service('tuned')
        svc.sudo().write({'allocated_vcpu': 12})
        self._change(owner, svc, self.plan_b)
        self.assertEqual(svc.allocated_vcpu, 12)
        self.assertEqual(svc.allocated_ram_gb, 4.0, '沒手調的維度照常更新')

    # ---------------- 停權（Q7） ---------------- #

    def test_suspended_service_stays_suspended_and_is_scaled_down(self):
        owner, svc = self._service('susp')
        svc.sudo().write({'state': 'suspended', 'pre_suspend_state': 'running'})
        client = self._client()
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(svc.state, 'suspended')
        client.upgrade_release.assert_called_once()
        self.assertEqual(client.scale_release.call_args.kwargs.get('replicas'), 0)

    def test_background_failure_says_the_plan_was_changed(self):
        """Q5：背景失敗不回捲帳務，error_message 要講清楚「方案已換、資源沒調成」。"""
        owner, svc = self._service('failmsg')
        with patch.object(PaasController, '_start_background_upgrade') as start:
            res = self._change(owner, svc, self.plan_b)
        self.assertTrue(res['success'], res.get('error'))
        self.assertTrue(res['data']['resources_reapplied'])
        # #1552：文案改由 model 依服務建立者的語言組（背景執行緒沒有語言）。
        self.assertIs(start.call_args.kwargs['plan_change'], True)
        message = svc._upgrade_failure_message(Exception('boom'), plan_change=True)
        self.assertIn('The plan was changed', message)
        self.assertIn('boom', message)

    # ---------------- 收斂點（Q5／Q6） ---------------- #

    def test_assemble_converges_to_the_current_plan(self):
        """背景 upgrade 失敗後，下一次 assemble（reconfigure／redeploy）收斂到目前方案。"""
        owner, svc = self._service('conv')
        svc._create_and_confirm_subscription(
            svc.workspace_id.owner_id.partner_id, self.plan_b)
        assembled = svc._assemble_deploy_values()['values']
        self.assertEqual(assembled['conf']['workers'], 3)
        self.assertEqual(assembled['resources']['limits']['memory'], '4Gi')
        self.assertEqual(assembled['persistence']['size'], '13Gi')

    def test_assemble_never_shrinks_storage(self):
        owner, svc = self._service('conv-down', plan=self.plan_b)
        svc._create_and_confirm_subscription(
            svc.workspace_id.owner_id.partner_id, self.plan_a)
        assembled = svc._assemble_deploy_values()['values']
        self.assertEqual(assembled['persistence']['size'], '13Gi')
        self.assertEqual(assembled['backup']['persistence']['size'], '13Gi')
        self.assertEqual(assembled['conf']['workers'], 2)

    def test_assemble_is_a_no_op_for_a_service_on_its_plan(self):
        """prod 漂移盤點 0 台的前提：值已等於方案時，搬進 assemble 不改任何東西。"""
        owner, svc = self._service('noop-asm')
        stored = self._values(svc)
        assembled = svc._assemble_deploy_values()['values']
        for key in ('resources', 'conf'):
            self.assertEqual(assembled[key], stored[key])
        self.assertEqual(assembled['persistence']['size'], stored['persistence']['size'])


@tagged('post_install', '-at_install')
class TestLegacyScriptOverlayNotReapplied1158(TransactionCase):
    """Q3：Bitnami 字串替換只在 create 用；既有服務重算時不碰（建好後已不是 'workers = 0'）。"""

    def test_existing_overlay_skips_bitnami_scripts(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Bitnami 1158', 'slug': 'bitnami-1158',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'bitnami-1158', 'helm_chart_version': '1.0.0',
        })
        plan = self.env['sh.subscription.plan'].sudo().create({
            'name': '1158 legacy', 'sh_plan_price': 1, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'cs_vcpu': 2, 'cs_ram_gb': 4.0, 'cs_storage_gb': 30, 'cs_odoo_workers': 3,
        })
        values = {'customPostInitScripts': {'a.sh': "echo 'workers = 2'"}}
        out = Svc._apply_plan_spec_overlay(values, tpl, plan, existing=True)
        self.assertEqual(out['customPostInitScripts'], {'a.sh': "echo 'workers = 2'"})
        self.assertEqual(out['resources']['limits']['memory'], '4Gi')


_CAPACITY_DETAIL = {
    'error_code': 'storage_expansion_rejected', 'reason': 'capacity',
    'namespace': 'paas-ws-test-1158-x',
    'pvcs': [{'name': 'svc-data', 'live': '20Gi', 'rendered': '30Gi', 'reason': 'capacity'}],
    'message': '升級前檢查發現磁碟無法擴充，已停止升級（沒有套用任何變更）：…',
}


@tagged('post_install', '-at_install')
class TestChangePlanExpansionPreflight1513(_ChangePlanFixture, TransactionCase):
    """#1513：扣款前問 operator（``upgrade-preflight``）磁碟擴得了嗎；擴不了整筆擋下、不扣款。

    #1158 實測：Longhorn 排程額度不足時 data PVC 擴容被拒，背景 upgrade 失敗，而帳單已換。
    """

    @staticmethod
    def _api_error(status, detail):
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorAPIError
        return PaaSOperatorAPIError(message='API error: %s' % detail, status_code=status,
                                    detail=detail)

    def test_preflight_runs_with_the_exact_upgrade_arguments(self):
        owner, svc = self._service('pf-args')
        client = self._client()
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))
        pre = client.upgrade_preflight.call_args.kwargs
        up = client.upgrade_release.call_args.kwargs
        self.assertEqual(pre, up, '預檢與真正的 upgrade 必須是同一組參數，否則預檢沒有意義')
        self.assertEqual(pre['values']['persistence']['size'], '13Gi')
        self.assertEqual(pre['values']['conf']['workers'], 3)
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index('upgrade_preflight'), names.index('update_namespace_quota'))

    def test_capacity_denial_blocks_before_billing(self):
        owner, svc = self._service('pf-cap')
        old_sub, before = svc.sudo().subscription_id, self._values(svc)
        allocated = (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb)
        client = self._client()
        client.upgrade_preflight.side_effect = self._api_error(409, _CAPACITY_DETAIL)
        Monitor = type(self.env['woow_paas_platform.platform_monitor'])
        with patch.object(Monitor, '_report_storage_expansion_rejection') as alert:
            res = self._change(owner, svc, self.plan_b, client)
        self.assertFalse(res['success'])
        self.assertIn('storage', res['error'])
        self.assertEqual(res['data']['code'], 'storage_expansion_rejected')
        self._assert_untouched(svc, old_sub, before)
        self.assertEqual(
            (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb), allocated)
        self.assertEqual(svc.state, 'running')
        client.upgrade_release.assert_not_called()
        client.update_namespace_quota.assert_not_called()
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[-1], _CAPACITY_DETAIL)

    def test_other_preflight_conflict_blocks_too(self):
        """#1407 不可變欄位衝突（detail 是字串）——真正的 upgrade 也一定 409，同樣擋在扣款前。"""
        owner, svc = self._service('pf-1407')
        old_sub, before = svc.sudo().subscription_id, self._values(svc)
        client = self._client()
        client.upgrade_preflight.side_effect = self._api_error(409, '升級前檢查發現下列欄位…')
        res = self._change(owner, svc, self.plan_b, client)
        self.assertFalse(res['success'])
        self._assert_untouched(svc, old_sub, before)
        client.upgrade_release.assert_not_called()

    def test_unreachable_operator_blocks(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorConnectionError, PaaSOperatorTimeoutError)
        for exc in (PaaSOperatorConnectionError('down'), PaaSOperatorTimeoutError('slow'),
                    self._api_error(500, 'Helm 升級失敗：…')):
            with self.subTest(exc=type(exc).__name__):
                owner, svc = self._service('pf-down-%s-%s' % (
                    type(exc).__name__.lower()[-10:], exc.status_code or 0))
                old_sub, before = svc.sudo().subscription_id, self._values(svc)
                client = self._client()
                client.upgrade_preflight.side_effect = exc
                res = self._change(owner, svc, self.plan_b, client)
                self.assertFalse(res['success'])
                self._assert_untouched(svc, old_sub, before)

    def test_operator_without_the_endpoint_is_allowed(self):
        """舊版 operator（404）：放行，行為與 #1513 之前相同（發版順序：operator 先）。"""
        owner, svc = self._service('pf-404')
        client = self._client()
        client.upgrade_preflight.side_effect = self._api_error(404, 'Not Found')
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))
        client.upgrade_release.assert_called_once()

    def test_same_spec_change_does_not_ask(self):
        owner, svc = self._service('pf-same')
        client = self._client()
        self._change(owner, svc, self.plan_a_yearly, client)
        client.upgrade_preflight.assert_not_called()

    def test_capacity_denial_records_one_admin_activity(self):
        """不 mock 告警：真的開 activity 給管理員（同 summary 未處理前只追加一行）。"""
        Monitor = self.env['woow_paas_platform.platform_monitor']
        summary = Monitor.SUMMARY_STORAGE_EXPANSION_REJECTED
        owner, svc = self._service('pf-alert')
        client = self._client()
        client.upgrade_preflight.side_effect = self._api_error(409, _CAPACITY_DETAIL)
        self._change(owner, svc, self.plan_b, client)
        activities = self.env['mail.activity'].sudo().search([('summary', '=', summary)])
        self.assertTrue(activities)
        self.assertIn('id=%s' % svc.id, activities[0].note)
        self.assertIn('svc-data 20Gi→30Gi', activities[0].note)
