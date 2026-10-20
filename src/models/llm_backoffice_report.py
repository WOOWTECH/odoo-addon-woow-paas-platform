"""Restricted, immutable ledger for queued monthly manual-credit reports."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_CAP_KEY = 'woow_llm_manual_credit_report_cap'
_CAP = object()
_CURSOR_CAP_KEY = 'woow_llm_manual_credit_report_cursor_cap'
_CURSOR_CAP = object()


class LlmManualCreditReport(models.Model):
    _name = 'woow_paas_platform.llm_manual_credit_report'
    _description = 'Queued monthly manual AI credit report'
    _order = 'period_id desc'

    period_id = fields.Char(required=True, readonly=True, index=True)
    start_utc = fields.Datetime(required=True, readonly=True)
    end_utc = fields.Datetime(required=True, readonly=True)
    order_count = fields.Integer(required=True, readonly=True)
    points_total = fields.Integer(required=True, readonly=True)
    # Keep the durable reference after Odoo auto-deletes a successfully sent
    # mail; the report body then follows the mail subsystem's retention policy.
    mail_ref = fields.Integer(required=True, readonly=True, index=True)
    queued_at = fields.Datetime(required=True, readonly=True, default=fields.Datetime.now)

    _sql_constraints = [
        ('period_unique', 'UNIQUE(period_id)', 'A Taipei month can only be reported once.'),
        ('mail_unique', 'UNIQUE(mail_ref)', 'A queued report mail can only be referenced once.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_CAP_KEY) is not _CAP:
            raise AccessError(_('Monthly report rows are created only by the report worker.'))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_('Monthly report history is immutable.'))

    def unlink(self):
        raise AccessError(_('Monthly report history cannot be deleted.'))

    @api.model
    def _create_period(self, *, period_id, start_utc, end_utc, order_count,
                       points_total, mail_ref):
        if (not isinstance(period_id, str) or len(period_id) != 7
                or type(order_count) is not int or order_count < 0
                or type(points_total) is not int or points_total < 0):
            raise ValidationError(_('The monthly report ledger values are invalid.'))
        return self.with_context(**{_CAP_KEY: _CAP}).create({
            'period_id': period_id, 'start_utc': start_utc, 'end_utc': end_utc,
            'order_count': order_count, 'points_total': points_total, 'mail_ref': mail_ref,
        })


class LlmManualCreditReportCursor(models.Model):
    """Durable retry position; advancing it is atomic with the report receipt."""
    _name = 'woow_paas_platform.llm_manual_credit_report_cursor'
    _description = 'Manual AI credit report cursor'

    cursor_key = fields.Char(required=True, readonly=True, index=True)
    next_period = fields.Char(required=True, readonly=True)

    _sql_constraints = [
        ('cursor_key_unique', 'UNIQUE(cursor_key)', 'Report cursor keys are unique.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_CURSOR_CAP_KEY) is not _CURSOR_CAP:
            raise AccessError(_('Report cursors can only be initialized by the report worker.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get(_CURSOR_CAP_KEY) is not _CURSOR_CAP:
            raise AccessError(_('Report cursors can only advance with a queued report.'))
        return super().write(vals)

    def unlink(self):
        raise AccessError(_('Report cursor history cannot be deleted.'))

    @api.model
    def _initialize(self, cursor_key, next_period):
        return self.with_context(**{_CURSOR_CAP_KEY: _CURSOR_CAP}).create({
            'cursor_key': cursor_key, 'next_period': next_period,
        })

    def _advance(self, next_period):
        self.ensure_one()
        return self.with_context(**{_CURSOR_CAP_KEY: _CURSOR_CAP}).write({
            'next_period': next_period,
        })
