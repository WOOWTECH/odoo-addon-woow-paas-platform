from odoo import _, fields, models
from odoo.exceptions import UserError


class AccountMoveInvoiceStatus(models.Model):
    _inherit = 'account.move'

    woow_invoice_issue_state = fields.Selection(
        [
            ('not_required', 'Not Required'),
            ('pending', 'Pending Issue'),
            ('issuing', 'Issuing'),
            ('issued', 'Issued'),
            ('failed', 'Failed (will retry)'),
            ('error', 'Error (max retries)'),
        ],
        default='not_required',
        index=True,
        copy=False,
    )
    woow_invoice_retry_count = fields.Integer(default=0, copy=False)
    woow_invoice_last_error = fields.Text(copy=False)
    woow_invoice_next_retry_at = fields.Datetime(copy=False)
    woow_skip_upstream_hook = fields.Boolean(
        default=False,
        copy=False,
        help='When True, account.move.action_post() bypasses the upstream '
             'ecpay_invoice_tw inline hook. PaaS-created invoices set this so '
             'our async ecpay_invoice_cron is the sole issuer. Has no effect '
             'when company.auto_invoice is "manual" (upstream hook is a no-op '
             'in that case anyway).',
    )

    def action_woow_retry_ecpay_invoice(self):
        """Manual fallback for `account.group_account_manager` users.

        Resets the retry tracking on this move and immediately tries to issue
        the ECPay e-invoice. The cron will not pick the move up again until
        this run completes.
        """
        self.ensure_one()
        if self.woow_invoice_issue_state == 'issued':
            raise UserError(_("This invoice has already been issued."))
        if self.woow_invoice_issue_state == 'not_required':
            raise UserError(_("This invoice does not need an ECPay e-invoice."))
        if self.state != 'posted':
            raise UserError(_("Invoice must be posted before issuing."))
        self.write({
            'woow_invoice_issue_state': 'pending',
            'woow_invoice_retry_count': 0,
            'woow_invoice_last_error': False,
            'woow_invoice_next_retry_at': False,
        })
        self.env['woow_paas_platform.ecpay_invoice_cron']._issue_one(self)
        return True
