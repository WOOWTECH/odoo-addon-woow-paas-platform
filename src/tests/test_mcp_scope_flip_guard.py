"""scope-flip ``write()`` guard — sibling model parity (issue #98 / F-SDS-1).

PR #96 在 ``src/models/skill.py:150-167`` 補了 scope-flip ``write()`` guard，
擋住「非 admin ``base.group_user`` 把自己 ``scope='user'`` 的 skill flip 成
``scope='system'``（平台公開、載進每個租戶 pod）→ 跨租戶外洩」。sibling model
``woow_paas_platform.mcp_server`` 形狀完全相同（``scope`` Selection + nullable
``project_id`` + 同條 D4 ``@api.constrains``），卻沒補同款 guard —— 這是 skill
修補的缺漏雙生。本檔是該 guard 的 **regression lock**（mcp_server 主體 + mcp_tool
``server_id`` re-point 變體）。

攻擊路徑（raw ORM / ``call_kw`` RPC）：``mcp_server_rule_workspace_user``
(``ir_rules.xml:156-172``) domain AND 在 ``('scope','=','user')`` 上，Odoo 用
**pre-write** 狀態判定 write rule → flip 前 row 仍是合法 user-scope → 放行 →
flip 落地。ir.rule 結構上擋不住，必須在 model ``write()`` 攔 vals。

Pattern：``docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md``
Test 守線：``docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md``
  （ORM-green ≠ endpoint-secure：ORM ``with_user()`` 負向 + HttpCase 對等）
鏡像：``src/tests/test_skill_dual_scope.py:165``
  ``test_group_user_cannot_flip_user_skill_to_system``
"""
import json as json_lib
import uuid

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase


