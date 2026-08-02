# -*- coding: utf-8 -*-
"""#332：CS tier→部署規格映射（plan cs_* 欄位＋runtime overlay）。"""
import copy
import json

from odoo.tests.common import HttpCase, TransactionCase, tagged


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


@tagged('post_install', '-at_install')
class TestOverlayLandsInStoredHelmValues(HttpCase):
    """建立服務後，**存下來的 helm_values** 必須帶著 plan 規格（wire-level）。

    取代原本的 `inspect.getsource(paas) + getsource(cloud_service)` 再
    `count('_apply_plan_spec_overlay') >= 2` 子字串守衛。那條有兩個洞：

    1. 註解裡提到兩次就餵飽（`# 用 _apply_plan_spec_overlay 疊規格` 寫兩行即可）；
    2. `>= 2` 讓「呼叫被刪、只剩定義 + 一個註解」照樣過——正是它該擋的那個回歸。

    改測行為結果：走使用者唯一入口（`/api/workspaces/<id>/services` 的
    `action='create'`）建立服務，再讀 DB 裡 `service.helm_values`——那份 JSON 就是
    之後交給 Helm 的東西。overlay 沒被呼叫時，值會停在 template 的 base 預設。

    🔴 陽性對照內建：base 預設值與 plan 規格**刻意取不同值**，且同一條測試先斷言
    「服務真的建起來了、helm_values 解得出 JSON、未被 overlay 的 key 原樣保留」。
    少了這些，`helm_values` 是空 dict／建立根本失敗時，規格斷言會退化成看不出
    差別的假綠。
    """

    # base 與 plan 規格刻意全維度不同：任何一維沒被 overlay 都會被抓到
    BASE_VALUES = {
        'resources': {'requests': {'cpu': '1', 'memory': '1Gi'},
                      'limits': {'cpu': '1', 'memory': '1Gi'}},
        'persistence': {'size': '10Gi'},
        'untouchedKey': 'keep-me',
    }
    PLAN_SPEC = {'cs_vcpu': 3, 'cs_ram_gb': 4.0, 'cs_storage_gb': 40}

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 332 overlay', 'owner_id': self.admin_user.id})

        # 訂閱建立需要 company.sh_journal_id；bare install 的 at_install 環境不
        # 保證已設好（見 test_create_service_validation_rollback_520 的同段註解）。
        # 沒有它「建立成功」走不完，本檔的規格斷言就會退化成假綠。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 332', 'code': 'TS332', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            company.sh_journal_id,
            '本檔需要可用的訂閱 journal，否則建立服務走不完、規格斷言退化成假綠')

        # 合成 plan／template：不綁 seed 商品的定價與規格（#478／#494 教訓）
        self.plan = self.env['sh.subscription.plan'].create(dict(
            self.PLAN_SPEC,
            name='Plan 332 overlay', sh_duration=1, sh_unit='month',
            sh_plan_price=100, sh_no_of_billing_cycle=-1,
            service_type='cloud_service'))
        self.env['product.template'].create({
            'name': 'Product 332 overlay', 'type': 'service', 'list_price': 100,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id})
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Overlay 332', 'slug': 'overlay-332',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/overlay-332',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False,
            'helm_default_values': json.dumps(self.BASE_VALUES),
            'subscription_plan_ids': [(6, 0, [self.plan.id])]})
        self.env.flush_all()

    def _create(self, name):
        body = (
            '{"jsonrpc":"2.0","method":"call","params":'
            '{"action":"create","template_id":%s,"name":"%s","plan_id":%s}}'
            % (self.tpl.id, name, self.plan.id)
        )
        resp = self.url_open(
            '/api/workspaces/%s/services' % self.ws.id,
            data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def test_created_service_helm_values_carry_plan_spec(self):
        result = self._create('svc-332-overlay')

        # 陽性對照①：服務真的建起來了（失敗的話下面全部無從談起）
        self.assertTrue(result.get('success'), '建立服務應成功（回 %r）' % result)
        svc = self.CloudService.search([
            ('template_id', '=', self.tpl.id),
            ('workspace_id', '=', self.ws.id)])
        self.assertEqual(len(svc), 1, '應恰建立一筆服務（實得 %s 筆）' % len(svc))

        # 陽性對照②：helm_values 解得出 JSON，且沒被 overlay 的 key 原樣保留
        # ——證明讀到的確實是組裝出來的那份 values，而不是空 dict／別的東西
        stored = json.loads(svc.helm_values)
        self.assertEqual(
            stored.get('untouchedKey'), 'keep-me',
            'template base values 沒進到 helm_values ⇒ 讀錯東西了，'
            '下面的規格斷言不具鑑別力')

        # 核心：plan 的 cs_* 必須落進要交給 Helm 的那份 values
        for scope in ('requests', 'limits'):
            self.assertEqual(
                stored['resources'][scope]['cpu'], '3',
                'resources.%s.cpu 仍是 template base 的 1 ⇒ overlay 沒接進'
                ' _create_service（cs_vcpu=3）' % scope)
            self.assertEqual(
                stored['resources'][scope]['memory'], '4Gi',
                'resources.%s.memory 仍是 template base 的 1Gi ⇒ overlay 沒接進'
                ' _create_service（cs_ram_gb=4.0）' % scope)
        self.assertEqual(
            stored['persistence']['size'], '40Gi',
            'persistence.size 仍是 template base 的 10Gi ⇒ overlay 沒接進'
            ' _create_service（cs_storage_gb=40）')

    def test_allocated_fields_follow_plan_spec(self):
        """同一條路徑的另一半：allocated_* 也必須跟著選定 plan（#332）。"""
        result = self._create('svc-332-allocated')
        self.assertTrue(result.get('success'), '建立服務應成功（回 %r）' % result)
        svc = self.CloudService.search([
            ('template_id', '=', self.tpl.id),
            ('workspace_id', '=', self.ws.id)])
        self.assertEqual(len(svc), 1)
        self.assertEqual(
            (svc.allocated_vcpu, svc.allocated_ram_gb, svc.allocated_storage_gb),
            (3, 4.0, 40),
            'allocated_* 應取自 plan 的 cs_*（實得 %s）'
            % [svc.allocated_vcpu, svc.allocated_ram_gb,
               svc.allocated_storage_gb])
