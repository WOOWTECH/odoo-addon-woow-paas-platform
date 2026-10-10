# -*- coding: utf-8 -*-
"""#1532：n8n 的方案規格要寫進 8gears chart 真正讀的路徑。

8gears n8n chart 2.0.1 沒有頂層 ``resources``／``persistence``：Pod 讀 ``main.resources``、
PVC 讀 ``main.persistence.size``。以前 overlay 一律寫頂層，chart 完全沒讀到——所有 n8n
方案實際都是範本預設（100m/256Mi～1/1Gi、PVC 2Gi），畫面卻顯示 10～40 GB；「Database
Type」選 postgres 也從來沒有作用（chart 不讀頂層 ``config``）。

這裡鎖：

- overlay 寫 ``main.*``、不再寫頂層；舊 overlay 留下的頂層殘值在 overlay 時移除
  （形狀不符的頂層值不碰）。其他範本（woow-odoo）仍寫頂層——由 test_cs_spec_332 鎖。
- 範本 seed 不再有 ``config.database.type``（預設值與選項都拿掉）。
- #1532 以前開通的 n8n（存的 values 是「頂層殘值＋main 2Gi」）：redeploy／reconfigure
  的 assemble 收斂到目前方案；換方案時 ``allocated_*`` 照常更新，不被誤判成營運手調。

render 層（helm template 8gears 2.0.1）的證據在 PR 說明；這裡只測平台送出的 values。
"""
import copy
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from .test_change_plan_reapply_1158 import _ChangePlanFixture

GIB = 1024 ** 3


def _legacy_values(template_values, plan_vcpu, plan_ram_gb, plan_storage_gb):
    """#1532 以前開通的 n8n 存下的 values：範本預設＋舊 seed 的 database.type＋頂層殘值。"""
    values = copy.deepcopy(template_values)
    values['config'] = {'database': {'type': 'sqlite'}}
    quantity = {'cpu': str(plan_vcpu), 'memory': '%sGi' % int(plan_ram_gb)}
    values['resources'] = {'requests': dict(quantity), 'limits': dict(quantity)}
    values['persistence'] = {'size': '%sGi' % plan_storage_gb}
    return values


