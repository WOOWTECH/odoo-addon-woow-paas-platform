# -*- coding: utf-8 -*-
"""#368 PR-1：後端 additive-only（group_key + specs）。

- TestGroupKeys368：對「可見且掛 template」的 base 全量斷言 _group_keys()——
  每個 group_key 恰有 month+year 兩筆，且與 base xmlid 的 cycle 尾綴交叉一致
  （防手改 seed XML 打錯 cycle）。
- TestDisplaySpecs368：_get_display_specs 依 service_type 分流，含 CS
  cs_vcpu=0 時 fallback template.min_vcpu 的守線（postgresql base）。
- TestEndpointOrderUnchanged368：neutrality guard——controller 加了
  group_key/specs 兩鍵後，可見 base 的回傳順序仍與原生 recordset filtered
  順序（未經任何 .sorted()）完全一致。

設計：本 PR 的 brief（scratchpad/pr1-brief.md）＋ #368 選擇器重設計母案。
Refs #368
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged

MOD = 'woow_paas_platform'


@tagged('post_install', '-at_install')
class TestGroupKeys368(TransactionCase):
    """可見且掛 template 的 base 兩兩配對（month+year），group_key 與 xmlid 一致。"""

    def _visible_templated_bases(self):
        Plan = self.env['sh.subscription.plan']
        bases = self.env['woow_paas_platform.cloud_app_template'].search(
            []).subscription_plan_ids
        bases |= self.env[
            'woow_paas_platform.smart_home_template'].search(
                []).subscription_plan_ids
        bases |= self.env[
            'woow_paas_platform.security_access_template'].search(
                []).subscription_plan_ids
        return bases.filtered(
            lambda p: p.ac_max_access_seats == 0 and not p.storefront_hidden)

    def test_group_key_pairs_month_and_year(self):
        bases = self._visible_templated_bases()
        # 前置：目前 seed 有 34 個可見且掛 template 的 base plan（17 tier）；
        # 數字變動代表 seed 拓撲改了，測試需同步檢視。
        self.assertEqual(len(bases), 34, '前置：34 個可見且掛 template 的 base plan')

        group_keys = bases._group_keys()
        self.assertEqual(
            set(group_keys.keys()), set(bases.ids),
            '每個可見 base 都應有 group_key（零略過）')

        # 獨立反查 xmlid（不透過 _group_keys 內部邏輯），交叉驗證 cycle 一致，
        # 防止手改 seed XML 把 monthly plan 的 sh_unit 打成 year 之類的錯誤。
        imd = self.env['ir.model.data'].sudo()
        rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', MOD),
            ('res_id', 'in', bases.ids),
        ])
        xmlid_by_res = {row.res_id: row.name for row in rows}

        by_group = {}
        for base in bases:
            xmlid = xmlid_by_res.get(base.id)
            self.assertTrue(xmlid, 'base id=%s 缺 xmlid' % base.id)
            if xmlid.endswith('_monthly'):
                cycle, expected_unit = 'monthly', 'month'
            elif xmlid.endswith('_yearly'):
                cycle, expected_unit = 'yearly', 'year'
            else:
                self.fail('base xmlid %s 無 _monthly/_yearly 尾綴' % xmlid)
            self.assertEqual(
                base.sh_unit, expected_unit,
                'xmlid %s 尾綴 cycle 與 sh_unit=%s 不一致' % (xmlid, base.sh_unit))
            by_group.setdefault(group_keys[base.id], set()).add(cycle)

        self.assertEqual(len(by_group), 17, '前置：17 個 group_key（tier）')
        for gk, cycles in by_group.items():
            self.assertEqual(
                cycles, {'monthly', 'yearly'},
                'group_key %s 應恰有 month+year 兩筆，實際 %s' % (gk, cycles))


@tagged('post_install', '-at_install')
class TestDisplaySpecs368(TransactionCase):
    """_get_display_specs 依 service_type 分流，含 CS fallback 守線。"""

    def test_display_specs_by_service_type(self):
        # (a) odoo base：CS，cs_vcpu 有設定值 → specs 含 vCPU
        odoo_tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        odoo_plan = self.env.ref('%s.plan_odoo_monthly' % MOD)
        odoo_specs = odoo_plan._get_display_specs(odoo_tpl)
        self.assertTrue(
            any('vCPU' in s['label'] for s in odoo_specs),
            'odoo base 應顯示 vCPU 規格：%s' % odoo_specs)

        # (b) postgresql base：cs_vcpu=0（seed 未設）→ 必須 fallback
        # template.min_vcpu，否則卡片空白（本 PR 的關鍵守線）。
        pg_tpl = self.env.ref('%s.cloud_app_postgresql' % MOD)
        pg_plan = self.env.ref('%s.plan_postgresql_monthly' % MOD)
        self.assertEqual(
            pg_plan.cs_vcpu, 0, '前置：postgresql base cs_vcpu 未設＝0')
        pg = pg_plan._get_display_specs(pg_tpl)
        self.assertTrue(
            any('vCPU' in s['label'] for s in pg),
            'postgresql base cs_vcpu=0 應 fallback template.min_vcpu，'
            '非空白：%s' % pg)

        # (c) SA r1 base：security_access → specs 含 routes
        sa_plan = self.env.ref('%s.plan_security_access_r1_monthly' % MOD)
        sa_specs = sa_plan._get_display_specs()
        self.assertTrue(
            any('route' in s['label'] for s in sa_specs),
            'SA r1 base 應顯示 routes 規格：%s' % sa_specs)

        # (d) SH p2（mcp_integration 非空）→ 含 MCP；p1（無 mcp）→ []
        sh_p2 = self.env.ref('%s.plan_smarthome_mcp_monthly' % MOD)
        sh_p2_specs = sh_p2._get_display_specs()
        self.assertTrue(
            any('MCP' in s['label'] for s in sh_p2_specs),
            'SH p2（mcp）base 應顯示 MCP 規格：%s' % sh_p2_specs)

        sh_p1 = self.env.ref('%s.plan_smarthome_monthly' % MOD)
        sh_p1_specs = sh_p1._get_display_specs()
        self.assertEqual(sh_p1_specs, [], 'SH p1（無 mcp）base 規格應為 []')


@tagged('post_install', '-at_install')
class TestEndpointOrderUnchanged368(HttpCase):
    """Neutrality guard：加 group_key/specs 後，controller 回傳序不得被重排。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')

    def _result(self, url, params='{}'):
        resp = self.url_open(
            url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params + '}',
            headers={'Content-Type': 'application/json'})
        body = resp.json()
        self.assertIn('result', body, 'resp: %s' % body)
        return body['result']

    def test_endpoint_order_and_keys_unchanged(self):
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        r = self._result('/api/cloud-services/templates/%s/plans' % tpl.id)
        self.assertTrue(r['success'], r)

        returned_ids = [row['id'] for row in r['data']]
        # 原生 recordset 序（未經任何 sort）＝ neutrality 基準。刻意不比較
        # sorted('id')：M2M 序未必＝id 升序，比對 sorted 值證不到 controller
        # 真的沒重排，只有比對「原生 filtered 序」才鎖得住 controller 沒
        # 誤加 .sorted()（例如誤用 group_key 字串排序）。
        native_ids = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden).ids
        self.assertGreater(
            len(native_ids), 1, '前置：odoo template 應有多個可見 base')
        self.assertEqual(
            returned_ids, native_ids,
            'controller 回傳序應與原生 recordset filtered 序完全一致'
            '（不得 .sorted()）')

        for row in r['data']:
            self.assertIn(
                'group_key', row, 'base id=%s 缺 group_key 鍵' % row['id'])
            self.assertIn(
                'specs', row, 'base id=%s 缺 specs 鍵' % row['id'])
