# -*- coding: utf-8 -*-
"""#554：MCP tool sync／connection test 失敗被雙層吞掉，API 回報 `success: True`。

`action_sync_tools`（`mcp_server.py:259`）自帶 catch-all：攔下例外、寫
`state='error'`，然後**隱式 return None**、不再往外拋。呼叫端
（`paas.py` 的 `_sync_mcp_server` / `_sync_project_mcp_server`）雖然有 try/except，
卻**收不到例外**（已被內層吃掉）⇒ 直接走到 `return {'success': True, ...}`。

⇒ 半同步的 tool 清單 ＋ `state='error'` 全部隨 request 正常 commit，而 API 回報成功。
**使用者看到「同步成功」。** `action_test_connection`（`:393`）同構，而且更徹底：
成功與失敗兩條分支都沒有 return 語句，回傳值連判讀空間都沒有。

#316 記錄的 stg 現象（`mcp_server` id=2 呈現 `state=error` / `tool_count=0`）就是這個
catch-all 寫下的。兩者不同層：#316 是環境問題（sync 為何失敗），本檔是程式問題
（sync 失敗時使用者看不到）。即使 #316 修好，只要 sync 因憑證過期／上游改版／逾時
而失敗，使用者一樣會收到「同步成功」。

## 修法契約（leader5 裁決，(b) 回傳結構化失敗值）

1. `action_sync_tools` 回 **bool**（`True`＝成功）。
2. controller 判讀回傳值，失敗時讀 `server.state_message` 組錯誤訊息——**不把訊息
   塞進回傳值**。
3. `action_test_connection` 同步改回傳 bool。
4. `action_sync_tools` 與 `action_sync_tools_safe` 的 bool 語意必須**一致**
   （都是 `True`＝成功）。兩支同回 bool 卻語意相反是未來最容易踩的坑。

**為什麼不是「往外 raise」**：raise 會讓例外逸出到 JSON-RPC dispatcher，而
`odoo/service/model.py` 的 `retrying()` 在例外路徑走 `env.reset()` → `raise`、
**不會**走到 `env.cr.commit()`，`Cursor.close(rollback=True)` 預設回捲 ⇒ callee 剛寫
的 `state='error'` 稽核紀錄會被一起捲掉。Odoo backend 的 form 按鈕
（`mcp_server_views.xml:10/15`）正是這條路徑。

**為什麼 bool 而不是 dict**：`web/static/src/webclient/actions/action_service.js:1478`
把 object 回傳值直接當 action 執行，缺 `type` 會在 :1421 throw。bool 落進 else 分支被
安全忽略（現行碼成功時本來就 return True，按鈕沒炸過即是實證）。

## 測試結構

每一條「API 應回 `success: False`」都配一條**前提斷言**：`state == 'error'`
（＝ sync 確實失敗了）。少了它，被測程式一改成「sync 其實成功」，斷言就退化成
恆真——API 回 True 也不會讓測試變紅。
"""
import json
import sys
import types
import uuid

from unittest.mock import patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.mcp_server import McpServer

_SYNC_FAILURE = 'injected: MCP endpoint unreachable'


def _seed_mcp_world(env, tag, owner=None):
    """建 workspace → template → service → project → user-scope mcp_server。

    ``owner`` 必須在 **create 當下**給：`workspace.create()` 的 override 是在那一刻
    依 ``owner_id`` 補 ``role=owner`` 的 access 列，事後 ``write({'owner_id': ...})``
    不會補列 ⇒ 端點會以「查無專案或無權限」擋下，測試就死在 ACL 而不是被測缺陷上。
    """
    workspace = env['woow_paas_platform.workspace'].sudo().create({
        'name': f'554 WS {tag}',
        **({'owner_id': owner.id} if owner else {}),
    })
    template = env['woow_paas_platform.cloud_app_template'].sudo().create({
        'name': f'554 Template {tag}',
        'slug': f'554-tpl-{uuid.uuid4().hex[:8]}',
        'helm_repo_url': 'https://charts.example.com',
        'helm_chart_name': '554-tpl',
        'helm_chart_version': '1.0.0',
        'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
    })
    service = env['woow_paas_platform.cloud_service'].sudo().create({
        'name': f'554 Service {tag}',
        'workspace_id': workspace.id,
        'template_id': template.id,
        'helm_release_name': f'p554-{tag}',
        'helm_namespace': f'paas-ws-p554-{tag}',
    })
    project = env['project.project'].sudo().create({
        'name': f'554 Project {tag}',
        'cloud_service_id': service.id,
    })
    server = env['woow_paas_platform.mcp_server'].sudo().create({
        'name': f'554 MCP {tag}',
        'url': f'http://p554-{tag}-mcp.paas-ws-p554-{tag}.svc.cluster.local:3001/mcp',
        'transport': 'streamable_http',
        'scope': 'user',
        'project_id': project.id,
    })
    return workspace, service, project, server


