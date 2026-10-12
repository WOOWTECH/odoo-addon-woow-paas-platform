"""Security Access 的 Access 分頁兩個警告：「保護未生效」與「登入方式缺 IdP」。

## 1. 保護未生效（``access_protection_lost``，#794）

* **何時被設**：只有 ``SecurityAccessRoute._access_mark_protection_lost()``——
  由 ``cf_access_policy_reap._compensate_fail_open_after_rollback`` 在「刪除交易
  回捲了、但 Cloudflare 上的 Access app 已經拆掉」時以獨立交易呼叫。一般 sync
  的成功、失敗、被 liveness gate 擋掉都**不會**設它（本檔 ``P1``）。
* **何時被清**：``_sync_access_to_operator()`` 真的推出去並拿到 2xx 時（不論
  ``enabled`` 真假）。失敗／被擋掉時留著。
* **回傳給前端**：``route.to_dict()['access_protection_lost']``（bool），經
  routes ``list``／SA ``get``／per-route ``access-config`` 回應送到
  ``AccessAppCard``（``protectionLost`` 為真時畫紅色警示，並從卡頭的
  「N of M routes protected」扣掉）。

既有測試已涵蓋（本檔不重複）：

* 補償只交給「真的拆成功」那幾列、標記內容與 ``is_protected`` 不動、成功 sync
  清旗標、to_dict 有吐 —— ``test_access_failopen_compensation_794.py``（A1–A7、
  F1–F3）與 integration ``test_failopen_real_rollback_794.py``
* cron 重推：成功清、失敗留、gate 擋掉留、不誤撈 —— ``test_access_resync_cron_803.py``
* SA 詳情 payload（``sa.to_dict()['routes']``）True／False 兩格 ——
  ``test_access_protection_lost_ui_payload.py``

本檔補：一般 sync 不會誤設（P1）、經 HTTP 的 JSON 型別（E1）、租戶無法經
白名單端點把它清掉（E2）、前端文案承諾的「關掉再打開保護」確實會清（E3）、
保護未生效的 route 健康檢查報 ``access_missing``（E4），以及一條**現況鎖定**
（P2：保護失效後只「關掉」保護，旗標不會被清——見 PR 描述的疑似缺陷）。

## 2. 登入方式在 Cloudflare 缺少對應 IdP（``access_unresolved_idp_types``）

* **何時產生**：route 勾了 ``login_google``／``login_email_otp``，payload 送出
  ``login_idp_types``（``google``／``onetimepin``），operator 在帳號上找不到對應
  IdP 時於回應的 ``unresolved_idp_types`` 列出；``_sync_access_to_operator()``
  以 ``', '.join(...)`` 存進欄位，空清單存 ``False``。
* **回傳格式**：``to_dict()['access_unresolved_idp_types']`` 一律是字串
  （沒有時是 ``''``），前端以 ``split(",")`` ＋ ``trim`` 拆開再對映成
  「Google」「Email OTP」。

既有測試已涵蓋（本檔不重複）：payload 的 ``login_idp_types`` 組法
（``test_security_access_access_policy.py``）、租戶以白名單端點偽造／清空它
（``test_security_access_subscription_flow.py::test_update_access_config_
whitelist_only`` 與 per-route 版本）。CS／SH 兩腿各有持久化測試
（``test_cs_access_sync_745.py::test_B4``、``test_sh_access_sync_768.py::
test_C3``），**SA 腿原本沒有**——本檔 I 系列補上。

operator 一律 mock：``models.security_access.get_paas_operator_client``（SA
model 以頂部 import 取得 client，patch 要打在 model namespace，見
``unittest-mock-dual-namespace-patch``）。回傳值用完整五個 key 的 dict，不用裸
MagicMock（裸 mock 的 ``orphaned_policy_ids`` 永遠 truthy）。
"""
import json
import uuid
from unittest.mock import MagicMock, patch
from .publication_receipt_fixture import access_receipt, routes_receipt

from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal_sa,
    _skip_route_sync,
)
from odoo.addons.woow_paas_platform.services import route_probe

MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
LOG_SA = 'odoo.addons.woow_paas_platform.models.security_access'
LOG_REAP = 'odoo.addons.woow_paas_platform.models.cf_access_policy_reap'

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_ACCESS_LOGIN = ('https://woowtech.cloudflareaccess.com/cdn-cgi/access/login/'
                 'x.woowtech.io?kid=abc')


