# -*- coding: utf-8 -*-
"""#332：CS tier→部署規格映射（plan cs_* 欄位＋runtime overlay）。"""
import copy
import json

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCsSpecFields(TransactionCase):

    def test_fields_exist_default_zero(self):
        plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        for f in ('cs_vcpu', 'cs_ram_gb', 'cs_storage_gb', 'cs_odoo_workers'):
            self.assertIn(f, plan._fields)

    def test_workers_require_ram_constraint(self):
        """M4 防呆：workers>0 且 ram=0 的組合被 constraint 擋。"""
        from odoo.exceptions import ValidationError
        plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        with self.assertRaises(ValidationError):
            plan.write({'cs_odoo_workers': 2})


CS_SPEC_EXPECTED = {
    'plan_odoo_monthly': (2, 2.0, 20, 2), 'plan_odoo_yearly': (2, 2.0, 20, 2),
    'plan_odoo_t2_monthly': (2, 4.0, 30, 3), 'plan_odoo_t2_yearly': (2, 4.0, 30, 3),
    'plan_odoo_t3_monthly': (3, 4.0, 40, 4), 'plan_odoo_t3_yearly': (3, 4.0, 40, 4),
    'plan_n8n_monthly': (1, 1.0, 10, 0), 'plan_n8n_yearly': (1, 1.0, 10, 0),
    'plan_n8n_t2_monthly': (1, 2.0, 20, 0), 'plan_n8n_t2_yearly': (1, 2.0, 20, 0),
    'plan_n8n_t3_monthly': (2, 2.0, 30, 0), 'plan_n8n_t3_yearly': (2, 2.0, 30, 0),
}

# 裁定①（2026-07-12 by Eugene）：T1 storage 採 matrix 值（odoo 20 / n8n 10），
# 為刻意 tier 提升（20>10、10>5，不 regress）。
T1_STORAGE_MATRIX = {'plan_odoo_monthly': 20, 'plan_n8n_monthly': 10}


@tagged('post_install', '-at_install')
class TestCsSpecSeed(TransactionCase):

    def test_cs_12_plans_spec(self):
        for xmlid, (vcpu, ram, storage, workers) in CS_SPEC_EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb,
                 plan.cs_odoo_workers),
                (vcpu, ram, storage, workers), xmlid)

    def test_migration_idempotent(self):
        Plan = self.env['sh.subscription.plan']
        Plan._woow_migrate_cs_spec_332()
        Plan._woow_migrate_cs_spec_332()
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        self.assertEqual(plan.cs_vcpu, 3)

    def test_non_cs_plans_untouched(self):
        for xmlid in ('plan_smarthome_monthly',
                      'plan_security_access_entry_monthly'):
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), (0, 0.0, 0))

    def test_t1_spec_vcpu_ram_match_min_and_storage_matrix(self):
        """T1 規格斷言（裁定① 2026-07-12）：vcpu/ram == template.min_*（兩維相等
        仍成立）；cs_storage_gb == matrix 常數（odoo 20/n8n 10）**且** >=
        template.min_storage_gb（不縮保證，20>10／10>5 為刻意 tier 提升非回歸）。
        """
        for tpl_xmlid, plan_xmlid in (
            ('cloud_app_odoo', 'plan_odoo_monthly'),
            ('cloud_app_n8n', 'plan_n8n_monthly'),
        ):
            tpl = self.env.ref(f'woow_paas_platform.{tpl_xmlid}')
            t1 = self.env.ref(f'woow_paas_platform.{plan_xmlid}')
            self.assertEqual(t1.cs_vcpu, tpl.min_vcpu, tpl_xmlid)
            self.assertEqual(t1.cs_ram_gb, tpl.min_ram_gb, tpl_xmlid)
            self.assertEqual(t1.cs_storage_gb, T1_STORAGE_MATRIX[plan_xmlid],
                             tpl_xmlid)
            self.assertGreaterEqual(
                t1.cs_storage_gb, tpl.min_storage_gb,
                f'{tpl_xmlid}：T1 storage 不得小於 template 現值')

    def test_xml_and_migration_tracks_identical(self):
        """雙軌防漂移：fresh XML 值經 migration delegate 重跑後必須不變。"""
        Plan = self.env['sh.subscription.plan']
        before = {
            xmlid: (p.cs_vcpu, p.cs_ram_gb, p.cs_storage_gb, p.cs_odoo_workers)
            for xmlid in CS_SPEC_EXPECTED
            for p in [self.env.ref(f'woow_paas_platform.{xmlid}')]
        }
        Plan._woow_migrate_cs_spec_332()
        for xmlid, snap in before.items():
            p = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (p.cs_vcpu, p.cs_ram_gb, p.cs_storage_gb, p.cs_odoo_workers),
                snap, f'{xmlid}：XML 軌與 migration 軌漂移')


@tagged('post_install', '-at_install')
class TestAllocatedFromPlan(TransactionCase):

    def test_allocated_prefers_plan_spec(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        vals = Svc._allocated_vals_from(tpl, plan)
        self.assertEqual(vals, {
            'allocated_vcpu': 3, 'allocated_ram_gb': 4.0,
            'allocated_storage_gb': 40,
        })

    def test_allocated_fallback_template(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        zero_plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')  # cs_* 全 0
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        vals = Svc._allocated_vals_from(tpl, zero_plan)
        self.assertEqual(vals, {
            'allocated_vcpu': tpl.min_vcpu,
            'allocated_ram_gb': tpl.min_ram_gb,
            'allocated_storage_gb': tpl.min_storage_gb,
        })


@tagged('post_install', '-at_install')
class TestPlanSpecOverlay(TransactionCase):

    def _base_values(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        return json.loads(tpl.helm_default_values)

    def test_overlay_resources_and_persistence(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        base = self._base_values()
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, plan)
        self.assertEqual(out['resources']['requests']['cpu'], '3')
        self.assertEqual(out['resources']['requests']['memory'], '4Gi')
        self.assertEqual(out['persistence']['size'], '40Gi')

    def test_overlay_workers_substitution(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        out = Svc._apply_plan_spec_overlay(self._base_values(), tpl, plan)
        scripts = json.dumps(out.get('customPostInitScripts', {}))
        self.assertIn('workers = 4', scripts)
        self.assertNotIn('workers = 0', scripts)
        self.assertNotIn('limit_memory_hard = 0', scripts)

    def test_zero_spec_plan_is_noop(self):
        """cs_* 全 0 → overlay 輸出必須與輸入完全相同（現狀零變保證）。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        zero_plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        base = self._base_values()
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, zero_plan)
        self.assertEqual(out, base)

    def test_n8n_no_workers_touch(self):
        """n8n plan（workers=0）：不碰 script；resources 照 overlay。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        plan = self.env.ref('woow_paas_platform.plan_n8n_t3_monthly')
        base = json.loads(tpl.helm_default_values)
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, plan)
        self.assertEqual(out['resources']['requests']['cpu'], '2')
        self.assertEqual(
            out.get('customPostInitScripts'), base.get('customPostInitScripts'))

    def test_overlay_wired_into_create_path(self):
        """接線守衛：overlay 必須在部署組裝路徑上被呼叫（定義 1 次＋呼叫 ≥1 次）。"""
        import inspect
        from odoo.addons.woow_paas_platform.controllers import paas
        from odoo.addons.woow_paas_platform.models import cloud_service
        combined = (inspect.getsource(paas) + inspect.getsource(cloud_service))
        self.assertGreaterEqual(
            combined.count('_apply_plan_spec_overlay'), 2,
            'overlay 已定義但未接進 _create_service 部署組裝路徑')
