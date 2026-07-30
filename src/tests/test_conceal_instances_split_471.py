"""#471：`is_hidden` 拆兩語意的行為矩陣（曝光層 vs 實例隱藏層）。

#336 讓一個旗標同時承載兩件事——「不上架」與「既有實例整條鏈消失」。對 HA PaaS
（租戶入口是 App、不是 /woow）恰好無害，套到「暫時下架的一般商品」就變成破壞性的：
租戶手上 state=running 的服務詳情頁打不開、rollback／change-plan／MCP 設定全不可達
（#464 實測 hermes-agent，見 docs/tracking/test-reports/464-*.md §2.1）。

拍板（#471 comment 3636，Eugene 2026-07-30）走方向 (b)：

| 語意 | 欄位 | 擋什麼 |
| --- | --- | --- |
| 曝光層 | `is_hidden` | marketplace 不列、template 詳情/plans 不可讀、generic create 拒收 |
| 實例層 | `conceal_instances` | 既有實例的讀取/操作端點全擋、HiddenChainMixin 全鏈過濾 |

本檔守**兩語意各自的行為矩陣**——單測任一欄位都無法證明拆分成立，必須成對比對：

- `TestConcealInstancesSeed`：seed 盤點＋「conceal ⟹ hidden」蘊含式。
- `TestHiddenOnlyExistingServiceReachable`：hidden-only（hermes-agent）既有服務全可達，
  但仍不上架、不可新建 —— 這組是 #471 的 RED。
- `TestConcealTemplateOperationsBlocked`：conceal（HA PaaS）操作端點照擋，含 C 組
  這次補上的 reset-admin-password —— reset 的 gate 缺失是 #471 順帶收斂項。

HA PaaS 既有隱藏鏈（workspace／dashboard／billing 全鏈）的零回歸由
`test_hidden_template_chain.py` 承擔，本檔不重複。
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.exceptions import ValidationError

_NOT_FOUND = 'Service not found'


class TestConcealInstancesSeed(TransactionCase):
    """seed 盤點：哪些 template 隱藏、哪些連既有實例一起隱藏。"""

    def test_ha_paas_conceals_instances(self):
        """HA PaaS＝兩層都關（#336 原始意圖：租戶入口是 App，不是 /woow）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.assertTrue(tpl.is_hidden, 'HA PaaS 不上架')
        self.assertTrue(
            tpl.conceal_instances,
            'HA PaaS 既有實例必須全鏈隱藏＋操作端點照擋（#336 設計意圖，'
            '拆語意後這條由 conceal_instances 承載）')

    def test_hermes_agent_hidden_only(self):
        """hermes-agent＝只關曝光層（#424 定價未拍板，但既有服務照用）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertTrue(tpl.is_hidden, '定價未拍板前不上架')
        self.assertFalse(
            tpl.conceal_instances,
            'hermes-agent 只是「未上架」，既有租戶服務必須照常可讀可操作'
            '（#471：stg 刻意留測的 svc60 曾因此整個不可達）')

    def test_visible_template_conceals_nothing(self):
        """正常販售的 template 兩層都開。"""
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertFalse(n8n.is_hidden)
        self.assertFalse(n8n.conceal_instances)

    def test_seed_inventory_is_exactly_these_two(self):
        """盤點鎖定：隱藏 template 恰為 HA PaaS ＋ hermes-agent，且只有前者 conceal。

        新增隱藏 template 時這條會紅——那正是要停下來決定它屬哪一類的時刻。
        """
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        hidden = Tpl.with_context(active_test=False).search([('is_hidden', '=', True)])
        self.assertEqual(
            set(hidden.mapped('slug')), {'ha-paas', 'hermes-agent'},
            '隱藏 template 盤點漂移；新增的請先判定屬曝光層還是實例層')
        concealed = hidden.filtered('conceal_instances')
        self.assertEqual(
            set(concealed.mapped('slug')), {'ha-paas'},
            '只有 HA PaaS 型（租戶不從 /woow 進）才 conceal 既有實例')

    def test_conceal_implies_hidden(self):
        """蘊含式守衛：conceal_instances=True 必須 is_hidden=True。

        反例（conceal 但仍上架）語意矛盾——marketplace 賣得到、買完立刻從前台消失。
        model 層 @api.constrains fail-loud，不留給 review 抓。
        """
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        with self.assertRaises(ValidationError):
            Tpl.create({
                'name': 'Conceal Without Hidden 471',
                'slug': 'conceal-no-hidden-471',
                # helm_* 三欄 required=True（NOT NULL）——不給會先撞
                # NotNullViolation，@api.constrains 根本跑不到，測試就變成在測
                # DB schema 而不是測蘊含式。
                'helm_repo_url': 'oci://example.invalid',
                'helm_chart_name': 'conceal-test',
                'helm_chart_version': '0.0.0',
                'is_hidden': False,
                'conceal_instances': True,
            })

    def test_flipping_hidden_off_while_concealed_is_rejected(self):
        """蘊含式在 write() 路徑一樣成立（解封只翻 is_hidden 會留下矛盾狀態）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        with self.assertRaises(ValidationError):
            tpl.sudo().write({'is_hidden': False})


