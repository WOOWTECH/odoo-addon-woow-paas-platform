from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class TopupPlan(models.Model):
    _name = 'woow_paas_platform.topup_plan'
    _description = 'Credit Top-up Plan'
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True)  # "NT$ 100 Credits"
    amount = fields.Float(string='Price (NT$)', required=True)  # purchase price
    credits = fields.Float(string='Credits', required=True)  # credits received
    bonus = fields.Float(string='Bonus Credits', default=0)  # extra bonus
    label = fields.Char(string='Badge Label')  # "BEST VALUE"
    category = fields.Selection([
        ('all', 'All'),
        ('recommended', 'Recommended'),
        ('large_volume', 'Large Volume'),
    ], string='Category', default='all')
    icon = fields.Char(string='Icon Name', default='toll')  # Material icon name
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)

    # Recomputed on every read. Safe at N<~50 plans; convert to stored if N grows.
    is_best_value = fields.Boolean(
        string='Best Value',
        compute='_compute_is_best_value',
        help='True when this plan has the highest bonus/credits ratio among active plans.',
    )

    @api.depends('credits', 'bonus', 'active')
    def _compute_is_best_value(self):
        active_plans = self.search([('active', '=', True)])
        ratios = [
            (p.bonus / p.credits) for p in active_plans if p.credits
        ]
        best_ratio = max(ratios) if ratios else 0.0
        for rec in self:
            ratio = (rec.bonus / rec.credits) if rec.credits else 0.0
            rec.is_best_value = (
                ratio > 0
                and float_compare(ratio, best_ratio, precision_digits=6) == 0
            )

    @api.constrains('amount', 'active')
    def _check_amount_whole_and_minimum(self):
        """上架中的方案：金額必須是整數 NT$，且 ≥ 最低扣款金額（預設 100）。

        下限沿用 #1745 的 ``woow_paas_platform.auto_topup_min_amount``（設 0 ＝
        不檢查下限，與自動儲值的關閉語意一致）。只檢查 active 方案——下架的舊
        方案（NT$50，由 migrations/18.0.1.0.240 停用）留著給歷史 topup 參照；
        ``@api.constrains`` 只在寫入這兩個欄位時觸發，既有資料不會被回頭驗證。
        """
        minimum = self.env['woow.wallet.topup']._auto_topup_min_amount()
        for plan in self.filtered('active'):
            if plan.amount != int(plan.amount):
                raise ValidationError(_(
                    'Top-up plan "%(name)s": the price must be a whole NT$ '
                    'amount (got %(amount)s).',
                    name=plan.name, amount=plan.amount))
            if minimum and plan.amount < minimum:
                raise ValidationError(_(
                    'Top-up plan "%(name)s": the price must be at least '
                    'NT$%(minimum)s (got %(amount)s).',
                    name=plan.name, minimum=int(minimum), amount=plan.amount))
