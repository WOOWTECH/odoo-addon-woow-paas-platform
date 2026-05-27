"""Smoke — skill admin backend views registered + admin-only menu.

Spec: docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md §②③
"""
import ast

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillAdminViews(TransactionCase):

    def test_skill_action_registered(self):
        action = self.env.ref('woow_paas_platform.action_skill')
        self.assertEqual(
            action.res_model, 'woow_paas_platform.skill',
            "action_skill must target the skill model",
        )
        ctx = ast.literal_eval(action.context or '{}')
        self.assertFalse(
            ctx.get('create', True),
            "action context must disable backend create",
        )

    def test_skill_menu_admin_only(self):
        menu = self.env.ref('woow_paas_platform.menu_skills')
        group_system = self.env.ref('base.group_system')
        self.assertIn(
            group_system, menu.groups_id,
            "menu_skills must be restricted to base.group_system",
        )
