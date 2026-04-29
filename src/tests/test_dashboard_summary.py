"""Tests for G4-P2: Dashboard summary endpoint.

`POST /api/dashboard/summary` aggregates the data the post-login dashboard
needs into a single response so the frontend doesn't have to fan out across
N endpoints (and so the wallet balance shown on the card is guaranteed to
match the header CREDITS — they share the `partner.wallet_balance` source).

These tests cover the five blocks in the response shape:
    user / billing / workspaces / members / recent_activity

We use HttpCase + the same JSON-RPC envelope as test_profile_api.py so the
endpoint is exercised through the real HTTP routing layer (auth, type=json,
session) — the unit-tested controller method alone wouldn't catch routing
or auth misconfiguration.
"""
from datetime import date, timedelta

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestDashboardSummary(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        # In HttpCase self.env.user is __system__; use the admin partner
        # that the HTTP session is authenticated as. (Same pattern as
        # test_profile_api.py.)
        self.partner = self.env.ref('base.partner_admin')
        self.admin_user = self.env.ref('base.user_admin')

        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Subscription = self.env['sh.subscription.subscription']
        self.Plan = self.env['sh.subscription.plan']
        self.Tax = self.env['account.tax']

        # Clear any pre-existing test artifacts (workspaces created in
        # earlier test runs in the same DB) by archiving — the dashboard
        # only counts workspaces the user has access_ids for.
        existing = self.WorkspaceAccess.search(
            [('user_id', '=', self.admin_user.id)],
        )
        existing.unlink()

        # Close any pre-existing active subscriptions for the admin partner
        # so the billing test fixture is deterministic. Without this, a
        # production DB with seed subscriptions (e.g. SUB-00001) would make
        # `next_bill_estimate` sum across (existing + test-fixture) subs.
        # The test transaction rolls back so the production rows are restored.
        self.Subscription.sudo().search([
            ('sh_partner_id', '=', self.partner.id),
            ('state', '=', 'in_progress'),
        ]).write({'state': 'closed'})

        # Wallet balance fixture: the partner's wallet_balance comes from
        # loyalty card points. We seed it via the documented public API so
        # the field's compute logic stays exercised end-to-end.
        self._reset_wallet_to(0.0)

    def _reset_wallet_to(self, target):
        """Reset the partner's wallet to exactly `target` points.

        Useful so consecutive tests have a deterministic balance regardless
        of state left by other test cases in the same DB.
        """
        loyalty = self.partner.sudo().get_wallet_loyalty_card()
        if loyalty:
            loyalty.points = 0
        if target > 0:
            self.partner.sudo().add_wallet_points(
                target, description='Dashboard summary test seed',
            )
            self.partner.sudo().invalidate_recordset(['wallet_balance'])

    def _post(self, url, params_json='{}'):
        return self.url_open(
            url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params_json + '}',
            headers={'Content-Type': 'application/json'},
        )

    def _summary(self):
        body = self._post('/api/dashboard/summary').json()
        self.assertIn('result', body, f"Unexpected response: {body}")
        return body['result']

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_summary_returns_user_block(self):
        """`data.user` carries id/name/avatar_url for the logged-in partner."""
        result = self._summary()
        self.assertTrue(result['success'], f"Unexpected: {result}")
        user = result['data']['user']
        self.assertEqual(user['id'], self.admin_user.id)
        self.assertEqual(user['name'], self.partner.name)
        self.assertEqual(
            user['avatar_url'],
            f'/web/image/res.partner/{self.partner.id}/image_128',
        )

    def test_summary_returns_billing_block(self):
        """`data.billing.credits_left` matches `partner.wallet_balance`.

        This is the load-bearing assertion: the original Issue #9 root cause
        was the dashboard card and header CREDITS reading from independent
        sources. Both must derive from `partner.wallet_balance`, so seeding
        the wallet here and asserting on the response value guarantees the
        card cannot drift from the header.
        """
        # Tax-15% sale tax for next-bill estimate verification.
        tax_15 = self.Tax.sudo().create({
            'name': 'Dashboard Test Tax 15%',
            'amount_type': 'percent',
            'amount': 15.0,
            'type_tax_use': 'sale',
            'price_include_override': 'tax_excluded',
        })
        plan = self.Plan.sudo().create({
            'name': 'Dashboard Test Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 499.0,
            'sh_no_of_billing_cycle': -1,
        })
        # Active in_progress subscription for this partner.
        today = date.today()
        self.Subscription.sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': 499.0,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_start_date': today,
            'sh_date_of_next_payment': today + timedelta(days=30),
            'sh_taxes_ids': [(6, 0, [tax_15.id])],
            'state': 'in_progress',
        })

        # Wallet seed at 576.15 — same fixture value as the API contract
        # example in the plan doc.
        self._reset_wallet_to(576.15)

        result = self._summary()
        self.assertTrue(result['success'], f"Unexpected: {result}")
        billing = result['data']['billing']

        # The load-bearing assertion: byte-equal to partner.wallet_balance.
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertAlmostEqual(
            billing['credits_left'], self.partner.wallet_balance, places=2,
        )
        self.assertAlmostEqual(billing['credits_left'], 576.15, places=2)

        # Cycle dates resolve to ISO strings (or None when no sub).
        self.assertIsNotNone(billing['current_cycle_start'])
        self.assertIsNotNone(billing['current_cycle_end'])
        self.assertIsNotNone(billing['next_bill_date'])

        # Next-bill estimate carries the 15% tax: 499 * 1.15 = 573.85.
        self.assertAlmostEqual(billing['next_bill_estimate'], 573.85, places=2)
        self.assertEqual(billing['currency'], 'pts')

    def test_summary_returns_workspaces_block(self):
        """`data.workspaces.total` counts workspaces accessible to the user."""
        # Create a template + a workspace owned by admin + a service.
        template = self.Template.sudo().create({
            'name': 'Dashboard Test App',
            'slug': 'dashboard-test-app',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
        })
        workspace = self.Workspace.sudo().with_user(self.admin_user).create({
            'name': 'Dashboard Test Workspace',
        })
        # `state == 'running'` is the field name verified against
        # cloud_service.py — the plan flagged this as something to verify.
        self.CloudService.sudo().create({
            'name': 'Dashboard Test Svc',
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
        })

        result = self._summary()
        self.assertTrue(result['success'], f"Unexpected: {result}")
        workspaces = result['data']['workspaces']
        self.assertEqual(workspaces['total'], 1)
        self.assertIsNone(workspaces['limit'])
        # services.running is the count of cloud_service rows in 'running'
        # state across the user's accessible workspaces.
        self.assertEqual(workspaces['services']['total'], 1)
        self.assertEqual(workspaces['services']['running'], 1)

    def test_summary_returns_members_block(self):
        """`data.members` mirrors workspace_access for accessible workspaces.

        `pending` is hardcoded 0 because workspace_access has no pending
        state (per plan's "out of scope" note); this test pins the contract
        so a future refactor that adds a state column doesn't accidentally
        break the dashboard payload.
        """
        # 1 workspace, 1 owner = 1 member.
        self.Workspace.sudo().with_user(self.admin_user).create({
            'name': 'Members Block Workspace',
        })

        result = self._summary()
        self.assertTrue(result['success'], f"Unexpected: {result}")
        members = result['data']['members']
        self.assertEqual(members['total'], 1)
        self.assertEqual(members['active'], 1)
        self.assertEqual(members['pending'], 0)

    def test_summary_returns_recent_activity(self):
        """`data.recent_activity` is the last N wallet.transaction rows DESC.

        Setup: create a topup row + a deduct row for the partner. Assert the
        list contains both, ordered DESC by ts, and that each row carries
        the documented keys with the action mapping applied (topup→'topup',
        deduct→'subscription_charge').
        """
        # Top-up via the public API (also seeds wallet, which we don't care
        # about here — the relevant artifact is the wallet.transaction row).
        self.partner.sudo().add_wallet_points(
            500.0, description='Dashboard test top-up',
        )
        # Deduct creates a 'deduct'-typed row.
        self.partner.sudo().deduct_wallet_points(
            50.0, description='Dashboard test deduct',
        )

        result = self._summary()
        self.assertTrue(result['success'], f"Unexpected: {result}")
        activity = result['data']['recent_activity']
        self.assertGreaterEqual(len(activity), 2,
                                "Both topup + deduct rows should appear")

        # Each row has the documented keys.
        for row in activity:
            for key in ('ts', 'actor', 'action', 'target', 'description'):
                self.assertIn(key, row,
                              f"Missing key {key!r} in row {row!r}")

        # DESC by ts: the deduct (created last) appears before the topup.
        # Use the test-specific description prefix so prior wallet.transaction
        # rows from other tests in the same session don't confuse the lookup.
        topup_idx = next(
            i for i, r in enumerate(activity)
            if 'Dashboard test top-up' in (r['description'] or '')
        )
        deduct_idx = next(
            i for i, r in enumerate(activity)
            if 'Dashboard test deduct' in (r['description'] or '')
        )
        self.assertLess(deduct_idx, topup_idx,
                        "Deduct (newer) must come before topup (older)")

        # Action mapping: topup→'topup', deduct→'subscription_charge'.
        topup_row = activity[topup_idx]
        deduct_row = activity[deduct_idx]
        self.assertEqual(topup_row['action'], 'topup')
        self.assertEqual(deduct_row['action'], 'subscription_charge')

        # Actor is the partner name (single-user view).
        self.assertEqual(topup_row['actor'], self.partner.name)