@tagged('post_install', '-at_install')
class TestMcpServerScopeFlipGuard(TransactionCase):
    """ORM 層：非 admin 不可把 user-scope mcp_server / mcp_tool flip 成 system。

    Fixture 鏡像已驗證為綠的 ``test_mcp_acl_isolation.py``：Bob 是 ws_bob owner
    （``workspace.create`` 自動把 owner 加進 ``access_ids``），mcp_bob 透過
    project → cloud_service → workspace 鏈對 Bob 有 workspace-member CRUD。
    """

    def setUp(self):
        super().setUp()
        self.McpServer = self.env['woow_paas_platform.mcp_server']
        self.McpTool = self.env['woow_paas_platform.mcp_tool']
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')
        suffix = uuid.uuid4().hex[:8]

        self.bob = User.create({
            'login': f'mcpflip_bob_{suffix}',
            'name': 'Bob (MCP-flip)',
            'email': f'bob-mcpflip-{suffix}@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        # owner_id auto-adds to access_ids in workspace.create.
        self.ws_bob = Workspace.create({
            'name': f'W Bob MCP-flip {suffix}', 'owner_id': self.bob.id,
        })
        self.template = Template.sudo().create({
            'name': 'Tmpl MCP-flip',
            'slug': f'tmpl-mcpflip-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mcpflip',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_bob = Service.sudo().create({
            'name': 'Svc Bob (MCP-flip)',
            'workspace_id': self.ws_bob.id,
            'template_id': self.template.id,
            'helm_release_name': f'bob-flip-svc-{suffix}',
            'helm_namespace': f'paas-ws-bobflip-{suffix}',
        })
        self.project_bob = Project.sudo().create({
            'name': 'Project Bob (MCP-flip)',
            'cloud_service_id': self.svc_bob.id,
        })
        # Bob 自己的 user-scope MCP —— 他有 workspace-member CRUD。
        self.mcp_bob = self.McpServer.sudo().create({
            'name': 'Bob User MCP',
            'url': 'http://bob-svc-mcp.paas-ws-bobflip.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project_bob.id,
        })
        self.tool_bob = self.McpTool.sudo().create({
            'name': 'bob_private_tool',
            'description': 'Bob private tool — must stay private.',
            'input_schema': '{}',
            'server_id': self.mcp_bob.id,
        })
        # 平台 system MCP（re-point / flip 的目標 scope）。
        self.system_mcp = self.McpServer.sudo().create({
            'name': 'Platform System MCP (flip target)',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
        })
        # 他人租戶的 user-scope MCP（bob 無存取權）—— re-point 注入目標。
        self.other_project = Project.sudo().create({
            'name': f'Other Tenant Project {suffix}',
        })
        self.other_user_mcp = self.McpServer.sudo().create({
            'name': 'Other Tenant User MCP',
            'url': 'http://other-tenant.mcp/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': self.other_project.id,
        })

    def _make_admin(self):
        suffix = uuid.uuid4().hex[:8]
        return self.env['res.users'].sudo().create({
            'login': f'mcpflip_admin_{suffix}',
            'name': 'Admin (MCP-flip)',
            'email': f'admin-mcpflip-{suffix}@test.example',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('base.group_system').id,
            ])],
        })

    # ------------------------------------------------------------------ #
    # Precondition / over-correction guard: benign write must still work  #
    # ------------------------------------------------------------------ #
    def test_bob_can_write_benign_field_on_own_mcp(self):
        """guard 不可誤擋非 scope 欄位的正常 write（過度修正守線）。"""
        self.mcp_bob.with_user(self.bob).write({'description': 'edited by bob'})
        self.mcp_bob.invalidate_recordset()
        self.assertEqual(self.mcp_bob.description, 'edited by bob')

    # ------------------------------------------------------------------ #
    # F-SDS-1 core negative — full flip (scope + project_id together)     #
    # ------------------------------------------------------------------ #
    def test_bob_cannot_flip_user_mcp_to_system(self):
        """鏡像 ``test_skill_dual_scope.py:165``：raw ORM flip 必須 raise
        AccessError（跨租戶提權阻擋）。RED today：mcp_server 無 ``write()``
        guard → flip 成功 → ``AccessError not raised``。"""
        with self.assertRaises(AccessError):
            self.mcp_bob.with_user(self.bob).write({
                'scope': 'system', 'project_id': False,
            })

    # ------------------------------------------------------------------ #
    # Single-field variants — guard must fire on either key alone         #
    # ------------------------------------------------------------------ #
    def test_bob_cannot_flip_scope_only(self):
        with self.assertRaises(AccessError):
            self.mcp_bob.with_user(self.bob).write({'scope': 'system'})

    def test_bob_cannot_clear_project_only(self):
        with self.assertRaises(AccessError):
            self.mcp_bob.with_user(self.bob).write({'project_id': False})

    # ------------------------------------------------------------------ #
    # No-op write must NOT raise (UI batch-send 含未改的 scope 欄位)        #
    # ------------------------------------------------------------------ #
    def test_bob_noop_scope_write_does_not_raise(self):
        """寫入與現值相同的 scope（整批送欄位常見）不該被誤擋 —— guard 比對
        實際值變動，非 key 是否存在。"""
        self.mcp_bob.with_user(self.bob).write({
            'scope': 'user', 'description': 'still bob',
        })
        self.mcp_bob.invalidate_recordset()
        self.assertEqual(self.mcp_bob.scope, 'user')
        self.assertEqual(self.mcp_bob.description, 'still bob')

    # ------------------------------------------------------------------ #
    # Admin / sudo boundary — flip 對 admin / sudo 放行                    #
    # ------------------------------------------------------------------ #
    def test_admin_can_flip_scope(self):
        admin = self._make_admin()
        self.mcp_bob.with_user(admin).write({'scope': 'system', 'project_id': False})
        self.mcp_bob.invalidate_recordset()
        self.assertEqual(self.mcp_bob.scope, 'system')
        self.assertFalse(self.mcp_bob.project_id)

    def test_sudo_can_flip_scope(self):
        self.mcp_bob.sudo().write({'scope': 'system', 'project_id': False})
        self.mcp_bob.invalidate_recordset()
        self.assertEqual(self.mcp_bob.scope, 'system')

    # ------------------------------------------------------------------ #
    # mcp_tool sibling — re-point user tool onto a system server (item #2)#
    # ------------------------------------------------------------------ #
    def test_bob_cannot_repoint_tool_to_system_server(self):
        """非 admin 不可把 user-scope tool re-point 到 system-scope server ——
        那會把 tool 定義偷渡上平台公開 server（載進每個租戶），繞過
        ``mcp_tool_rule_system_readonly`` perm_create=False 的 forgery 阻擋。
        ir.rule 用 pre-write row（server 仍 user-scope）判定 → 擋不住 re-point。
        RED today：mcp_tool 無 ``write()`` guard → re-point 成功。"""
        with self.assertRaises(AccessError):
            self.tool_bob.with_user(self.bob).write({
                'server_id': self.system_mcp.id,
            })

    def test_bob_cannot_repoint_tool_to_other_tenant_user_server(self):
        """re-point 到「他人 workspace 的 user-scope server」同樣是跨租戶 tool
        注入（victim 的 AI 會載入此 tool）。guard 對非 admin 一律擋 server_id
        實際變更，不只擋 system-scope 目標。"""
        with self.assertRaises(AccessError):
            self.tool_bob.with_user(self.bob).write({
                'server_id': self.other_user_mcp.id,
            })

    def test_bob_noop_tool_server_write_does_not_raise(self):
        """寫入與現值相同的 server_id 不該被誤擋。"""
        self.tool_bob.with_user(self.bob).write({
            'server_id': self.mcp_bob.id, 'description': 'still bob tool',
        })
        self.tool_bob.invalidate_recordset()
        self.assertEqual(self.tool_bob.server_id.id, self.mcp_bob.id)
        self.assertEqual(self.tool_bob.description, 'still bob tool')

    def test_admin_can_repoint_tool_to_system_server(self):
        admin = self._make_admin()
        self.tool_bob.with_user(admin).write({'server_id': self.system_mcp.id})
        self.tool_bob.invalidate_recordset()
        self.assertEqual(self.tool_bob.server_id.id, self.system_mcp.id)


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestMcpEndpointScopeFlipDefense(HttpCase):
    """Endpoint 對等守線（per orm-vs-endpoint-acl-test-coverage）。

    F-SDS-1 標註 endpoint 本就安全：``_update_mcp_server``（``paas.py:1393``）的
    writable-field allowlist 丟掉 client 的 scope/project_id；``_create_mcp_server``
    硬編 ``scope='user'``。本類是該「endpoint secure」聲明的 **regression lock**
    —— green today，但若未來有人把 scope 加進 allowlist 就會轉紅。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.alice = _new_user(self.env, f'mcpflip_ep_{suffix}', 'Alice (mcp-flip-ep)')
        self.ws = self.Workspace.sudo().create({
            'name': f'flip-ep WS {suffix}', 'owner_id': self.alice.id,
        })
        self.template = self.Template.sudo().create({
            'name': 'flip-ep Tmpl',
            'slug': f'flipep-tpl-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'flipep-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = self.Service.sudo().create({
            'name': 'flip-ep Svc',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
            'helm_release_name': f'flipep-svc-{suffix}',
            'helm_namespace': f'paas-ws-flipep-{suffix}',
        })
        self.project = self.Project.sudo().create({
            'name': 'flip-ep Project',
            'cloud_service_id': self.service.id,
        })
        self.existing_mcp = self.McpServer.sudo().create({
            'name': 'flip-ep Existing MCP',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': self.project.id,
        })
        self.login = f'mcpflip_ep_{suffix}'

    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            f"Endpoint {url!r} returned status={response.status_code}. "
            f"Body excerpt: {response.text[:200]!r}",
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def _legacy_url(self):
        return (
            f'/api/workspaces/{self.ws.id}/services/{self.service.id}'
            f'/mcp-servers'
        )

    def test_update_endpoint_drops_client_scope_flip(self):
        """租戶透過 update endpoint 夾帶 ``scope='system'`` / ``project_id=False``
        必須被 allowlist 丟掉 —— benign 欄位（name）照常套用，但 scope/project_id
        維持不變。"""
        self.authenticate(self.login, self.login)
        result = self._post(self._legacy_url(), {
            'action': 'update',
            'server_id': self.existing_mcp.id,
            'name': 'flip-ep Renamed',
            'scope': 'system',
            'project_id': False,
        })
        self.assertTrue(
            result.get('success'),
            f"benign update（只有 name 落地）應成功。Got: {result!r}",
        )
        self.existing_mcp.invalidate_recordset()
        self.assertEqual(
            self.existing_mcp.scope, 'user',
            "endpoint allowlist 必須丟掉 client 的 scope flip。",
        )
        self.assertEqual(
            self.existing_mcp.project_id.id, self.project.id,
            "endpoint allowlist 必須丟掉 client 的 project_id 清除。",
        )
        self.assertEqual(
            self.existing_mcp.name, 'flip-ep Renamed',
            "allowlist 內的 name 欄位仍應正常套用。",
        )

    def test_create_endpoint_forces_user_scope(self):
        """租戶透過 create endpoint 夾帶 ``scope='system'`` 必須被硬編覆寫成
        ``scope='user'``（``_create_mcp_server`` 硬編 scope/project_id）。"""
        self.authenticate(self.login, self.login)
        result = self._post(self._legacy_url(), {
            'action': 'create',
            'name': 'flip-ep Created',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'system',
        })
        self.assertTrue(
            result.get('success'),
            f"合法 create 應成功。Got: {result!r}",
        )
        created = self.McpServer.sudo().search([
            ('project_id', '=', self.project.id),
            ('name', '=', 'flip-ep Created'),
        ])
        self.assertTrue(created, "create 應落地一筆 MCP row。")
        self.assertEqual(
            created.scope, 'user',
            "create endpoint 不理會 client 的 scope payload，硬編 scope='user'。",
        )
