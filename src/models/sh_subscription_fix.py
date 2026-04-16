import base64
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ShSubscriptionFix(models.Model):
    """Fix upstream sh_subscription bug in _sh_create_auto_invoice.

    The original method uses `self._generate_invoice_vals()` inside a loop
    over `subscription` records, but since it's an @api.model method, `self`
    is an empty recordset — producing invoice vals with all NULL fields.

    This override replaces `self` with `subscription` so each invoice
    references the correct partner, journal, and product.
    """
    _inherit = 'sh.subscription.subscription'

    @api.model
    def _sh_create_auto_invoice(self):
        subscriptions = self.env['sh.subscription.subscription'].sudo().search([
            ('state', '=', 'in_progress'),
        ])
        for subscription in subscriptions:
            if subscription.sh_date_of_next_payment != fields.Date.today():
                continue

            invoice_vals = subscription._generate_invoice_vals()
            invoice_id = self.env['account.move'].sudo().create(invoice_vals)
            subscription.compute_view_invoice()

            unit_deltas = {
                'day': relativedelta(days=subscription.sh_recurrency),
                'week': relativedelta(weeks=subscription.sh_recurrency),
                'month': relativedelta(months=subscription.sh_recurrency),
                'year': relativedelta(years=subscription.sh_recurrency),
            }
            delta = unit_deltas.get(subscription.sh_unit)
            if delta:
                subscription.sh_date_of_next_payment += delta

            # Check wallet balance before posting/paying the invoice
            partner = subscription.sh_partner_id
            if partner.wallet_balance >= invoice_id.amount_total:
                subscription._handle_invoice_creation(invoice_id)
            else:
                _logger.warning(
                    "Insufficient wallet balance for subscription %s: "
                    "balance=%.2f, required=%.2f — invoice %s left unpaid",
                    subscription.name, partner.wallet_balance,
                    invoice_id.amount_total, invoice_id.name,
                )
                self._notify_low_balance(partner, invoice_id)

            try:
                pdf_content, _content_type = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
                    'account.account_invoices', invoice_id.id
                )
                attachment_id = self.env['ir.attachment'].sudo().create({
                    'name': 'Subscription Invoice',
                    'type': 'binary',
                    'datas': base64.b64encode(pdf_content),
                    'mimetype': 'application/pdf',
                    'res_id': invoice_id.id,
                    'res_model': 'account.move',
                })
                subscription._sh_send_subscription_email(attachment_id)
            except Exception:
                _logger.warning(
                    "Failed to generate/send invoice PDF for subscription %s",
                    subscription.name, exc_info=True,
                )

            _logger.info(
                "Auto-invoice created for subscription %s (amount=%s, next=%s)",
                subscription.name, invoice_id.amount_total,
                subscription.sh_date_of_next_payment,
            )

    @api.model
    def _notify_low_balance(self, partner, invoice):
        """Send a low-wallet-balance email to the partner.

        Called when an auto-invoice cannot be paid because wallet balance
        is insufficient.
        """
        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("mail_template_low_balance not found; skip notification")
            return

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        ctx = {
            'invoice_name': invoice.name or '',
            'amount_text': '%.2f' % invoice.amount_total,
            'balance_text': '%.2f' % partner.wallet_balance,
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        try:
            template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
            _logger.info("Low-balance notification queued for partner %s", partner.id)
        except Exception:
            _logger.exception(
                "Failed to queue low-balance notification for partner %s",
                partner.id,
            )
