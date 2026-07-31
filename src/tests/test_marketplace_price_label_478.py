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

- **可見性判準必須與前台 plans 端點同源**（``api_template_plans``）。不同源 ⇒ 卡片標
  一個使用者點進去看不到的價格——那是把 #478 從「謊報免費」換成「謊報另一個數字」，
  沒有比較好。這條由 ``test_card_price_exists_in_the_plans_endpoint`` **實打另一個
  端點**比對；刻意不在測試裡重寫一份過濾條件（那是拿實作驗實作，兩邊一起錯仍全綠）。
- **archived plan 不得計入**。2026-07-31 stg 收尾把 0 元測試 plan 268 archive 掉，
  正是現成情境：inactive plan 若仍計入，起價會被拉回 0 → 付費商品又顯示 Free。
- **每一種「有價格卻顯示不出來」都是 #478 的鏡像**：年繳-only、day/week 計價、
  freemium 的 0 元入門階。三者各有一條測試釘住——判準統一是「有價格就該顯示」，
  「不渲染」只保留給真的沒有可見 plan。

⚠ **本檔一律用合成 fixture，不錨定任何真實商品的價目**。價格是商業決策、隨時會改，
寫進斷言就是把「今天賣多少」鎖成契約，下次調價會在一個講「價格摘要邏輯」的檔案裡集體
變紅（本 repo 反覆踩過）。各商品的價目錨定留在該商品自己的 seed 測試檔。唯一掃真實資料
的兩條走「不變式」形態——付費商品不得標 Free、卡片起價必須在 plans 端點的價目內——
任何商品調價都不該讓它們紅。
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

    # ── 性質（一律合成 fixture）──────────────────────────────────────────
    #
    # ⚠ 這裡**刻意不錨定任何真實商品的價格**。價目是商業決策、隨時會改，寫進斷言
    # 就是把「今天賣多少」鎖成契約，下次調價會在一個講「價格摘要邏輯」的檔案裡
    # 集體變紅（本 repo 反覆踩過，見
    # docs 與 feedback_test_pins_transient_fact_as_contract 的教訓）。各商品自己的
    # 價目錨定留在該商品的 seed 測試檔（例 test_opendesign_seed_380.py 已在釘
    # 4500＋變體 hidden），本檔只測**規則**。
    def test_single_tier_has_no_from_marker(self):
        """單一月繳價位 ⇒ 起價＝該價、tier_count 1（前端不加「起」）。"""
        tpl = self._make_template('single-tier-478')
        tpl.subscription_plan_ids = [(6, 0, [self._make_plan('S 478', 4500).id])]
        pricing = self._pricing(tpl)
        self.assertTrue(pricing['has_plans'])
        self.assertEqual(pricing['min_price'], 4500)
        self.assertEqual(pricing['unit'], 'month')
        self.assertEqual(pricing['tier_count'], 1)
        self.assertFalse(pricing['is_free'])

    def test_multi_tier_takes_the_minimum(self):
        """多階月繳 ⇒ 起價取最低、tier_count＝distinct 價位數（前端加「起」）。"""
        tpl = self._make_template('multi-tier-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('T%d 478' % i, price).id
            for i, price in enumerate((2250, 750, 7500, 4500))
        ])]
        pricing = self._pricing(tpl)
        self.assertEqual(pricing['min_price'], 750, '起價必須是最低價，不是第一筆')
        self.assertEqual(pricing['tier_count'], 4)
        self.assertEqual(pricing['unit'], 'month')

    def test_duplicate_prices_count_as_one_tier(self):
        """同價位重複不算多階——否則卡片會憑空出現「起」。"""
        tpl = self._make_template('dup-price-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('A 478', 3000).id,
            self._make_plan('B 478', 3000).id,
        ])]
        self.assertEqual(self._pricing(tpl)['tier_count'], 1)

    def test_storefront_hidden_plans_are_excluded(self):
        """🔴 與前台 plans 端點同源：storefront_hidden 的 plan 不進起價。

        席數變體就藏在這個旗標後面。被計入的話 tier_count 會多一個**使用者點進去
        看不到**的價位，卡片憑空長出「起」字。

        ⚠ 用合成 fixture 而不是拿 open-design 當樣本：變體的 storefront_hidden 是
        **明文的暫時狀態**（subscription_products_variants.xml：「Wave C picker
        落地前不上前台」，且 plan visibility 已有翻正機制、#431 對 Hermes 實際
        執行過）。Wave C 一落地，錨在真實商品上的 tier_count 斷言會在 pricing
        完全正確的情況下變紅，還會把 reviewer 導向不存在的問題。
        """
        tpl = self._make_template('hidden-plan-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('Base 478', 4500).id,
            self._make_plan('Variant 478', 5100, storefront_hidden=True).id,
        ])]
        pricing = self._pricing(tpl)
        self.assertEqual(pricing['min_price'], 4500)
        self.assertEqual(
            pricing['tier_count'], 1,
            'storefront_hidden 的 plan 被算進 tier_count ⇒ 卡片會多一個'
            '使用者看不到的價位')

    def test_hidden_plan_does_not_become_the_floor(self):
        """更嚴的一面：隱藏 plan 比 base 便宜時，也不得被當成起價。"""
        tpl = self._make_template('hidden-cheaper-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('Base 478', 4500).id,
            self._make_plan('Cheap Hidden 478', 100, storefront_hidden=True).id,
        ])]
        self.assertEqual(
            self._pricing(tpl)['min_price'], 4500,
            '起價取到了 storefront_hidden 的價位 ⇒ 卡片標一個買不到的便宜價')

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

    def test_unusual_billing_unit_still_gets_a_price(self):
        """🔴 day/week 之類的週期也要有價格，不能落回「不渲染」。

        `api_template_plans` **完全不濾 unit**，所以只認 month/year 的話，這種
        template 會變成「卡片沒有價格標籤、點進去 plan 選卡區卻列得出價目」——
        那是 #478 的鏡像（謊報「不知道」而不是謊報免費）。判準與年繳-only 的
        fallback 同源：有價格就該顯示。
        """
        tpl = self._make_template('weekly-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('W 478', 300, unit='week').id])]
        pricing = self._pricing(tpl)
        self.assertTrue(pricing['has_plans'])
        self.assertEqual(pricing['min_price'], 300)
        self.assertEqual(pricing['unit'], 'week')

    def test_freemium_entry_tier_is_not_plain_free(self):
        """🔴 0 元入門階＋付費階 ⇒ is_free 但 tier_count>1（前端顯示「Free 起」）。

        只寫 Free 是 #478「謊報免費」的鏡像：使用者以為整個商品免費，點進去才
        看到付費階。後端在這裡只負責把兩個事實都送出去，措辭由
        `pricing_label.formatTemplatePrice` 決定。
        """
        tpl = self._make_template('freemium-478')
        tpl.subscription_plan_ids = [(6, 0, [
            self._make_plan('Free tier 478', 0).id,
            self._make_plan('Pro tier 478', 4500).id,
        ])]
        pricing = self._pricing(tpl)
        self.assertTrue(pricing['is_free'], '起價 0')
        self.assertEqual(
            pricing['tier_count'], 2,
            'freemium 的付費階必須反映在 tier_count，否則前端只會寫 Free')


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

    def test_no_paid_template_is_advertised_as_free(self):
        """🔴 #478 本體：只要 template 掛著非 0 元的可見 plan，就不得被標成 Free。

        用「全清單掃描」而不是釘某個商品的價目——價目是商業決策會變，而「付費的
        不准標免費」是不變式。任何一個商品哪天調價都不該讓這條紅。
        """
        r = self._result('/api/cloud/templates')
        self.assertTrue(r.get('success'), r)
        offenders = [
            t['name'] for t in r['data']
            if t['pricing']['has_plans'] and t['pricing']['min_price']
            and t['pricing']['is_free']
        ]
        self.assertFalse(offenders, '付費商品被標成 Free：%s' % offenders)
        priced = [t for t in r['data'] if t['pricing']['has_plans']]
        self.assertTrue(priced, '前提：marketplace 至少有一個掛了 plan 的商品')
        for t in priced:
            self.assertIsNotNone(
                t['pricing']['min_price'], '%s 有 plan 卻算不出起價' % t['name'])
            self.assertIn(
                t['pricing']['unit'], ('month', 'year', 'day', 'week'),
                '%s 的計價週期異常：%r' % (t['name'], t['pricing']['unit']))

    def test_detail_endpoint_includes_pricing(self):
        """configure header 讀的是 by-id 端點，同樣要帶 pricing。"""
        tpl = self.env.ref('%s.cloud_app_hermes_agent' % MOD)
        r = self._result('/api/cloud/templates/%s' % tpl.id)
        self.assertTrue(r.get('success'), r)
        self.assertIn('pricing', r['data'])
        self.assertIsNotNone(r['data']['pricing']['min_price'])

    def test_card_price_exists_in_the_plans_endpoint(self):
        """🔴 跨端點一致性——**實打** ``/api/cloud-services/templates/<id>/plans``。

        卡片起價必須真的出現在 plan 選卡區給前台的價目裡，否則就是「卡片一個價、
        點進去另一個價」。

        ⚠ 比對來源刻意取**另一個端點的回傳**，不是在測試裡重寫一份
        ``active and not storefront_hidden``：那樣等於拿實作驗實作，兩邊一起錯
        時仍然全綠（本檔前一版就是這個毛病，#498 review 抓到）。這條之所以關鍵，
        是因為整個 PR 的核心宣稱與 sudo justification 都建立在「兩端點同源」上。
        （只能走 HTTP：``api_template_plans`` 內部用 ``request.env``，直呼會炸。）
        """
        Tpl = self.env['%s.cloud_app_template' % MOD]
        listed = self._result('/api/cloud/templates')
        self.assertTrue(listed.get('success'), listed)
        checked = 0
        for item in listed['data']:
            pricing = item['pricing']
            if not pricing['has_plans']:
                # 卡片不顯示價格時，plans 端點也不該有前台可見的價目。
                plans = self._result(
                    '/api/cloud-services/templates/%s/plans' % item['id'])
                self.assertFalse(
                    plans.get('data'),
                    '%s 卡片無價格標籤，plan 選卡區卻列得出價目' % item['name'])
                continue
            plans = self._result(
                '/api/cloud-services/templates/%s/plans' % item['id'])
            self.assertTrue(plans.get('success'), plans)
            prices = [p['price'] for p in plans['data']
                      if p['unit'] == pricing['unit']]
            self.assertIn(
                pricing['min_price'], prices,
                '%s 的卡片起價 %s 不在 plans 端點回的 %s 價目內'
                % (item['name'], pricing['min_price'], pricing['unit']))
            self.assertEqual(
                pricing['min_price'], min(prices),
                '%s 的卡片起價不是該週期的最低價' % item['name'])
            # ⚠ 起價相同**不代表**兩側同源：多出來的價位若比 base 貴，起價一樣、
            # 只有 tier_count 會變——而 tier_count 決定卡片加不加「起」，正是
            # 使用者看得到的差異。第一版漏了這條，注入式驗證（拿掉
            # storefront_hidden 過濾）當場證明它仍然恆綠。
            self.assertEqual(
                pricing['tier_count'], len(set(prices)),
                '%s 的卡片價位數（%s）與 plans 端點的 %s 價目數（%s）不一致 ⇒ '
                '卡片會多／少一個「起」字'
                % (item['name'], pricing['tier_count'], pricing['unit'],
                   len(set(prices))))
            checked += 1
        self.assertTrue(checked, '前提：至少要有一個有價格的 template 被比對到')
        self.assertTrue(Tpl.search_count([]), '前提：seed 應有 template')


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
        """🔴 sudo 之後的過濾仍然生效：storefront_hidden 價位不得外洩給一般租戶。

        這是 sudo 舉證的核心——繞過 ir.rule 之後，**應用層的可見性過濾還在**。
        用合成 fixture（而非 open-design 的席數變體）：變體的 storefront_hidden
        是明文的暫時狀態，Wave C picker 一落地就會翻正，錨在它上面會讓這條在
        pricing 完全正確時變紅，還把 reviewer 導向不存在的權限問題。
        """
        Tpl = self.env['%s.cloud_app_template' % MOD].sudo()
        Plan = self.env['sh.subscription.plan'].sudo()

        def _plan(name, price, hidden):
            return Plan.create({
                'name': name, 'sh_duration': 1, 'sh_unit': 'month',
                'sh_plan_price': price, 'service_type': 'cloud_service',
                'sh_no_of_billing_cycle': -1, 'storefront_hidden': hidden})

        tpl = Tpl.create({
            'name': 'Sudo Leak Fixture 478', 'slug': 'sudo-leak-478',
            'helm_repo_url': 'oci://example.invalid',
            'helm_chart_name': 'sudo-leak-fixture', 'helm_chart_version': '0.0.0',
        })
        tpl.subscription_plan_ids = [(6, 0, [
            _plan('Visible 478', 4500, False).id,
            _plan('Hidden cheap 478', 100, True).id,
        ])]
        self.env.cr.flush()

        pricing = self._templates_as(
            self.tenant_user.login, self.PASSWORD)[tpl.id]['pricing']
        self.assertEqual(
            pricing['min_price'], 4500,
            '起價取到了 storefront_hidden 的價位 ⇒ sudo 之後的過濾失效')
        self.assertEqual(
            pricing['tier_count'], 1, '隱藏價位被算進 tier_count')

    # 刪除說明（#498 review finding 3）：原本這裡還有一條
    # ``test_sudo_does_not_bypass_marketplace_visibility``，斷言 hidden template
    # 不因 pricing 的 sudo 而出現在清單。它對**任何** pricing 實作都不可能變紅——
    # template 是否曝光由 ``api_cloud_templates`` 的 domain 決定，發生在
    # ``_template_pricing`` 之前，pricing 的 sudo 物理上碰不到那層；而 HA PaaS
    # 日後若比照 #431／#494 解封，它反而會假紅。曝光層本身的守線在
    # test_conceal_instances_split_471.py，不需要在這裡複製一份沒有偵測力的。
