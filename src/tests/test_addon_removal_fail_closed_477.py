# -*- coding: utf-8 -*-
"""#477：移除 addon 的模組狀態守衛——advisory 預檢與 authoritative 寫入必須同源 fail-closed。

``_update_service``（真正會跑 helm upgrade 的那條）此前有兩個 fail-open 縫：

**縫 (a) — operator 回 ``success: false`` 靜默放行**。operator 對
``OdooModuleError`` 回的是 **HTTP 200 ＋ ``{"success": false, "error": "[step] ..."}"``**
（``extra/paas-operator/src/api/odoo_modules.py:59``），而平台側 client ``_request``
**只在 HTTP ≥ 400 才 raise**（``src/services/paas_operator.py:125``）。舊碼寫的是
``if check_result.get('success'):`` 而**沒有 else**——所以這條路既沒有例外、也進不了
隔壁那個註明 ``# Fail-closed`` 的 ``except``，直接掉出檢查區塊繼續往下跑 helm
upgrade。會產生 ``success: false`` 的 step 是 ``find_pod`` / ``query_modules`` /
``validate``；``find_pod`` 失敗＝租戶 Odoo 不健康（rolling restart、OOMKilled、node
drain），**保護最該生效的那一刻，守衛自己讓路**。

**縫 (b) — 非 running 狀態整段跳過**。守衛條件是
``if removed_ids and service.state == 'running'``，而 ``_update_service`` 的前置閘只擋
``pending`` / ``deleting`` / ``error``。8 個 state 扣掉這 3 個與 ``running``，還剩
``deploying`` / ``initializing`` / ``upgrading`` / ``suspended`` 四個進得了
``_update_service`` 卻完全不檢查模組狀態就移除。上游沒有別的閘擋得住：
``_check_operation_allowed`` 是**訂閱**閘（``subscription_lifecycle_mixin.py``，判
active / grace / past-grace，且 ``base.group_system`` 直接 bypass），與 service state
正交；前端 ``AddonsTab.canEdit`` 的 disabled 名單不含 ``suspended``，另外三個也可直接
RPC 繞過。

兩個縫的共同根因是**同一個裁決有兩份實作**：同一份 operator 回應，advisory 的預檢
端點 fail-closed、authoritative 的寫入路徑放行，方向剛好相反。所以本檔的斷言一律
**成對**——同一個情境同時打預檢與寫入，要求兩邊方向一致。只測一邊證不出「兩條路
已收斂」，而收斂才是本 issue 真正要修的東西（見
``docs/decisions/patterns/two-layer-acl-lockstep.md``：同一判準的兩份實作必然漂移）。

Refs #474 #476。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, tagged
from ..models.cloud_service import _internal_provision

# 端點取 client 的名字是在 controller 模組的 namespace 上解析的（``paas.py`` 頂部
# ``from ..services.paas_operator import get_paas_operator_client``），patch services
# 那一側對它無效。裁決點也住在同一個 module，所以這一個 target 同時攔兩條路徑；
# 若哪天裁決點搬到 model 層，這裡就要變成雙 target（見
# docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）。
_CLIENT_FACTORY = (
    'odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client')

_TECH_NAME = 'sale_477'

# 寫入路徑的探針一律夾帶一個**不在 template ``helm_value_specs`` 白名單內**的 key。
# 它同時扮演兩個角色，缺一不可：
#
# 1. **fail-open 偵測器**。``_filter_allowed_helm_values`` 的拒絕發生在 addon 守衛
#    **之後**、``state='upgrading'`` 與 helm upgrade **之前**。所以「回應錯誤是哪一
#    句」直接讀出執行流停在哪一關：守衛擋下 → 守衛自己的訊息；守衛放行 → 這個
#    sentinel 訊息。縫 (a)/(b) 的 RED 就是「拿到 sentinel 訊息」——證明執行流已經
#    越過守衛往 helm upgrade 去了，不是靠猜。
# 2. **煞車**。讓 ``action='update'`` 永遠到不了真的 helm upgrade：HttpCase 與 HTTP
#    handler 共用測試 cursor，一旦跑進背景 upgrade 執行緒（它自己開 registry cursor
#    寫 state），transaction 會被打壞，接著字母序在後的測試全部
#    ``InFailedSqlTransaction`` 連坐。
_SENTINEL_KEY = 'unauthorizedKey477'
_SENTINEL_ERROR = 'Unauthorized configuration keys'


class _AddonRemovalCase(HttpCase):
    """共用 fixture：admin 自己名下的 workspace ＋ 一個帶單一 addon 的 Odoo 服務。

    刻意讓 owner 就是登入者本人——被擋/不被擋才是語意判準，不是權限不足的副作用。
    ``admin`` 有 ``base.group_system``，所以 ``_check_operation_allowed`` 的訂閱閘
    直接 bypass；擋下來的一定是本 issue 要修的那道守衛。
    """

    service_state = 'running'

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 477', 'owner_id': self.admin_user.id})
        self.addon = self.env['woow_paas_platform.cloud_odoo_addon'].create({
            'name': 'Sales 477', 'technical_name': _TECH_NAME})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-477', 'reference_id': 'test-477',
            'state': self.service_state, 'subdomain': 'svc-477',
            'helm_namespace': 'paas-ws-test-477',
            'helm_release_name': 'svc-477',
            # 刻意**不**給 provision_partner_id：那欄只給「每帳號限一」的平台自動
            # 開通服務填，帶 partial unique index，填了會讓本檔對 DB 既有 row 產生
            # 依賴（沿用 #474 的 fixture 紀律）。
            'odoo_addon_ids': [(6, 0, [self.addon.id])],
        })
        self.preflight_url = (
            '/api/cloud-services/services/%s/check-addon-removal' % self.svc.id)
        self.write_url = (
            '/api/workspaces/%s/services/%s' % (self.ws.id, self.svc.id))

    # ---- HTTP 驅動 ----------------------------------------------------

    def _rpc(self, url, params):
        """打端點——**照前端的形狀**：鍵住在 JSON-RPC ``params`` 裡（#474）。"""
        resp = self.url_open(url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 477,
        }), headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn(
            'result', parsed,
            '端點沒回 JSON-RPC result（dispatch 期就炸了）：%s' % parsed)
        return parsed['result']

    def _preflight(self):
        """advisory 預檢端點——同一情境的對照組（#476 讓它恢復作用）。"""
        return self._rpc(
            self.preflight_url, {'removed_addon_ids': [self.addon.id]})

    def _write_removal(self):
        """authoritative 寫入路徑：取消勾選唯一的 addon（夾 sentinel 煞車）。"""
        return self._rpc(self.write_url, {
            'action': 'update',
            'odoo_addon_ids': [],
            'values': {_SENTINEL_KEY: 'x'},
        })

    def _addon_still_selected(self):
        """守衛擋下時 addon 選擇必須 rollback——寫了又不部署會讓 DB 與叢集對不上。"""
        self.svc.invalidate_recordset(['odoo_addon_ids'])
        return self.addon.id in self.svc.odoo_addon_ids.ids

    # ---- mock client 工廠 ---------------------------------------------

    @staticmethod
    def _client(modules=None, operator_error=None, exc=None):
        client = MagicMock()
        if exc is not None:
            client.get_odoo_module_states.side_effect = exc
        elif operator_error is not None:
            # operator 對 OdooModuleError 的真實回法：HTTP 200 ＋ success=false，
            # 平台側 client 不 raise（這就是縫 (a) 的機制）。
            client.get_odoo_module_states.return_value = {
                'success': False, 'error': operator_error}
        else:
            client.get_odoo_module_states.return_value = {
                'success': True, 'modules': modules or {}}
        return client

    # ---- 成對斷言 ------------------------------------------------------

    def _assert_both_paths_reject(self, client, expect_in_error):
        """同一情境下 advisory 預檢與 authoritative 寫入必須方向一致地擋下。

        斷言順序刻意把 **authoritative 寫入路徑排最前面**：它才是本 issue 的受測
        對象，紅的時候要第一眼看到 ``_SENTINEL_ERROR``——那句就是 fail-open 的直接
        證據（執行流越過守衛、抵達 helm value 白名單）。若讓預檢的訊息斷言先紅，
        RED 輸出會只反映「訊息還沒統一」，看不出真正的縫。

        只斷言 ``success is False`` 會**假綠**：fail-open 的路徑也會因為 sentinel
        而回 ``success=False``，只是理由完全不同。
        """
        with patch(_CLIENT_FACTORY, return_value=client):
            preflight = self._preflight()
            write = self._write_removal()

        self.assertNotIn(
            _SENTINEL_ERROR, write.get('error') or '',
            '寫入路徑跑到 sentinel＝守衛整段被跳過，helm upgrade 只差一步就會把 '
            'addon 程式碼抽掉而模組仍留在租戶 DB 裡（回 %r）' % write)
        self.assertFalse(
            write.get('success'),
            'authoritative 寫入路徑必須擋（回 %r）' % write)
        self.assertIn(
            expect_in_error, write.get('error') or '',
            '兩條路徑的理由必須同源；不同源代表裁決還是兩份實作（回 %r）' % write)
        self.assertTrue(
            self._addon_still_selected(),
            '被拒時 addon 選擇必須 rollback 回原狀，否則 DB 記的與叢集跑的對不上')

        self.assertFalse(
            preflight.get('success'),
            'advisory 預檢必須擋（回 %r）' % preflight)
        self.assertIn(
            expect_in_error, preflight.get('error') or '',
            '預檢的錯誤理由要落在同一道守衛上（回 %r）' % preflight)


