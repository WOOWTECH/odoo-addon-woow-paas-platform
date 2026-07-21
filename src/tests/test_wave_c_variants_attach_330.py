# -*- coding: utf-8 -*-
"""Wave C PR-C1：後端 variants 回帶（_attach_variants helper ＋ 三端點擴充）。

- TestAttachVariantsCorpus：全庫 216 hidden 席數變體以 xmlid 反查歸戶，零跳過、
  皆掛到「可見」base（fail-loud 測試層，把 generator xmlid 慣例釘死）。
- TestAttachVariantsShape：抽 tier 代表驗 seats 集合／升序／price／plan_id 反查。
- TestEndpointsWiring：機械守衛三端點皆呼叫 helper 並塞 variants key。
- TestTemplatePlansEndpointsVariants：三個 *_template_plans 端點回傳的 base dict
  帶正確 variants（wire-level，HttpCase json-rpc）。

設計：docs/plans/2026-Q3/2026-07-14-wave-c-seat-picker-{design,implementation}.md
Refs #330（AC 席數變體母案）。
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged

MOD = 'woow_paas_platform'


@tagged('post_install', '-at_install')
class TestAttachVariantsCorpus(TransactionCase):
    """全庫反解斷言：216 變體零跳過、皆掛可見 base。"""

    def test_all_hidden_variants_resolve(self):
        Plan = self.env['sh.subscription.plan']
        variants = Plan.search(
            [('ac_max_access_seats', '>', 0), ('storefront_hidden', '=', True)])
        self.assertEqual(len(variants), 216, '前置：216 個 hidden 席數變體')

        # helper 對「全庫」跑：base 反解走 ir.model.data，不依賴 base 在 self 內。
        result = Plan.search([])._attach_variants()

        attached = {v['plan_id'] for lst in result.values() for v in lst}
        self.assertEqual(
            attached, set(variants.ids),
            '每筆 hidden 變體都應反解出 base 並掛上（零跳過）')

        # 每個 base key 存在且「非 hidden」：否則變體掛到隱藏 base、picker 靜默缺失
        # 而 corpus 計數仍算 resolved——把這個 invariant 對未來釘死。
        for base_id in result:
            base = Plan.browse(base_id)
            self.assertTrue(base.exists(), 'base id=%s 不存在' % base_id)
            self.assertFalse(
                base.storefront_hidden,
                '變體不應掛到 hidden base %s' % base.name)

    def test_base_and_variant_ids_disjoint(self):
        """base key 與變體 plan_id 不得交集（反解不得把變體當 base）。"""
        Plan = self.env['sh.subscription.plan']
        result = Plan.search([])._attach_variants()
        base_ids = set(result.keys())
        variant_ids = {v['plan_id'] for lst in result.values() for v in lst}
        self.assertTrue(base_ids, '前置：應有 base 掛上變體')
        self.assertFalse(base_ids & variant_ids, 'base 與變體 id 不得交集')


@tagged('post_install', '-at_install')
class TestAttachVariantsShape(TransactionCase):
    """抽 tier 代表：seats 集合／升序／price／plan_id 反查正確。"""

    def _variants_for(self, template_xmlid, base_xmlid):
        template = self.env.ref('%s.%s' % (MOD, template_xmlid))
        result = template.sudo().subscription_plan_ids._attach_variants()
        base = self.env.ref('%s.%s' % (MOD, base_xmlid))
        return result.get(base.id, [])

    def test_odoo_t1_monthly_three_seats(self):
        variants = self._variants_for('cloud_app_odoo', 'plan_odoo_monthly')
        self.assertEqual([v['seats'] for v in variants], [1, 2, 3])
        # price 對 seed（ac1=2100, ac2=2700, ac3=3300）
        self.assertEqual([v['price'] for v in variants], [2100, 2700, 3300])
        ac1 = self.env.ref('%s.plan_odoo_ac1_monthly' % MOD)
        self.assertEqual(variants[0]['plan_id'], ac1.id)

    def test_sa_pro_yearly_ten_seats(self):
        variants = self._variants_for(
            'security_access_default', 'plan_security_access_pro_yearly')
        self.assertEqual([v['seats'] for v in variants], list(range(1, 11)))

    def test_mcp_odoo_single_seat(self):
        variants = self._variants_for(
            'cloud_app_mcp_odoo', 'plan_mcp_odoo_monthly')
        self.assertEqual([v['seats'] for v in variants], [1])

    def test_n8n_t4_monthly_sorted_ten(self):
        variants = self._variants_for('cloud_app_n8n', 'plan_n8n_t4_monthly')
        seats = [v['seats'] for v in variants]
        self.assertEqual(seats, sorted(seats), 'seats 必須升序')
        self.assertEqual(seats, list(range(1, 11)), 'n8n t4 = 1-10 席')

    def test_base_without_variants_absent(self):
        """無變體的 base（Hermes：hidden base 且無變體）→ helper 不含其 key。"""
        template = self.env.ref('%s.cloud_app_mcp_hermes' % MOD)
        result = template.sudo().subscription_plan_ids._attach_variants()
        for xmlid in ('plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly'):
            base = self.env.ref('%s.%s' % (MOD, xmlid))
            self.assertNotIn(base.id, result)


@tagged('post_install', '-at_install')
class TestEndpointsWiring(TransactionCase):
    """機械守衛：三端點必須呼叫 _attach_variants 並塞 variants key。"""

    def test_three_endpoints_wire_variants(self):
        import inspect
        from odoo.addons.woow_paas_platform.controllers import (
            paas, smart_home, security_access,
        )
        for mod in (paas, smart_home, security_access):
            src = inspect.getsource(mod).replace('"', "'")
            self.assertIn(
                '_attach_variants', src,
                '%s 未呼叫 _attach_variants' % mod.__name__)
            self.assertIn(
                "'variants'", src,
                '%s 未塞 variants key' % mod.__name__)


@tagged('post_install', '-at_install')
class TestTemplatePlansEndpointsVariants(HttpCase):
    """三個 *_template_plans 端點回傳 base dict 帶正確 variants（wire-level）。"""

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

    def _find(self, data, plan_xmlid):
        pid = self.env.ref('%s.%s' % (MOD, plan_xmlid)).id
        for row in data:
            if row['id'] == pid:
                return row
        self.fail('base plan %s 不在回傳集合' % plan_xmlid)

    def test_cs_endpoint_variants(self):
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        r = self._result('/api/cloud-services/templates/%s/plans' % tpl.id)
        self.assertTrue(r['success'], r)
        row = self._find(r['data'], 'plan_odoo_monthly')
        self.assertIn('variants', row)
        self.assertEqual([v['seats'] for v in row['variants']], [1, 2, 3])
        self.assertEqual(
            set(row['variants'][0].keys()), {'seats', 'plan_id', 'price'})

    def test_sa_endpoint_variants(self):
        tpl = self.env.ref('%s.security_access_default' % MOD)
        r = self._result('/api/security-access/templates/%s/plans' % tpl.id)
        self.assertTrue(r['success'], r)
        row = self._find(r['data'], 'plan_security_access_pro_yearly')
        self.assertIn('variants', row)
        self.assertEqual(
            [v['seats'] for v in row['variants']], list(range(1, 11)))

    def test_sh_endpoint_variants(self):
        tpl = self.env.ref('%s.smart_home_home_assistant' % MOD)
        r = self._result('/api/smart-home/templates/%s/plans' % tpl.id)
        self.assertTrue(r['success'], r)
        row = self._find(r['data'], 'plan_smarthome_monthly')
        self.assertIn('variants', row)
        self.assertEqual(
            [v['seats'] for v in row['variants']], list(range(1, 11)))

    def test_variant_not_a_top_level_row(self):
        """變體不得自身成為頂層 base row（storefront_hidden 過濾仍在）。"""
        tpl = self.env.ref('%s.cloud_app_odoo' % MOD)
        r = self._result('/api/cloud-services/templates/%s/plans' % tpl.id)
        top_ids = {row['id'] for row in r['data']}
        variant = self.env.ref('%s.plan_odoo_ac1_monthly' % MOD)
        self.assertNotIn(
            variant.id, top_ids, '變體不得出現在頂層 plans 清單')
