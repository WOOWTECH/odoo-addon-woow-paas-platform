"""Portal-leg access predicate truth tables (two-layer-acl-lockstep Layer 2).

Mirrors the native portal ir.rules so the controller carve-out can never
silently diverge from the rule layer:

* project.task     ``_portal_partner_can_access``  ⇄ project.project_task_rule_portal
* project.project  ``_portal_partner_can_access``  ⇄ project.project_project_rule_portal
                                                     (+ deliberate task-leg extension)
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPortalAgentAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        Partner = cls.env['res.partner']
        User = cls.env['res.users']
        portal_group = cls.env.ref('base.group_portal')

        cls.partner_alice = Partner.create({
            'name': f'Alice Portal {suffix}',
            'email': f'alice-{suffix}@test.example',
        })
        cls.partner_bob = Partner.create({
            'name': f'Bob Portal {suffix}',
            'email': f'bob-{suffix}@test.example',
        })
        cls.portal_alice = User.create({
            'login': f'portal_alice_{suffix}',
            'name': 'Alice',
            'partner_id': cls.partner_alice.id,
            'groups_id': [(6, 0, [portal_group.id])],
        })

        cls.project = cls.env['project.project'].create({
            'name': f'Portal Proj {suffix}',
            'privacy_visibility': 'portal',
        })
        cls.task = cls.env['project.task'].create({
            'name': f'Portal Task {suffix}',
            'project_id': cls.project.id,
        })

    # ---- task predicate -------------------------------------------------- #

    def test_task_follower_allowed(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_non_follower_denied(self):
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_bob))

    def test_task_denied_when_visibility_not_portal(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.project.privacy_visibility = 'followers'
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_collaborator_allowed_limited_denied(self):
        collab = self.env['project.collaborator'].create({
            'project_id': self.project.id,
            'partner_id': self.partner_alice.id,
        })
        self.assertTrue(
            self.task._portal_partner_can_access(self.partner_alice))
        collab.limited_access = True
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_archived_denied(self):
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.task.active = False
        self.assertFalse(
            self.task._portal_partner_can_access(self.partner_alice))

    def test_task_without_project_denied(self):
        orphan = self.env['project.task'].create({'name': 'orphan'})
        orphan.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertFalse(
            orphan._portal_partner_can_access(self.partner_alice))

    # ---- project predicate ----------------------------------------------- #

    def test_project_follower_allowed(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.project._portal_partner_can_access(self.partner_alice))

    def test_project_task_leg_extension(self):
        """Task-only share grants project-level agent status/chat access."""
        self.task.message_subscribe(partner_ids=[self.partner_alice.id])
        self.assertTrue(
            self.project._portal_partner_can_access(self.partner_alice))

    def test_project_stranger_denied(self):
        self.assertFalse(
            self.project._portal_partner_can_access(self.partner_bob))

    def test_project_denied_when_visibility_not_portal(self):
        self.project.message_subscribe(partner_ids=[self.partner_alice.id])
        self.project.privacy_visibility = 'employees'
        self.assertFalse(
            self.project._portal_partner_can_access(self.partner_alice))