class _ServiceMatrixCase(HttpCase):
    """共用 fixture：admin session ＋ 自己名下一個 state=running 的服務。

    刻意讓 owner 就是登入者本人——證明擋/不擋是語意判準，不是權限不足的副作用。
    """

    template_xmlid = None

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.partner = self.env.ref('base.partner_admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self.env.ref(self.template_xmlid)
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 471 %s' % self.tpl.slug, 'owner_id': self.admin_user.id})
        self.svc = self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-471', 'reference_id': 'test-471-%s' % self.tpl.slug,
            'state': 'running', 'subdomain': 'svc-471-%s' % self.tpl.slug,
            'provision_partner_id': self.partner.id})

    def _result(self, url, params='{}', top_level=''):
        """打 JSON-RPC 端點。

        ``top_level``：額外塞在 **envelope 頂層**（與 ``params`` 同層）的鍵。只有
        ``check-addon-removal`` 需要——見 `_addon_removal_body` 的說明。
        """
        body = ('{"jsonrpc":"2.0","method":"call","params":' + params
                + (',' + top_level if top_level else '') + '}')
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    # ⚠️ 既有缺陷（非本 issue scope，僅在測試中繞過）：
    # `api_check_addon_removal` 讀 ``request.get_json_data()``——那回的是**整個
    # request body**（odoo/http.py:1810 `json.loads(httprequest.get_data())`），不是
    # JSON-RPC 的 ``params``。而前端 `cloud_service.js:500` 走標準 envelope
    # （`{jsonrpc, method, params:{removed_addon_ids}}`），於是端點永遠讀到
    # ``removed_addon_ids=None`` → 命中 `if not removed_addon_ids` 提早回
    # ``{'success': True, 'modules': {}}``，**隱藏 gate 與後續檢查全都跑不到**。
    # 本檔要守的是 gate 判準，故刻意把鍵塞在頂層讓它抵達 gate；那個 param
    # plumbing bug 另案處理（修它會改變端點對前端的行為，超出 #471 拍板範圍）。
    _ADDON_REMOVAL_TOP = '"removed_addon_ids":[1]'

    # ── 語意斷言的兩個方向 ────────────────────────────────────────────────
    def _assert_gate_open(self, url, params='{}', label='', top_level=''):
        """端點沒被隱藏語意擋住。

        刻意**不**斷言 success=True：這些端點的隱藏 gate 之後還有訂閱閘、狀態閘、
        operator 呼叫等獨立關卡，在無 operator／無訂閱的測試環境本來就會回別的錯。
        受測面向只有一個——回的**不是** hidden gate 的 'Service not found'。
        """
        r = self._result(url, params, top_level)
        self.assertNotEqual(
            r.get('error'), _NOT_FOUND,
            '%s 不該被隱藏語意擋住（回 %r）' % (label or url, r))

    def _assert_gate_closed(self, url, params='{}', label='', top_level=''):
        r = self._result(url, params, top_level)
        self.assertFalse(r.get('success'), '%s 應被擋（回 %r）' % (label or url, r))
        self.assertEqual(
            r.get('error'), _NOT_FOUND,
            '%s 應回 hidden gate 的 not found、而非其他關卡的錯（回 %r）'
            % (label or url, r))