@tagged('post_install', '-at_install')
class TestOperatorFailureIsFailClosed(_AddonRemovalCase):
    """縫 (a) RED：operator 回 ``success: false`` 時寫入路徑必須擋下。"""

    def test_operator_success_false_blocks_removal(self):
        """``find_pod`` 找不到租戶 Odoo pod → 查不到狀態就不准移除。

        修前：``if check_result.get('success'):`` 為 false → 整段被跳過 → 拿到
        sentinel 訊息（證明執行流已越過守衛）。修後：與預檢端點同一句
        ``Cannot verify module states``。
        """
        self._assert_both_paths_reject(
            self._client(operator_error='[find_pod] No Odoo pod found in namespace'),
            'Cannot verify module states')

    def test_query_modules_failure_blocks_removal(self):
        """``query_modules``（kubectl exec 失敗）走的是同一條無例外路徑。"""
        self._assert_both_paths_reject(
            self._client(operator_error='[query_modules] kubectl exec failed'),
            'Cannot verify module states')


@tagged('post_install', '-at_install')
class TestOperatorExceptionStaysFailClosed(_AddonRemovalCase):
    """迴歸錨：HTTP ≥ 400 那條（會 raise）本來就 fail-closed，修完不許退化。"""

    def test_transport_exception_blocks_removal(self):
        self._assert_both_paths_reject(
            self._client(exc=Exception('connection refused')),
            'Cannot verify module states')


