# -*- coding: utf-8 -*-
"""#478：marketplace 卡片／configure header 的價格標籤（「Free」價格誤導）。

**根因**（動手前實查，比 issue 推測更直接）：不是「拿到 falsy 值 → fallback 顯示
Free」，而是 ``AppCard.js`` 的 ``formattedPrice`` getter **寫死 ``return "Free"``**、
``AppConfigurationPage.xml`` 的 header 直接硬寫字面值 ``Free``。列表 API 的
``_format_template`` 從來就沒回過任何價格欄位——兩件事疊在一起，於是每張卡片、每個
配置頁 header 都謊報免費，而下方 plan 區與總計是對的（同一畫面自相矛盾）。

裁決（leader3）：**A 為主、B 為底**——列表 API 帶起價、卡片顯示真實價格；查無價格時
**不渲染標籤**（B 的原則當底線），只有真 0 元才叫 Free。

本檔守 pricing 摘要的**行為矩陣**，重點在幾個「錯了就再次謊報價格」的點：

- **可見性判準必須與前台 plans 端點同源**（``api_template_plans`` 過濾
  ``storefront_hidden``）。不同源 ⇒ 卡片標一個使用者點進去看不到的價格——那是把
  #478 從「謊報免費」換成「謊報另一個數字」，沒有比較好。
- **archived plan 不得計入**。2026-07-31 stg 收尾把 0 元測試 plan 268 archive 掉，
  正是現成情境：inactive plan 若仍計入，hermes-agent 的起價會變回 0 → 又顯示 Free。
- **年繳-only** 的邊角要有明確 fallback 並固定住，否則它會落回「沒有價格」而不渲染，
  使用者又看不到價格。
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged

MOD = 'woow_paas_platform'


def _ctrl():
    from odoo.addons.woow_paas_platform.controllers.paas import PaasController
    return PaasController()


@tagged('post_install', '-at_install')
class TestTemplatePricingSummary(TransactionCase):
    """``_format_template()['pricing']`` 的行為矩陣。"""

    def _pricing(self, template):
        return _ctrl()._format_template(template)['pricing']

    def _make_template(self, slug):
        return self.env['%s.cloud_app_template' % MOD].create({
            'name': 'Pricing Fixture %s' % slug,
            'slug': slug,
            'helm_repo_url': 'oci://example.invalid',
            'helm_chart_name': 'pricing-fixture',
            'helm_chart_version': '0.0.0',
        })

    def _make_plan(self, name, price, unit='month', **extra):
        vals = {
            'name': name,
            'sh_duration': 1,
            'sh_unit': unit,
            'sh_plan_price': price,
            'service_type': 'cloud_service',
            # NOT NULL（無 default）——不給會撞 IntegrityError，測試就變成在測
            # DB schema 而不是測 pricing 摘要。-1 ＝不限次數，沿 seed 慣例。
            'sh_no_of_billing_cycle': -1,
        }
        vals.update(extra)
        return self.env['sh.subscription.plan'].create(vals)

    # ── seed 錨定（真實商品的實際顯示）────────────────────────────────────
    def test_single_monthly_tier_has_no_from_marker(self):
        """單一月繳價位 ⇒ 起價＝該價、tier_count 1（前端不加「起」）。"""
        pricing = self._pricing(self.env.ref('%s.cloud_app_hermes_agent' % MOD))
        self.assertTrue(pricing['has_plans'])
        self.assertEqual(pricing['min_price'], 4500)
        self.assertEqual(pricing['unit'], 'month')
        self.assertEqual(pricing['tier_count'], 1)
        self.assertFalse(pricing['is_free'])

    def test_multi_tier_takes_the_minimum(self):
        """n8n T1–T4（月繳 750／2250／4500／7500）⇒ 起價 750、tier_count 4。"""
        pricing = self._pricing(self.env.ref('%s.cloud_app_n8n' % MOD))
        self.assertEqual(pricing['min_price'], 750)
        self.assertEqual(pricing['tier_count'], 4)
        self.assertEqual(pricing['unit'], 'month')

    def test_storefront_hidden_variants_are_excluded(self):
        """🔴 與前台 plans 端點同源：席數變體（storefront_hidden）不進起價。

        open-design 的 base 是 4500、+1access 變體是 5100（後者
        ``storefront_hidden=True``）。變體若被計入，tier_count 會變 2 而讓卡片
        顯示「4500 pts/mo 起」——但使用者點進去只看得到一個 4500 的選項，
        「起」字就是憑空的第二個價位。
        """
        pricing = self._pricing(self.env.ref('%s.cloud_app_opendesign' % MOD))
        self.assertEqual(pricing['min_price'], 4500)
        self.assertEqual(
            pricing['tier_count'], 1,
            '變體被算進 tier_count ⇒ 卡片會多一個使用者看不到的價位')

    # ── 邊角（合成 fixture）──────────────────────────────────────────────
    def test_no_plans_renders_nothing(self):
        """無 plan ⇒ has_plans False ＋ min_price None（前端據此**不渲染**標籤）。

        這是 B 方案的底線：沒有價格資訊時保持沉默，而不是編一個「Free」出來。
        """
        pricing = self._pricing(self._make_template('no-plan-478'))
        self.assertFalse(pricing['has_plans'])
        self.assertIsNone(pricing['min_price'])
        self.assertFalse(pricing['is_free'])
        self.assertEqual(pricing['tier_count'], 0)

    def test_real_zero_price_is_free(self):
        """真 0 元 plan 才叫 Free（唯一還會顯示 Free 的情境）。"""
        tpl = self._make_template('zero-478')
        tpl.subscription_plan_ids = [(6, 0, [self._make_plan('Zero 478', 0).id])]
        pricing = self._pricing(tpl)
        self.assertTrue(pricing['has_plans'])
        self.assertEqual(pricing['min_price'], 0)
        self.assertTrue(pricing['is_free'])

    def test_archived_plan_is_excluded(self):
        """🔴 archived（active=False）plan 不得計入——stg 的 plan 268 現成情境。

        2026-07-31 stg 收尾把 0 元測試 plan archive 掉。若 inactive 仍被算進來，
        起價會被那顆 0 元拉到 0 ⇒ 付費商品又顯示 Free，#478 原地復活。
        """
        tpl = self._make_template('archived-478')
        paid = self._make_plan('Paid 478', 4500)
        zero = self._make_plan('Archived Zero 478', 0)
        tpl.subscription_plan_ids = [(6, 0, [paid.id, zero.id])]
        self.assertEqual(
            self._pricing(tpl)['min_price'], 0, '前提：兩顆都在時起價是 0')

        zero.active = False

        pricing = self._pricing(tpl)
        self.assertEqual(
            pricing['min_price'], 4500,
            'archived plan 仍被計入 ⇒ 付費商品又被標成 Free')
        self.assertFalse(pricing['is_free'])
        self.assertEqual(pricing['tier_count'], 1)

    def test_yearly_only_falls_back_to_year_unit(self):
        """年繳-only ⇒ 用年繳起價、unit='year'（而不是退回「沒有價格」）。

        沒有月繳不代表沒有價格；退回不渲染會讓使用者在卡片上完全看不到價錢，
        那是用另一種方式重演 #478。前端據 unit 顯示 ``pts/yr``。
        """
        tpl = self._make_template('yearly-only-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('Yearly Only 478', 36000, unit='year').id])]
        pricing = self._pricing(tpl)
        self.assertTrue(pricing['has_plans'])
        self.assertEqual(pricing['min_price'], 36000)
        self.assertEqual(pricing['unit'], 'year')
        self.assertEqual(pricing['tier_count'], 1)

    def test_monthly_wins_when_both_cycles_exist(self):
        """月繳與年繳並存 ⇒ 取月繳（36000 的年繳不得被當成「起價」）。"""
        tpl = self._make_template('both-cycles-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('M 478', 4500).id,
            self._make_plan('Y 478', 36000, unit='year').id,
        ])]
        pricing = self._pricing(tpl)
        self.assertEqual(pricing['min_price'], 4500)
        self.assertEqual(pricing['unit'], 'month')
        self.assertEqual(
            pricing['tier_count'], 1,
            '同一 tier 的月／年兩筆不是兩個價位，不該讓卡片顯示「起」')

    def test_pricing_matches_visible_plans_endpoint(self):
        """跨端點一致性：卡片起價必須存在於 plans 端點回給前台的價目中。

        兩邊各自演化就會再次出現「卡片一個價、點進去另一個價」——這條把它們綁死。
        """
        for xmlid in ('cloud_app_hermes_agent', 'cloud_app_n8n',
                      'cloud_app_opendesign', 'cloud_app_haos'):
            tpl = self.env.ref('%s.%s' % (MOD, xmlid))
            pricing = self._pricing(tpl)
            visible = tpl.sudo().subscription_plan_ids.filtered(
                lambda p: p.active and not p.storefront_hidden)
            prices = visible.filtered(
                lambda p: p.sh_unit == pricing['unit']).mapped('sh_plan_price')
            self.assertIn(
                pricing['min_price'], prices,
                '%s 的卡片起價不在前台看得到的價目內' % xmlid)


@tagged('post_install', '-at_install')
class TestMarketplaceEndpointCarriesPricing(HttpCase):
    """端到端：``/api/cloud/templates`` 真的把 pricing 帶到前端。

    unit 層證明算得對，這組證明「送得到」——#478 的另一半成因正是 API 從未回傳
    價格欄位，卡片只能自己編一個。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')

    def _result(self, url, params='{}'):
        body = '{"jsonrpc":"2.0","method":"call","params":' + params + '}'
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    def test_list_endpoint_includes_pricing(self):
        r = self._result('/api/cloud/templates')
        self.assertTrue(r.get('success'), r)
        self.assertTrue(r['data'], '前提：marketplace 至少有一個可見 template')
        for item in r['data']:
            self.assertIn(
                'pricing', item,
                '%s 缺 pricing ⇒ 卡片拿不到價格，只能重演硬編 Free' % item['name'])
            for key in ('has_plans', 'min_price', 'unit', 'tier_count', 'is_free'):
                self.assertIn(key, item['pricing'], '%s.pricing 缺 %s' % (item['name'], key))

    def test_hermes_agent_card_price_is_the_real_one(self):
        """實際商品：Hermes Agent 的卡片價格＝4500／月，不是 Free。"""
        tpl = self.env.ref('%s.cloud_app_hermes_agent' % MOD)
        r = self._result('/api/cloud/templates')
        item = next((t for t in r['data'] if t['id'] == tpl.id), None)
        self.assertIsNotNone(item, 'Hermes Agent 應在 marketplace 清單內')
        self.assertEqual(item['pricing']['min_price'], 4500)
        self.assertEqual(item['pricing']['unit'], 'month')
        self.assertFalse(
            item['pricing']['is_free'], '付費商品不得被標成 Free（#478 本體）')

    def test_detail_endpoint_includes_pricing(self):
        """configure header 讀的是 by-id 端點，同樣要帶 pricing。"""
        tpl = self.env.ref('%s.cloud_app_hermes_agent' % MOD)
        r = self._result('/api/cloud/templates/%s' % tpl.id)
        self.assertTrue(r.get('success'), r)
        self.assertIn('pricing', r['data'])
        self.assertEqual(r['data']['pricing']['min_price'], 4500)
