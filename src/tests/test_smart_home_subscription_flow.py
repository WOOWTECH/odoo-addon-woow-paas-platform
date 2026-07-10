"""Tests for the Smart Home subscription/skip-trial flow.

Covers the controller-level wiring that mirrors cloud-service: create-with-
template-validation, the public `/api/smart-home/templates/<id>/plans` hard
fails, and skip-trial pass-through to the lifecycle mixin (with the role
guard added in review C2).

We exercise the controller's `_create_smarthome` / route handlers
directly rather than via HttpCase — the workspace-access plumbing they
read from `request.env` is exposed through the controller class methods,
so behaviour matches what the routes do without the HTTP overhead.
"""
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.smart_home import (
    SmartHomeController,
)


def _patch_request(env):
    """Patch the controller's `request` global with a MagicMock that exposes env."""
    return patch(
        'odoo.addons.woow_paas_platform.controllers.smart_home.request',
        new=MagicMock(env=env),
    )


class TestSmartHomeSubscriptionFlow(TransactionCase):
    """End-to-end create flow + skip-trial integration with the mixin."""

    def setUp(self):
        super().setUp()
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']

        # Two plans dedicated to SH; one is bound to the template, the other
        # is the "wrong" plan we expect the controller to reject.
        self.plan_in = self.Plan.create({
            'name': 'SH In-Template Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        self.plan_out = self.Plan.create({
            'name': 'SH Foreign Plan',
            'sh_plan_price': 50.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })

        # _create_and_confirm_subscription resolves the subscription product
        # via product.template.sh_subscription_plan_id — without a product
        # for our in-template plan the mixin raises and the happy-path
        # test wouldn't actually exercise the controller.
        self.env['product.template'].sudo().create({
            'name': 'SH In-Template Plan Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan_in.id,
            'list_price': 100.0,
        })

        self.template = self.SHTemplate.create({
            'name': 'Test HA',
            'slug': 'test-ha',
            'subscription_plan_ids': [(6, 0, [self.plan_in.id])],
        })

        self.user = self.env.ref('base.user_admin')
        self.workspace = self.Workspace.create({
            'name': 'SH Sub Test Workspace',
            'owner_id': self.user.id,
        })

        self.controller = SmartHomeController()

    def test_create_rejects_missing_template_id(self):
        """Hard fail when caller forgets template_id — Q2 / brainstorm decision."""
        with _patch_request(self.env):
            res = self.controller._create_smarthome(
                workspace=self.workspace,
                name='Forgotten template',
                ha_port=8123,
                plan_id=self.plan_in.id,
                template_id=None,
            )
        self.assertFalse(res['success'])
        self.assertIn('template', res['error'].lower())

    def test_create_rejects_plan_not_in_template(self):
        """Plan validation is symmetric to paas.py:1586 — out-of-template plan
        is rejected so admin misconfiguration produces a clear error rather
        than a silent subscription with the wrong plan."""
        with _patch_request(self.env):
            res = self.controller._create_smarthome(
                workspace=self.workspace,
                name='Wrong plan',
                ha_port=8123,
                plan_id=self.plan_out.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('not available', res['error'].lower())

    def test_create_rejects_inactive_template(self):
        """An inactive template should not be selectable."""
        self.template.is_active = False
        with _patch_request(self.env):
            res = self.controller._create_smarthome(
                workspace=self.workspace,
                name='Inactive tmpl',
                ha_port=8123,
                plan_id=self.plan_in.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('inactive', res['error'].lower())

    def test_create_rejects_template_with_no_plans(self):
        """Template active but no plans linked — surface the empty-template
        message verbatim (S2). Without this the next branch would fire and
        misleadingly tell the admin the *plan* is unavailable.
        """
        self.template.subscription_plan_ids = [(5, 0, 0)]  # clear M2M
        with _patch_request(self.env):
            res = self.controller._create_smarthome(
                workspace=self.workspace,
                name='Empty template',
                ha_port=8123,
                plan_id=self.plan_in.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('no subscription plans', res['error'].lower())

    def test_create_happy_path_creates_subscription(self):
        """Successful create: SmartHome record exists + has a confirmed
        subscription tied to the chosen plan, with trial active."""
        with _patch_request(self.env):
            res = self.controller._create_smarthome(
                workspace=self.workspace,
                name='Happy HA',
                ha_port=8123,
                plan_id=self.plan_in.id,
                template_id=self.template.id,
            )

        self.assertTrue(res['success'], res.get('error'))
        sh = self.SmartHome.search([('name', '=', 'Happy HA')], limit=1)
        self.assertTrue(sh)
        self.assertTrue(sh.subscription_id)
        self.assertEqual(sh.subscription_id.sh_subscription_plan_id, self.plan_in)
        self.assertTrue(sh.subscription_id.sh_trial)


class TestSmartHomeTemplatePlansEndpoint(TransactionCase):
    """Hard-fail paths on the public `/api/smart-home/templates/<id>/plans`.

    The endpoint refuses to fall back to a name-search if the template is
    misconfigured — admins should see the specific reason, not a blank
    "no plans" UI (Q2 brainstorm decision).
    """

    def setUp(self):
        super().setUp()
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.controller = SmartHomeController()

    def test_template_not_found(self):
        """Browse a non-existent id."""
        with _patch_request(self.env):
            res = self.controller.api_smarthome_template_plans(template_id=999999)
        self.assertFalse(res['success'])
        self.assertIn('not found', res['error'].lower())

    def test_template_inactive(self):
        """Active=False template is filtered out of the catalog."""
        tmpl = self.SHTemplate.create({
            'name': 'Inactive Tmpl',
            'is_active': False,
        })
        with _patch_request(self.env):
            res = self.controller.api_smarthome_template_plans(template_id=tmpl.id)
        self.assertFalse(res['success'])
        # The "not found or inactive" message covers both branches; either
        # word should be enough for the admin to investigate.
        self.assertTrue(
            'inactive' in res['error'].lower() or 'not found' in res['error'].lower(),
            res['error'],
        )

    def test_template_no_plans(self):
        """Active template with no plans linked — hard fail with the
        canonical message that admins see in both list and create paths."""
        tmpl = self.SHTemplate.create({
            'name': 'Empty Tmpl',
            'is_active': True,
        })
        with _patch_request(self.env):
            res = self.controller.api_smarthome_template_plans(template_id=tmpl.id)
        self.assertFalse(res['success'])
        self.assertIn('no subscription plans', res['error'].lower())


class TestSmartHomeSkipTrialEndpoints(TransactionCase):
    """Skip-trial endpoints delegate to the lifecycle mixin and gate on role.

    We don't retest the mixin's logic (covered in
    test_skip_trial_estimate.py) — we verify the controller-side guards
    (workspace access, owner/admin role check from review C2) and that
    the mixin is invoked when guards pass.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Plan = self.env['sh.subscription.plan']

        plan = self.Plan.create({
            'name': 'Skip Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        self.template = self.SHTemplate.create({
            'name': 'Skip TMPL',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })

        self.admin_user = self.env.ref('base.user_admin')
        # Workspace.create auto-provisions a WorkspaceAccess for `owner_id`
        # with role='owner', so we don't need to create one manually here.
        # The previous version of this setUp tried to and hit
        # "duplicate key value violates unique_user_workspace".
        self.workspace = self.Workspace.create({
            'name': 'Skip WS',
            'owner_id': self.admin_user.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'Skip Home',
            'workspace_id': self.workspace.id,
        })
        self.controller = SmartHomeController()

    def _add_member(self, login, role):
        """Create a fresh user and grant them the given role on the workspace."""
        user = self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.test' % login,
        })
        self.WorkspaceAccess.create({
            'workspace_id': self.workspace.id,
            'user_id': user.id,
            'role': role,
        })
        return user

    def test_skip_trial_estimate_admin_invokes_mixin(self):
        """Admin can request the estimate; mixin is called and result returned.

        Replaces the previous test_skip_trial_estimate_called_on_smarthome
        which guarded the assertion with `if res['success']:` and so passed
        even when the underlying delegation never happened.
        """
        sentinel = {
            'untaxed': 100.0, 'tax_rate': 0.0, 'tax_amount': 0.0,
            'total': 100.0, 'currency': 'pts',
            'wallet_balance': 200.0, 'sufficient': True, 'shortage': 0.0,
            'billing_cycle_start': '2026-05-10',
            'billing_cycle_end': '2026-06-10',
        }
        with patch.object(
            type(self.smart_home), '_skip_trial_estimate',
            return_value=sentinel,
        ) as mocked, _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthome_skip_trial_estimate(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data'], sentinel)
        mocked.assert_called_once()

    def test_skip_trial_estimate_non_admin_rejected(self):
        """Workspace 'user'-role member cannot call skip-trial-estimate.

        Regression for review C2 — before the role guard, any member
        could trigger billing-affecting code paths against the workspace
        owner's wallet via the mutating skip-trial endpoint.
        """
        member = self._add_member('sh_member_estimate', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthome_skip_trial_estimate(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    def test_skip_trial_admin_invokes_mixin(self):
        """Admin can call the mutating skip-trial; mixin is invoked + invoice info returned."""
        invoice_sentinel = {'invoice_id': 42, 'amount': 115.0}
        sub_info_sentinel = {
            'id': 1, 'state': 'in_progress', 'is_trial': False,
            'plan_name': 'Skip Plan', 'plan_price': 100.0,
        }
        with patch.object(
            type(self.smart_home), '_skip_trial',
            return_value=invoice_sentinel,
        ) as mocked, patch.object(
            type(self.smart_home), '_get_subscription_info',
            return_value=sub_info_sentinel,
        ), _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthome_skip_trial(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(res['data']['invoice'], invoice_sentinel)
        self.assertEqual(res['data']['subscription'], sub_info_sentinel)
        mocked.assert_called_once()

    def test_skip_trial_non_admin_rejected(self):
        """Member without admin role cannot trigger billing — mutating variant."""
        member = self._add_member('sh_member_skip', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthome_skip_trial(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())


class TestSmartHomeChangePlanACL(TransactionCase):
    """Regression for the missing `.sudo()` on the change-plan endpoint's plan read.

    Before the fix, an internal user without the Subscription/Manager group
    couldn't change SH plan because `Plan.browse(plan_id).exists()` would
    return False under ACL, causing the endpoint to misreport "Plan not
    found". The fix adds `.sudo()` at controllers/smart_home.py:127.

    We don't drive the full happy path (which would require seeded products,
    journal config, partner wallet etc.) — instead we drive the request just
    far enough to prove the Plan read gets past the ACL boundary, by
    asserting that the resulting error is *not* "Plan not found".
    """

    def setUp(self):
        super().setUp()
        self.controller = SmartHomeController()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Plan = self.env['sh.subscription.plan']

        plan_a = self.Plan.create({
            'name': 'ACL Plan A',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.plan_b = self.Plan.create({
            'name': 'ACL Plan B',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.template = self.SHTemplate.create({
            'name': 'ACL Tmpl',
            'subscription_plan_ids': [(6, 0, [plan_a.id, self.plan_b.id])],
        })

        # Internal user *not* in the Subscription/Manager group — models the
        # portal-like permission profile that surfaced the staging bug. The
        # default `res.users.create` here yields an internal user bound only
        # to base.group_user (no subscription_user / subscription_manager).
        self.member = self.env['res.users'].create({
            'name': 'Plain Internal User',
            'login': 'sh_change_plan_member',
            'email': 'sh_change_plan_member@example.test',
        })
        # Workspace.create auto-provisions WorkspaceAccess for owner_id;
        # no need to create another one (would violate unique constraint).
        self.workspace = self.Workspace.create({
            'name': 'Change Plan WS',
            'owner_id': self.member.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'Change Plan Home',
            'workspace_id': self.workspace.id,
        })

    def test_plan_read_works_under_sudo_for_non_manager(self):
        """Non-Manager user gets past `Plan.browse(plan_id).exists()`.

        If the `.sudo()` weren't there, `exists()` would return False under
        ACL and the endpoint would short-circuit with "Plan not found".
        We provide a valid plan id, so the only way for the assertion below
        to succeed is for the sudo'd read to find the plan and the request
        to advance to the next guard ("No active subscription").
        """
        with _patch_request(self.env(user=self.member)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan_b.id,
            )
        self.assertFalse(res['success'])
        self.assertNotIn('plan not found', res['error'].lower())
        self.assertIn('subscription', res['error'].lower())


class TestSmartHomeChangePlanCascadeSudo(TransactionCase):
    """Regression for #27: workspace owner without sh_subscription manager
    group can pass the model ACL on change-plan's subscription traversal.

    PR for #24 added the [OWNER, ADMIN] role check; stg E2E then found the
    next gate — ``ir.model.access`` on ``sh.subscription.subscription`` —
    is *not* waived by workspace ownership. This pins the cascade sudo at
    ``smart_home.py`` so typical workspace owners (NOT in
    ``group_sh_subscription_manager``) reach the same outcome cloud-service
    already does at ``paas.py:874``.

    We don't drive the full happy path (wallet rules, journal config,
    invoice generation depend on demo data we don't seed) — instead we
    drive the request far enough to prove the ACL gate no longer surfaces,
    by asserting the error string does NOT contain the ir.model.access
    rejection.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']
        self.controller = SmartHomeController()

        self.plan_a = self.Plan.create({
            'name': 'Cascade Plan A',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.plan_b = self.Plan.create({
            'name': 'Cascade Plan B',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        # _create_and_confirm_subscription resolves the subscription product
        # via product.template.sh_subscription_plan_id — seed one product
        # per plan because the test exercises change-plan A → B and the
        # endpoint creates a fresh subscription for plan_b, which also
        # needs a backing product.
        for plan, label in [(self.plan_a, 'A'), (self.plan_b, 'B')]:
            self.env['product.template'].sudo().create({
                'name': 'Cascade Plan %s Product' % label,
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': plan.sh_plan_price,
            })
        self.template = self.SHTemplate.create({
            'name': 'Cascade Tmpl',
            'subscription_plan_ids': [(6, 0, [self.plan_a.id, self.plan_b.id])],
        })

        # Workspace owner WITHOUT any sh_subscription group — models the
        # real-world default where workspace ownership grants PaaS-side
        # access but the user remains an ordinary internal user (only
        # base.group_user). This is the profile that surfaced the bug on
        # stg: Mitchell Admin (uid=2) was workspace owner on WS 27 but
        # not in group_sh_subscription_manager, so the relation traversal
        # `smarthome.subscription_id` was denied by ir.model.access.
        self.owner = self.env['res.users'].create({
            'name': 'Workspace Owner Without Manager Group',
            'login': 'sh_owner_no_subscription_manager',
            'email': 'sh_owner_no_subscription_manager@example.test',
        })
        # Explicitly reset groups: sandbox DBs cloned from stg inherit a
        # res.config.settings.default_user_template that auto-grants 4
        # Administrator groups + Invoicing on user.create. Without this
        # reset the fixture would be admin-equivalent and the regression
        # would silently no-op (the R1 reviewer's revert-cycle caught
        # exactly this — pre-fix, test still passed). Hard-pinning groups
        # to base.group_user makes the fixture name match its semantics.
        base_user = self.env.ref('base.group_user')
        self.owner.groups_id = [(6, 0, [base_user.id])]
        # Sanity assertion: pins the regression's value. If a future
        # default_user_template change re-introduces the manager group on
        # ordinary internal users, this assertion fails loudly rather than
        # letting the test silently lose meaning.
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)
        # Workspace.create auto-provisions owner WorkspaceAccess(role=owner).
        self.workspace = self.Workspace.create({
            'name': 'Cascade WS',
            'owner_id': self.owner.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'Cascade Home',
            'workspace_id': self.workspace.id,
        })
        # Seed an in_progress subscription as the test admin (TransactionCase
        # default env). The user-scoped call happens in the test body, where
        # we switch to self.owner via _patch_request.
        partner = self.workspace.owner_id.partner_id
        self.smart_home._create_and_confirm_subscription(partner, self.plan_a)
        # Sanity: fixture actually seeded an in_progress subscription. If this
        # fails, the test below would still pass under cache-only behaviour
        # and silently lose its regression value.
        self.assertTrue(self.smart_home.subscription_id)
        self.assertEqual(self.smart_home.subscription_id.state, 'in_progress')
        # Drop the env cache before the user-scoped call. Odoo's env.cache is
        # transaction-scoped and *shared across envs* — without this, the
        # admin-context reads above (sanity + mixin internals) would prime
        # the cache for sh.subscription.subscription, and the owner-env
        # traversal in the test would hit cache instead of re-checking ACL,
        # masking the very bug we're regressing against. The pre-fix
        # AccessError only surfaces on a fresh read, which is the situation
        # in production (each HTTP request starts with an empty cache).
        self.env.invalidate_all()

    def test_change_plan_workspace_owner_without_manager_group_passes_acl(self):
        """As workspace OWNER (not in group_sh_subscription_manager), the
        change-plan request must pass both gates: the [OWNER, ADMIN] role
        check (#24) AND the model ACL on sh.subscription.subscription.

        The fixture explicitly resets ``self.owner.groups_id`` to only
        ``base.group_user`` (see setUp); without that reset the sandbox-DB
        default_user_template would seed the user with admin-equivalent
        groups and this regression test would no-op.

        Pre-fix: ``old_sub = smarthome.subscription_id`` raised
        ``AccessError`` → ``{success: false, error: "You are not allowed
        to access sh.subscription.subscription"}``.

        Post-fix: ``smarthome.sudo().subscription_id`` reads through; the
        endpoint advances past the ACL gate. The remaining business path
        (refund calc, wallet credit, new subscription creation) may still
        fail under the minimal fixture — those are out of scope here.
        """
        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan_b.id,
            )
        error_text = (res.get('error') or '').lower()
        # The ACL gate must not be the failure mode.
        self.assertNotIn('not allowed to access', error_text, res)
        self.assertNotIn('sh.subscription.subscription', error_text, res)
        # The role check from #24 must also still be passing for this user
        # (workspace OWNER), so the failure mode (if any) is downstream of
        # both gates — not 'Insufficient permissions'.
        self.assertNotIn('insufficient permissions', error_text, res)


class TestSmartHomeChangePlanTrialMode(TransactionCase):
    """Allow change-plan during trial state (issue #34).

    Before the fix at smart_home.py:164, the controller hard-failed any
    change-plan against a trial subscription because sh_end_date is
    legitimately NULL in trial state. The fix wraps the NULL-date guard
    with `if not old_sub.sh_trial:`. This class covers the three product
    paths the brainstorm enumerated:

    - Q1: trial -> trial restarts the trial countdown from new plan
    - Q2: trial -> no-trial enters active state immediately
    - regression: active -> active still computes proration as before

    Refund-in-trial is covered by the existing `test_change_plan_refund`
    suite at the model layer (`_compute_unused_refund` returns 0); these
    tests assert the controller surfaces that 0 in `res['data']`.

    Fixture follows the PR #28 hardening (Q8): owner.groups_id is
    explicitly reset to base.group_user only, env cache is dropped before
    the user-scoped call, and the manager-group absence is sanity-asserted.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']
        self.controller = SmartHomeController()

        # Three plans wired into one template so the change-plan flow has
        # legitimate destinations for all three product paths.
        self.plan_a_trial7 = self.Plan.create({
            'name': 'Trial Mode A (7d trial)',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        self.plan_b_trial14 = self.Plan.create({
            'name': 'Trial Mode B (14d trial)',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 14,
            'sh_trial_unit': 'day',
        })
        self.plan_c_notrial = self.Plan.create({
            'name': 'Trial Mode C (no trial)',
            'sh_plan_price': 300.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_trial': False,
        })
        # _create_and_confirm_subscription resolves the subscription product
        # via product.template.sh_subscription_plan_id — seed one per plan.
        for plan, label in [
            (self.plan_a_trial7, 'A'),
            (self.plan_b_trial14, 'B'),
            (self.plan_c_notrial, 'C'),
        ]:
            self.env['product.template'].sudo().create({
                'name': 'Trial Mode Plan %s Product' % label,
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': plan.sh_plan_price,
            })
        self.template = self.SHTemplate.create({
            'name': 'Trial Mode Tmpl',
            'subscription_plan_ids': [(6, 0, [
                self.plan_a_trial7.id,
                self.plan_b_trial14.id,
                self.plan_c_notrial.id,
            ])],
        })

        self.owner = self.env['res.users'].create({
            'name': 'Trial Mode Owner',
            'login': 'sh_trial_mode_owner',
            'email': 'sh_trial_mode_owner@example.test',
        })
        # Q8 / PR #28 fixture hygiene: drop sandbox-DB default groups so
        # the fixture genuinely models the bug-#34 user profile (workspace
        # owner WITHOUT sh_subscription manager group).
        base_user = self.env.ref('base.group_user')
        self.owner.groups_id = [(6, 0, [base_user.id])]
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)

        self.workspace = self.Workspace.create({
            'name': 'Trial Mode WS',
            'owner_id': self.owner.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'Trial Mode Home',
            'workspace_id': self.workspace.id,
        })

    def _seed_subscription(self, plan):
        """Seed an in_progress sub on `plan`, then force the NULL-end-date
        shape on trial subs to model historical stg DB residue.

        Post-#33, fresh ``_create_and_confirm_subscription`` writes a
        coherent (start, trial_end, end) triple on every trial sub — so
        a brand-new sub no longer naturally reaches the NULL-end-date
        state. But subs 22 / 35–39 on stg, and any equivalent rows on
        prod, were created pre-fix and still carry that shape. The
        controller's relaxation (#34) must continue to accept them, so
        this fixture explicitly NULLs sh_end_date to reproduce that
        legacy state for the #34 regression test.

        Cache invalidation: env.cache is transaction-scope and shared
        across envs; admin-context reads above prime sh.subscription.
        subscription, so the user-scoped call below would otherwise hit
        cache instead of re-evaluating ACL + the date guard.
        """
        partner = self.workspace.owner_id.partner_id
        self.smart_home._create_and_confirm_subscription(partner, plan)
        sub = self.smart_home.sudo().subscription_id
        if sub.sh_trial:
            sub.write({'sh_end_date': False})
        self.env.invalidate_all()

    def test_trial_to_trial_new_sub_restarts_trial(self):
        """Q1: trial -> trial change-plan creates a new sub in trial state
        with the new plan's trial duration counted from today.

        Post-#33 the date triple (start, trial_end, end) is asserted: the
        unconditional plan-driven force in
        ``_force_trial_dates_from_plan`` runs for every trial sub,
        regardless of whether the onchange chain already flipped
        sh_trial=True. Pre-#33 these assertions had to be skipped because
        the dedup gate short-circuited the helper on the onchange-set
        sh_trial=True path.
        """
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta
        self._seed_subscription(self.plan_a_trial7)
        # Pre-condition: old sub is in trial. If this fails the rest of the
        # test is meaningless; assert loudly.
        self.assertTrue(self.smart_home.sudo().subscription_id.sh_trial)

        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan_b_trial14.id,
            )
        self.assertTrue(res['success'], res.get('error'))

        new_sub = self.smart_home.sudo().subscription_id
        self.assertTrue(new_sub.sh_trial, 'new sub should be in trial state')
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_b_trial14)
        # Old sub was trial -> _compute_unused_refund returns 0.
        self.assertEqual(res['data']['refund_amount'], 0)

        # #33: date triple is plan-driven, not onchange-derived.
        today = date.today()
        expected_trial_end = today + timedelta(days=14) - timedelta(days=1)
        expected_start = expected_trial_end + timedelta(days=1)
        expected_end = (
            expected_start + relativedelta(months=1) - timedelta(days=1)
        )
        self.assertEqual(new_sub.sh_trial_end_date, expected_trial_end)
        self.assertEqual(new_sub.sh_start_date, expected_start)
        self.assertEqual(new_sub.sh_end_date, expected_end)

    def test_trial_to_no_trial_enters_active(self):
        """Q2: trial -> no-trial change-plan creates an active (non-trial)
        sub with sh_start_date=today (immediate billing).

        Issue #39: also assert the active-path date triple is coherent —
        pre-fix the new non-trial sub had sh_end_date = start - 32d
        because ``_create_and_confirm_subscription`` fell through to the
        onchange chain that multiplies cycle_delta by
        sh_no_of_billing_cycle (-1, unlimited billing).
        """
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta
        self._seed_subscription(self.plan_a_trial7)

        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan_c_notrial.id,
            )
        self.assertTrue(res['success'], res.get('error'))

        new_sub = self.smart_home.sudo().subscription_id
        self.assertFalse(new_sub.sh_trial, 'new sub should NOT be in trial')
        self.assertEqual(new_sub.sh_subscription_plan_id, self.plan_c_notrial)
        self.assertEqual(new_sub.sh_start_date, date.today())
        # Old sub was trial -> refund still 0.
        self.assertEqual(res['data']['refund_amount'], 0)

        # Issue #39 invariants on the new active sub.
        cycle_delta = relativedelta(months=1)
        expected_end = new_sub.sh_start_date + cycle_delta - timedelta(days=1)
        self.assertGreaterEqual(
            new_sub.sh_end_date, new_sub.sh_start_date,
            'invariant: sh_end_date must never be earlier than sh_start_date',
        )
        self.assertEqual(
            new_sub.sh_end_date, expected_end,
            'sh_end_date must be plan-driven (#39): start + cycle_delta - 1d',
        )
        self.assertEqual(
            new_sub.sh_date_of_next_payment,
            new_sub.sh_start_date + cycle_delta,
            'next payment must be plan-driven (#39): start + cycle_delta',
        )

    def test_active_to_active_regression_proration(self):
        """Regression: active -> active path still produces a positive
        proration refund for a mid-cycle switch.

        Seeds a no-trial sub then re-dates it 15 days in / 15 days out, so
        `_compute_unused_refund` (called by the controller) returns a
        clearly non-zero value (half of price_c = 150)."""
        from datetime import date, timedelta
        self._seed_subscription(self.plan_c_notrial)
        today = date.today()
        old_sub = self.smart_home.sudo().subscription_id
        old_sub.write({
            'sh_trial': False,
            'sh_start_date': today - timedelta(days=15),
            'sh_end_date': today + timedelta(days=15),
        })
        # #324：退點前提為本訂閱真已付 — seed 一筆 done deduct
        self.env['wallet.transaction'].sudo().create({
            'partner_id': old_sub.sh_partner_id.id,
            'transaction_type': 'deduct',
            'amount': -old_sub.sh_plan_price,
            'subscription_id': old_sub.id,
            'state': 'done',
        })
        self.env.invalidate_all()

        with _patch_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan_a_trial7.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        # Half of 300 (15 / 30 days remaining) = 150.0
        self.assertEqual(res['data']['refund_amount'], 150.0)


class TestSmartHomeMutatingEndpointsRoleCheck(TransactionCase):
    """Role-check coverage for the four mutating SH endpoints.

    Sibling of TestSmartHomeSkipTrialEndpoints — same fixture shape, same
    `_add_member` helper, two tests per endpoint (admin happy + non-admin
    rejected). The reject branch asserts the unified "Insufficient
    permissions" wording introduced in this PR.

    For the dispatch endpoints (api_smarthomes / api_smarthome_detail) the
    role check is intentionally inside each mutating action branch so that
    read-only actions (list / get / refresh_status) stay open to every
    workspace member — those reads are not retested here, they are
    exercised by TestSmartHomeSubscriptionFlow and the SH detail page.
    """

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Plan = self.env['sh.subscription.plan']

        self.plan = self.Plan.create({
            'name': 'Role Check Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.template = self.SHTemplate.create({
            'name': 'Role Check Tmpl',
            'subscription_plan_ids': [(6, 0, [self.plan.id])],
        })

        self.admin_user = self.env.ref('base.user_admin')
        # Workspace.create auto-provisions WorkspaceAccess(role='owner') for
        # owner_id — no need to insert another row (would violate the
        # unique_user_workspace constraint).
        self.workspace = self.Workspace.create({
            'name': 'Role Check WS',
            'owner_id': self.admin_user.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'Role Check Home',
            'workspace_id': self.workspace.id,
        })
        self.controller = SmartHomeController()

    def _add_member(self, login, role):
        """Create a fresh user and grant them the given role on the workspace."""
        user = self.env['res.users'].create({
            'name': login,
            'login': login,
            'email': '%s@example.test' % login,
        })
        self.WorkspaceAccess.create({
            'workspace_id': self.workspace.id,
            'user_id': user.id,
            'role': role,
        })
        return user

    # ---------------- create ----------------

    def test_create_admin_succeeds(self):
        """Admin clears the role check and the controller's _create_smarthome
        helper is invoked. We mock the helper because its plan/product
        wiring is exercised separately in TestSmartHomeSubscriptionFlow."""
        sentinel = {'success': True, 'data': {'id': 999}}
        with patch.object(
            SmartHomeController, '_create_smarthome',
            return_value=sentinel,
        ) as mocked, _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthomes(
                workspace_id=self.workspace.id,
                action='create',
                name='Admin Home',
                plan_id=self.plan.id,
                template_id=self.template.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_create_non_admin_rejected(self):
        """A workspace 'user'-role member is blocked before _create_smarthome
        runs — proves the role guard is the gate, not template/plan validation."""
        member = self._add_member('sh_create_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthomes(
                workspace_id=self.workspace.id,
                action='create',
                name='Should Be Rejected',
                plan_id=self.plan.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    # ---------------- delete ----------------

    def test_delete_admin_succeeds(self):
        """Admin passes the role check; both _check_operation_allowed and
        _delete_smarthome are mocked so the test focuses on the gate."""
        with patch.object(
            type(self.smart_home), '_check_operation_allowed',
            return_value=None,
        ), patch.object(
            SmartHomeController, '_delete_smarthome',
            return_value={'success': True, 'data': {'message': 'deleted'}},
        ) as mocked, _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_delete_non_admin_rejected(self):
        """Workspace 'user'-role member cannot delete another owner's SH."""
        member = self._add_member('sh_delete_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    # ---------------- provision ----------------

    def test_provision_admin_succeeds(self):
        """Admin passes the role check on provision; the K8s-side work
        (_provision_smarthome) is mocked since this test is about the gate."""
        with patch.object(
            type(self.smart_home), '_check_operation_allowed',
            return_value=None,
        ), patch.object(
            SmartHomeController, '_provision_smarthome',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='provision',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_provision_non_admin_rejected(self):
        """Provision creates the tunnel + consumes K8s quota; member-level
        users must not be able to trigger it on the owner's behalf."""
        member = self._add_member('sh_provision_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='provision',
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())

    # ---------------- change-plan ----------------

    def test_change_plan_admin_passes_role_check(self):
        """Admin gets past the role check. We don't drive the full happy
        path (which needs an active subscription, partner wallet, journal
        config, etc. — covered separately); we prove the gate by asserting
        the resulting error is the subsequent business-state error, *not*
        a permission error.

        Same approach as TestSmartHomeChangePlanACL above, applied to the
        new role-check guard."""
        with _patch_request(self.env(user=self.admin_user)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan.id,
            )
        self.assertFalse(res['success'])
        self.assertNotIn('permission', res['error'].lower())
        self.assertIn('subscription', res['error'].lower())

    def test_change_plan_non_admin_rejected(self):
        """Member without admin role cannot change the plan — change-plan
        moves money (refund + new charge), so the gate matters here even
        more than for create/delete/provision."""
        member = self._add_member('sh_change_plan_role_member', 'user')
        with _patch_request(self.env(user=member)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                plan_id=self.plan.id,
            )
        self.assertFalse(res['success'])
        self.assertIn('permission', res['error'].lower())
