# -*- coding: utf-8 -*-
"""#1634（使用者 2026-10-10）：Nextcloud 整台磁碟 20G——四顆 PVC 加起來剛好 20Gi。

以前：使用者檔案 80＋html 5＋PostgreSQL 10＋Redis 1＝96Gi，方案 cs_storage_gb＝80 只對應
使用者檔案那顆。Longhorn 依宣告大小預留（96Gi × 2 副本＝192G），31zix 的排程額度快滿。

現在：方案 cs_storage_gb＝20＝**整台總量**；overlay 寫進 chart 方案鍵 ``persistence.size``
（chart ``nextcloud.dataSize``＝``persistence.size | default persistence.data.size``）的是
20 − html 3 − PostgreSQL 3 − Redis 1＝13Gi（``_PLAN_STORAGE_SPLIT``）。

這裡鎖：

- seed：四顆加總＝min_storage_gb＝兩個方案的 cs_storage_gb＝20；分配 13／3／3／1。
- 開通：每個 Nextcloud 方案 overlay 後，chart 會用的四顆加總＝方案總量；帳面
  allocated_storage_gb＝方案總量。方案比固定開銷還小、固定開銷讀不到 ⇒ fail-loud。
- 既有服務／換方案：月繳↔年繳四顆不變；升級擴使用者檔案那顆、加總＝新方案；降級只擴
  不縮；比較用 chart 實際用的大小（``persistence.size`` 空時讀 ``persistence.data.size``），
  不會把舊的 80Gi 縮成 13Gi；算不出來保留現值（redeploy 不失敗）。
- migration 18.0.1.0.223：noupdate=1 的方案 80 → 20，手調過的不覆蓋、冪等。
- 其他範本（odoo／n8n）不受影響：方案鍵＝方案總量。

render 層（nextcloud chart 0.1.7 的 ``persistence.size`` → data PVC）由 charts repo
``charts/nextcloud/tests/test-render-contract.sh`` 鎖；沙盒實裝證據在 PR 說明。
"""
import copy
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from .test_change_plan_reapply_1158 import _ChangePlanFixture

MOD = 'woow_paas_platform'
PLAN_XMLIDS = ('plan_nextcloud_monthly', 'plan_nextcloud_yearly')


def _gi(size):
    assert size.endswith('Gi'), size
    return int(size[:-2])


def _chart_pvc_gib(values):
    """nextcloud chart 0.1.7 會建的四顆 PVC（GiB）：data 讀 persistence.size，空時讀 data.size。"""
    persistence = values['persistence']
    return {
        'data': _gi(persistence.get('size') or persistence['data']['size']),
        'html': _gi(persistence['html']['size']),
        'db': _gi(values['postgresql']['persistence']['size']),
        'redis': _gi(values['redis']['persistence']['size']),
    }


def _legacy_values(base, with_plan_key=True):
    """#1634 以前開通的 Nextcloud 存下的 values：80＋5＋10＋1，方案鍵 80Gi。"""
    values = copy.deepcopy(base)
    values['persistence']['data']['size'] = '80Gi'
    values['persistence']['html']['size'] = '5Gi'
    values['postgresql']['persistence']['size'] = '10Gi'
    values['redis']['persistence']['size'] = '1Gi'
    if with_plan_key:
        values['persistence']['size'] = '80Gi'
    return values