@tagged('post_install', '-at_install')
class TestHiddenOnlyExistingServiceReachable(_ServiceMatrixCase):
    """hidden-only（hermes-agent）：既有服務全可達，但仍不上架、不可新建。

    這組是 #471 的 RED —— 修前 8 個操作端點 ＋ 清單全部回 'Service not found'。
    """

    template_xmlid = 'woow_paas_platform.cloud_app_hermes_agent'

    # ── A 組：既有服務的讀取／操作，全部應可達 ──────────────────────────
    def test_service_detail_reachable(self):
        r = self._result('/api/workspaces/%s/services/%s' % (self.ws.id, self.svc.id))
        self.assertTrue(r.get('success'), '既有服務詳情頁必須打得開（回 %r）' % r)
        self.assertEqual(r['data']['id'], self.svc.id)

    # 刻意**不**單獨測 action='update' / 'delete'：三個 action 共用同一道 gate
    # （`api_workspace_service` 進 action 分派**之前**那一個 if），`test_service_detail_
    # reachable` 已完整覆蓋。而 update 會往下走到真的 `helm upgrade`——實測在沙盒裡
    # 對不存在的 release 呼叫 operator 會把 HttpCase 共用的測試 transaction 打壞
    # （savepoint 消失 → 之後 3 條測試全部 InFailedSqlTransaction 連坐）。gate 測試
    # 不該觸發真實部署副作用。

    def test_service_rollback_reachable(self):
        self._assert_gate_open(
            '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, self.svc.id),
            '{"revision":1}', 'rollback')

    def test_service_revisions_reachable(self):
        self._assert_gate_open(
            '/api/workspaces/%s/services/%s/revisions' % (self.ws.id, self.svc.id),
            '{}', 'revisions')

    def test_service_change_plan_reachable(self):
        self._assert_gate_open(
            '/api/cloud-services/services/%s/change-plan' % self.svc.id,
            '{"plan_id":1}', 'change-plan')

    def test_service_skip_trial_estimate_reachable(self):
        self._assert_gate_open(
            '/api/cloud-services/services/%s/skip-trial-estimate' % self.svc.id,
            '{}', 'skip-trial-estimate')

    def test_service_skip_trial_reachable(self):
        self._assert_gate_open(
            '/api/cloud-services/services/%s/skip-trial' % self.svc.id,
            '{}', 'skip-trial')

    def test_check_addon_removal_reachable(self):
        self._assert_gate_open(
            '/api/cloud-services/services/%s/check-addon-removal' % self.svc.id,
            '{}', 'check-addon-removal', top_level=self._ADDON_REMOVAL_TOP)

    def test_service_mcp_servers_reachable(self):
        self._assert_gate_open(
            '/api/workspaces/%s/services/%s/mcp-servers' % (self.ws.id, self.svc.id),
            '{"action":"list"}', 'mcp-servers')

    def test_service_reset_admin_password_reachable(self):
        """C 組一致性：hermes-agent 是 has_admin_gui=True，reset 不得被隱藏語意擋。

        本條在修前即綠（reset 原本沒有任何 hidden gate ＝ #471 的不一致點）。留著
        是為了鎖住「補 gate 時不要順手補成 is_hidden」——那會讓 #460／#464 的
        Reset 按鈕修復對 hermes 租戶再次不可達。
        """
        self._assert_gate_open(
            '/api/workspaces/%s/services/%s/reset-admin-password'
            % (self.ws.id, self.svc.id), '{}', 'reset-admin-password')

    def test_workspace_service_list_includes(self):
        r = self._result('/api/workspaces/%s/services' % self.ws.id, '{"action":"list"}')
        self.assertTrue(r.get('success'), r)
        self.assertIn(
            self.svc.id, [s['id'] for s in r['data']],
            'hidden-only template 的既有服務必須留在 workspace 清單')

    # ── B 組：曝光面仍須關著（拆語意不得放寬上架閘）────────────────────
    def test_marketplace_still_excludes(self):
        r = self._result('/api/cloud/templates')
        self.assertTrue(r.get('success'), r)
        self.assertNotIn(
            self.tpl.id, [t['id'] for t in r['data']],
            'is_hidden 仍須擋 marketplace 曝光')

    def test_template_detail_still_not_found(self):
        r = self._result('/api/cloud/templates/%s' % self.tpl.id)
        self.assertFalse(r.get('success'), 'hidden template 詳情仍不可讀')

    def test_template_plans_still_not_found(self):
        r = self._result('/api/cloud-services/templates/%s/plans' % self.tpl.id)
        self.assertFalse(r.get('success'), 'hidden template plans 仍不可讀')

    def test_generic_create_still_rejected(self):
        r = self._result(
            '/api/workspaces/%s/services' % self.ws.id,
            '{"action":"create","template_id":%s,"name":"x","plan_id":1}' % self.tpl.id)
        self.assertFalse(
            r.get('success'), 'hidden template 仍不得經 generic create 新建')


