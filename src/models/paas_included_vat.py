# -*- coding: utf-8 -*-
"""2026-10-14 政策 A：PaaS 標價**含** 5% 營業稅。

使用者裁決（取代 #366「點數訂閱免稅」）：目錄價＝客戶付的錢＝發票總額。例：300 點方案 →
扣 300 點，發票未稅 286＋稅 14＝300；儲值 NT$50 → 發票 48＋2＝50（＝刷卡金額）。

做法（PaaS 自己隔離，不動公司預設稅、不改 l10n 紀錄——CLAUDE.md／billing-philosophy §5）：

1. **PaaS 專用內含稅**：以公司既有的 5% 銷項稅（正式站＝l10n_tw ``account.1_tw_tax_sale_5``，
   外加）為來源 ``copy()`` 一份，只改 ``price_include_override='tax_included'`` 與名稱
   ``5% (含稅)``；稅群組、申報科目（repartition lines）沿用來源。登記 xmlid
   ``woow_paas_platform.paas_sale_tax_5_included_company_<company_id>``（``noupdate=True``，
   模組升級不會當成孤兒刪掉）。
2. **找不到 5% 銷項稅時刻意跳過**（例：沒裝 l10n_tw 的測試 DB 只有 generic 15%）：不從零
   憑空建稅——沒有來源就沒有正確的稅群組與申報科目，建出來的稅會讓帳對不起來。這時
   PaaS 商品維持現況（seed 為空集合＝標價即總額，card == charged 仍成立），只記 log。
3. **套用範圍**：模組 seed 的訂閱商品（xmlid 錨定、``sh_product_subscribe``，與 #366
   delegate 同一個集合）＋儲值佔位商品 ``WALLET_TOPUP``。只換掉**該公司**的稅，其他公司
   的稅原樣保留。由 ``data/paas_included_vat_sync.xml`` 的 ``<function>`` 在每次 -u
   重套（seed 是 noupdate=1，日後新增的訂閱商品也會被收斂），migration 18.0.1.0.237 另外
   一次性轉換進行中訂閱的稅快照（``sh_taxes_ids``）。
"""
import logging

from odoo import api, models
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

MODULE = 'woow_paas_platform'
INCLUDED_TAX_XMLID_PREFIX = 'paas_sale_tax_5_included_company_'
INCLUDED_TAX_NAME = '5% (含稅)'
TOPUP_PRODUCT_CODE = 'WALLET_TOPUP'
#: 上游 sh.subscription.subscription.state 的終態（已結束的訂閱不改稅快照，帳上歷史不動）。
TERMINAL_SUBSCRIPTION_STATES = ('cancel', 'close', 'expire', 'renewed')


def _is_vat5(tax, *, included):
    return bool(
        tax
        and tax.amount_type == 'percent'
        and float_compare(tax.amount, 5.0, precision_digits=4) == 0
        and tax.type_tax_use == 'sale'
        and bool(tax.price_include) == included
        and not tax.children_tax_ids
    )