@tagged('post_install', '-at_install')
class TestN8nPlanOverlayPaths1532(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.base = json.loads(self.tpl.helm_default_values)

    def _overlay(self, plan_xmlid, values=None, existing=False):
        plan = self.env.ref('woow_paas_platform.%s' % plan_xmlid)
        values = copy.deepcopy(self.base if values is None else values)
        return plan, self.Svc._apply_plan_spec_overlay(
            values, self.tpl, plan, existing=existing)

    def test_seed_main_defaults_differ_from_every_plan(self):
        """陽性對照的前提：範本預設與每個 n8n 方案都不同，overlay 沒寫到就看得出來。"""
        main = self.base['main']
        self.assertEqual(main['persistence']['size'], '2Gi')
        self.assertEqual(main['resources']['requests'], {'cpu': '100m', 'memory': '256Mi'})
        for xmlid in ('plan_n8n_monthly', 'plan_n8n_t4_monthly'):
            plan = self.env.ref('woow_paas_platform.%s' % xmlid)
            self.assertGreater(plan.cs_storage_gb, 2, xmlid)
            self.assertTrue(plan.cs_vcpu and plan.cs_ram_gb, xmlid)

    def test_overlay_writes_main_paths_for_every_plan(self):
        for xmlid in ('plan_n8n_monthly', 'plan_n8n_t2_monthly',
                      'plan_n8n_t3_monthly', 'plan_n8n_t4_monthly'):
            with self.subTest(plan=xmlid):
                plan, out = self._overlay(xmlid)
                cpu = str(plan.cs_vcpu)
                mem = '%sGi' % int(plan.cs_ram_gb)
                self.assertEqual(out['main']['resources'], {
                    'requests': {'cpu': cpu, 'memory': mem},
                    'limits': {'cpu': cpu, 'memory': mem},
                })
                self.assertEqual(out['main']['persistence']['size'],
                                 '%sGi' % plan.cs_storage_gb)
                self.assertNotIn('resources', out, '8gears chart 不讀頂層 resources')
                self.assertNotIn('persistence', out, '8gears chart 不讀頂層 persistence')

    def test_overlay_keeps_the_rest_of_main(self):
        """只動規格鍵：persistence 的其他欄位、config、secret 原樣保留。"""
        _plan, out = self._overlay('plan_n8n_t4_monthly')
        for key in ('enabled', 'type'):
            self.assertEqual(out['main']['persistence'][key], self.base['main']['persistence'][key])
        self.assertEqual(out['main']['config'], self.base['main']['config'])
        self.assertEqual(out['main']['secret'], self.base['main']['secret'])
        self.assertEqual(out['image'], self.base['image'])

    def test_existing_service_storage_only_grows(self):
        """既有服務（換方案／assemble）：main.persistence.size 同樣只擴不縮（#1512）。"""
        values = copy.deepcopy(self.base)
        values['main']['persistence']['size'] = '50Gi'
        _plan, out = self._overlay('plan_n8n_t4_monthly', values, existing=True)
        self.assertEqual(out['main']['persistence']['size'], '50Gi')
        values['main']['persistence']['size'] = '2Gi'
        plan, out = self._overlay('plan_n8n_monthly', values, existing=True)
        self.assertEqual(out['main']['persistence']['size'], '%dGi' % plan.cs_storage_gb)

    def test_legacy_top_level_residue_is_removed(self):
        legacy = _legacy_values(self.base, 1, 1.0, 10)
        plan, out = self._overlay('plan_n8n_t4_monthly', legacy, existing=True)
        self.assertNotIn('resources', out)
        self.assertNotIn('persistence', out)
        self.assertEqual(out['main']['persistence']['size'], '%dGi' % plan.cs_storage_gb)
        self.assertEqual(out['main']['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})

    def test_top_level_values_of_another_shape_are_kept(self):
        """殘值判準是形狀：不是舊 overlay 寫的形狀就不碰（不誤刪有意寫的值）。"""
        values = copy.deepcopy(self.base)
        values['resources'] = {'requests': {'cpu': '1', 'nvidia.com/gpu': '1'}}
        values['persistence'] = {'size': '10Gi', 'annotations': {'a': 'b'}}
        _plan, out = self._overlay('plan_n8n_monthly', values)
        self.assertEqual(out['resources'], values['resources'])
        self.assertEqual(out['persistence'], values['persistence'])

    def test_zero_spec_plan_is_noop(self):
        zero_plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        legacy = _legacy_values(self.base, 1, 1.0, 10)
        out = self.Svc._apply_plan_spec_overlay(
            copy.deepcopy(legacy), self.tpl, zero_plan)
        self.assertEqual(out, legacy, 'cs_* 全 0 的方案不動任何鍵（含殘值）')

    def test_other_templates_still_write_top_level(self):
        odoo = self.env.ref('woow_paas_platform.cloud_app_odoo')
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        out = self.Svc._apply_plan_spec_overlay(
            json.loads(odoo.helm_default_values), odoo, plan)
        self.assertEqual(out['resources']['limits']['cpu'], '3')
        # #1637：odoo 方案是整台總量 30＝資料 13＋PG 4＋備份 13；資料那顆仍在頂層。
        self.assertEqual(out['persistence']['size'], '13Gi')
        self.assertNotIn('resources', out.get('main') or {})


@tagged('post_install', '-at_install')
class TestN8nSeedDatabaseOption1532(TransactionCase):
    """「Database Type」選項拿掉：chart 不讀、也沒附 PostgreSQL，選了只會誤導。"""

    def test_no_database_type_default(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        values = json.loads(tpl.helm_default_values)
        self.assertNotIn('config', values)
        self.assertNotIn('DB_TYPE', values['main']['config'])

    def test_no_database_type_option(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        keys = tpl._tenant_value_keys()
        self.assertNotIn('config.database.type', keys)
        self.assertFalse([k for k in keys if 'database' in k.lower() or 'DB_' in k], keys)


class _N8nChangePlanFixture(_ChangePlanFixture):
    """沿用 #1158 的換方案 fixture，但範本換成 n8n、方案沒有 workers。"""

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
        cls.tpl = env.ref('woow_paas_platform.cloud_app_n8n')
        cls.plan_a = cls._plan('1532 A', 100.0, vcpu=1, ram=1.0, storage=10, workers=0)
        cls.plan_a_yearly = cls._plan(
            '1532 A yearly', 1000.0, vcpu=1, ram=1.0, storage=10, workers=0)
        cls.plan_b = cls._plan('1532 B', 200.0, vcpu=2, ram=4.0, storage=40, workers=0)
        cls.tpl.sudo().write({'subscription_plan_ids': [
            (4, p.id) for p in (cls.plan_a, cls.plan_a_yearly, cls.plan_b)]})
        cls.controller = PaasController()

    def _legacy_service(self, tag):
        """#1532 以前開通的樣子：stored values 是頂層殘值＋main 2Gi，allocated 照方案。"""
        owner, svc = self._service(tag)
        svc._apply_helm_values(_legacy_values(
            json.loads(self.tpl.helm_default_values), 1, 1.0, 10))
        return owner, svc


@tagged('post_install', '-at_install')
class TestN8nChangePlan1532(_N8nChangePlanFixture, TransactionCase):

    def test_new_service_values_carry_the_plan_in_main(self):
        _owner, svc = self._service('new')
        values = self._values(svc)
        self.assertEqual(values['main']['persistence']['size'], '10Gi')
        self.assertEqual(values['main']['resources']['limits'], {'cpu': '1', 'memory': '1Gi'})
        self.assertNotIn('persistence', values)

    def test_upgrade_reapplies_into_main(self):
        owner, svc = self._service('up')
        client = self._client()
        res = self._change(owner, svc, self.plan_b, client)
        self.assertTrue(res['success'], res.get('error'))
        values = self._values(svc)
        self.assertEqual(values['main']['resources']['limits'], {'cpu': '2', 'memory': '4Gi'})
        self.assertEqual(values['main']['persistence']['size'], '40Gi')
        self.assertEqual(
            (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb),
            (2, 4.0, 40))
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertEqual(sent['main']['resources']['requests'], {'cpu': '2', 'memory': '4Gi'})
        self.assertEqual(sent['main']['persistence']['size'], '40Gi')
        self.assertNotIn('resources', sent)
        self.assertNotIn('persistence', sent)

    def test_downgrade_keeps_main_storage(self):
        owner, svc = self._service('down', plan=self.plan_b)
        res = self._change(owner, svc, self.plan_a)
        self.assertTrue(res['success'], res.get('error'))
        values = self._values(svc)
        self.assertEqual(values['main']['persistence']['size'], '40Gi', '儲存只擴不縮（#1512）')
        self.assertEqual(values['main']['resources']['limits'], {'cpu': '1', 'memory': '1Gi'})
        self.assertEqual(svc.allocated_storage_gb, 40, 'allocated 記實際大小')

    def test_legacy_service_change_plan_updates_allocation(self):
        """舊服務的 main 還是 2Gi：不得被當成「營運手調」而保留舊的 allocated。"""
        owner, svc = self._legacy_service('legacy-up')
        self.assertEqual(svc.allocated_storage_gb, 10, '前提：allocated 照舊方案')
        res = self._change(owner, svc, self.plan_b)
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(
            (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb),
            (2, 4.0, 40))
        values = self._values(svc)
        self.assertEqual(values['main']['persistence']['size'], '40Gi')
        self.assertNotIn('persistence', values)

    def test_legacy_service_same_spec_change_now_applies_the_spec(self):
        """月繳↔年繳同規格：舊服務的 values 原本沒套到方案，這次會 upgrade 補上（收得到錢的資源）。"""
        owner, svc = self._legacy_service('legacy-same')
        client = self._client()
        res = self._change(owner, svc, self.plan_a_yearly, client)
        self.assertTrue(res['success'], res.get('error'))
        self.assertTrue(res['data']['resources_reapplied'])
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertEqual(sent['main']['persistence']['size'], '10Gi')
        self.assertEqual(sent['main']['resources']['limits'], {'cpu': '1', 'memory': '1Gi'})
        self.assertEqual(svc.allocated_storage_gb, 10)

    def test_legacy_service_assemble_converges(self):
        """redeploy／reconfigure 的 assemble：舊服務收斂到目前方案、頂層殘值不再送出。"""
        _owner, svc = self._legacy_service('legacy-asm')
        assembled = svc._assemble_deploy_values()['values']
        self.assertEqual(assembled['main']['persistence']['size'], '10Gi')
        self.assertEqual(assembled['main']['resources']['requests'], {'cpu': '1', 'memory': '1Gi'})
        self.assertNotIn('resources', assembled)
        self.assertNotIn('persistence', assembled)

    def test_assemble_is_a_no_op_for_a_new_service_on_its_plan(self):
        _owner, svc = self._service('noop-asm')
        stored = self._values(svc)
        assembled = svc._assemble_deploy_values()['values']
        self.assertEqual(assembled['main']['resources'], stored['main']['resources'])
        self.assertEqual(assembled['main']['persistence']['size'],
                         stored['main']['persistence']['size'])
