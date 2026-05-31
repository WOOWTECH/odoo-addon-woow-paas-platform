"""Smoke — skill admin backend views registered + admin-only menu.

Spec: docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md §②③
"""
import ast

from lxml import etree

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

    def test_skill_list_has_always_visible_upload_button(self):
        """F-SKU-1 / issue #102: the create=false skill list must expose an
        always-visible Upload Skill button in its header so the empty-state
        list is not a dead end when no skill exists to select."""
        view = self.env.ref('woow_paas_platform.view_skill_list')
        arch = self.env['woow_paas_platform.skill'].get_view(
            view.id, 'list')['arch']
        tree = etree.fromstring(arch)
        buttons = tree.xpath(
            "//header/button[@name='action_open_upload_wizard']")
        self.assertTrue(
            buttons,
            "skill list header must carry an action_open_upload_wizard button",
        )
        self.assertEqual(
            buttons[0].get('display'), 'always',
            "Upload button must be display='always' so it shows with no "
            "record selected (empty-state entry point)",
        )

    def test_open_upload_wizard_returns_modal_action(self):
        """The header button opens the upload wizard as a modal even with no
        record selected (method called on an empty recordset)."""
        action = self.env['woow_paas_platform.skill'].action_open_upload_wizard()
        self.assertEqual(
            action['res_model'], 'woow_paas_platform.skill_upload_wizard')
        self.assertEqual(action['target'], 'new')
        self.assertEqual(action['type'], 'ir.actions.act_window')