class ProductTemplatePaasIncludedVat(models.Model):
    _inherit = 'product.template'

    # ------------------------------------------------------------------
    # 稅
    # ------------------------------------------------------------------
    @api.model
    def _woow_paas_vat_source_tax(self, company):
        """公司既有的 5% **外加**銷項稅（複製來源）；沒有回空 recordset。

        優先公司預設銷項稅，其次同公司任一符合的稅（sequence, id 最小者）。
        """
        Tax = self.env['account.tax'].sudo()
        default = company.account_sale_tax_id
        if default.company_id == company and default.active and _is_vat5(default, included=False):
            return default
        candidates = Tax.search([
            ('company_id', '=', company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', 5.0),
        ], order='sequence, id')
        return candidates.filtered(lambda t: _is_vat5(t, included=False))[:1]

    @api.model
    def _woow_paas_included_sale_tax(self, company=None, create=True):
        """PaaS 專用 5% 內含稅（見模組 docstring）；``create=False`` 時只查不建。

        回傳空 recordset 的情況：公司沒有 5% 銷項稅可當來源（刻意跳過）、或已有同名但
        規格不符的稅（不覆蓋人家的稅，記 error）。
        """
        company = (company or self.env.company).sudo()
        Tax = self.env['account.tax'].sudo().with_context(active_test=False)
        Imd = self.env['ir.model.data'].sudo()
        xmlid_name = INCLUDED_TAX_XMLID_PREFIX + str(company.id)
        row = Imd.search([('module', '=', MODULE), ('name', '=', xmlid_name)], limit=1)
        tax = Tax.browse(row.res_id).exists() if row and row.model == 'account.tax' else Tax
        if tax or not create:
            return tax

        source = self._woow_paas_vat_source_tax(company)
        if not source:
            _logger.info(
                "[PAAS_VAT] company %s has no 5%% sale tax to derive from; PaaS prices stay "
                "tax-free (no included tax created)", company.id)
            return Tax
        same_name = Tax.search([
            ('company_id', '=', company.id), ('name', '=', INCLUDED_TAX_NAME),
            ('type_tax_use', '=', 'sale')], limit=1)
        if same_name:
            if not _is_vat5(same_name, included=True):
                _logger.error(
                    "[PAAS_VAT] company %s already has a sale tax named %r that is not a 5%% "
                    "tax-included tax (id=%s); refusing to adopt or overwrite it",
                    company.id, INCLUDED_TAX_NAME, same_name.id)
                return Tax
            tax = same_name
        else:
            tax = source.with_company(company).copy({
                'name': INCLUDED_TAX_NAME,
                'price_include_override': 'tax_included',
            })
        vals = {'module': MODULE, 'name': xmlid_name, 'model': 'account.tax',
                'res_id': tax.id, 'noupdate': True}
        if row:
            row.write(vals)
        else:
            Imd.create(vals)
        _logger.info(
            "[PAAS_VAT] company %s: PaaS tax-included sale tax id=%s derived from id=%s",
            company.id, tax.id, source.id)
        return tax

    # ------------------------------------------------------------------
    # 商品
    # ------------------------------------------------------------------
    @api.model
    def _woow_paas_vat_product_templates(self):
        """(seed 訂閱商品 templates, WALLET_TOPUP templates)；集合與 #366 delegate 同源。"""
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', MODULE), ('model', '=', 'product.product')])
        subscription = Product.browse(imd.mapped('res_id')).exists().filtered(
            'sh_product_subscribe').product_tmpl_id
        topup = Product.search([('default_code', '=', TOPUP_PRODUCT_CODE)]).product_tmpl_id
        return subscription, topup

    @api.model
    def _woow_sync_paas_included_vat(self, company=None):
        """把 PaaS 商品的（該公司）稅收斂成 PaaS 內含稅；回傳被改的 templates。冪等。

        ``data/paas_included_vat_sync.xml`` 每次 -u 呼叫；沒有內含稅（無 5% 來源）時 no-op。
        """
        company = (company or self.env.company).sudo()
        tax = self._woow_paas_included_sale_tax(company)
        if not tax:
            return self.browse()
        subscription, topup = self._woow_paas_vat_product_templates()
        changed = self.browse()
        for tmpl in (subscription | topup).with_context(active_test=False):
            target = tmpl.taxes_id.filtered(lambda t: t.company_id != company) | tax
            if tmpl.taxes_id != target:
                tmpl.write({'taxes_id': [(6, 0, target.ids)]})
                changed |= tmpl
        _logger.info(
            "[PAAS_VAT] product sync company=%s tax=%s subscription_templates=%s topup=%s changed=%s",
            company.id, tax.id, len(subscription), len(topup), len(changed))
        return changed


class ShSubscriptionPaasIncludedVat(models.Model):
    _inherit = 'sh.subscription.subscription'

    @api.model
    def _woow_convert_paas_subscriptions_to_included_vat(self, company=None):
        """非終態 PaaS 訂閱的稅快照（``sh_taxes_ids``）換成 PaaS 內含稅；回傳統計 dict。

        只動 ``sh_taxes_ids``——已過帳發票、付款、錢包紀錄一律不碰（歷史照原樣）。原本帶
        外加 5%（或其他該公司的稅）與原本無稅的都換；其他公司的稅保留。冪等。
        """
        company = (company or self.env.company).sudo()
        stats = {'company': company.id, 'tax': False, 'scanned': 0, 'converted': 0,
                 'from_taxed': 0, 'from_untaxed': 0}
        tax = self.env['product.template']._woow_paas_included_sale_tax(company)
        if not tax:
            _logger.warning(
                "[PAAS_VAT] subscription conversion skipped for company %s: no PaaS "
                "tax-included sale tax (no 5%% source tax)", company.id)
            return stats
        stats['tax'] = tax.id
        subscription, _topup = self.env['product.template']._woow_paas_vat_product_templates()
        products = subscription.with_context(active_test=False).product_variant_ids
        subs = self.sudo().search([
            ('product_id', 'in', products.ids),
            ('state', 'not in', TERMINAL_SUBSCRIPTION_STATES),
            ('sh_company_id', 'in', [company.id, False]),
        ])
        stats['scanned'] = len(subs)
        for sub in subs:
            current = sub.sh_taxes_ids
            target = current.filtered(lambda t: t.company_id != company) | tax
            if current == target:
                continue
            if current.filtered(lambda t: t.company_id == company):
                stats['from_taxed'] += 1
            else:
                stats['from_untaxed'] += 1
            sub.write({'sh_taxes_ids': [(6, 0, target.ids)]})
            stats['converted'] += 1
        _logger.info("[PAAS_VAT] subscription conversion %s", stats)
        return stats