@tagged('post_install', '-at_install')
class TestConcealTemplateOperationsBlocked(_ServiceMatrixCase):
    """conceal（HA PaaS）：既有實例的操作端點照擋——現行行為零回歸。

    `test_service_reset_admin_password_blocked` 是 C 組的 RED：修前 reset 沒有任何
    hidden gate，於是「API 重設得了密碼、唯一 UI 入口（詳情頁）卻打不開」。
    """

    template_xmlid = 'woow_paas_platform.cloud_app_ha_paas'

    def test_service_detail_blocked(self):
        self._assert_gate_closed(
            '/api/workspaces/%s/services/%s' % (self.ws.id, self.svc.id),
            '{}', 'detail')

    def test_service_rollback_blocked(self):
        self._assert_gate_closed(
            '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, self.svc.id),
            '{"revision":1}', 'rollback')

    def test_service_revisions_blocked(self):
        self._assert_gate_closed(
            '/api/workspaces/%s/services/%s/revisions' % (self.ws.id, self.svc.id),
            '{}', 'revisions')

    def test_service_change_plan_blocked(self):
        self._assert_gate_closed(
            '/api/cloud-services/services/%s/change-plan' % self.svc.id,
            '{"plan_id":1}', 'change-plan')

    def test_service_skip_trial_estimate_blocked(self):
        self._assert_gate_closed(
            '/api/cloud-services/services/%s/skip-trial-estimate' % self.svc.id,
            '{}', 'skip-trial-estimate')

    def test_service_skip_trial_blocked(self):
        self._assert_gate_closed(
            '/api/cloud-services/services/%s/skip-trial' % self.svc.id,
            '{}', 'skip-trial')

    def test_check_addon_removal_blocked(self):
        self._assert_gate_closed(
            '/api/cloud-services/services/%s/check-addon-removal' % self.svc.id,
            '{}', 'check-addon-removal', top_level=self._ADDON_REMOVAL_TOP)

    def test_service_mcp_servers_blocked(self):
        self._assert_gate_closed(
            '/api/workspaces/%s/services/%s/mcp-servers' % (self.ws.id, self.svc.id),
            '{"action":"list"}', 'mcp-servers')

    def test_service_reset_admin_password_blocked(self):
        """C 組 RED：reset 的 gate 必須與其他操作端點同判準（conceal_instances）。

        HA PaaS 是 has_admin_gui=False，故修前這裡回的是 admin-GUI 不支援的錯；
        gate 補在 exists() 之後、has_admin_gui 之前，才能觀測到語意本身。
        """
        self._assert_gate_closed(
            '/api/workspaces/%s/services/%s/reset-admin-password'
            % (self.ws.id, self.svc.id), '{}', 'reset-admin-password')

    def test_workspace_service_list_excludes(self):
        r = self._result('/api/workspaces/%s/services' % self.ws.id, '{"action":"list"}')
        self.assertTrue(r.get('success'), r)
        self.assertNotIn(
            self.svc.id, [s['id'] for s in r['data']],
            'conceal template 的服務不得列在 workspace 清單')
