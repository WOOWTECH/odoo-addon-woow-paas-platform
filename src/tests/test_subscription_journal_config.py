"""sh_journal_id auto-config idempotency + invoice-creation defense.

Regression guard for the gap found during Smart Home gift-points Phase 5
verification: ``company.sh_journal_id`` was set only by ``post_init_hook``
(install-only), so a freshly-onboarded environment whose sale journal is
created *after* this module installs would leave it permanently NULL. Upstream
``_generate_invoice_vals`` then stamps ``journal_id=False`` → ``account.move``
NOT NULL violation → swallowed by ``_process_subscription_billing``'s outer
``except`` = a *silent* billing outage.

The fix wires ``res.company._ensure_subscription_journal`` into both install
and every ``-u`` (data/subscription_journal_config.xml ``<function>``), and
adds a fallback in ``_get_or_create_cycle_invoice`` so invoice creation never
silently dies on an unset journal.
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSubscriptionJournalConfig(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Company = cls.env['res.company']
        cls.Journal = cls.env['account.journal']
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.company = cls.env.company
        # Guarantee a sale journal exists for this company so the tests
        # validate the gap the fix targets — "sale journal present but
        # sh_journal_id unset" — independent of whether the bare install /
        # at_install environment has finished setting up account journals.
        if not cls.env['account.journal'].search([
            ('company_id', '=', cls.company.id), ('type', '=', 'sale'),
        ], limit=1):
            cls.env['account.journal'].create({
                'name': 'Test Sales', 'code': 'TSAL', 'type': 'sale',
                'company_id': cls.company.id,
            })
        # Guarantee an income account so the invoice LINE is accountable in a
        # bare --without-demo env that ships no chart of accounts. Orthogonal
        # to the journal_id gap under test, but required for the invoice to
        # persist at all.
        cls.income_account = cls.env['account.account'].search(
            [('account_type', '=', 'income')], limit=1)
        if not cls.income_account:
            cls.income_account = cls.env['account.account'].create({
                'code': 'TINC', 'name': 'Test Income', 'account_type': 'income',
            })

    def _a_sale_journal(self):
        return self.Journal.search([
            ('company_id', '=', self.company.id),
            ('type', '=', 'sale'),
        ], limit=1)

    # --- _ensure_subscription_journal ----------------------------------- #

    def test_ensure_sets_sh_journal_when_unset(self):
        """Unset sh_journal_id → ensure picks a sale journal."""
        self.company.sh_journal_id = False
        self.Company._ensure_subscription_journal()
        self.assertTrue(
            self.company.sh_journal_id,
            "sh_journal_id must be auto-configured when a sale journal exists",
        )
        self.assertEqual(self.company.sh_journal_id.type, 'sale')

    def test_ensure_is_idempotent_does_not_overwrite(self):
        """Already-set sh_journal_id must NOT be overwritten on re-run."""
        sale2 = self.Journal.create({
            'name': 'Test Sale 2', 'code': 'TSAL2', 'type': 'sale',
        })
        self.company.sh_journal_id = sale2
        self.Company._ensure_subscription_journal()
        self.assertEqual(
            self.company.sh_journal_id, sale2,
            "ensure must skip a company whose journal is already configured",
        )

    # --- defense: invoice creation must not silently fail --------------- #

    def test_cycle_invoice_falls_back_when_journal_unset(self):
        """sh_journal_id unset → _get_or_create_cycle_invoice still builds an
        invoice with a fallback sale journal (not a swallowed NOT NULL crash)."""
        self.company.sh_journal_id = False
        partner = self.env['res.partner'].create({
            'name': 'JournalGapTest', 'email': 'jgap@example.test',
        })
        product = self.env['product.product'].create({
            'name': 'SH svc (journal gap)', 'list_price': 100.0, 'type': 'service',
            'property_account_income_id': self.income_account.id,
        })
        today = date.today()
        sub = self.Sub.sudo().create({
            'sh_partner_id': partner.id,
            'product_id': product.id,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_plan_price': 100.0, 'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress', 'sh_trial': False,
        })
        invoice, is_new = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertTrue(is_new)
        self.assertTrue(
            invoice.journal_id,
            "invoice must receive a fallback journal instead of a NULL crash",
        )
        self.assertEqual(invoice.journal_id.type, 'sale')
