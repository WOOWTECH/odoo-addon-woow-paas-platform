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


@tagged('post_install', '-at_install')
class TestPricingSudoIsPublicReadOnly(HttpCase):
    """`_template_pricing` 的 `sudo()` 舉證（repo-lint controller-sudo gate 要求）。

    為什麼需要 sudo：起價要與前台 plan 選卡區（``api_template_plans``，同樣 sudo）
    **同源**，否則卡片會標一個使用者點進去看不到的價格。

    為什麼是安全的 —— 這組測試就是那個舉證：sudo 只用來**算聚合**，回給 caller 的
    永遠是 5 個純量（起價／單位／價位數／旗標），從不把 plan recordset 或其 id
    交出去；而且 sudo **之後**的過濾（storefront_hidden／active）仍然生效，隱藏
    價位不會因為繞過 ir.rule 而外洩。屬 gate 說的 platform public-read 型：
    marketplace 價格對每個登入者本來就該一樣。
    """

    PASSWORD = 'pricing478pw'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = cls.env.cr.dbname[-6:]
        partner = cls.env['res.partner'].create({
            'name': 'Pricing 478 Tenant', 'email': f'p478-{suffix}@t.example'})
        # 用**非 admin 的 internal user**當對照，而不是 portal：實測
        # （2026-07-31）portal 連 cloud_app_template 都讀不到（該 model 的 ACL
        # 只開 Internal User），marketplace 端點本來就不對 portal 開放——拿 portal
        # 當對照只會測到「它進不來」，測不到 sudo 有沒有替不同人算出不同價格。
        cls.tenant_user = cls.env['res.users'].create({
            'login': f'p478_{suffix}',
            'name': 'Pricing 478 Tenant',
            'password': cls.PASSWORD,
            'partner_id': partner.id,
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })

    def _result(self, url, params='{}'):
        body = '{"jsonrpc":"2.0","method":"call","params":' + params + '}'
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    def _templates_as(self, login, password):
        self.authenticate(login, password)
        r = self._result('/api/cloud/templates')
        self.assertTrue(r.get('success'), r)
        return {t['id']: t for t in r['data']}

    def test_pricing_is_identical_across_users(self):
        """平台級公開事實：一般租戶（internal user）看到的起價與 admin 完全一致。

        不一致就代表這個 sudo 在替不同人算出不同價格——那就不是 public-read，
        而是需要收斂的權限洩漏面。
        """
        as_admin = self._templates_as('admin', 'admin')
        as_portal = self._templates_as(self.tenant_user.login, self.PASSWORD)
        self.assertEqual(
            set(as_admin), set(as_portal), 'marketplace 可見清單不該因使用者而異')
        for tid, item in as_admin.items():
            self.assertEqual(
                item['pricing'], as_portal[tid]['pricing'],
                '%s 的起價因使用者而異' % item['name'])

    def test_pricing_leaks_no_plan_entities(self):
        """🔴 只回聚合純量，不回 plan recordset／id ——非 sudo-list-and-return。"""
        allowed = {'has_plans', 'min_price', 'unit', 'tier_count', 'is_free'}
        for item in self._templates_as(
                self.tenant_user.login, self.PASSWORD).values():
            pricing = item['pricing']
            self.assertEqual(
                set(pricing), allowed,
                '%s.pricing 多了非聚合鍵（疑似把 plan 實體交出去）：%s'
                % (item['name'], sorted(set(pricing) - allowed)))
            for value in pricing.values():
                self.assertNotIsInstance(
                    value, (list, dict),
                    '%s.pricing 出現容器型值 ⇒ 可能夾帶 plan 清單' % item['name'])

    def test_sudo_does_not_leak_hidden_price_tiers(self):
        """🔴 sudo 之後的過濾仍然生效：storefront_hidden 價位不得外洩。

        open-design 的 +1access 變體（5100，storefront_hidden）是現成樣本——
        它若因為繞過 ir.rule 而被算進去，tier_count 會變 2、卡片憑空多一個
        使用者看不到的價位。
        """
        tpl = self.env.ref('%s.cloud_app_opendesign' % MOD)
        hidden_prices = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: p.storefront_hidden and p.sh_unit == 'month'
        ).mapped('sh_plan_price')
        self.assertTrue(hidden_prices, '前提：open-design 應有隱藏的席數變體')

        pricing = self._templates_as(
            self.tenant_user.login, self.PASSWORD)[tpl.id]['pricing']
        self.assertEqual(pricing['tier_count'], 1)
        self.assertNotIn(
            pricing['min_price'], hidden_prices,
            '起價取到了 storefront_hidden 的價位')

    def test_sudo_does_not_bypass_marketplace_visibility(self):
        """sudo 只作用在價格，不得讓 hidden template 重新出現在清單（#471 曝光層）。"""
        ha_paas = self.env.ref('%s.cloud_app_ha_paas' % MOD)
        listed = self._templates_as(self.tenant_user.login, self.PASSWORD)
        self.assertNotIn(
            ha_paas.id, listed, 'hidden template 不得因 pricing 的 sudo 而曝光')
