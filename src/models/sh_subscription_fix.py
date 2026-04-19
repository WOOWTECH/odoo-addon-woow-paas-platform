import base64
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.tools.misc import formatLang

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
                shortfall = invoice_id.amount_total - partner.wallet_balance
                topup_ok, topup_msg = self._attempt_auto_topup(partner, shortfall)
                if topup_ok:
                    _logger.info(
                        'Auto-topup covered shortfall=%.2f for subscription %s; '
                        'proceeding with invoice %s',
                        shortfall, subscription.name, invoice_id.name,
                    )
                    subscription._handle_invoice_creation(invoice_id)
                else:
                    _logger.warning(
                        "Insufficient wallet balance for subscription %s and "
                        "auto-topup failed (%s): balance=%.2f, required=%.2f — "
                        "invoice %s left unpaid",
                        subscription.name, topup_msg, partner.wallet_balance,
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
        amount_text = formatLang(
            self.env, invoice.amount_total, currency_obj=invoice.currency_id,
        )
        balance_text = formatLang(
            self.env, partner.wallet_balance,
            currency_obj=partner.company_id.currency_id,
        )
        ctx = {
            'invoice_name': invoice.name or '',
            'amount_text': amount_text,
            'balance_text': balance_text,
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

    @api.model
    def _attempt_auto_topup(self, partner, shortfall):
        """Charge the partner's default bound card to top up the wallet.

        Args:
            partner: res.partner record
            shortfall: float amount needed (TWD)

        Returns:
            (True, 'ok')           — charge succeeded, wallet credited
            (False, 'no_shortfall')— shortfall <= 0
            (False, 'no_token')    — partner has no usable default ECPG token
            (False, 'provider_not_enabled') — token's provider does not have enable_ecpg
            (False, 'currency_not_configured') — neither provider nor TWD available
            (False, '3ds_required')— ECPG returned ThreeDURL; cron cannot complete 3DS
            (False, '<state>')     — payment.transaction ended in a non-done state
            (False, 'sdk_error')   — unexpected exception from _send_payment_request
        """
        if shortfall <= 0:
            return False, 'no_shortfall'

        if 'property_payment_token_id' not in partner._fields:
            return False, 'no_token'

        token = partner.with_company(
            partner.company_id or self.env.company
        ).sudo().property_payment_token_id

        if not token or not token.active or not token.ecpg_bindcard_id:
            return False, 'no_token'

        provider = token.provider_id
        if not provider or not getattr(provider, 'enable_ecpg', False):
            return False, 'provider_not_enabled'

        currency = provider.main_currency_id or self.env.ref(
            'base.TWD', raise_if_not_found=False,
        )
        if not currency:
            return False, 'currency_not_configured'

        reference = f'AUTOTOPUP-{partner.id}-{fields.Datetime.now().strftime("%Y%m%d%H%M%S")}'
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': token.payment_method_id.id,
            'reference': reference,
            'amount': float(shortfall),
            'currency_id': currency.id,
            'partner_id': partner.id,
            'token_id': token.id,
        })

        try:
            result = tx._send_payment_request()
        except Exception as e:
            _logger.exception('Auto-topup send_payment_request failed: %s', e)
            return False, 'sdk_error'

        # 3DS redirect — cron cannot drive it. Treat as failure.
        if isinstance(result, dict) and result.get('redirect_url'):
            _logger.info('Auto-topup requires 3DS for partner=%s; aborting', partner.id)
            return False, '3ds_required'

        if tx.state != 'done':
            _logger.warning(
                'Auto-topup transaction %s ended in state=%s', tx.reference, tx.state,
            )
            return False, tx.state

        partner.sudo().add_wallet_points(
            float(shortfall),
            description='Auto top-up for subscription renewal',
            payment_transaction=tx,
        )
        _logger.info(
            'Auto-topup succeeded: partner=%s amount=%.2f tx=%s',
            partner.id, shortfall, tx.reference,
        )
        return True, 'ok'
