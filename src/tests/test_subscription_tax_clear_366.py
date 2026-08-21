# -*- coding: utf-8 -*-
"""#366：訂閱方案 product 不應課稅（pts 交易免稅，稅在儲值端處理）。

根因：seed 建 product 時未明寫 taxes_id → 繼承 company.account_sale_tax_id。
prod/stg 的 Odoo company 從未正式設定（殘留 My Company/US/15% 預設銷項稅），
不同時期建的 product 因此稅待遇不一（237/251 掛稅、卡片價≠實扣價）。

三層修法（缺一會復發）：
  A. migration force-write 清既有 251 product 的 taxes_id（空集合，非 0% 稅）
     ——subscription_products*.xml 是 noupdate=1，光 -u 吃不到既有 record，
     必走 migration（delegate=product.template._woow_clear_subscription_product_taxes_366）。
  B. seed 明寫 taxes_id=[(6,0,[])]（fresh 裝與未來 re-seed 不再繼承公司預設）。
  C. 公司預設稅 account_sale_tax_id 不動（公司層級、會波及官網 ERP）。

全庫零跳過斷言（比照 test_wave_c_variants_attach_330 風格）：把「PaaS 訂閱 product
一律無稅」這條 invariant 對未來釘死——任何新增 seed product 漏寫 taxes_id、或
company 預設稅回流既有 product，本測試立刻紅。

「清 taxes_id（空集合）」≠「設 0% 稅」：前者發票不出現稅列（total_included==
sh_plan_price），後者仍掛一列 0 稅。驗收要前者。
"""
from odoo.tests.common import TransactionCase, tagged

MOD = 'woow_paas_platform'


def _paas_subscription_products(env):
    """PaaS seed 出的訂閱 product（xmlid 定位，module=woow_paas_platform、
    model=product.product、且 sh_product_subscribe=True）。

    xmlid 錨定＝嚴格限 PaaS seed，demo 資料／官網 ERP product 不入列，
    與 migration delegate 的 scope 一致（Layer C：不碰非 PaaS）。
    """
    imd = env['ir.model.data'].sudo().search([
        ('module', '=', MOD),
        ('model', '=', 'product.product'),
    ])
    products = env['product.product'].sudo().browse(
        [row.res_id for row in imd]).exists()
    return products.filtered('sh_product_subscribe')


@tagged('post_install', '-at_install')
class TestSeedProductCorpusIntact(TransactionCase):
    """#433：seed product 語料庫「零丟棄」守線——本檔唯一持有數字的地方。

    原本 TestSubscriptionProductsTaxFree / TestCardEqualsCharged 各自硬編
    ``assertEqual(len(products), 251)`` 當前置（雙寫漂移源；8 天內漂到 267 而恆紅）。

    修法**不是**把 ``==`` 放寬成 ``>=``：真正的威脅是「集合被靜默縮小 ⇒ 下游斷言
    vacuously pass」（``taxed`` 是 filtered 出來的，集合縮水時 ``assertFalse(taxed)``
    自動綠），而 ``>=`` 恰恰對縮小最寬容——守衛會裝反。正確做法是把「丟棄量」本身
    斷言成 0：任何一步過濾都不得少掉東西。這抓得到「某族變體被誤寫
    ``sh_product_subscribe=False`` 導致 16 筆靜默退出稅務檢查」，而任何數值下界都
    抓不到。

    ⚠️ 這裡刻意**不**用「解析 subscription_products*.xml 算 record 數」那招
    （test_plan_group_keys_368.py 的 ``_seeded_link_refs()`` 對 links.xml 用的）：
    products XML 是 ``noupdate="1"``，升級軌上「從 XML 刪掉的 record 仍留在 DB」
    是正常的 ⇒ XML 數 ≠ DB 數，照抄會在沙盒 ``-u`` 軌假紅。links.xml 能用是因為
    它是 ``noupdate="0"`` ＋ ``(6, 0, [...])`` 全量 reset。這個不對稱別搞混。
    """

    # 下界 backstop，**非不變式**：只用來抓「seed 整批沒載入」這種災難級狀況。
    # 真正的守線是下面兩條零丟棄斷言；這個數字允許隨 seed 成長調高。
    # 267 → 269：#423 W5b 新增 HAOS 兩顆 base product（月/年，無加購變體）。
    # 269 → 271：#494 新增 Hermes Agent 兩顆 base product（月/年，無加購變體）。
    # 271 → 273：#661 新增 Home Assistant 完整版兩顆 base product（月/年，無加購變體）。
    # 273 → 319：#765 補 AC 席數變體 46 顆 product（Hermes Agent 1 席、HA 完整版
    #            1-3 席、Hermes MCP Server T1/T2/T3 各 1-3／1-6／1-10 席，皆 ×月/年）。
    _MIN_SEEDED_PRODUCTS = 319

    def test_no_seed_product_is_silently_dropped(self):
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', MOD),
            ('model', '=', 'product.product'),
        ])
        self.assertTrue(imd, '前置：應有 PaaS seed 出的 product.product xmlid')

        res_ids = imd.mapped('res_id')
        alive = self.env['product.product'].sudo().browse(res_ids).exists()
        dangling = sorted(set(res_ids) - set(alive.ids))
        self.assertFalse(
            dangling,
            'ir.model.data 指向已不存在的 product（seed 被刪）：%s' % dangling)

        products = alive.filtered('sh_product_subscribe')
        self.assertEqual(
            len(products), len(alive),
            '每個 seed product 都應是訂閱 product，否則會靜默退出全庫稅務檢查；'
            '漏網 %s' % (alive - products).mapped('name'))

        self.assertEqual(
            set(products.ids), set(_paas_subscription_products(self.env).ids),
            'helper 取得的集合應與本守線驗過的集合同源（防 helper 日後被改動'
            '而守線失效）')

        self.assertGreaterEqual(
            len(products), self._MIN_SEEDED_PRODUCTS,
            'backstop：seed 整批未載入？只有 %d 個訂閱 product'
            '（下界非不變式，seed 成長時可調高）' % len(products))


