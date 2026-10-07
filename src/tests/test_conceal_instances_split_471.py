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
- `TestHiddenOnlyExistingServiceReachable`：hidden-only 既有服務全可達，但仍不上架、
  不可新建 —— 這組是 #471 的 RED。
- `TestConcealTemplateOperationsBlocked`：conceal（HA PaaS）操作端點照擋，含 C 組
  這次補上的 reset-admin-password —— reset 的 gate 缺失是 #471 順帶收斂項。

⚠ hidden-only 那組**刻意用合成 fixture、不綁任何 seed 商品**（#494）：原本綁的是
當時唯一的 hidden-only 商品 hermes-agent，它一定價上架（is_hidden 翻 False）整組
B 組斷言就集體變紅——受測的是「hidden-only 語意」，卻被某個商品的**商業狀態**綁架。
seed 側的對應守線各自留在該商品的 seed 測試檔（hermes ＝
test_hermes_agent_seed_424.test_conceal_instances_untouched）。

HA PaaS 既有隱藏鏈（workspace／dashboard／billing 全鏈）的零回歸由
`test_hidden_template_chain.py` 承擔，本檔不重複。
"""
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.exceptions import ValidationError
from ..models.cloud_service import _internal_provision

_NOT_FOUND = 'Service not found'


def make_hidden_only_template(env, slug='hidden-only-fixture-471'):
    """建一個「只關曝光層」的合成 template（is_hidden ＋ 不 conceal）。

    has_admin_gui=True 是刻意的——reset-admin-password 的 gate 只有在支援 admin GUI
    的 template 上才觀測得到（否則會先撞「不支援」而看不到隱藏語意本身）。
    """
    return env['woow_paas_platform.cloud_app_template'].sudo().create({
        'name': 'Hidden Only Fixture 471',
        'slug': slug,
        # helm_* 三欄 required=True（NOT NULL），不給會撞 NotNullViolation。
        'helm_repo_url': 'oci://example.invalid',
        'helm_chart_name': 'hidden-only-fixture',
        'helm_chart_version': '0.0.0',
        'is_active': True,
        'is_hidden': True,
        'conceal_instances': False,
        'has_admin_gui': True,
        'admin_password_carrier': 'helm_secret',
    })


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

    def test_hermes_agent_unlocked_without_touching_instance_layer(self):
        """hermes-agent＝#494 定價拍板後兩層都開（解封只翻曝光層）。

        它曾是本 repo 唯一的 hidden-only 商品（#424① 定價未拍板期間）。解封時
        **只翻 is_hidden**、conceal_instances 自始不設——這條鎖住「別在解封或
        再次下架時順手動實例層」：設了會讓既有租戶服務詳情頁打不開、rollback／
        change-plan／MCP 設定全不可達（#464 在本 template 實測過，stg 刻意留測的
        svc60 就是這樣整個不可達的）。
        """
        tpl = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertFalse(tpl.is_hidden, '#494 已定價上架')
        self.assertFalse(
            tpl.conceal_instances,
            '實例層自始不該設——它與上不上架正交')

    def test_visible_template_conceals_nothing(self):
        """正常販售的 template 兩層都開。"""
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertFalse(n8n.is_hidden)
        self.assertFalse(n8n.conceal_instances)

    def test_seed_inventory_is_exactly_ha_paas(self):
        """盤點鎖定：seed 的隱藏 template 恰為 HA PaaS，且它是 conceal 型。

        新增隱藏 template 時這條會紅——那正是要停下來決定它屬哪一類的時刻。
        （#494 前這裡還有 hermes-agent；它定價上架後 seed 已無 hidden-only 商品，
        故該語意的行為矩陣改用合成 fixture，見 make_hidden_only_template。）
        """
        Tpl = self.env['woow_paas_platform.cloud_app_template'].sudo()
        hidden = Tpl.with_context(active_test=False).search([('is_hidden', '=', True)])
        # ha-full-apporo：hidden-only（曝光層）——APPORO 品牌的 ha-full。
        # 2026-09-25 已連同 is_active=False 一起封存（APPORO 展示機不走 PaaS），但仍
        # 留在本盤點裡：is_active 是普通 Boolean、不是 Odoo 的 magic `active`，所以
        # 上面的 search 本來就撈得到它（`active_test=False` 在此為無效參數，留著是
        # 為了與其他盤點測試同形）。封存守線在 test_ha_full_apporo_seed.py。
        # （pi-agent 2026-09-24 已上架＝不再 hidden，image 釘 20260924.0235@sha256。）
        # （omnigent 2026-09-29 以隱藏卡片進 seed，2026-09-30 隨 .188 公開上架＝不再 hidden。）
        # （code-server 2026-09-30 image 釘上 20260930.1503@sha256 後公開上架＝不再 hidden。）
        # （emqx／nextcloud 2026-10-02 #1321 以隱藏卡片進 seed，10-02 image 鏡像與 chart 0.1.1
        #  落地後公開上架＝不再 hidden；當時只翻 is_hidden，實例層從未設。）
        # （headscale／tailscale／litellm 2026-10-04 #1413 以隱藏卡片進 seed、隨 .206 上正式站，
        #  image 鏡像核可、chart 0.1.0 發佈與整合實測通過後同日公開上架＝不再 hidden；
        #  當時只翻 is_hidden，實例層從未設。）
        # （mcp-litellm 2026-10-05 #1487 以隱藏卡片進 seed、隨 .210 上正式站，沙盒實測（woow-mcp-server
        #  #33）通過後 2026-10-07 公開上架＝不再 hidden；只翻 is_hidden，實例層從未設。）
        # mcp-odoo-manage：hidden-only（曝光層）——2026-10-06 #1507 下架（Odoo MCP 只保留
        # mcp-odoo），只翻 is_hidden、實例層不設；重新上架時改回 False 並把 slug 從這裡拿掉。
        self.assertEqual(
            set(hidden.mapped('slug')),
            {'ha-paas', 'ha-full-apporo', 'mcp-odoo-manage'},
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

    def _template(self):
        """受測 template。預設取 seed xmlid；hidden-only 組覆寫成合成 fixture。"""
        return self.env.ref(self.template_xmlid)

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.partner = self.env.ref('base.partner_admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self._template()
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 471 %s' % self.tpl.slug, 'owner_id': self.admin_user.id})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-471', 'reference_id': 'test-471-%s' % self.tpl.slug,
            'state': 'running', 'subdomain': 'svc-471-%s' % self.tpl.slug,
            'provision_partner_id': self.partner.id})

    def _result(self, url, params='{}'):
        """打 JSON-RPC 端點——鍵一律住在 ``params`` 裡（＝前端的形狀）。"""
        body = '{"jsonrpc":"2.0","method":"call","params":' + params + '}'
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    # check-addon-removal 的 gate 在 `if not removed_addon_ids` 的短路**之後**，
    # 所以要抵達它就必須送非空的 id。刻意用一個不存在的 id：conceal 型會停在
    # gate，hidden-only 型則往下走到 `CloudOdooAddon.browse(...).exists()` 為空
    # 就返回——不會真的打 operator，測試無網路副作用。
    #
    # 這裡曾經是 #474 的重現點：端點原本讀 ``request.get_json_data()``（＝整個
    # request body），這個鍵得塞在 envelope **頂層**才到得了 gate。#474 修好後
    # 已回歸正規 params 路徑；若哪天又有人把端點改回讀 body，本檔這兩條會紅。
    _ADDON_REMOVAL_PARAMS = '{"removed_addon_ids":[999999999]}'

    # ── 語意斷言的兩個方向 ────────────────────────────────────────────────
    def _assert_gate_open(self, url, params='{}', label=''):
        """端點沒被隱藏語意擋住。

        刻意**不**斷言 success=True：這些端點的隱藏 gate 之後還有訂閱閘、狀態閘、
        operator 呼叫等獨立關卡，在無 operator／無訂閱的測試環境本來就會回別的錯。
        受測面向只有一個——回的**不是** hidden gate 的 'Service not found'。
        """
        r = self._result(url, params)
        self.assertNotEqual(
            r.get('error'), _NOT_FOUND,
            '%s 不該被隱藏語意擋住（回 %r）' % (label or url, r))

    def _assert_gate_closed(self, url, params='{}', label=''):
        r = self._result(url, params)
        self.assertFalse(r.get('success'), '%s 應被擋（回 %r）' % (label or url, r))
        self.assertEqual(
            r.get('error'), _NOT_FOUND,
            '%s 應回 hidden gate 的 not found、而非其他關卡的錯（回 %r）'
            % (label or url, r))


@tagged('post_install', '-at_install')
class TestHiddenOnlyExistingServiceReachable(_ServiceMatrixCase):
    """hidden-only：既有服務全可達，但仍不上架、不可新建。

    這組是 #471 的 RED —— 修前 8 個操作端點 ＋ 清單全部回 'Service not found'。

    fixture 是**合成** template（原本綁 hermes-agent，#494 定價上架後 seed 已無
    hidden-only 商品）：受測的是語意，不該隨任何商品的上架與否而生滅。
    """

    def _template(self):
        return make_hidden_only_template(self.env)

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
            self._ADDON_REMOVAL_PARAMS, 'check-addon-removal')

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
            self._ADDON_REMOVAL_PARAMS, 'check-addon-removal')

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
