import base64
import logging
import math
import uuid

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.tools.misc import formatLang

# Return codes from _attempt_auto_topup.
TOPUP_OK = 'ok'
TOPUP_NO_SHORTFALL = 'no_shortfall'
TOPUP_NO_TOKEN = 'no_token'
TOPUP_PROVIDER_NOT_ENABLED = 'provider_not_enabled'
TOPUP_CURRENCY_NOT_CONFIGURED = 'currency_not_configured'
TOPUP_3DS_REQUIRED = '3ds_required'
TOPUP_SDK_ERROR = 'sdk_error'
TOPUP_WALLET_CREDIT_FAILED = 'wallet_credit_failed'

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

            try:
                with self.env.cr.savepoint():
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
                            # Dedup: reuse the pre-renewal warning field so a partner who
                            # already received today's warning doesn't get a second email.
                            today = fields.Date.today()
                            if subscription.woow_last_low_balance_warned_for != today:
                                self._notify_low_balance(partner, invoice_id)
                                subscription.write({'woow_last_low_balance_warned_for': today})

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
            except Exception:
                _logger.exception(
                    'Auto-invoice failed for subscription %s; continuing',
                    subscription.name,
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
            (False, 'wallet_credit_failed') — ECPay charged but wallet credit failed;
                                              MANUAL RECONCILIATION REQUIRED
        """
        if shortfall <= 0:
            return False, TOPUP_NO_SHORTFALL

        # ECPay requires TotalAmount as a positive integer TWD. Ceil so the wallet
        # covers the invoice even if the shortfall has a fractional TWD component.
        charge_amount = int(math.ceil(shortfall))

        # Default-token preference: Odoo Enterprise's account_payment adds a
        # company-dependent `property_payment_token_id` on res.partner. On
        # community installs that field is absent, so we fall back to the most
        # recently created active ECPG-bound token for this partner. The
        # set-default UI is a no-op without the Enterprise field — that is a
        # known limitation tracked separately for Phase 2c.
        token = self.env['payment.token'].sudo()
        if 'property_payment_token_id' in partner._fields:
            token = partner.with_company(
                partner.company_id or self.env.company
            ).sudo().property_payment_token_id
        if not token or not token.active or not token.ecpg_bindcard_id:
            token = self.env['payment.token'].sudo().search(
                [
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                    ('ecpg_bindcard_id', '!=', False),
                ],
                order='id desc',
                limit=1,
            )

        if not token or not token.active or not token.ecpg_bindcard_id:
            return False, TOPUP_NO_TOKEN

        provider = token.provider_id
        if not provider or not getattr(provider, 'enable_ecpg', False):
            return False, TOPUP_PROVIDER_NOT_ENABLED

        currency = provider.main_currency_id or self.env.ref(
            'base.TWD', raise_if_not_found=False,
        )
        if not currency:
            return False, TOPUP_CURRENCY_NOT_CONFIGURED

        # 20-char limit: put the unique portion first (uuid) so ecpay's
        # _ecpg_build_merchant_trade_no truncation preserves uniqueness.
        # Matches upstream's BIND{uuid16} pattern.
        reference = f'TOPUP{uuid.uuid4().hex[:15].upper()}'
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': token.payment_method_id.id,
            'reference': reference,
            'amount': float(charge_amount),
            'currency_id': currency.id,
            'partner_id': partner.id,
            'token_id': token.id,
            'operation': 'offline',
        })

        try:
            result = tx._send_payment_request()
        except Exception as e:
            _logger.exception('Auto-topup send_payment_request failed: %s', e)
            return False, TOPUP_SDK_ERROR

        # 3DS redirect — cron cannot drive it. Treat as failure.
        if isinstance(result, dict) and result.get('redirect_url'):
            _logger.info('Auto-topup requires 3DS for partner=%s; aborting', partner.id)
            return False, TOPUP_3DS_REQUIRED

        if tx.state != 'done':
            _logger.warning(
                'Auto-topup transaction %s ended in state=%s', tx.reference, tx.state,
            )
            return False, tx.state

        # ECPay has already charged the card. A subsequent failure during
        # add_wallet_points must NOT propagate — otherwise the outer cron
        # transaction rolls back, the payment.transaction row disappears, and
        # the next cron tick will charge the card AGAIN.
        #
        # We catch any wallet-credit failure, log it as a critical operational
        # event (manual reconciliation required), and return a dedicated
        # failure code so the caller does not retry this run.
        try:
            partner.sudo().add_wallet_points(
                float(charge_amount),
                description='Auto top-up for subscription renewal',
                payment_transaction=tx,
            )
        except Exception:
            _logger.exception(
                'CRITICAL: auto-topup charged at ECPay (tx=%s, charge_amount=%.2f) '
                'but wallet credit failed; manual reconciliation required '
                '(partner=%s)',
                tx.reference, charge_amount, partner.id,
            )
            return False, TOPUP_WALLET_CREDIT_FAILED

        _logger.info(
            'Auto-topup succeeded: partner=%s charge_amount=%.2f tx=%s',
            partner.id, charge_amount, tx.reference,
        )
        return True, TOPUP_OK