@tagged('post_install', '-at_install')
class TestSubscriptionProductsTaxFree(TransactionCase):
    """全庫訂閱 product 的 taxes_id 皆空（seed Layer B / migration Layer A 終態）。"""

    def test_all_subscription_products_have_no_tax(self):
        products = _paas_subscription_products(self.env)
        # #433：計數守線已集中到 TestSeedProductCorpusIntact（零丟棄斷言）。
        # 這裡只需擋 vacuous——集合為空時下面的 assertFalse(taxed) 會假綠。
        self.assertTrue(
            products, '前置：應有 PaaS 訂閱 product（防 vacuously pass）')
        taxed = products.filtered(lambda p: p.taxes_id)
        self.assertFalse(
            taxed,
            '訂閱 product 不應掛任何銷項稅（pts 免稅）；仍掛稅 %d 個：%s'
            % (len(taxed), taxed.mapped('name')))


@tagged('post_install', '-at_install')
class TestCardEqualsCharged(TransactionCase):
    """卡片價 == 實扣價：每個 plan 的 sh_plan_price 經稅計算後 total_included 不變。

    用 taxes.compute_all（發票稅計算的同一路徑）驗全庫 plan 零落差；product
    無稅時 total_included == sh_plan_price（card == charged）。product 若殘留
    15% 稅則 total_included = price*1.15 ≠ price → 立刻紅。
    """

    def test_card_price_equals_charged_total_all_plans(self):
        products = _paas_subscription_products(self.env)
        # #433：計數守線已集中到 TestSeedProductCorpusIntact（零丟棄斷言）。
        self.assertTrue(
            products, '前置：應有 PaaS 訂閱 product（防 vacuously pass）')

        mismatches = []
        results = {}
        for product in products:
            plan = product.sh_subscription_plan_id
            self.assertTrue(
                plan, '訂閱 product %s 未連 plan' % product.name)
            price = plan.sh_plan_price
            total_included = product.taxes_id.compute_all(
                price,
                currency=self.env.company.currency_id,
            )['total_included']
            results[product.id] = total_included
            if abs(total_included - price) > 0.001:
                mismatches.append(
                    '%s：卡片 %s → 實扣 %s' % (plan.name, price, total_included))
        # #433：原為 assertEqual(checked, 251)。checked 在迴圈內無條件 +1、迴圈無
        # continue/break ⇒ 該斷言可證恆真。恆真斷言比沒有斷言更糟：讀起來像守衛、
        # review 時被當成已覆蓋，而未來有人加 `if xxx: continue` 時它**還是綠的**
        # （checked 跟著少），正好在最需要它的那一刻失效。改成對結果面斷言才有牙。
        self.assertEqual(
            set(results), set(products.ids),
            '每個訂閱 product 都應被計算（零跳過）')
        self.assertFalse(
            mismatches,
            'card ≠ charged（%d 個落差）：%s' % (len(mismatches), mismatches))


@tagged('post_install', '-at_install')
class TestClearDelegateForceWrites(TransactionCase):
    """migration delegate（Layer A）force-write：設稅→清除→驗空。

    這條不依賴 seed／company 預設稅是否課稅，恆有牙——直驅 delegate 證明
    升級軌 force-write 真的把既有 taxes_id 清成空集合（noupdate 擋不住）。
    """

    def test_delegate_clears_existing_tax_to_empty(self):
        tax = self.env['account.tax'].sudo().create({
            'name': '#366 TEST 15% 銷項',
            'amount': 15.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
        })
        products = _paas_subscription_products(self.env)[:5]
        self.assertTrue(products, '前置：應有 PaaS 訂閱 product')
        products.write({'taxes_id': [(6, 0, tax.ids)]})
        self.assertTrue(
            all(p.taxes_id for p in products), '前置：product 已掛 15% 稅')

        cleared = self.env['product.template'].sudo(
        )._woow_clear_subscription_product_taxes_366()

        self.assertTrue(
            all(not p.taxes_id for p in products),
            'force-write 後 5 個 product 皆應無稅')
        for product in products:
            self.assertIn(
                product.product_tmpl_id, cleared,
                '被清的 template 應在 delegate 回傳集合')

    def test_delegate_is_idempotent(self):
        """已無稅時重跑不炸、仍無稅（migration 冪等，可安全重跑）。"""
        cleared = self.env['product.template'].sudo(
        )._woow_clear_subscription_product_taxes_366()
        self.assertTrue(
            all(not t.taxes_id for t in cleared),
            '重跑後 PaaS 訂閱 product template 皆無稅')