def _sync_result(app_id='app-new', unresolved=None):
    """operator ``PUT /api/access/sync`` 的真實回傳形狀（五個 key）。"""
    return {
        'app_id': app_id,
        'policy_ids': [],
        'allowed_idps': [],
        'unresolved_idp_types': list(unresolved or []),
        'orphaned_policy_ids': [],
    }


def _client(result=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.sync_access_app.side_effect = side_effect
    elif result is None:
        client.sync_access_app.side_effect = access_receipt
    else:
        client.sync_access_app.return_value = result
    client.sync_tunnel_routes.side_effect = routes_receipt
    return client


class _SaRouteFixture:
    """一台 active＋live tunnel 的 SA，route 全程以 `_skip_route_sync` 建立
    （fixture 自己不打 operator，受測動作才會）。"""

    def _make_sa(self, state='active'):
        suffix = uuid.uuid4().hex[:6]
        workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS warn %s' % suffix})
        sa = self.env[_SA].sudo().create(
            {'name': 'SA warn %s' % suffix, 'workspace_id': workspace.id})
        _internal_sa(sa).write(
            {'state': state, 'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        return sa

    def _make_route(self, sa, protected=True, app_id='app-live', **vals):
        route = _skip_route_sync(self.env[_ROUTE].sudo()).create(dict({
            'security_access_id': sa.id,
            'subdomain_prefix': 'w%s' % uuid.uuid4().hex[:10],
            'is_protected': protected}, **vals))
        route = route.with_context(sa_skip_route_sync=False)
        if app_id:
            _internal_sa(route).access_app_id = app_id
        return route

    def _reload(self, route, *fnames):
        route.invalidate_recordset(list(fnames) or None)
        return route


# ====================================================================== #
#  1. 保護未生效（access_protection_lost）                                #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteProtectionLostLifecycle(TransactionCase, _SaRouteFixture):

    def setUp(self):
        super().setUp()
        self.sa = self._make_sa()
        self.route = self._make_route(self.sa)

    def test_P1_ordinary_sync_outcomes_never_raise_the_flag(self):
        """旗標只能由補償的標記設。一般 sync 的三種結局都不得誤設：

        * 推成功（enabled=True）
        * 推失敗（operator 拋例外——CF 上的 app 仍在，那不是「保護失效」）
        * 被 liveness gate 擋掉（SA 停權）
        """
        with patch(MOCK_SA_CLIENT, return_value=_client(_sync_result('app-live'))):
            self.assertTrue(self.route._sync_access_to_operator())
        self.assertFalse(self.route.access_protection_lost, '推成功不得設旗標')

        with patch(MOCK_SA_CLIENT,
                   return_value=_client(side_effect=Exception('operator down'))):
            try:
                self.route._sync_access_to_operator()
                self.fail('前提：operator 拋例外時 sync 應往外拋')
            except Exception as exc:  # noqa: BLE001
                self.assertIn('operator down', str(exc))
        self.assertFalse(self.route.access_protection_lost, '推失敗不得設旗標')
        self.assertEqual(self.route.access_app_id, 'app-live',
                         '推失敗時 app id 是重試錨點，不得被清')

        _internal_sa(self.sa).state = 'suspended'
        client = _client()
        with patch(MOCK_SA_CLIENT, return_value=client), mute_logger(LOG_SA, LOG_REAP):
            self.assertFalse(self.route._sync_access_to_operator())
        client.sync_access_app.assert_not_called()
        self.assertFalse(self.route.access_protection_lost, '被 gate 擋掉不得設旗標')

        # 陽性對照：只有標記會設，且不動 is_protected。
        self.route._access_mark_protection_lost()
        self.assertTrue(self.route.access_protection_lost)
        self.assertTrue(self.route.is_protected)
        self.assertTrue(self.route.to_dict()['access_protection_lost'])

    def test_P2_turning_protection_off_after_loss_clears_flag(self):
        """保護失效後租戶只把保護**關掉**：旗標必須清掉，且不外呼。

        `_access_mark_protection_lost()` 會清掉 `access_app_id`；之後
        `is_protected=False` 且沒有 app id ⇒ `_access_sync_skip_reason()` 判成
        「unprotected and never had an Access app」⇒ 不推。修前旗標因此永遠
        留著：租戶刻意設為公開的 route 仍掛紅色「Access protection is not in
        effect」，#803 cron 每輪把它算進 gated。修後 sync 在略過時就地清旗標——
        列不再宣稱保護，CF 上也沒有 app，兩邊一致（三腿同修，另見
        `test_access_resync_cron_803` 的 A6／B6／C6）。
        """
        self.route._access_mark_protection_lost()
        self.assertFalse(self.route.access_app_id, '前提：標記清掉了 app id')
        self.assertTrue(self.route.access_protection_lost, '前提：旗標打開')
        client = _client(_sync_result(app_id=None))
        with patch(MOCK_SA_CLIENT, return_value=client), mute_logger(LOG_SA, LOG_REAP):
            self.route.write({'is_protected': False})
        self._reload(self.route)
        client.sync_access_app.assert_not_called()
        self.assertEqual(
            self.route._access_sync_skip_reason(),
            'the route is unprotected and never had an Access app')
        self.assertFalse(self.route.is_protected)
        self.assertFalse(
            self.route.access_protection_lost,
            '改成公開 ⇒ 列不再宣稱保護 ⇒ 「保護未生效」必須清掉')

    def test_P3_teardown_sync_clears_flag_while_app_id_is_still_held(self):
        """對照 P2：只要列上還握著 app id，`enabled=False` 那一趟就會真的推出去，
        推成功即清旗標（註解描述的行為在這個前提下成立）。"""
        self.route.access_protection_lost = True
        self.assertEqual(self.route.access_app_id, 'app-live')
        client = _client(_sync_result(app_id=None))
        with patch(MOCK_SA_CLIENT, return_value=client):
            self.route.write({'is_protected': False})
        self._reload(self.route)
        sent = client.sync_access_app.call_args.args[0]
        self.assertIs(sent['enabled'], False)
        self.assertEqual(sent['existing_app_id'], 'app-live')
        self.assertFalse(self.route.access_protection_lost)
        self.assertFalse(self.route.access_app_id)


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


class _HttpSaFixture(_SaRouteFixture):
    """workspace owner（只有 base.group_user）＋ active SA。"""

    def _setup_owner_sa(self):
        suffix = uuid.uuid4().hex[:6]
        self.owner = _new_user(self.env, 'sa_warn_owner_%s' % suffix)
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS warn http %s' % suffix, 'owner_id': self.owner.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        self.sa = self.env[_SA].sudo().create(
            {'name': 'SA warn http %s' % suffix, 'workspace_id': self.workspace.id})
        _internal_sa(self.sa).write(
            {'state': 'active', 'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8]})
        self.base = '/api/workspaces/%s/security-accesses/%s' % (
            self.workspace.id, self.sa.id)

    def _rpc(self, path, params, expect_success=True):
        # SA ``get`` 會即時問 operator 的 tunnel 狀態（只讀疊加）；本檔不測它，
        # 一律回 {}（＝沿用 DB 值），免得 fixture 的 tunnel_id 引來一次外呼。
        # patch 打在註冊表上的實際 class（odoo-test-registry-patch 的教訓）。
        with patch.object(type(self.env[_SA]), '_live_tunnel_status',
                          return_value={}):
            resp = self.url_open(
                self.base + path,
                data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                                 'params': params}),
                headers={'Content-Type': 'application/json'})
        body = resp.json()
        self.assertNotIn('error', body, body.get('error'))
        result = body['result']
        self.assertIs(bool(result.get('success')), expect_success, result)
        self.env.invalidate_all()
        return result.get('data') if expect_success else result

    @staticmethod
    def _by_id(items, route):
        match = [r for r in items if r['id'] == route.id]
        assert len(match) == 1, items
        return match[0]


@tagged('post_install', '-at_install')
class TestSaRouteProtectionLostEndpoints(HttpCase, _HttpSaFixture):

    def setUp(self):
        super().setUp()
        self._setup_owner_sa()
        self.lost = self._make_route(self.sa, protected=True, app_id=None)
        self.lost.access_protection_lost = True
        self.fine = self._make_route(self.sa, protected=True, app_id='app-fine')
        self.authenticate(self.owner.login, self.owner.login)

    def test_E1_list_and_detail_return_flag_as_json_bool(self):
        """routes ``list`` 與 SA ``get`` 都吐 JSON 布林：旗標列 true、其他 false
        （不是缺席——缺席與「沒事」在前端長得一樣）。"""
        listed = self._rpc('/routes', {'action': 'list'})
        detail = self._rpc('', {'action': 'get'})['routes']
        for where, items in (('routes list', listed), ('SA get', detail)):
            with self.subTest(where=where):
                self.assertIs(self._by_id(items, self.lost)['access_protection_lost'], True)
                self.assertIs(self._by_id(items, self.lost)['is_protected'], True,
                              'is_protected 仍是意圖 True，前端要靠旗標而不是它')
                self.assertIs(self._by_id(items, self.fine)['access_protection_lost'], False)

    def test_E2_client_cannot_clear_flag_through_whitelisted_endpoints(self):
        """租戶在 per-route access-config 與 routes update 塞
        ``access_protection_lost: false`` ⇒ 白名單丟掉、旗標不動、operator 一次都
        不被打。對照：同一支 routes update 的白名單欄位（service_url）確實寫入。"""
        client = _client()
        with patch(MOCK_SA_CLIENT, return_value=client):
            data = self._rpc('/routes/%s/access-config' % self.lost.id,
                             {'access_protection_lost': False})
            self.assertIs(data['access_protection_lost'], True)
            data = self._rpc('/routes', {
                'action': 'update', 'route_id': self.lost.id,
                'access_protection_lost': False,
                'service_url': 'http://localhost:8099'})
        self.assertIs(data['access_protection_lost'], True)
        self.assertEqual(data['service_url'], 'http://localhost:8099',
                         '對照：白名單欄位有寫入，證明請求確實走到寫入路徑')
        self.assertTrue(self.lost.access_protection_lost)
        client.sync_access_app.assert_not_called()

    def test_E3_toggle_off_and_on_reapplies_protection_and_clears_flag(self):
        """前端警示叫租戶「turn protection off and on again」：打開那一趟以建立
        語意（``existing_app_id`` 為 None）重推，成功後回應與詳情都變 false。"""
        client = _client(_sync_result('app-reborn'))
        path = '/routes/%s/access-config' % self.lost.id
        with patch(MOCK_SA_CLIENT, return_value=client), mute_logger(LOG_SA, LOG_REAP):
            self._rpc(path, {'is_protected': False})
            data = self._rpc(path, {'is_protected': True})
        enabled_calls = [c.args[0] for c in client.sync_access_app.call_args_list
                         if c.args[0]['enabled']]
        self.assertEqual(len(enabled_calls), 1, client.sync_access_app.call_args_list)
        self.assertIsNone(enabled_calls[0]['existing_app_id'],
                          '死 id 已清 ⇒ 重新打開保護必須是建立語意')
        self.assertIs(data['access_protection_lost'], False)
        self.assertIs(data['is_protected'], True)
        self.assertEqual(self.lost.access_app_id, 'app-reborn')
        detail = self._rpc('', {'action': 'get'})['routes']
        self.assertIs(self._by_id(detail, self.lost)['access_protection_lost'], False)

    def test_E4_lost_route_probes_as_access_missing(self):
        """保護未生效的 route 對外免登入可達 ⇒ 健康檢查報 access_missing；
        保護有效的 route 被導去 Access ⇒ access_login。兩種警告講的是同一件事。"""
        route_probe._cache.clear()
        responses = {
            self.lost.hostname: (200, {'Server': 'cloudflare'}, '<h1>app</h1>'),
            self.fine.hostname: (302, {'Location': _ACCESS_LOGIN}, ''),
        }
        with patch.object(route_probe, '_fetch', side_effect=responses.__getitem__):
            data = self._rpc('/routes', {'action': 'health', 'force': True})
        self.assertEqual(data[str(self.lost.id)]['status'], 'access_missing')
        self.assertFalse(data[str(self.lost.id)]['healthy'])
        self.assertEqual(data[str(self.fine.id)]['status'], 'access_login')


# ====================================================================== #
#  2. 登入方式缺少對應 IdP（access_unresolved_idp_types）                  #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteUnresolvedIdpWarning(TransactionCase, _SaRouteFixture):

    def setUp(self):
        super().setUp()
        self.sa = self._make_sa()
        self.route = self._make_route(self.sa, login_google=True, login_email_otp=True)

    def _sync(self, unresolved=None, app_id='app-live'):
        result = (_sync_result(app_id, unresolved) if unresolved else None)
        with patch(MOCK_SA_CLIENT, return_value=_client(result)):
            return self.route._sync_access_to_operator()

    def _detail_value(self):
        routes = self.sa.to_dict()['routes']
        return [r for r in routes if r['id'] == self.route.id][0][
            'access_unresolved_idp_types']

    def test_I1_unresolved_types_are_persisted_comma_joined(self):
        """Partial IdP resolution is rejected and the old anchor is retained."""
        old_app = self.route.access_app_id
        with self.assertRaises(UserError):
            self._sync(unresolved=['google', 'onetimepin'])
        self.route.invalidate_recordset()
        self.assertEqual(self.route.access_app_id, old_app)
        self.assertFalse(self.route.access_unresolved_idp_types)

    def test_I2_resolved_login_methods_leave_no_warning(self):
        """對照：登入方式都有 IdP（``unresolved_idp_types=[]``）⇒ 欄位 False、
        payload 是空字串（型別穩定，前端 ``t-if`` 判假值）。"""
        self.assertTrue(self._sync(unresolved=[]))
        self.assertFalse(self.route.access_unresolved_idp_types)
        self.assertEqual(self.route.to_dict()['access_unresolved_idp_types'], '')
        self.assertEqual(self._detail_value(), '')

    def test_I3_warning_cleared_once_operator_resolves(self):
        """A rejected partial receipt can be retried with a complete receipt."""
        with self.assertRaises(UserError):
            self._sync(unresolved=['google'])
        self.assertTrue(self._sync(unresolved=[]))
        self.assertEqual(self.route.to_dict()['access_unresolved_idp_types'], '')

    def test_I4_gated_or_failed_sync_keeps_previous_warning(self):
        """推不出去時沒有新答案 ⇒ 沿用上一次的警告（不得被清成「沒事」）。"""
        self.route.access_unresolved_idp_types = 'google'

        with patch(MOCK_SA_CLIENT,
                   return_value=_client(side_effect=Exception('operator down'))):
            try:
                self.route._sync_access_to_operator()
                self.fail('前提：operator 拋例外時 sync 應往外拋')
            except Exception as exc:  # noqa: BLE001
                self.assertIn('operator down', str(exc))
        self.assertEqual(self.route.access_unresolved_idp_types, 'google')

        _internal_sa(self.sa).state = 'suspended'
        client = _client(_sync_result(unresolved=[]))
        with patch(MOCK_SA_CLIENT, return_value=client), mute_logger(LOG_SA, LOG_REAP):
            self.assertFalse(self.route._sync_access_to_operator())
        client.sync_access_app.assert_not_called()
        self.assertEqual(self.route.access_unresolved_idp_types, 'google')

    def test_I5_teardown_sync_clears_warning(self):
        """關掉保護（``enabled=False``）那一趟 operator 回空清單 ⇒ 警告一併清掉，
        不會在公開的 route 上留著「登入限制被放寬」。"""
        self.route.access_unresolved_idp_types = 'google'
        client = _client(_sync_result(app_id=None, unresolved=[]))
        with patch(MOCK_SA_CLIENT, return_value=client):
            self.route.write({'is_protected': False})
        self._reload(self.route)
        self.assertIs(client.sync_access_app.call_args.args[0]['enabled'], False)
        self.assertEqual(self.route.to_dict()['access_unresolved_idp_types'], '')


@tagged('post_install', '-at_install')
class TestSaRouteUnresolvedIdpEndpoint(HttpCase, _HttpSaFixture):

    def setUp(self):
        super().setUp()
        self._setup_owner_sa()
        self.route = self._make_route(self.sa, protected=True, app_id='app-idp')
        self.authenticate(self.owner.login, self.owner.login)

    def test_access_config_response_and_detail_carry_the_warning(self):
        """租戶勾 Google、CF 帳號沒有 Google IdP ⇒ per-route access-config 的回應
        與 SA 詳情都帶 ``'google'``；改成 Email OTP 且 IdP 存在 ⇒ 兩邊都變 ``''``。"""
        path = '/routes/%s/access-config' % self.route.id
        client = _client(_sync_result('app-idp', ['google']))
        with patch(MOCK_SA_CLIENT, return_value=client):
            data = self._rpc(path, {'login_google': True}, expect_success=False)
        sent = client.sync_access_app.call_args.args[0]
        self.assertEqual(sent['login_idp_types'], ['google'],
                         '前提：payload 要求的正是 operator 回報缺少的那個')
        self.assertFalse(data['success'])
        self.route.invalidate_recordset()
        self.assertEqual(self.route.access_app_id, 'app-idp')

        client = _client()
        with patch(MOCK_SA_CLIENT, return_value=client):
            data = self._rpc(path, {'login_google': False, 'login_email_otp': True})
        self.assertEqual(client.sync_access_app.call_args.args[0]['login_idp_types'],
                         ['onetimepin'])
        self.assertEqual(data['access_unresolved_idp_types'], '')
        detail = self._rpc('', {'action': 'get'})['routes']
        self.assertEqual(self._by_id(detail, self.route)['access_unresolved_idp_types'], '')