class _NonRunningRemovalCase(_AddonRemovalCase):
    """縫 (b) RED 的共用斷言：非 running 狀態帶 removals 必須被擋，不是整段跳過。

    四個 state 各一個具體 class——用 class 屬性在 create 當下就設好 state，避免
    「測試環境改完 state 有沒有 flush 到 HTTP handler 看得到」這種與受測面向無關
    的不確定性。

    ⚠ 每個具體 class **必須自己定義 test method**（哪怕只是一行轉呼）。實測 Odoo 18
    的 test 收集不會把只從 base/mixin 繼承來的 test method 算進去：原本寫成 mixin
    帶 ``test_*``＋4 個空 body 的 class，7 個 class 只跑到 6 個 test（4 個非 running
    class 各 0 個），**靜默不跑、不報錯**——正是 woow-test.sh 那種假綠。
    """

    def _assert_non_running_rejected(self):
        client = self._client(
            modules={_TECH_NAME: {'state': 'uninstalled', 'dependents': []}})
        self._assert_both_paths_reject(client, 'must be running')
        client.get_odoo_module_states.assert_not_called()


@tagged('post_install', '-at_install')
class TestSuspendedRemovalRejected(_NonRunningRemovalCase):
    """``suspended`` 最要緊：前端 ``canEdit`` 的 disabled 名單不含它，UI 上就可編輯。"""

    service_state = 'suspended'

    def test_non_running_service_removal_is_rejected(self):
        self._assert_non_running_rejected()


@tagged('post_install', '-at_install')
class TestUpgradingRemovalRejected(_NonRunningRemovalCase):
    service_state = 'upgrading'

    def test_non_running_service_removal_is_rejected(self):
        self._assert_non_running_rejected()


