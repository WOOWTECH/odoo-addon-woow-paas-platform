"""ACL regression tests for P9: Subscription Plan access boundaries.

Locks the upstream ``sh_subscription`` module's portal/internal ACL behavior
so future upstream upgrades cannot silently break PaaS multi-tenant isolation.

Scenarios covered (matching meeting-goal-progress.md P9 risk matrix):

1. Portal user only sees own subscription via ORM
   — upstream ``sh_subscription_portal_rule`` filters ``sh_partner_id``.
2. Portal user can read all plans via ORM
   — upstream gives ``base.group_portal`` direct read ACL.
3. Internal user without sh_subscription role is blocked from reading plans
   — upstream provides no ACL for ``base.group_user``;
     PaaS controllers bypass this via ``.sudo()``.
4. Signup template user holds only ``base.group_portal``
   — Odoo 18 default (``base.template_portal_user_id``);
     PaaS depends on ``/web/signup`` yielding portal users.
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError


class TestACLPortalSubscriptionBoundary(TransactionCase):
    """Lock upstream sh_subscription portal record rule + ACL behavior."""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']
        self.Sub = self.env['sh.subscription.subscription']
        Partner = self.env['res.partner']
        User = self.env['res.users']

        portal_group = self.env.ref('base.group_portal')
        internal_group = self.env.ref('base.group_user')

        # Two distinct portal users mapped to two distinct partners.
        self.partner_alice = Partner.create({
            'name': 'Alice (P9 ACL)',
            'email': 'alice-p9@test.example',
        })
        self.partner_bob = Partner.create({
            'name': 'Bob (P9 ACL)',
            'email': 'bob-p9@test.example',
        })

        self.portal_alice = User.create({
            'login': 'p9_portal_alice',
            'name': 'Alice Portal',
            'partner_id': self.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])],
        })
        self.portal_bob = User.create({
            'login': 'p9_portal_bob',
            'name': 'Bob Portal',
            'partner_id': self.partner_bob.id,
            'groups_id': [(6, 0, [portal_group.id])],
        })

        # Internal user without any sh_subscription functional role.
        self.internal_no_role = User.create({
            'login': 'p9_internal_norole',
            'name': 'Internal No Role',
            'groups_id': [(6, 0, [internal_group.id])],
        })

        # Two subscriptions, one per partner.
        plan = self._ensure_plan()
        self.alice_sub = self.Sub.sudo().create({
            'sh_partner_id': self.partner_alice.id,
            'sh_subscription_plan_id': plan.id,
        })
        self.bob_sub = self.Sub.sudo().create({
            'sh_partner_id': self.partner_bob.id,
            'sh_subscription_plan_id': plan.id,
        })

    def _ensure_plan(self):
        plan = self.Plan.sudo().search(
            [('name', '=', 'P9 ACL Test Plan')], limit=1
        )
        if not plan:
            plan = self.Plan.sudo().create({
                'name': 'P9 ACL Test Plan',
                'sh_duration': 1,
                'sh_unit': 'month',
                'sh_plan_price': 100,
                'sh_no_of_billing_cycle': -1,
            })
        return plan

    # ------------------------------------------------------------------ #
    # 1. Portal user only sees own subscription via ORM
    # ------------------------------------------------------------------ #
    def test_portal_user_only_sees_own_subscription_via_orm(self):
        """Upstream ``sh_subscription_portal_rule`` must filter ``sh_partner_id``."""
        Sub_as_alice = self.Sub.with_user(self.portal_alice)
        results = Sub_as_alice.search([])
        partner_ids = set(results.mapped('sh_partner_id.id'))
        self.assertIn(self.partner_alice.id, partner_ids)
        self.assertNotIn(
            self.partner_bob.id, partner_ids,
            "Portal user must NOT see other partners' subscriptions "
            "(upstream sh_subscription_portal_rule regression)",
        )

    # ------------------------------------------------------------------ #
    # 2. Portal user can read all plans via ORM
    # ------------------------------------------------------------------ #
    def test_portal_user_can_read_all_plans_via_orm(self):
        """Upstream grants ``base.group_portal`` direct read ACL on plans."""
        plan = self._ensure_plan()
        Plan_as_alice = self.Plan.with_user(self.portal_alice)
        # ``read`` forces ACL evaluation; an unauthorized user would raise.
        data = Plan_as_alice.browse(plan.id).read(['name'])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'P9 ACL Test Plan')

    # ------------------------------------------------------------------ #
    # 3. Internal user without sh_subscription role is blocked from plan
    # ------------------------------------------------------------------ #
    def test_internal_user_without_role_blocked_from_plan(self):
        """Upstream provides no ``base.group_user`` ACL — must raise AccessError.

        PaaS depends on this so internal users without explicit role
        cannot inspect the plan catalog via raw RPC. Controllers bypass
        this gap via ``.sudo()`` for legitimate UI flows.
        """
        plan = self._ensure_plan()
        Plan_as_internal = self.Plan.with_user(self.internal_no_role)
        with self.assertRaises(AccessError):
            Plan_as_internal.browse(plan.id).read(['name'])

    # ------------------------------------------------------------------ #
    # 4. Signup template user holds only base.group_portal
    # ------------------------------------------------------------------ #
    def test_signup_template_user_only_has_portal_group(self):
        """Odoo 18 default: ``base.template_portal_user_id`` → portal-only.

        ``/web/signup`` copies this template user; if an admin redirects
        the config_parameter to an internal user, every new signup would
        gain internal access. We don't *prevent* admin reconfiguration
        (admin is a trusted role) but we lock the default at install time.
        """
        param_value = self.env['ir.config_parameter'].sudo().get_param(
            'base.template_portal_user_id'
        )
        self.assertTrue(
            param_value,
            "ir.config_parameter 'base.template_portal_user_id' must be set",
        )
        template_user = self.env['res.users'].sudo().browse(int(param_value))
        self.assertTrue(template_user.exists(), "Template user must exist")

        groups = template_user.groups_id
        self.assertIn(
            self.env.ref('base.group_portal'), groups,
            "Signup template user must include base.group_portal",
        )
        self.assertNotIn(
            self.env.ref('base.group_user'), groups,
            "Signup template user must NOT include base.group_user "
            "(would grant every new /web/signup user internal access)",
        )