# =========================================================================== #
#  API 層：失敗必須對使用者可見                                                #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestMcpSyncFailureVisibility(HttpCase):
    """sync／test 失敗時，兩個入口的 API 都必須回報失敗。"""

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = self.env['res.users'].sudo().create({
            'name': '554 Alice',
            'login': 'p554_alice',
            'password': 'p554_alice',
            'email': 'p554-alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        (self.workspace, self.service, self.project,
         self.server) = _seed_mcp_world(self.env, 'api', owner=self.alice)
        self.authenticate('p554_alice', 'p554_alice')

    # ── helpers ──────────────────────────────────────────────────────
    def _post(self, url, params):
        resp = self.url_open(
            url,
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            resp.status_code, 200,
            'JSON-RPC 端點必須回 HTTP 200（實得 %s，body: %r）'
            % (resp.status_code, resp.text[:300]))
        parsed = resp.json()
        self.assertIn('result', parsed,
                      '本端點不應把失敗變成框架層錯誤：%r' % (parsed,))
        self.env.invalidate_all()
        return parsed['result']

    def _project_action(self, action):
        return self._post(
            '/api/support-projects/%s/mcp-servers' % self.project.id,
            {'action': action, 'server_id': self.server.id})

    def _service_action(self, action):
        return self._post(
            '/api/workspaces/%s/services/%s/mcp-servers'
            % (self.workspace.id, self.service.id),
            {'action': action, 'server_id': self.server.id})

    def _stored_state(self):
        self.env.invalidate_all()
        return self.server.sudo().read(['state', 'state_message'])[0]

    @staticmethod
    def _fail_async_sync():
        return patch.object(
            McpServer, '_async_sync_tools',
            side_effect=ConnectionError(_SYNC_FAILURE))

    @staticmethod
    def _fail_async_test():
        return patch.object(
            McpServer, '_async_test_connection',
            side_effect=ConnectionError(_SYNC_FAILURE))

    @staticmethod
    def _succeed_async_sync():
        async def _ok(self):
            return True
        return patch.object(McpServer, '_async_sync_tools', _ok)

    @staticmethod
    def _succeed_async_test():
        async def _ok(self):
            return True
        return patch.object(McpServer, '_async_test_connection', _ok)

    # ── 陽性對照：成功時 API 回 True 且 state 變 connected ─────────────
    def test_positive_control_project_sync_succeeds(self):
        with self._succeed_async_sync():
            result = self._project_action('sync')

        self.assertTrue(result.get('success'),
                        'sync 成功時 API 必須回 success（回 %r）' % (result,))
        self.assertEqual(
            self._stored_state()['state'], 'connected',
            '成功路徑必須把 state 寫成 connected——探針有鑑別力的證明')

    def test_positive_control_project_test_succeeds(self):
        with self._succeed_async_test():
            result = self._project_action('test')

        self.assertTrue(result.get('success'),
                        'test 成功時 API 必須回 success（回 %r）' % (result,))
        self.assertEqual(
            self._stored_state()['state'], 'connected',
            '成功路徑必須把 state 寫成 connected——探針有鑑別力的證明')

    # ── 主斷言：sync 失敗必須回 success: False ───────────────────────
    def test_project_sync_failure_is_reported(self):
        with self._fail_async_sync():
            result = self._project_action('sync')

        stored = self._stored_state()
        # 前提：sync 真的失敗了。少了它，被測程式一改成「sync 其實成功」，
        # 下面那條就退化成恆真。
        self.assertEqual(
            stored['state'], 'error',
            '前提斷言：注入後 sync 必須真的失敗（state 應為 error，實得 %r）'
            % (stored['state'],))
        self.assertFalse(
            result.get('success'),
            'sync 失敗時 API 不得回報成功——使用者會以為 tool 清單是最新的'
            '（回 %r）' % (result,))

    def test_service_sync_failure_is_reported(self):
        with self._fail_async_sync():
            result = self._service_action('sync')

        stored = self._stored_state()
        self.assertEqual(
            stored['state'], 'error',
            '前提斷言：注入後 sync 必須真的失敗（state 應為 error，實得 %r）'
            % (stored['state'],))
        self.assertFalse(
            result.get('success'),
            'sync 失敗時 API 不得回報成功（回 %r）' % (result,))

    # ── 主斷言：connection test 失敗必須回 success: False ─────────────
    def test_project_connection_test_failure_is_reported(self):
        with self._fail_async_test():
            result = self._project_action('test')

        stored = self._stored_state()
        self.assertEqual(
            stored['state'], 'error',
            '前提斷言：注入後連線測試必須真的失敗（state 應為 error，實得 %r）'
            % (stored['state'],))
        self.assertFalse(
            result.get('success'),
            '連線測試失敗時 API 不得回報成功（回 %r）' % (result,))

    def test_service_connection_test_failure_is_reported(self):
        with self._fail_async_test():
            result = self._service_action('test')

        stored = self._stored_state()
        self.assertEqual(
            stored['state'], 'error',
            '前提斷言：注入後連線測試必須真的失敗（state 應為 error，實得 %r）'
            % (stored['state'],))
        self.assertFalse(
            result.get('success'),
            '連線測試失敗時 API 不得回報成功（回 %r）' % (result,))

    # ── 錯誤訊息要帶得出來（裁決點 2：controller 讀 state_message）────
    def test_failure_surfaces_the_underlying_reason(self):
        with self._fail_async_sync():
            result = self._project_action('sync')

        stored = self._stored_state()
        self.assertIn(
            _SYNC_FAILURE, stored['state_message'] or '',
            '前提斷言：失敗原因必須先被 callee 寫進 state_message')
        self.assertIn(
            _SYNC_FAILURE, str(result.get('error') or ''),
            '光說失敗不夠——API 要把 state_message 裡的原因帶給使用者，'
            '否則使用者只知道失敗、不知道為什麼（回 %r）' % (result,))

    # ── 稽核紀錄不得被回捲（否決 (a) raise 的那條理由）───────────────
    def test_failure_keeps_the_error_state_audit_trail(self):
        with self._fail_async_sync():
            self._project_action('sync')

        stored = self._stored_state()
        self.assertEqual(
            stored['state'], 'error',
            'API 回報失敗之後，`state=error` 必須留在 DB。若改用「往外 raise」，'
            '框架會回捲整筆交易，使用者連「失敗過」都看不到。')


# =========================================================================== #
#  Model 層：回傳契約                                                          #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestMcpActionReturnContract(TransactionCase):
    """`action_sync_tools` / `action_test_connection` 必須回可判讀的 bool。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, _, _, cls.server = _seed_mcp_world(cls.env, 'model')

    @staticmethod
    def _fail_async_sync():
        return patch.object(
            McpServer, '_async_sync_tools',
            side_effect=ConnectionError(_SYNC_FAILURE))

    @staticmethod
    def _succeed_async_sync():
        async def _ok(self):
            return True
        return patch.object(McpServer, '_async_sync_tools', _ok)

    def test_sync_tools_returns_true_on_success(self):
        with self._succeed_async_sync():
            result = self.server.action_sync_tools()

        self.assertIs(result, True,
                      'action_sync_tools 成功時必須回 True（實得 %r）' % (result,))
        self.assertEqual(self.server.state, 'connected')

    def test_sync_tools_returns_false_on_failure(self):
        with self._fail_async_sync():
            result = self.server.action_sync_tools()

        self.assertEqual(
            self.server.state, 'error',
            '前提斷言：注入後 sync 必須真的失敗')
        self.assertIs(
            result, False,
            'action_sync_tools 失敗時必須回 False；隱式 return None 讓呼叫端'
            '無從分辨成敗（實得 %r）' % (result,))

    def test_test_connection_returns_true_on_success(self):
        async def _ok(self):
            return True
        with patch.object(McpServer, '_async_test_connection', _ok):
            result = self.server.action_test_connection()

        self.assertIs(
            result, True,
            'action_test_connection 成功時必須回 True（實得 %r）——'
            '現行碼兩條分支都沒有 return 語句' % (result,))

    def test_test_connection_returns_false_on_failure(self):
        with patch.object(McpServer, '_async_test_connection',
                          side_effect=ConnectionError(_SYNC_FAILURE)):
            result = self.server.action_test_connection()

        self.assertEqual(
            self.server.state, 'error',
            '前提斷言：注入後連線測試必須真的失敗')
        self.assertIs(
            result, False,
            'action_test_connection 失敗時必須回 False（實得 %r）' % (result,))

    # ── 裁決點 5：兩支 bool 語意必須一致 ─────────────────────────────
    def test_sync_tools_and_safe_variant_agree_on_bool_semantics(self):
        """兩支同回 bool 卻語意相反，是未來最容易踩的坑。"""
        self.server.write({'auto_created': True, 'sync_retry_count': 0})

        with self._succeed_async_sync():
            regular_ok = self.server.action_sync_tools()
            safe_ok = self.server.action_sync_tools_safe()
        with self._fail_async_sync():
            regular_fail = self.server.action_sync_tools()
            safe_fail = self.server.action_sync_tools_safe()

        self.assertIs(regular_ok, True, 'action_sync_tools 成功應回 True')
        self.assertIs(safe_ok, True, 'action_sync_tools_safe 成功應回 True')
        self.assertIs(regular_fail, False, 'action_sync_tools 失敗應回 False')
        self.assertIs(safe_fail, False, 'action_sync_tools_safe 失敗應回 False')


# =========================================================================== #
#  半寫入 tool 列的可達性（savepoint 的前提，leader5 指定要實測）              #
# =========================================================================== #


class _FakeTool:
    """duck-type 模仿 langchain 的 BaseTool——`_async_sync_tools` 只讀這三個屬性。"""

    def __init__(self, name, description, args_schema):
        self.name = name
        self.description = description
        self.args_schema = args_schema


class _PydanticLikeSchema:
    """模仿 langchain 實際給的 `args_schema`：一個 pydantic model **類別**。

    `json.dumps` 對它會 TypeError——這正是 `mcp_server.py:374` 沒有防到的東西。
    """


class _FakeMcpClient:
    """取代 `MultiServerMCPClient`：只換掉「上游 MCP server 回了什麼」這個邊界，
    `_async_sync_tools` 的迴圈、create/write、停用邏輯全部跑真的。"""

    tools = []

    def __init__(self, config):
        self.config = config

    async def get_tools(self):
        return list(self.tools)


@tagged('post_install', '-at_install')
class TestMcpSyncPartialWriteReachability(TransactionCase):
    """第 3 個 tool 的 schema 不可序列化時，前 2 個已經建立 ⇒ 半寫入。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, _, _, cls.server = _seed_mcp_world(cls.env, 'partial')

    def _fake_upstream(self, tools):
        module = types.ModuleType('langchain_mcp_adapters.client')
        client_cls = type('MultiServerMCPClient', (_FakeMcpClient,),
                          {'tools': tools})
        module.MultiServerMCPClient = client_cls
        parent = sys.modules.get('langchain_mcp_adapters') \
            or types.ModuleType('langchain_mcp_adapters')
        return patch.dict(sys.modules, {
            'langchain_mcp_adapters': parent,
            'langchain_mcp_adapters.client': module,
        })

    def _tool_names(self):
        self.env.invalidate_all()
        return sorted(self.server.sudo().tool_ids.mapped('name'))

    def test_third_tool_with_unserializable_schema_aborts_mid_loop(self):
        """前提斷言：這條路徑真的會在迴圈中途炸，而且前兩個已經落地。"""
        tools = [
            _FakeTool('tool_a', 'first', {'type': 'object'}),
            _FakeTool('tool_b', 'second', {'type': 'object'}),
            _FakeTool('tool_c', 'third', _PydanticLikeSchema),
        ]
        with self._fake_upstream(tools):
            result = self.server.action_sync_tools()

        self.assertEqual(
            self.server.state, 'error',
            '前提斷言：不可序列化的 schema 必須讓這次 sync 真的失敗')
        self.assertFalse(
            result,
            'sync 中途失敗必須回報失敗（實得 %r）' % (result,))

    def test_failed_sync_leaves_no_half_written_tool_list(self):
        """半寫入的 tool 列不得隨 request commit 落地。"""
        tools = [
            _FakeTool('tool_a', 'first', {'type': 'object'}),
            _FakeTool('tool_b', 'second', {'type': 'object'}),
            _FakeTool('tool_c', 'third', _PydanticLikeSchema),
        ]
        with self._fake_upstream(tools):
            self.server.action_sync_tools()

        self.assertEqual(
            self._tool_names(), [],
            'sync 在第 3 個 tool 炸掉時，前 2 個不得留下——否則 tool 清單處於'
            '半同步狀態（實得 %r）' % (self._tool_names(),))

    def test_failed_resync_does_not_deactivate_surviving_tools(self):
        """既有 tool 列在一次失敗的 re-sync 之後必須原封不動。"""
        async def _ok(self):
            return True

        first_batch = [
            _FakeTool('tool_a', 'first', {'type': 'object'}),
            _FakeTool('tool_b', 'second', {'type': 'object'}),
        ]
        with self._fake_upstream(first_batch):
            self.server.action_sync_tools()
        self.assertEqual(
            self._tool_names(), ['tool_a', 'tool_b'],
            '前提斷言：第一次 sync 必須成功建立兩個 tool')

        broken_batch = [
            _FakeTool('tool_a', 'first', {'type': 'object'}),
            _FakeTool('tool_c', 'third', _PydanticLikeSchema),
        ]
        with self._fake_upstream(broken_batch):
            self.server.action_sync_tools()

        self.assertEqual(
            self.server.state, 'error',
            '前提斷言：第二次 sync 必須真的失敗')
        self.assertEqual(
            self._tool_names(), ['tool_a', 'tool_b'],
            '失敗的 re-sync 不得改動既有 tool 清單（實得 %r）'
            % (self._tool_names(),))