@tagged('post_install', '-at_install')
class TestDeployingRemovalRejected(_NonRunningRemovalCase):
    service_state = 'deploying'

    def test_non_running_service_removal_is_rejected(self):
        self._assert_non_running_rejected()


@tagged('post_install', '-at_install')
class TestInitializingRemovalRejected(_NonRunningRemovalCase):
    service_state = 'initializing'

    def test_non_running_service_removal_is_rejected(self):
        self._assert_non_running_rejected()


@tagged('post_install', '-at_install')
class TestAdjudicationIsSharedNotDuplicated(_AddonRemovalCase):
    """裁決點單一化後，兩條路徑對「模組實際狀態」的處置也必須成對可預測。

    這一組是**反過度修復**的錨：fail-closed 不可以變成「什麼都擋」。
    """

    def test_installed_module_preflight_reports_but_write_path_rejects(self):
        """唯一一個**故意**不對稱的 cell，寫死在測試裡免得後人「順手修掉」。

        預檢端點的 ``success`` 只反映「能不能做出裁決」——查得到就回
        ``success=True`` 連同模組狀態，由前端 ``blocksRemoval`` 決定要不要擋（#476
        的行為矩陣鎖的就是這個）。「安不安全」這個維度住在 ``modules`` 裡，不在
        ``success`` 裡。authoritative 的寫入路徑則是把兩個維度都當拒絕理由。
        """
        client = self._client(modules={
            _TECH_NAME: {'state': 'installed', 'dependents': ['sale_management_477']}})
        with patch(_CLIENT_FACTORY, return_value=client):
            preflight = self._preflight()
            write = self._write_removal()

        self.assertTrue(
            preflight.get('success'),
            'advisory 預檢查得到狀態就是 success=True，擋不擋由前端判（回 %r）'
            % preflight)
        self.assertEqual(
            preflight.get('modules'),
            {_TECH_NAME: {
                'addon_id': self.addon.id,
                'state': 'installed',
                'dependents': ['sale_management_477'],
            }},
            '前端對話框要靠這包列「哪個模組、什麼狀態、被誰依賴」（回 %r）' % preflight)

        self.assertFalse(write.get('success'), write)
        self.assertIn(
            'Cannot remove', write.get('error') or '',
            'authoritative 路徑必須擋 installed 模組（回 %r）' % write)
        self.assertTrue(
            self._addon_still_selected(),
            '被拒時 addon 選擇必須 rollback')

    def test_safe_module_passes_the_guard_on_both_paths(self):
        """已 uninstalled → 兩條路都放行；fail-closed 不許連合法移除一起擋掉。

        寫入路徑「放行」的觀測量就是**拿到 sentinel 訊息**：執行流越過守衛、抵達
        下一關（helm value 白名單）才會產生它。這條在修前修後都必須綠。
        """
        client = self._client(
            modules={_TECH_NAME: {'state': 'uninstalled', 'dependents': []}})
        with patch(_CLIENT_FACTORY, return_value=client):
            preflight = self._preflight()
            write = self._write_removal()

        self.assertTrue(preflight.get('success'), preflight)
        self.assertEqual(
            preflight.get('modules'),
            {_TECH_NAME: {
                'addon_id': self.addon.id,
                'state': 'uninstalled',
                'dependents': [],
            }},
            preflight)

        self.assertIn(
            _SENTINEL_ERROR, write.get('error') or '',
            '安全的移除必須通過守衛抵達下一關；擋在守衛＝過度修復（回 %r）' % write)

    def test_operator_not_configured_blocks_removal(self):
        """裁決點拿不到 client 也是「查不到狀態」，兩條路都必須擋。"""
        with patch(_CLIENT_FACTORY, return_value=None):
            preflight = self._preflight()
            write = self._write_removal()

        self.assertFalse(preflight.get('success'), preflight)
        self.assertIn('not configured', preflight.get('error') or '', preflight)
        self.assertFalse(write.get('success'), write)
        self.assertNotIn(
            _SENTINEL_ERROR, write.get('error') or '',
            '沒有 operator 也照樣跑到下一關＝守衛沒 fire（回 %r）' % write)
        self.assertTrue(self._addon_still_selected(), '被拒時必須 rollback')
