"""2026-10-14 政策 A：PaaS 標價含 5% 營業稅（models/paas_included_vat.py）。

目錄價＝預估＝發票總額＝扣點＝刷卡金額；退點以實付含稅總額按比例計算，貸項總額＝退點。

測試 DB 沒裝 l10n_tw（只有 generic 15%），fixture 自建一顆與 l10n_tw ``tw_tax_sale_5`` 同規格
的 5% 外加銷項稅當來源（正式站的現況）。走真的 Smart Home 下單路徑（seed 方案＋
``_create_and_confirm_subscription``）。
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.paas_included_vat import (
    INCLUDED_TAX_NAME, INCLUDED_TAX_XMLID_PREFIX, MODULE)
from odoo.addons.woow_paas_platform.models.smart_home import _platform_order


def _today(day):
    return patch.object(fields.Date, 'today', staticmethod(lambda *args, **kwargs: day))


class _VatFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company.sudo()
        Journal = self.env['account.journal'].sudo()
        if not Journal.search([('company_id', '=', self.company.id), ('type', '=', 'sale')], limit=1):
            Journal.create({'name': 'Test Sales VAT', 'code': 'TSVAT', 'type': 'sale',
                            'company_id': self.company.id})
        if not Journal.search([('company_id', '=', self.company.id),
                               ('type', 'in', ('bank', 'cash'))], limit=1):
            Journal.create({'name': 'Test Bank VAT', 'code': 'TBVAT', 'type': 'bank',
                            'company_id': self.company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.Tmpl = self.env['product.template'].sudo()
        self.Tax = self.env['account.tax'].sudo()
        self.assertFalse(self.Tmpl._woow_paas_included_sale_tax(create=False),
                         '前提：測試 DB 尚無 PaaS 內含稅')

    def _make_source(self):
        """與 l10n_tw tw_tax_sale_5 同規格：5%、percent、sale、外加。"""
        group = self.company.account_sale_tax_id.tax_group_id
        return self.Tax.create({
            'name': 'TW 5% src', 'amount_type': 'percent', 'amount': 5.0,
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded',
            'tax_group_id': group.id, 'company_id': self.company.id})


@tagged('post_install', '-at_install')
class TestPaasIncludedVatTax(_VatFixture):

    def test_derives_included_copy_and_registers_xmlid(self):
        source = self._make_source()
        default_before = self.company.account_sale_tax_id
        tax = self.Tmpl._woow_paas_included_sale_tax(self.company)
        self.assertTrue(tax)
        self.assertNotEqual(tax, source)
        self.assertEqual((tax.name, tax.amount, tax.amount_type, tax.type_tax_use),
                         (INCLUDED_TAX_NAME, 5.0, 'percent', 'sale'))
        self.assertTrue(tax.price_include)
        self.assertEqual(tax.tax_group_id, source.tax_group_id)
        self.assertEqual(
            [(r.repartition_type, r.document_type, r.account_id.id, r.factor_percent)
             for r in tax.repartition_line_ids],
            [(r.repartition_type, r.document_type, r.account_id.id, r.factor_percent)
             for r in source.repartition_line_ids],
            '申報科目（repartition lines）沿用來源')
        self.assertFalse(source.price_include, '來源稅不得被改')
        self.assertEqual(self.company.account_sale_tax_id, default_before, '公司預設稅不得被改')
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', MODULE),
            ('name', '=', INCLUDED_TAX_XMLID_PREFIX + str(self.company.id))])
        self.assertEqual((imd.model, imd.res_id, imd.noupdate), ('account.tax', tax.id, True))
        self.assertEqual(self.Tmpl._woow_paas_included_sale_tax(self.company), tax, '冪等')

    def test_prefers_company_default_when_it_is_the_5pct_tax(self):
        first = self._make_source()
        other = first.copy({'name': 'TW 5% src B'})
        self.assertEqual(self.Tmpl._woow_paas_vat_source_tax(self.company), first,
                         '前提：公司預設不是 5% 時取 sequence/id 最前的那顆')
        self.company.account_sale_tax_id = other.id
        self.assertEqual(self.Tmpl._woow_paas_vat_source_tax(self.company), other)

    def test_without_5pct_source_creates_nothing(self):
        five = self.Tax.search([('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
                                ('amount_type', '=', 'percent'), ('amount', '=', 5.0)])
        five.write({'active': False})
        count = self.Tax.with_context(active_test=False).search_count([])
        self.assertFalse(self.Tmpl._woow_paas_included_sale_tax(self.company))
        self.assertEqual(self.Tax.with_context(active_test=False).search_count([]), count)
        self.assertFalse(self.Tmpl._woow_sync_paas_included_vat(self.company),
                         '沒有內含稅時商品同步是 no-op')

    def test_refuses_to_adopt_same_named_foreign_tax(self):
        self._make_source()
        self.Tax.create({'name': INCLUDED_TAX_NAME, 'amount_type': 'percent', 'amount': 10.0,
                         'type_tax_use': 'sale', 'company_id': self.company.id})
        self.assertFalse(self.Tmpl._woow_paas_included_sale_tax(self.company))


@tagged('post_install', '-at_install')
class TestPaasIncludedVatBilling(_VatFixture):
    """estimate＝invoice＝points＝list；儲值 50 ⇒ 發票 50；退點＝實付含稅、貸項＝退點。"""

    def setUp(self):
        super().setUp()
        self.source = self._make_source()
        self.Tmpl._woow_sync_paas_included_vat(self.company)
        self.vat = self.Tmpl._woow_paas_included_sale_tax(create=False)
        self.owner = self.env['res.users'].sudo().create({
            'name': 'VAT Owner', 'login': 'vat-owner@example.invalid'})
        self.partner = self.owner.partner_id
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'VAT WS', 'owner_id': self.owner.id})

    def _balance(self):
        self.partner.invalidate_recordset(['wallet_balance'])
        return round(self.partner.wallet_balance, 2)

    def _home(self, plan_xmlid):
        plan = self.env.ref('woow_paas_platform.' + plan_xmlid)
        home = _platform_order(self.env['woow_paas_platform.smart_home'].sudo()).create({
            'name': 'VAT Home', 'workspace_id': self.workspace.id})
        sub = home._create_and_confirm_subscription(self.partner, plan)
        return plan, home, sub

    def _paid(self, plan_xmlid):
        plan, home, sub = self._home(plan_xmlid)
        self.partner.add_wallet_points(plan.sh_plan_price, description='VAT seed')
        before = self._balance()
        estimate = home._skip_trial_estimate()
        invoice = self.env['account.move'].browse(home._skip_trial()['invoice_id'])
        return plan, sub, estimate, invoice, round(before - self._balance(), 2)

    def test_products_carry_only_the_included_tax(self):
        subscription, topup = self.Tmpl._woow_paas_vat_product_templates()
        self.assertTrue(subscription)
        self.assertFalse(subscription.filtered(lambda t: t.taxes_id != self.vat))
        self.assertFalse(self.Tmpl._woow_sync_paas_included_vat(self.company), '冪等')

    def test_estimate_invoice_points_equal_list_price(self):
        for xmlid in ('plan_smarthome_monthly', 'plan_smarthome_mcp_monthly',
                      'plan_smarthome_yearly'):
            with self.subTest(plan=xmlid):
                plan, sub, estimate, invoice, debit = self._paid(xmlid)
                price = plan.sh_plan_price
                self.assertEqual(sub.sh_taxes_ids, self.vat)
                self.assertAlmostEqual(estimate['total'], price, places=2)
                self.assertAlmostEqual(invoice.amount_total, price, places=2)
                self.assertAlmostEqual(debit, price, places=2)
                self.assertAlmostEqual(invoice.amount_untaxed + invoice.amount_tax, price, places=2)
                self.assertAlmostEqual(invoice.amount_tax, round(price - price / 1.05, 2), places=2)
                self.assertEqual(invoice.invoice_line_ids.tax_ids, self.vat)
                self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

    def test_immediate_delete_refunds_full_paid_and_credit_note_equals_refund(self):
        plan, sub, _estimate, invoice, debit = self._paid('plan_smarthome_mcp_monthly')
        before = self._balance()
        credited = sub._settle_on_entity_delete(reason='Smart Home deleted')
        self.assertAlmostEqual(credited, debit, places=2)
        self.assertAlmostEqual(self._balance() - before, debit, places=2)
        cn = self.env['account.move'].search([('reversed_entry_id', '=', invoice.id)])
        self.assertAlmostEqual(cn.amount_total, credited, places=2)
        self.assertAlmostEqual(cn.amount_tax, invoice.amount_tax, places=2)
        self.assertEqual(cn.payment_state, 'paid')

    def test_mid_period_refund_is_prorated_tax_included(self):
        plan, sub, _estimate, invoice, debit = self._paid('plan_smarthome_mcp_monthly')
        next_payment = sub.sh_date_of_next_payment
        cycle_days = (next_payment - sub.sh_start_date).days
        with _today(sub.sh_start_date + timedelta(days=10)):
            credited = sub._settle_on_entity_delete(reason='Smart Home deleted')
        expected = invoice.currency_id.round(debit * (cycle_days - 10) / cycle_days)
        self.assertAlmostEqual(credited, expected, places=2)
        cn = self.env['account.move'].search([('reversed_entry_id', '=', invoice.id)])
        self.assertAlmostEqual(cn.amount_total, credited, places=2)
        self.assertEqual(cn.payment_state, 'paid')

    def test_topup_invoice_total_equals_card_charge(self):
        provider = self.env['payment.provider'].sudo().search([('code', '=', 'ECPay')], limit=1)
        method = provider.payment_method_ids[:1] or self.env['payment.method'].with_context(
            active_test=False).search([('code', '=', 'ecpay')], limit=1)
        if not provider or not method:
            self.skipTest('ECPay provider not installed')
        self.env['product.product'].sudo().search(
            [('default_code', '=', 'WALLET_TOPUP')]).write({'default_code': False})
        currency = self.env.company.currency_id
        topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.partner.id, 'amount': 50, 'credits': 50, 'bonus': 0,
            'currency_id': currency.id, 'state': 'pending', 'invoice_carrier_type': 'email'})
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id, 'payment_method_id': method.id,
            'partner_id': self.partner.id, 'amount': 50, 'currency_id': currency.id,
            'reference': 'TOPUPVAT%d' % topup.id, 'woow_topup_id': topup.id})
        # 公司預設稅是外加 5%（正式站現況）也不得滲進儲值發票。
        self.company.account_sale_tax_id = self.source.id
        topup._on_payment_done(tx)
        move = topup.invoice_account_move_id
        self.assertAlmostEqual(move.amount_total, 50.0, places=2)
        self.assertAlmostEqual(move.amount_tax, round(50 - 50 / 1.05, 2), places=2)
        self.assertEqual(move.invoice_line_ids.tax_ids, self.vat)
        product = self.env['product.product'].sudo().search([('default_code', '=', 'WALLET_TOPUP')])
        self.assertEqual(product.taxes_id, self.vat)


@tagged('post_install', '-at_install')
class TestPaasIncludedVatMigration(_VatFixture):
    """migration 18.0.1.0.237 的 delegate：正式站現況（商品＋進行中訂閱帶 5% 外加）→ 內含稅。"""

    def setUp(self):
        super().setUp()
        self.source = self._make_source()
        self.company.account_sale_tax_id = self.source.id
        self.subscription_tmpls, _ = self.Tmpl._woow_paas_vat_product_templates()
        # 正式站現況：多數商品帶外加 5%，部分無稅。
        taxed = self.subscription_tmpls[:-3]
        taxed.write({'taxes_id': [(6, 0, self.source.ids)]})
        self.subscription_tmpls[-3:].write({'taxes_id': [(5, 0, 0)]})
        owner = self.env['res.users'].sudo().create({
            'name': 'VAT Mig Owner', 'login': 'vat-mig-owner@example.invalid'})
        self.partner = owner.partner_id
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'VAT Mig WS', 'owner_id': owner.id})
        self.plan = self.env.ref('woow_paas_platform.plan_smarthome_mcp_monthly')
        self.product = self.env.ref('woow_paas_platform.product_smarthome_mcp_monthly')
        self.assertIn(self.product.product_tmpl_id, taxed)

    def _home(self):
        home = _platform_order(self.env['woow_paas_platform.smart_home'].sudo()).create({
            'name': 'VAT Mig Home', 'workspace_id': self.workspace.id})
        return home, home._create_and_confirm_subscription(self.partner, self.plan)

    def test_converts_products_and_open_subscriptions_only_and_is_idempotent(self):
        self.partner.add_wallet_points(315, description='legacy')
        home, paid = self._home()
        invoice = self.env['account.move'].browse(home._skip_trial()['invoice_id'])
        self.assertAlmostEqual(invoice.amount_total, 315.0, places=2, msg='前提：舊制外加 315')
        _home2, trial = self._home()
        _home3, closed = self._home()
        closed.write({'state': 'cancel'})
        untaxed_open = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id, 'product_id': self.product.id,
            'sh_subscription_plan_id': self.plan.id, 'sh_plan_price': 300.0,
            'state': 'draft'})
        self.assertFalse(untaxed_open.sh_taxes_ids)

        changed = self.Tmpl._woow_sync_paas_included_vat()
        stats = self.env['sh.subscription.subscription']._woow_convert_paas_subscriptions_to_included_vat()
        vat = self.Tmpl._woow_paas_included_sale_tax(create=False)

        self.assertFalse(self.subscription_tmpls - changed, '每個 seed 訂閱商品都被收斂')
        self.assertFalse(self.subscription_tmpls.filtered(lambda t: t.taxes_id != vat))
        for sub in (paid, trial, untaxed_open):
            self.assertEqual(sub.sh_taxes_ids, vat)
        self.assertEqual(closed.sh_taxes_ids, self.source, '終態訂閱不動')
        self.assertEqual(invoice.invoice_line_ids.tax_ids, self.source, '已過帳發票不動')
        self.assertAlmostEqual(invoice.amount_total, 315.0, places=2)
        self.assertGreaterEqual(stats['converted'], 3)
        self.assertGreaterEqual(stats['from_untaxed'], 1)
        self.assertEqual(self.company.account_sale_tax_id, self.source, '公司預設稅不動')
        self.assertAlmostEqual(paid._sh_get_period_billing_amount(), 300.0, places=2,
                               msg='轉換後下一期扣 300（＝目錄價）')

        again = self.env['sh.subscription.subscription']._woow_convert_paas_subscriptions_to_included_vat()
        self.assertEqual(again['converted'], 0, '冪等')
        self.assertFalse(self.Tmpl._woow_sync_paas_included_vat(), '冪等')

    def test_legacy_paid_cycle_refund_uses_what_was_paid(self):
        """舊制付 315 的這一期，轉換後中途刪除：照實付 315 按比例退、貸項＝退點。"""
        self.partner.add_wallet_points(315, description='legacy')
        home, sub = self._home()
        invoice = self.env['account.move'].browse(home._skip_trial()['invoice_id'])
        self.env['sh.subscription.subscription']._woow_convert_paas_subscriptions_to_included_vat()
        cycle_days = (sub.sh_date_of_next_payment - sub.sh_start_date).days
        with _today(sub.sh_start_date + timedelta(days=10)):
            credited = sub._settle_on_entity_delete(reason='Smart Home deleted')
        self.assertAlmostEqual(
            credited, invoice.currency_id.round(315.0 * (cycle_days - 10) / cycle_days), places=2)
        cn = self.env['account.move'].search([('reversed_entry_id', '=', invoice.id)])
        self.assertAlmostEqual(cn.amount_total, credited, places=2)
        self.assertEqual(cn.payment_state, 'paid')
