# -*- coding: utf-8 -*-
"""#525 B4：`_update_project_mcp_server` 吞掉 `ValidationError` → 欄位其實已改，
API 卻說失敗。

`try` 內唯一的寫入是 `server.write(vals)`。`ValidationError` 被
`except ValidationError` 吃掉、以 HTTP 200 + `success: False` 返回 → 不傳播到
框架層 → request 正常結束 → **transaction 照常 commit** → 白名單六欄的新值留了
下來：**MCP server 實際已被改名／改 URL**，而 API 說失敗。

同一支功能的另一個入口 `_update_mcp_server`（service-centric，`paas.py`）**沒有**
包 try，例外一路傳到 JSON-RPC dispatcher，框架不 commit ⇒ 已寫的值不落地。
兩支對同一個 model 例外的行為不一致，本檔把「一致」釘成斷言。

## 可達性（重要，先讀）

**今天的 constrains 與 controller 前置驗證下，這條路徑不是可重現的線上事故**：

- 白名單六欄（name／url／transport／api_key／headers_json／description）中，能觸發
  `@api.constrains` 的只有 `url`。
- `_check_scope_project_consistency`（`mcp_server.py:119`）的 constrains 欄位是
  `scope`／`project_id`，兩者都不在白名單 ⇒ 永不觸發。
- `_check_sidecar_url_in_cluster`（`mcp_server.py:152`）與 controller 的
  `_validate_mcp_url`（`paas.py:114`）**共用同一個 `_is_in_cluster_url`**
  （`mcp_server.py:135` 註明 single source of truth）⇒ 通得過 controller 就一定
  通得過 constrains，兩層不會漂移。
- `mcp_server` 全 repo 沒有 `_inherit` 擴充 ⇒ 沒有外掛 constrains。
- 前後空白也不是縫：controller 拿 `.strip()` 過的字串驗、`write` 寫未 strip 的
  原值，但 Python 的 `urlsplit` 自己會剝掉前後空白，兩層判定仍然一致。

本檔用 patch 在「值已寫入 → `_validate_fields` 才跑」的視窗注入例外來取得可達的
RED——**這是模擬一個今天還不存在的 constrains**，不是重現今天就會發生的事故。
結構缺陷成立且與 #520／A1／A3 同構，白名單一放寬或 constrains 一收緊，殘留立即可達。

**刻意沒有做的事**：不放寬白名單、不拆掉 `_validate_mcp_url` 的前置驗證。那是拿
放寬安全設定去換一個綠燈，換到的紅燈也不是真的。

## 為什麼注入點要挑在 `_validate_fields`，而且要帶欄位過濾

`_validate_fields` 迭代的 `self._constraint_methods` 存的是 registry setup 當下抓下
來的 function 物件，patch `_check_sidecar_url_in_cluster` 這個類別屬性**不會生效**
（測試會靜默地什麼都沒注入）。掛在 `_validate_fields` 本身才是走一般屬性查找、
patch 得到的位置，而它正是「值已寫進 towrite buffer」之後的第一站：`models.py:4829`
`real_recs._validate_fields(vals, inverse_fields)`，緊接在 :4807 `field.write(...)` 之後。

注入帶欄位過濾（只在 `name` 被寫入時才拋），忠實模仿真 `@api.constrains` 的欄位
相交行為，同時避免 request 尾端 computed 欄位重算時二次觸發——那時已經離開
controller 的 try，例外會逸出到框架，殘留歸零、結局變成框架層錯誤（那正是
`test_injection_fires_after_the_new_value_is_written` 這條前提斷言要擋住的假綠）。

## 測試結構

「沒有殘留」的斷言配一條把**前提**釘成斷言的測試：
`test_injection_fires_after_the_new_value_is_written` 證明注入觸發時新值已經寫進
記錄。少了它，`server.write` 一被重排，殘留不再產生，「沒有殘留」就全部退化成恆真。

`test_legacy_service_update_leaves_no_residue` 是**對照組實測**：它證明「不包 try、
讓框架處理」這條路徑真的不留殘留。這是本修法的前提，不照抄 issue 的論證。
"""
import json
import uuid

from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.mcp_server import McpServer

_ORIGINAL_NAME = 'B4 Original MCP Name'
_NEW_NAME = 'B4 Renamed By Update'
_INJECT_MESSAGE = 'injected: post-write validation failed'


