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

    def test_skill_menu_relocated_under_project_config(self):
        """會議決議 2026-06-03 議題 D：Skill 設定 by project，後台選單
        從 Woow PaaS root 移到原生 Project > Configuration 之下。"""
        menu = self.env.ref('woow_paas_platform.menu_skills')
        project_config = self.env.ref('project.menu_project_config')
        old_root = self.env.ref(
            'woow_paas_platform.menu_woow_paas_platform_root')
        self.assertEqual(
            menu.parent_id, project_config,
            "menu_skills must live under Project > Configuration",
        )
        self.assertNotEqual(
            menu.parent_id, old_root,
            "menu_skills must no longer hang under the Woow PaaS root menu",
        )

    def test_mcp_menu_relocated_under_project_config(self):
        """同上決議：MCP Servers 選單也移到 Project > Configuration。
        （此前 menu_mcp_servers 無任何測試覆蓋，一併補上守線。）"""
        menu = self.env.ref('woow_paas_platform.menu_mcp_servers')
        project_config = self.env.ref('project.menu_project_config')
        old_root = self.env.ref(
            'woow_paas_platform.menu_woow_paas_platform_root')
        self.assertEqual(
            menu.parent_id, project_config,
            "menu_mcp_servers must live under Project > Configuration",
        )
        self.assertNotEqual(
            menu.parent_id, old_root,
            "menu_mcp_servers must no longer hang under the Woow PaaS root "
            "menu",
        )

    def test_settings_view_has_no_mcp_block(self):
        """PaaS Settings 頁不再保留 Manage MCP Servers 捷徑區塊
        （MCP 入口統一收斂到 Project > Configuration）。"""
        view = self.env.ref(
            'woow_paas_platform.res_config_settings_view_form')
        self.assertNotIn(
            'mcp_servers', view.arch_db or '',
            "res.config.settings form must no longer carry the MCP Servers "
            "settings block",
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
