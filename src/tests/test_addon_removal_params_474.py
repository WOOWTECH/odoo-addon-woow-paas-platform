# -*- coding: utf-8 -*-
"""#474：check-addon-removal 的參數必須從 JSON-RPC ``params`` 進來。

端點原本讀 ``request.get_json_data()``——Odoo 18 那支回的是**整個 request body**
（``odoo/http.py``：``json.loads(httprequest.get_data(as_text=True))``），不是
JSON-RPC 的 ``params``。前端 ``cloud_service.js`` 走標準 envelope
（``{jsonrpc, method, params:{removed_addon_ids}}``），於是端點恆讀到
``removed_addon_ids=None`` → 命中 ``if not removed_addon_ids`` 提早回
``{'success': True, 'modules': {}}``，**它之後的每一道檢查一次都沒跑過**。

本檔的受測面向是 **plumbing ＋ 行為矩陣**，兩者缺一不可：

- 只斷言 ``success`` 會**假綠**——修前的 early return 也回 ``success=True``。所以
  每條斷言都必須落在「早期短路產生不出來的觀測量」上：非 running 的狀態錯誤、
  ``modules`` 的實際內容、以及 operator **是否真的被呼叫過**。
- 行為矩陣兩型：模組仍 installed（且被其他模組依賴）→ 端點如實回報，前端據此擋；
  模組已 uninstalled → 端點回報安全狀態，前端放行。

⚠ 這些測試**必須**走真的 HTTP（``url_open``），不能直接呼叫 controller method——
受測的正是 Odoo 對 ``type='json'`` route 把 ``params`` 展開成 kwargs 的那段
plumbing，直接呼叫等於把受測對象整段繞過去（那也正是舊測試看不出這個 bug 的原因）。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, tagged

# 端點取 client 的名字是在 controller 模組的 namespace 上解析的（``from ..services
# .paas_operator import get_paas_operator_client``），patch services 那一側對它無效。
_CLIENT_FACTORY = (
    'odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client')

_TECH_NAME = 'sale_474'


class _AddonRemovalCase(HttpCase):
    """共用 fixture：admin session ＋ 自己名下一個帶 addon 的 Odoo 服務。

    刻意讓 owner 就是登入者本人——被擋/不被擋才是語意判準，不是權限不足的副作用。
    """

    service_state = 'running'

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 474', 'owner_id': self.admin_user.id})
        self.addon = self.env['woow_paas_platform.cloud_odoo_addon'].create({
            'name': 'Sales 474', 'technical_name': _TECH_NAME})
        self.svc = self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-474', 'reference_id': 'test-474',
            'state': self.service_state, 'subdomain': 'svc-474',
            'helm_namespace': 'paas-ws-test-474',
            'helm_release_name': 'svc-474',
            # 刻意**不**給 provision_partner_id：那欄只給「每帳號限一」的平台自動
            # 開通服務（HA PaaS 類）填，且帶 partial unique index。一般 cloud
            # service 填了會讓本檔對「DB 裡是否已有同 partner 的服務」產生依賴。
            'odoo_addon_ids': [(6, 0, [self.addon.id])],
        })
        self.url = (
            '/api/cloud-services/services/%s/check-addon-removal' % self.svc.id)

    def _call(self, params):
        """打端點——**照前端的形狀**：鍵住在 JSON-RPC ``params`` 裡。"""
        resp = self.url_open(self.url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 474,
        }), headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn(
            'result', parsed,
            '端點沒回 JSON-RPC result（dispatch 期就炸了）：%s' % parsed)
        return parsed['result']


@tagged('post_install', '-at_install')
class TestAddonRemovalParamsReachChecks(_AddonRemovalCase):
    """#474 的 RED：前端形狀的請求必須抵達 early return **之後**的檢查。"""

    service_state = 'suspended'

    def test_non_running_service_is_rejected(self):
        """非 running 的服務必須回狀態錯誤，而不是靜默成功。

        這是「安全檢查從未執行」的最短證明：狀態閘就在 early return 的下一層，
        修前恆回 ``{'success': True, 'modules': {}}``——連第一道閘都到不了。
        """
        r = self._call({'removed_addon_ids': [self.addon.id]})
        self.assertFalse(
            r.get('success'),
            'suspended 服務的模組狀態查不了，端點不該回成功（回 %r）' % r)
        self.assertIn(
            'must be running', r.get('error') or '',
            '應回狀態閘的錯；回別的代表卡在更前面的閘或根本沒收到參數（回 %r）' % r)