@tagged('post_install', '-at_install')
class TestUpdateProjectMcpServerResidue(HttpCase):
    """update 失敗時不得把新值留在 DB，且兩個入口行為必須一致。"""

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')

        self.alice = self.env['res.users'].sudo().create({
            'name': 'B4 Alice',
            'login': 'b4_alice',
            'password': 'b4_alice',
            'email': 'b4-alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'B4 Workspace',
            'owner_id': self.alice.id,
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'B4 Template',
            'slug': f'b4-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'b4-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'B4 Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'b4-svc',
            'helm_namespace': 'paas-ws-b4-svc',
        })
        self.project = self.env['project.project'].sudo().create({
            'name': 'B4 Project',
            'cloud_service_id': self.service.id,
        })
        self.server = self.env['woow_paas_platform.mcp_server'].sudo().create({
            'name': _ORIGINAL_NAME,
            'url': 'http://b4-svc-mcp.paas-ws-b4-svc.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
        })
        self.authenticate('b4_alice', 'b4_alice')

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _post(self, url, params):
        """回 `(result, error)`——**兩種 JSON-RPC 結局都要能觀察**。

        修法會把失敗的結局從 `result={'success': False}` 換成框架層 `error`，
        helper 若像其他測試檔那樣硬 assert `'result' in parsed`，修好之後會死在
        helper 裡、而不是死在被測的斷言上。
        """
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
        self.env.invalidate_all()
        return parsed.get('result'), parsed.get('error')

    def _project_update(self, **params):
        """project-centric 入口——本單的被測對象。"""
        return self._post(
            '/api/support-projects/%s/mcp-servers' % self.project.id,
            dict(action='update', server_id=self.server.id, **params))

    def _service_update(self, **params):
        """service-centric 入口——「不包 try」的對照組。"""
        return self._post(
            '/api/workspaces/%s/services/%s/mcp-servers'
            % (self.workspace.id, self.service.id),
            dict(action='update', server_id=self.server.id, **params))

    # ── 探針：DB 裡現在到底叫什麼名字 ─────────────────────────────────
    def _stored_name(self):
        self.env.invalidate_all()
        return self.server.sudo().read(['name'])[0]['name']

    # ── 注入：值已寫入之後、`_validate_fields` 當下拋例外 ──────────────
    def _fail_post_write_validation(self, recorder=None):
        original = McpServer._validate_fields

        def validate_then_boom(model_self, field_names, excluded_names=()):
            # 先物化：傳進來的可能是 generator，餵給 original 之後就空了。
            names = set(field_names)
            original(model_self, names, excluded_names)
            # 模仿 `@api.constrains` 的欄位相交過濾（見檔頭）：少了這一步，
            # request 尾端 computed 欄位的重算會二次觸發，例外就跑到 controller
            # 的 try 之外去了。
            if 'name' not in names:
                return
            if recorder is not None:
                recorder.append(model_self.read(['name'])[0]['name'])
            raise ValidationError(_INJECT_MESSAGE)

        return patch.object(McpServer, '_validate_fields', validate_then_boom)

    # ================================================================
    #  陽性對照：兩個入口在成功時，探針都看得見新值
    # ================================================================
    def test_positive_control_project_update_persists(self):
        self.assertEqual(self._stored_name(), _ORIGINAL_NAME,
                         'fixture 前提：一開始是原名')

        result, error = self._project_update(name=_NEW_NAME)

        self.assertIsNone(error, '成功路徑不該有框架層錯誤：%r' % (error,))
        self.assertTrue(result and result.get('success'),
                        'project-centric update 應成功（回 %r）' % (result,))
        self.assertEqual(
            self._stored_name(), _NEW_NAME,
            '成功 update 後 DB 必須是新名字——探針有鑑別力的證明')

    def test_positive_control_service_update_persists(self):
        result, error = self._service_update(name=_NEW_NAME)

        self.assertIsNone(error, '成功路徑不該有框架層錯誤：%r' % (error,))
        self.assertTrue(result and result.get('success'),
                        'service-centric update 應成功（回 %r）' % (result,))
        self.assertEqual(
            self._stored_name(), _NEW_NAME,
            '對照組的探針也要有鑑別力，否則下面那條「無殘留」是恆真')

    # ================================================================
    #  前提：注入確實發生在新值寫入之後
    # ================================================================
    def test_injection_fires_after_the_new_value_is_written(self):
        """注入點若跑到寫入之前，殘留根本不會產生，下面兩條「沒有殘留」
        就退化成恆真——而假綠與真綠的輸出一模一樣。"""
        seen = []
        with self._fail_post_write_validation(recorder=seen):
            self._project_update(name=_NEW_NAME)

        self.assertEqual(
            len(seen), 1,
            '`_validate_fields` 必須因 name 被寫而觸發恰一次——'
            '0 次代表注入根本沒生效，多次代表欄位過濾沒擋住重算的二次觸發')
        self.assertEqual(
            seen[0], _NEW_NAME,
            '注入觸發時記錄上必須已經是新值（＝寫入已發生），'
            '「沒有殘留」的斷言才有守到東西；實得 %r' % (seen[0],))

    # ================================================================
    #  對照組實測：不包 try 的那支真的不留殘留（本修法的前提）
    # ================================================================
    def test_legacy_service_update_leaves_no_residue(self):
        with self._fail_post_write_validation():
            result, error = self._service_update(name=_NEW_NAME)

        self.assertFalse(
            result and result.get('success'),
            'service-centric update 在注入下不得回報成功（回 %r）' % (result,))
        self.assertEqual(
            self._stored_name(), _ORIGINAL_NAME,
            '不包 try ⇒ 例外傳到 dispatcher ⇒ 框架不 commit ⇒ 新值不得落地。'
            '這條若紅，代表「拿掉 try 就能靠框架回捲」這個前提不成立，'
            '修法必須改用 savepoint。')

    # ================================================================
    #  主斷言：project-centric 那支不得留下殘留
    # ================================================================
    def test_project_update_leaves_no_residue(self):
        with self._fail_post_write_validation():
            result, error = self._project_update(name=_NEW_NAME)

        self.assertFalse(
            result and result.get('success'),
            'project-centric update 在注入下不得回報成功（回 %r）' % (result,))
        self.assertEqual(
            self._stored_name(), _ORIGINAL_NAME,
            'update 回報失敗時，新值不得留在 DB——'
            '否則使用者看到「更新失敗」但 MCP server 已經被改名')

    # ================================================================
    #  create 這組：同款結構，同樣要對齊對照組
    # ================================================================
    #  只改 update 會把「update 包／不包不一致」換成「create 包、update
    #  不包」的新不一致——那是我們自己造成的。對照組 `_create_mcp_server`
    #  的 `McpServer.create(...)` 同樣裸露、沒有 try。
    # ----------------------------------------------------------------
    def _project_create(self, name):
        return self._post(
            '/api/support-projects/%s/mcp-servers' % self.project.id,
            {'action': 'create', 'name': name,
             'url': 'http://b4-new-mcp.paas-ws-b4-svc.svc.cluster.local:3001/mcp',
             'transport': 'streamable_http'})

    def _service_create(self, name):
        return self._post(
            '/api/workspaces/%s/services/%s/mcp-servers'
            % (self.workspace.id, self.service.id),
            {'action': 'create', 'name': name,
             'url': 'http://b4-new-mcp.paas-ws-b4-svc.svc.cluster.local:3001/mcp',
             'transport': 'streamable_http'})

    def _named_server_count(self, name):
        self.env.invalidate_all()
        return self.env['woow_paas_platform.mcp_server'].sudo().search_count(
            [('name', '=', name)])

    def test_positive_control_project_create_persists(self):
        result, error = self._project_create('B4 Created OK')

        self.assertIsNone(error, '成功路徑不該有框架層錯誤：%r' % (error,))
        self.assertTrue(result and result.get('success'),
                        'project-centric create 應成功（回 %r）' % (result,))
        self.assertEqual(
            self._named_server_count('B4 Created OK'), 1,
            '成功 create 後記錄必須在——探針有鑑別力的證明')

    def test_create_injection_fires_after_the_row_is_inserted(self):
        """注入點若跑到 INSERT 之前，孤兒記錄根本不會產生，下面兩條「沒有殘留」
        就退化成恆真。"""
        seen = []
        with self._fail_post_write_validation(recorder=seen):
            self._project_create('B4 Proj Create Probe')

        self.assertEqual(
            len(seen), 1,
            '`_validate_fields` 必須因 name 被寫而觸發恰一次——'
            '0 次代表注入根本沒生效')
        self.assertEqual(
            seen[0], 'B4 Proj Create Probe',
            '注入觸發時記錄必須已經可讀（＝INSERT 已下），'
            '「沒有殘留」的斷言才有守到東西；實得 %r' % (seen[0],))

    def test_legacy_service_create_leaves_no_residue(self):
        """對照組實測：不包 try 的那支真的不留殘留。"""
        with self._fail_post_write_validation():
            result, error = self._service_create('B4 Svc Create Boom')

        self.assertFalse(
            result and result.get('success'),
            'service-centric create 在注入下不得回報成功（回 %r）' % (result,))
        self.assertEqual(
            self._named_server_count('B4 Svc Create Boom'), 0,
            '不包 try ⇒ 例外傳到 dispatcher ⇒ 框架不 commit ⇒ 記錄不得落地。'
            '這條若紅，代表「拿掉 try 就能靠框架回捲」的前提不成立。')

    def test_project_create_leaves_no_residue(self):
        with self._fail_post_write_validation():
            result, error = self._project_create('B4 Proj Create Boom')

        self.assertFalse(
            result and result.get('success'),
            'project-centric create 在注入下不得回報成功（回 %r）' % (result,))
        self.assertEqual(
            self._named_server_count('B4 Proj Create Boom'), 0,
            'create 回報失敗時不得留下孤兒記錄——'
            '否則使用者看到「建立失敗」但清單裡多了一筆')

    # ================================================================
    #  一致性：兩個入口對同一個 model 例外的可觀察行為必須相同
    # ================================================================
    def test_both_entry_points_agree_on_failure(self):
        with self._fail_post_write_validation():
            project_result, project_error = self._project_update(name=_NEW_NAME)
        project_name_after = self._stored_name()

        self.server.sudo().write({'name': _ORIGINAL_NAME})
        self.env.invalidate_all()

        with self._fail_post_write_validation():
            service_result, service_error = self._service_update(name=_NEW_NAME)
        service_name_after = self._stored_name()

        self.assertEqual(
            project_name_after, service_name_after,
            '同一個 model 例外、同一支功能的兩個入口，殘留與否必須一致'
            '（project 入口後為 %r、service 入口後為 %r）'
            % (project_name_after, service_name_after))
        self.assertEqual(
            bool(project_result and project_result.get('success')),
            bool(service_result and service_result.get('success')),
            '兩個入口回報成敗的方式也必須一致')
