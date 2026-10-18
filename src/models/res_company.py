import logging

from odoo import api, models
from odoo.tools import formataddr
from odoo.tools.mail import email_split_tuples

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    _inherit = 'res.company'

    def _woow_paas_mail_sender(self, original_sender=None):
        """Format PaaS mail without a duplicated trailing company VAT number.

        Company/partner names are shared with ERP documents. Keep those fields
        and the native ``email_formatted`` unchanged; only PaaS senders opt in.
        A VAT embedded in a name or attached without whitespace is not a suffix.
        When given a rendered sender, retain any intentional template override.
        """
        self.ensure_one()
        if original_sender is not None and email_split_tuples(
                original_sender or '') != email_split_tuples(self.email_formatted or ''):
            return original_sender
        if not self.email:
            return False
        name = (self.name or '').strip()
        vat = (self.vat or '').strip()
        if vat and name.endswith(vat):
            prefix = name[:-len(vat)]
            if prefix and prefix[-1].isspace() and prefix.strip():
                name = prefix.rstrip()
        return formataddr((name, self.email))

    @api.model
    def _ensure_subscription_journal(self):
        """Idempotently point each company's ``sh_journal_id`` at a sale journal.

        ``sh_journal_id`` is the journal upstream ``_generate_invoice_vals``
        stamps on every subscription cycle invoice. When it is unset the
        generated vals carry ``journal_id=False``, so ``account.move.create``
        hits the ``journal_id`` NOT NULL constraint — and
        ``_process_subscription_billing`` swallows that in its outer
        ``except``, so the whole auto-invoice run fails *silently* (no console
        trace, wallet untouched).

        ``post_init_hook`` set this once at install, but post_init does not run
        on ``-u``. A freshly-onboarded environment whose sale journal is
        created *after* this module installs would leave ``sh_journal_id``
        permanently NULL. This method is therefore wired into BOTH install
        (``post_init_hook``) and every upgrade
        (``data/subscription_journal_config.xml`` ``<function>``), mirroring
        the oauth-scope idempotent-ensure pattern
        (``data/oauth_scope_updates.xml``). Idempotent: an already-configured
        journal is never overwritten.
        """
        for company in self.env['res.company'].sudo().search([]):
            if company.sh_journal_id:
                continue
            sale_journal = self.env['account.journal'].sudo().search([
                ('company_id', '=', company.id),
                ('type', '=', 'sale'),
            ], limit=1)
            if sale_journal:
                company.sh_journal_id = sale_journal
                _logger.info(
                    "Auto-configured subscription journal for %s: %s (id=%s)",
                    company.name, sale_journal.name, sale_journal.id,
                )
            else:
                _logger.warning(
                    "No sales journal for company %s — subscription invoices "
                    "will rely on the creation-time fallback; configure "
                    "sh_journal_id once a sale journal exists.",
                    company.name,
                )
