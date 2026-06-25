"""雙層架構第二層 — project form Settings tab 的 MCP / Skill 內嵌 list。

會議決議 2026-06-03 議題 D：「在 Project 點進去的 setting 旁新增一欄」。
設計：docs/decisions/refactor/mcp-skill-two-layer-config-entry.md
- Configuration 選單 = 全域目錄（scope='system'）
- project form Settings tab = 該 project 的 user-scope 綁定

斷言全部 intent-based（存在性 / 屬性），不硬編 sequence / 欄位順序
（docs/decisions/patterns/phase-versioned-test-assertion.md）。
"""
import ast

from lxml import etree

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProjectFormMcpSkillTab(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        view = cls.env.ref('project.edit_project')
        cls.arch = cls.env['project.project'].get_view(
            view.id, 'form')['arch']
        cls.tree = etree.fromstring(cls.arch)
        # get_view post-processing consumes the `groups=` attribute (the node
        # is kept for users who have the group, but the attribute is stripped)
        # — so `groups=` intent is asserted against the stored inherit arch,
        # like test_skill_admin_views asserts against view.arch. Node presence
        # / non-group attributes are fine to read off get_view's `cls.tree`.
        inherit = cls.env.ref(
            'woow_paas_platform.view_project_form_inherit_mcp_skill')
        cls.inherit_tree = etree.fromstring(inherit.arch)

    def test_settings_tab_has_mcp_server_list(self):
        """MCP 內嵌 list 必須住在原生 Settings tab 內。"""
        nodes = self.tree.xpath(
            "//page[@name='settings']//field[@name='mcp_server_ids']")
        self.assertTrue(
            nodes,
            "project form Settings tab must embed the mcp_server_ids list",
        )

    def test_mcp_embedded_list_defaults_user_scope(self):
        """model default 是 scope='system'；內嵌 list 不帶 default_scope='user'
        會讓使用者從 project form 新增時直接撞 D4 constraint。"""
        node = self.tree.xpath(
            "//page[@name='settings']//field[@name='mcp_server_ids']")[0]
        ctx = ast.literal_eval(node.get('context') or '{}')
        self.assertEqual(
            ctx.get('default_scope'), 'user',
            "mcp_server_ids embedded list must default scope='user' "
            "(D4: scope='user' iff project_id set)",
        )

    def test_settings_tab_has_skill_list(self):
        """Skill 內嵌 list 必須住在原生 Settings tab 內。"""
        nodes = self.tree.xpath(
            "//page[@name='settings']//field[@name='skill_ids']")
        self.assertTrue(
            nodes,
            "project form Settings tab must embed the skill_ids list",
        )

    def test_skill_embedded_list_is_bundle_lifecycle_safe(self):
        """skill 是 bundle-backed：只能走 upload wizard / agent-runtime API
        建立。內嵌 list 必須 create=false + delete=false，避免繞過
        bundle_sha256 required / is_latest 不變式（F-SKU-1 紀律）。"""
        field_node = self.tree.xpath(
            "//page[@name='settings']//field[@name='skill_ids']")[0]
        list_node = field_node.xpath("./list")[0]
        self.assertEqual(
            list_node.get('create'), 'false',
            "skill embedded list must not allow inline create",
        )
        self.assertEqual(
            list_node.get('delete'), 'false',
            "skill embedded list must not allow inline delete",
        )

    def test_mcp_inline_form_does_not_expose_scope(self):
        """row dialog 不給 inline form 會 fallback 到 view_mcp_server_form
        （scope/project_id 可編輯）→ default_scope 只是預設值、可被改回
        system。inline form 必須存在且不暴露 scope / project_id，把
        scope-flip 的 UI 面收掉（defense in depth；record-level 另有
        dual-leg rule + write guard）。"""
        field_node = self.tree.xpath(
            "//page[@name='settings']//field[@name='mcp_server_ids']")[0]
        forms = field_node.xpath("./form")
        self.assertTrue(
            forms,
            "mcp_server_ids must define an inline form (no fallback to "
            "view_mcp_server_form whose scope is editable)",
        )
        self.assertFalse(
            forms[0].xpath(".//field[@name='scope']"),
            "inline MCP form must not expose the scope field",
        )
        self.assertFalse(
            forms[0].xpath(".//field[@name='project_id']"),
            "inline MCP form must not expose the project_id field",
        )

    def test_mcp_created_with_user_scope_passes_d4(self):
        """模擬 project form 內嵌 list 的建立路徑：default_scope='user' +
        One2many 自動帶 project_id → 通過 D4 constraint。"""
        project = self.env['project.project'].create({
            'name': 'D4 form-path project',
        })
        server = self.env['woow_paas_platform.mcp_server'] \
            .with_context(default_scope='user') \
            .create({
                'name': 'form-path mcp',
                'url': 'https://mcp.example.com/sse',
                'project_id': project.id,
            })
        self.assertEqual(server.scope, 'user')
        self.assertEqual(server.project_id, project)
        self.assertIn(server, project.mcp_server_ids)

    def test_d4_rejects_system_scope_with_project(self):
        """D4 反向：scope='system' + project_id 有值必須被 constraint 擋下
        （admin 在 dialog 亂填的最後防線）。"""
        project = self.env['project.project'].create({
            'name': 'D4 negative project',
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.mcp_server'].create({
                'name': 'bad system mcp',
                'url': 'https://mcp.example.com/sse',
                'scope': 'system',
                'project_id': project.id,
            })

    def test_non_member_sees_empty_embedded_mcp_list(self):
        """fail-closed：非 member / 非 follower 的 internal user 開 project
        form，內嵌 list 經 ORM 讀取被 dual-leg rule 過濾 → 空 list
        （orm-vs-endpoint-acl-test-coverage：view 綠 ≠ record secure，
        這裡補 record-level negative）。"""
        project = self.env['project.project'].create({
            'name': 'ACL negative project',
        })
        self.env['woow_paas_platform.mcp_server'] \
            .with_context(default_scope='user') \
            .create({
                'name': 'member-only mcp',
                'url': 'https://mcp.example.com/sse',
                'project_id': project.id,
            })
        outsider = self.env['res.users'].create({
            'name': 'mcp tab outsider',
            'login': 'mcp_tab_outsider',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        visible = self.env['woow_paas_platform.mcp_server'] \
            .with_user(outsider) \
            .search([('project_id', '=', project.id)])
        self.assertFalse(
            visible,
            "non-member internal user must not see the project's "
            "user-scope MCP servers (dual-leg rule fail-closed)",
        )

    def test_skill_block_has_admin_upload_button(self):
        """§2.1 — Upload 按鈕（groups=base.group_system）。get_view 渲染後
        節點存在；groups= 屬性被 post-processing 吃掉，故對 stored arch 斷言。"""
        self.assertTrue(
            self.tree.xpath(
                "//page[@name='settings']"
                "//button[@name='action_open_skill_upload_wizard']"),
            "project form must expose Upload Skill")
        button = self.inherit_tree.xpath(
            "//button[@name='action_open_skill_upload_wizard']")
        self.assertTrue(button)
        self.assertEqual(button[0].get('groups'), 'base.group_system')

    def test_skill_embedded_list_shows_is_effective(self):
        self.assertTrue(self.tree.xpath(
            "//field[@name='skill_ids']//field[@name='is_effective']"))

    def test_skill_embedded_list_still_lifecycle_safe(self):
        """守 F-SKU-1：create/delete 仍鎖死（不因本次強化鬆動）。"""
        node = self.tree.xpath("//field[@name='skill_ids']/list")[0]
        self.assertEqual(node.get('create'), 'false')
        self.assertEqual(node.get('delete'), 'false')

    def test_agent_template_section_present(self):
        """§2.3 — resolved template 顯示 + fork 按鈕 + 條件唯讀 m2m。
        節點存在斷言走 get_view；groups= 屬性走 stored arch（同上）。"""
        self.assertTrue(self.tree.xpath(
            "//field[@name='resolved_agent_template_id']"))
        self.assertTrue(self.tree.xpath(
            "//button[@name='action_create_dedicated_agent_template']"))
        self.assertTrue(self.tree.xpath(
            "//field[@name='resolved_template_skill_ids']"))
        fork = self.inherit_tree.xpath(
            "//button[@name='action_create_dedicated_agent_template']")
        self.assertTrue(fork)
        self.assertEqual(fork[0].get('groups'), 'base.group_system')
        m2m = self.inherit_tree.xpath(
            "//field[@name='resolved_template_skill_ids']")
        self.assertTrue(m2m)
        self.assertEqual(m2m[0].get('groups'), 'base.group_system')

    def test_skill_catalog_action_defaults_system_filter(self):
        """Configuration > Skills = 全域目錄：action 必須帶
        search_default_filter_scope_system（soft filter，admin 可移除以管理
        user-scope，故不用 hard domain）。"""
        action = self.env.ref('woow_paas_platform.action_skill')
        ctx = ast.literal_eval(action.context or '{}')
        self.assertEqual(
            ctx.get('search_default_filter_scope_system'), 1,
            "action_skill must default-filter to system scope "
            "(catalog semantics)",
        )
        self.assertFalse(
            ctx.get('create', True),
            "catalog default filter must not loosen the create=False intent",
        )
        # get_view（過 inheritance merge）而非 arch_db：今天無繼承者兩者等價，
        # 但日後有人 inherit 此 search view 時 arch_db 路徑會看不到合併結果
        search_view = self.env.ref('woow_paas_platform.view_skill_search')
        search_arch = self.env['woow_paas_platform.skill'].get_view(
            search_view.id, 'search')['arch']
        search_tree = etree.fromstring(search_arch)
        self.assertTrue(
            search_tree.xpath("//filter[@name='filter_scope_system']"),
            "skill search view must define the filter_scope_system filter "
            "the action context references",
        )
