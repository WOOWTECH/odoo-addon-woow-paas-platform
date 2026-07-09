"""#324 part (a): 上游兩支 buggy wallet cron 被 neutralize 成 no-op。

紅→綠也同時複現 bug：override 前，retry cron 會對窗內 failed 交易重試、
再建一筆新 failed（缺陷 A）；charge cron 會對 due 訂閱扣款失敗建 failed、
且不推進 next_payment（缺陷 B）。override 後兩者皆 no-op。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class _WalletCronFixture(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Cron Neu Plan', 'sh_plan_price': 499.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        # 真 product：讓 _generate_invoice_vals 產出有金額的發票（否則 amount 0
        # → 不扣款、無 done deduct）。鏡像 #318 _make_plan 的 product 設定。
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Cron Neu Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
            'list_price': 499.0,
        })
        self.product = product_tmpl.product_variant_id
        self.partner = self.env['res.partner'].create({'name': 'Cron Neu Partner'})

    def _due_sub(self):
        """建一張 due today、in_progress、餘額為 0（扣款必失敗）的訂閱。
        直接 create 免走 confirm/onchange（保持單元純度）。"""
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'product_id': self.product.id,
            'sh_plan_price': 499.0, 'sh_qty': 1,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today, 'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
        })

    def _deduct_txns(self, sub):
        return self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'),
        ])


class TestWalletRetryCronNeutralized(_WalletCronFixture):
    def test_retry_cron_does_not_create_new_failed_txn(self):
        sub = self._due_sub()
        # 種一筆「窗內」failed deduct（模擬缺陷 A 的既有殘留）。
        seed = self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -499.0, 'subscription_id': sub.id, 'state': 'failed',
        })
        before = self._deduct_txns(sub)
        self.assertEqual(len(before), 1)

        self.Sub._cron_wallet_charge_retry()

        after = self._deduct_txns(sub)
        self.assertEqual(
            len(after), 1,
            "retry cron 已被 neutralize，不得再產生新的 failed deduct 交易",
        )
        self.assertEqual(after.id, seed.id)


class TestWalletChargeCronNeutralized(_WalletCronFixture):
    def test_charge_cron_is_noop_on_due_sub(self):
        sub = self._due_sub()
        original_next = sub.sh_date_of_next_payment

        self.Sub._cron_wallet_charge()

        self.assertFalse(
            self._deduct_txns(sub),
            "charge cron 已被 neutralize，不得對 due 訂閱建任何 deduct 交易",
        )
        self.assertEqual(
            sub.sh_date_of_next_payment, original_next,
            "neutralize 後 charge cron 不得改動 next_payment",
        )


class TestSoleBillingPathChargesOncePerCycle(_WalletCronFixture):
    """有足額餘額的訂閱：_sh_create_auto_invoice 一週期只扣一次
    （成功後 next_payment 前進，同日再跑不再命中 `<= today`）。"""

    def _fund(self, amount):
        # loyalty card 加點 → wallet_balance 反映
        card = self.partner.get_wallet_loyalty_card()
        card.sudo().points = amount
        self.partner.invalidate_recordset(['wallet_balance'])

    def test_sufficient_balance_charged_once_per_cycle(self):
        # journal 前置（_get_or_create_cycle_invoice 需要）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        sub = self._due_sub()
        self._fund(2000.0)
        original_next = sub.sh_date_of_next_payment

        # 第一次 cron：扣一次、next_payment 前進一個月
        self.Sub._sh_create_auto_invoice()
        done1 = self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'), ('state', '=', 'done')])
        self.assertTrue(done1, "有餘額應成功扣款一次（done deduct）")
        self.assertGreater(
            sub.sh_date_of_next_payment, original_next,
            "成功扣款後 next_payment 必須前進一個週期")

        # 第二次同日再跑 cron：next_payment 已在未來 → 不再命中 → 不重扣
        self.Sub._sh_create_auto_invoice()
        done2 = self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'), ('state', '=', 'done')])
        self.assertEqual(
            len(done2), len(done1),
            "同一週期內第二次 cron 不得重複扣款（一週期只扣一次）")