@tagged('post_install', '-at_install')
class TestNextcloudStorage20g1634(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.Plan = self.env['sh.subscription.plan']
        self.tpl = self.env.ref('%s.cloud_app_nextcloud' % MOD)
        self.base = json.loads(self.tpl.helm_default_values)

    def _plan(self, xmlid):
        return self.env.ref('%s.%s' % (MOD, xmlid))

    def _test_plan(self, storage):
        return self.Plan.sudo().create({
            'name': '1634 NC %s' % storage, 'sh_plan_price': 1.0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'cs_vcpu': 2, 'cs_ram_gb': 4.0, 'cs_storage_gb': storage,
        })

    def _overlay(self, plan, values=None, existing=False):
        values = copy.deepcopy(self.base if values is None else values)
        return self.Svc._apply_plan_spec_overlay(values, self.tpl, plan, existing=existing)

    # ---------------- seed ---------------- #

    def test_seed_allocation_is_20_in_total(self):
        self.assertEqual(self.tpl.min_storage_gb, 20)
        self.assertEqual(_chart_pvc_gib(self.base),
                         {'data': 13, 'html': 3, 'db': 3, 'redis': 1})
        for xmlid in PLAN_XMLIDS:
            with self.subTest(plan=xmlid):
                self.assertEqual(self._plan(xmlid).cs_storage_gb, 20)
        self.assertEqual(self._plan('plan_nextcloud_monthly').sh_plan_price, 4500, '價格不變')
        self.assertEqual(self._plan('plan_nextcloud_yearly').sh_plan_price, 45000, '價格不變')

    def test_descriptions_say_20_gb_in_total(self):
        self.assertIn('20 GB 磁碟（含使用者檔案 13 GB）', self.tpl.description)
        self.assertIn('整台 20 GB，其中使用者檔案 13 GB', self.tpl.full_description)
        for text in (self.tpl.description, self.tpl.full_description):
            self.assertNotIn('80 GB', text)
            self.assertNotIn('96 GB', text)

    def test_chart_pvcs_without_plan_key_also_sum_to_min_storage(self):
        """範本自己（沒有方案 overlay）也要一致：data.size 是 chart 的 fallback。"""
        self.assertNotIn('size', self.base['persistence'])
        self.assertEqual(sum(_chart_pvc_gib(self.base).values()), self.tpl.min_storage_gb)

    # ---------------- 開通 ---------------- #

    def test_new_service_four_pvcs_sum_to_the_plan(self):
        """方案 20 → data PVC 13Gi，四顆加總 20Gi＝帳面 allocated_storage_gb。"""
        for xmlid in PLAN_XMLIDS:
            with self.subTest(plan=xmlid):
                plan = self._plan(xmlid)
                out = self._overlay(plan)
                self.assertEqual(out['persistence']['size'], '13Gi')
                pvcs = _chart_pvc_gib(out)
                self.assertEqual(pvcs['data'], 13)
                self.assertEqual(sum(pvcs.values()), plan.cs_storage_gb)
                self.assertEqual(
                    self.Svc._allocated_vals_from(self.tpl, plan)['allocated_storage_gb'],
                    20)
                self.assertEqual(out['resources']['limits'], {'cpu': '2', 'memory': '4Gi'},
                                 'CPU／RAM 照舊寫頂層 resources（chart deep-merge 進 nextcloud）')

    def test_bigger_plan_grows_only_the_user_data_volume(self):
        out = self._overlay(self._test_plan(50))
        pvcs = _chart_pvc_gib(out)
        self.assertEqual(pvcs, {'data': 43, 'html': 3, 'db': 3, 'redis': 1})
        self.assertEqual(sum(pvcs.values()), 50)

    def test_plan_not_bigger_than_fixed_volumes_fails_loud(self):
        for storage in (7, 5):
            with self.subTest(storage=storage):
                with self.assertRaises(ValueError):
                    self._overlay(self._test_plan(storage))

    def test_unreadable_fixed_volume_fails_loud_on_create(self):
        values = copy.deepcopy(self.base)
        del values['postgresql']['persistence']['size']
        with self.assertRaises(ValueError):
            self._overlay(self._plan('plan_nextcloud_monthly'), values)

    def test_zero_storage_plan_leaves_the_template_sizes(self):
        plan = self._test_plan(0)
        out = self._overlay(plan)
        self.assertNotIn('size', out['persistence'])
        self.assertEqual(sum(_chart_pvc_gib(out).values()), 20)

    # ---------------- 既有服務 ---------------- #

    def test_existing_service_on_its_plan_is_a_no_op(self):
        plan = self._plan('plan_nextcloud_monthly')
        created = self._overlay(plan)
        again = self._overlay(plan, created, existing=True)
        self.assertEqual(again['persistence'], created['persistence'])

    def test_existing_service_never_shrinks(self):
        """#1634 以前開通的 80Gi 服務：套 20 方案不得送出 13Gi（PVC 不能縮，整次 upgrade 會失敗）。"""
        plan = self._plan('plan_nextcloud_monthly')
        out = self._overlay(plan, _legacy_values(self.base), existing=True)
        self.assertEqual(out['persistence']['size'], '80Gi')

    def test_existing_compares_against_what_the_chart_uses(self):
        """方案鍵空、chart 用的是 data.size 80Gi：不能當成「沒有現值」寫入 13Gi。"""
        plan = self._plan('plan_nextcloud_monthly')
        legacy = _legacy_values(self.base, with_plan_key=False)
        out = self._overlay(plan, legacy, existing=True)
        self.assertEqual(_chart_pvc_gib(out)['data'], 80)

    def test_existing_unreadable_fixed_volume_keeps_the_size(self):
        plan = self._plan('plan_nextcloud_monthly')
        values = _legacy_values(self.base)
        del values['redis']['persistence']['size']
        with mute_logger('odoo.addons.woow_paas_platform.models.cloud_service'):
            out = self._overlay(plan, values, existing=True)
        self.assertEqual(out['persistence']['size'], '80Gi')

    # ---------------- 其他範本 ---------------- #

    def test_other_templates_keep_plan_key_equal_to_plan_total(self):
        odoo = self.env.ref('%s.cloud_app_odoo' % MOD)
        out = self.Svc._apply_plan_spec_overlay(
            json.loads(odoo.helm_default_values), odoo,
            self._plan('plan_odoo_t3_monthly'))
        self.assertEqual(out['persistence']['size'], '40Gi')
        n8n = self.env.ref('%s.cloud_app_n8n' % MOD)
        out = self.Svc._apply_plan_spec_overlay(
            json.loads(n8n.helm_default_values), n8n, self._plan('plan_n8n_t4_monthly'))
        self.assertEqual(out['main']['persistence']['size'], '40Gi')

    # ---------------- migration 18.0.1.0.223 ---------------- #

    def test_migration_table_matches_the_seed(self):
        table = self.Plan._NEXTCLOUD_STORAGE_1634
        self.assertEqual(set(table), set(PLAN_XMLIDS))
        for xmlid, (old, new) in table.items():
            self.assertEqual((old, new), (80, 20), xmlid)
            self.assertEqual(self._plan(xmlid).cs_storage_gb, new, 'seed 與 migration 同值')

    def test_migration_moves_old_plans_to_20(self):
        for xmlid in PLAN_XMLIDS:
            self._plan(xmlid).write({'cs_storage_gb': 80})
        self.Plan._woow_migrate_nextcloud_storage_1634()
        for xmlid in PLAN_XMLIDS:
            plan = self._plan(xmlid)
            self.assertEqual(plan.cs_storage_gb, 20, xmlid)
            self.assertEqual((plan.cs_vcpu, plan.cs_ram_gb), (2, 4.0), 'CPU／RAM 不動')
        self.assertEqual(self._plan('plan_nextcloud_monthly').sh_plan_price, 4500)
        self.assertEqual(self._plan('plan_nextcloud_yearly').sh_plan_price, 45000)

    def test_migration_keeps_hand_tuned_plans_and_is_idempotent(self):
        self._plan('plan_nextcloud_monthly').write({'cs_storage_gb': 50})
        self._plan('plan_nextcloud_yearly').write({'cs_storage_gb': 80})
        self.Plan._woow_migrate_nextcloud_storage_1634()
        self.Plan._woow_migrate_nextcloud_storage_1634()
        self.assertEqual(self._plan('plan_nextcloud_monthly').cs_storage_gb, 50, '手調過的不覆蓋')
        self.assertEqual(self._plan('plan_nextcloud_yearly').cs_storage_gb, 20)


class _NextcloudChangePlanFixture(_ChangePlanFixture):
    """沿用 #1158 的換方案 fixture，範本換成 Nextcloud、方案用 seed 的月繳／年繳＋一個 50G 測試方案。"""

    @classmethod
    def setUpClass(cls):
        super(_ChangePlanFixture, cls).setUpClass()  # 跳過 woow-odoo 專用的那段
        env = cls.env
        company = env.company.sudo()
        if not company.sh_journal_id:
            journal = env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        cls.tpl = env.ref('%s.cloud_app_nextcloud' % MOD)
        cls.plan_a = env.ref('%s.plan_nextcloud_monthly' % MOD)
        cls.plan_a_yearly = env.ref('%s.plan_nextcloud_yearly' % MOD)
        cls.plan_b = cls._plan('1634 NC 50', 200.0, vcpu=2, ram=4.0, storage=50, workers=0)
        cls.tpl.sudo().write({'subscription_plan_ids': [(4, cls.plan_b.id)]})
        cls.controller = PaasController()


@tagged('post_install', '-at_install')
class TestNextcloudChangePlan1634(_NextcloudChangePlanFixture, TransactionCase):

    def _pvcs(self, svc):
        return _chart_pvc_gib(self._values(svc))

    def test_new_service_is_20_in_total(self):
        _owner, svc = self._service('nc-new')
        self.assertEqual(self._pvcs(svc), {'data': 13, 'html': 3, 'db': 3, 'redis': 1})
        self.assertEqual(svc.allocated_storage_gb, 20)

    def test_monthly_to_yearly_keeps_20_and_does_not_restart(self):
        owner, svc = self._service('nc-same')
        client = self._client()
        res = self._change(owner, svc, self.plan_a_yearly, client)
        self.assertTrue(res['success'], res.get('error'))
        client.upgrade_release.assert_not_called()
        self.assertEqual(sum(self._pvcs(svc).values()), 20)
        self.assertEqual(svc.allocated_storage_gb, 20)

    def test_upgrade_grows_user_data_and_tracks_the_total(self):
        owner, svc = self._service('nc-up')
        client = self._client()
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(self._pvcs(svc), {'data': 43, 'html': 3, 'db': 3, 'redis': 1})
        self.assertEqual(svc.allocated_storage_gb, 50)
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertEqual(sent['persistence']['size'], '43Gi')

    def test_downgrade_keeps_storage_and_records_the_real_total(self):
        owner, svc = self._service('nc-down', plan=self.plan_b)
        self.assertEqual(svc.allocated_storage_gb, 50)
        res = self._change(owner, svc, self.plan_a)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(self._pvcs(svc)['data'], 43, '儲存只擴不縮（#1512）')
        self.assertEqual(svc.allocated_storage_gb, 50, 'allocated 記實際四顆加總')

    def test_assemble_is_a_no_op_for_a_new_service(self):
        _owner, svc = self._service('nc-asm')
        stored = self._values(svc)
        assembled = svc._assemble_deploy_values()['values']
        self.assertEqual(assembled['persistence']['size'], stored['persistence']['size'])
        self.assertEqual(sum(_chart_pvc_gib(assembled).values()), 20)