@tagged('post_install', '-at_install')
class TestAddonRemovalBehaviourMatrix(_AddonRemovalCase):
    """安全檢查的行為矩陣：擋 vs 放行，兩型都要有觀測得到的差異。"""

    def _call_with_module_state(self, state, dependents):
        """讓 operator 回一組固定的模組狀態，回傳 (client, result)。

        回傳 client 是為了斷言「**真的呼叫過**」——沒有這一條，修前的 early return
        會讓「放行」那型變成 vacuous pass（success=True 但根本沒查）。
        """
        client = MagicMock()
        client.get_odoo_module_states.return_value = {
            'success': True,
            'modules': {_TECH_NAME: {'state': state, 'dependents': dependents}},
        }
        with patch(_CLIENT_FACTORY, return_value=client):
            result = self._call({'removed_addon_ids': [self.addon.id]})
        return client, result

    def test_installed_module_with_dependents_is_reported(self):
        """仍 installed ＋ 有其他模組依賴 → 端點如實回報，前端據此擋下取消勾選。

        前端判準是 ``!SAFE_TO_REMOVE.has(state)``（AddonsTab.saveAddons），
        ``dependents`` 則是對話框要顯示的「被誰依賴」。兩者都必須原封不動送到前端。
        """
        client, r = self._call_with_module_state(
            'installed', ['sale_management_474'])

        client.get_odoo_module_states.assert_called_once()
        self.assertEqual(
            client.get_odoo_module_states.call_args.kwargs['module_names'],
            [_TECH_NAME],
            'operator 收到的是 addon 的 technical_name，不是 id')
        self.assertTrue(r.get('success'), r)
        self.assertEqual(
            r.get('modules'),
            {_TECH_NAME: {
                'addon_id': self.addon.id,
                'state': 'installed',
                'dependents': ['sale_management_474'],
            }},
            '前端要靠這包判斷擋不擋、以及對話框列什麼（回 %r）' % r)

    def test_uninstalled_module_is_reported_as_safe(self):
        """已 uninstalled → 端點回報安全狀態，前端放行去跑 helm upgrade。

        不能只斷言 ``success``：修前的 early return 也是 success=True。真正的
        受測面向是「查過了，而且查出來是安全的」。
        """
        client, r = self._call_with_module_state('uninstalled', [])

        client.get_odoo_module_states.assert_called_once()
        self.assertTrue(r.get('success'), r)
        self.assertEqual(
            r.get('modules'),
            {_TECH_NAME: {
                'addon_id': self.addon.id,
                'state': 'uninstalled',
                'dependents': [],
            }},
            r)

    def test_empty_selection_short_circuits_without_calling_operator(self):
        """沒有要移除任何 addon 時仍舊短路——修這個 bug 不該順手把短路拆掉。

        （這條修前修後都綠，是防止過度修復的錨點。）
        """
        client = MagicMock()
        with patch(_CLIENT_FACTORY, return_value=client):
            r = self._call({'removed_addon_ids': []})

        self.assertEqual(r, {'success': True, 'modules': {}})
        client.get_odoo_module_states.assert_not_called()

    def test_malformed_ids_are_rejected_cleanly(self):
        """型別不對時回錯，不得 500。

        修前參數根本沒被讀進來，這條路徑不可達；改成從 params 收之後它第一次
        暴露在外部輸入下——``browse('sale_474')`` 會直接炸成 500（``_call`` 會在
        ``assertIn('result')`` 那行紅）。
        """
        client = MagicMock()
        with patch(_CLIENT_FACTORY, return_value=client):
            r = self._call({'removed_addon_ids': _TECH_NAME})

        self.assertFalse(r.get('success'), '不是 id 清單就該被拒（回 %r）' % r)
        client.get_odoo_module_states.assert_not_called()

    def test_extra_params_do_not_break_dispatch(self):
        """``**kw`` 守衛：前端多送鍵不得變成 unexpected keyword argument。

        改用具名參數收 params 之後，簽名沒吸收的鍵會讓 Odoo 的 dispatch 直接
        TypeError → JSON-RPC error（``_call`` 會在 assertIn('result') 那行紅）。
        """
        client = MagicMock()
        client.get_odoo_module_states.return_value = {
            'success': True,
            'modules': {_TECH_NAME: {'state': 'uninstalled', 'dependents': []}},
        }
        with patch(_CLIENT_FACTORY, return_value=client):
            r = self._call({
                'removed_addon_ids': [self.addon.id], 'noise_474': True})

        self.assertTrue(r.get('success'), r)
