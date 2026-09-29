"""Smart Home 詳情頁／HA status 端點的 tunnel 狀態改用即時值（只讀疊加，不寫 DB）。

與 SA 詳情頁 #1256 同一個形狀：``SmartHome.action_refresh_status`` 只在 /woow
的 ``refresh_status`` action（owner/admin，#524）被呼叫時才寫回，詳情頁載入時
從不呼叫、也沒有排程 ⇒ 「Tunnel Information」停在上一次寫入的值。另一邊，HA
machine API 的 status 端點（``/api/smarthome/homes/<id>/status`` 與
``/api/security-access/accesses/<id>/status``）只過 OAuth scope ＋ workspace
成員資格就呼叫 ``action_refresh_status()``（sudo 記錄上的 DB 寫入）——與 /woow
的 owner/admin 閘不一致。

守的性質：
* ``_tunnel_vals_from_status`` 與重構前 ``action_refresh_status`` 內嵌的判讀一致
  （含 #833 ``deleted_at`` 語意）；
* detail ``get`` 的 tunnel 欄位是 operator 即時值，所有成員（含 role=user）都拿得到；
* 這條路徑**不寫 DB**；operator 拿不到時沿用 DB 值，不會讓整頁失敗；
* HA status 端點不再呼叫 ``action_refresh_status``、回即時值、key 集合與
  ``to_dict()`` 相同（不外流 ``tunnel_status_live``）；
* status 改只讀之後 stored 值不再被 HA 輪詢自動刷新 ⇒ HA 的 list／detail 端點也
  疊即時值（list 有熔斷：operator 第一次拿不到就停，最多吃一次逾時）。
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal,
)
from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorAPIError

from .smart_home_fixture import sh_model  # #524：建立閘 fixture helper

_SH_MODEL = 'woow_paas_platform.smart_home'
_SA_MODEL = 'woow_paas_platform.security_access'

_SH_CLIENT_FACTORY = ('odoo.addons.woow_paas_platform.models.smart_home.'
                      'get_paas_operator_client')
_SA_CLIENT_FACTORY = ('odoo.addons.woow_paas_platform.models.security_access.'
                      'get_paas_operator_client')

_CONNECTED = {
    'tunnel_id': 't', 'status': 'healthy', 'deleted_at': None,
    'connections': [
        {'connector_id': 'c-live', 'type': 'cloudflared',
         'opened_at': '2026-09-29T11:29:42Z'},
        {'connector_id': 'c-live', 'type': 'cloudflared'},
    ],
}
_DOWN = {'tunnel_id': 't', 'status': 'down', 'deleted_at': None, 'connections': []}
_DELETED = dict(_CONNECTED, deleted_at='2026-09-29T00:00:00Z')
# 舊版 operator：沒有 deleted_at 這個 key（#833：缺席不是訊號，落回既有判斷）。
_LEGACY_NO_DELETED_AT = {'connections': [
    {'connector_id': 'c-old', 'type': 'cloudflared'}]}

# DB 裡「上一次寫入」的舊值——與 SA #1256 當時正式站的樣子同形。
_STALE = {
    'tunnel_status': 'connected', 'connector_id': 'c-stale',
    'connector_type': 'cloudflared', 'tunnel_uptime': '2026-09-25T04:09:25Z',
}


def _client(**kw):
    client = MagicMock()
    client.get_tunnel_status = MagicMock(**kw)
    return client


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestShTunnelValsFromStatus(TransactionCase):
    """純判讀：與重構前 action_refresh_status 內嵌的邏輯逐格相同。"""

    def setUp(self):
        super().setUp()
        self.SH = self.env[_SH_MODEL]
        ws = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SH Vals WS %s' % uuid.uuid4().hex[:6]})
        self.home = sh_model(self.env).create({
            'name': 'SH Vals', 'workspace_id': ws.id,
            'tunnel_id': 'tun-sh-vals', 'state': 'active',
        })

    def test_connected(self):
        self.assertEqual(self.SH._tunnel_vals_from_status(_CONNECTED), {
            'tunnel_status': 'connected', 'connector_id': 'c-live',
            'connector_type': 'cloudflared',
            'tunnel_uptime': '2026-09-29T11:29:42Z',
        })

    def test_disconnected_keeps_connector_bookkeeping_untouched(self):
        # SH 原本在 disconnected 時從不清 connector_id／uptime——重構不改行為。
        self.assertEqual(self.SH._tunnel_vals_from_status(_DOWN),
                         {'tunnel_status': 'disconnected'})

    def test_deleted_clears_connector_bookkeeping(self):
        self.assertEqual(self.SH._tunnel_vals_from_status(_DELETED), {
            'tunnel_status': 'deleted', 'connector_id': '', 'connector_type': '',
        })

    def test_missing_deleted_at_is_not_a_signal(self):
        vals = self.SH._tunnel_vals_from_status(_LEGACY_NO_DELETED_AT)
        self.assertEqual(vals['tunnel_status'], 'connected')
        self.assertNotIn('tunnel_uptime', vals, 'no opened_at ⇒ uptime untouched')

    def test_refresh_writes_exactly_the_parsed_values(self):
        """action_refresh_status 寫進 DB 的就是 parser 的輸出（逐 payload 比對）。"""
        for payload in (_CONNECTED, _DOWN, _DELETED, _LEGACY_NO_DELETED_AT):
            with self.subTest(payload=payload):
                self.home.write(_STALE)
                expected = dict(_STALE)
                expected.update(self.SH._tunnel_vals_from_status(payload))
                with patch(_SH_CLIENT_FACTORY,
                           return_value=_client(return_value=payload)):
                    self.home.action_refresh_status()
                self.assertEqual(
                    {k: self.home[k] or '' for k in expected},
                    {k: v or '' for k, v in expected.items()})


@tagged('post_install', '-at_install')
class TestShDetailLiveTunnelStatus(HttpCase):
    """/woow detail ``get``：成員看得到即時值、不寫 DB、拿不到時沿用 DB 值。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.owner = owner = _new_user(self.env, f'sh_live_owner_{suffix}')
        self.member = _new_user(self.env, f'sh_live_user_{suffix}')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.workspace = Workspace.create(
            {'name': f'SH Live {suffix}', 'owner_id': owner.id})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.member.id, 'role': 'user'})
        self.home = sh_model(self.env).sudo().create(dict(
            _STALE, name='Live HA', workspace_id=self.workspace.id,
            state='active', tunnel_id='tun-sh-live',
            subdomain=f'paas-sm-live{suffix}-01020304'))

    def _get(self, home=None, action='get'):
        home = home or self.home
        resp = self.url_open(
            '/api/workspaces/%s/smarthomes/%s' % (self.workspace.id, home.id),
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': {'action': action}}),
            headers={'Content-Type': 'application/json'})
        self._last_body = resp.json()
        return self._last_body.get('result') or {}

    def test_provision_response_carries_live_overlay(self):
        """Retry setup 成功後前端直接收 provision 的回應（`state.home = result.data`），
        所以它也要疊即時值——否則 Tunnel Information 會從即時值倒退回 DB 的舊值，
        而收斂（`_converge_existing_tunnel`）本身從不寫 tunnel_status／connector_*。"""
        home = sh_model(self.env).sudo().create(dict(
            _STALE, name='Live retry', workspace_id=self.workspace.id,
            state='error', tunnel_id='tun-sh-live-retry',
            tunnel_name='paas-prod-sm-live-retry',
            subdomain='paas-sm-%s-0a1b2c3d' % self.workspace.ws_hash))
        self.authenticate(self.owner.login, self.owner.login)
        client = _client(return_value=_DOWN)
        with patch(_SH_CLIENT_FACTORY, return_value=client):
            res = self._get(home, action='provision')
        self.assertTrue(res.get('success'), self._last_body)
        client.sync_tunnel_routes.assert_called_once()
        data = res['data']
        self.assertEqual(data['state'], 'active')
        self.assertEqual((data['tunnel_status'], data['tunnel_uptime']),
                         ('disconnected', ''))
        self.assertTrue(data['tunnel_status_live'])
        self.assertIn('routes', data, 'provision 仍回 detail 形狀（#768）')
        home.invalidate_recordset()
        self.assertEqual(home.sudo().tunnel_status, 'connected',
                         'the overlay is read-only')

    def test_member_sees_live_disconnected_over_stale_db(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        data = res['data']
        self.assertEqual(data['tunnel_status'], 'disconnected')
        self.assertEqual(data['tunnel_uptime'], '', 'a dead connector has no "opened" time')
        self.assertTrue(data['tunnel_status_live'])
        # 生命週期 state 不受疊加影響（header 的 badge 仍讀它）。
        self.assertEqual(data['state'], 'active')

    def test_live_connected_values(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_CONNECTED)):
            data = self._get()['data']
        self.assertEqual(
            (data['tunnel_status'], data['connector_id'], data['tunnel_uptime']),
            ('connected', 'c-live', '2026-09-29T11:29:42Z'))

    def test_live_deleted_clears_uptime(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DELETED)):
            data = self._get()['data']
        self.assertEqual((data['tunnel_status'], data['connector_id'],
                          data['tunnel_uptime']), ('deleted', '', ''))

    def test_live_path_does_not_write_db(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            self._get()
        self.home.invalidate_recordset()
        self.assertEqual(self.home.sudo().tunnel_status, 'connected')
        self.assertEqual(self.home.sudo().tunnel_uptime, _STALE['tunnel_uptime'])

    def test_operator_failure_falls_back_to_db_values(self):
        self.authenticate(self.member.login, self.member.login)
        client = _client(side_effect=PaaSOperatorAPIError('boom', status_code=502))
        with patch(_SH_CLIENT_FACTORY, return_value=client):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(res['data']['tunnel_status'], 'connected')
        self.assertEqual(res['data']['connector_id'], 'c-stale')
        self.assertNotIn('tunnel_status_live', res['data'])
        # 失敗也不寫 DB（action_refresh_status 會寫 'error'，這條路徑不會）。
        self.home.invalidate_recordset()
        self.assertEqual(self.home.sudo().tunnel_status, 'connected')

    def test_no_operator_configured_falls_back(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_SH_CLIENT_FACTORY, return_value=None):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(res['data']['tunnel_uptime'], _STALE['tunnel_uptime'])
        self.assertNotIn('tunnel_status_live', res['data'])


@tagged('post_install', '-at_install')
class TestHaApiStatusLive(HttpCase):
    """HA machine API 的兩支 status 端點：只讀即時值，不再 action_refresh_status。

    呼叫者刻意用 role=user 的成員——舊行為下正是這個身分能經 sudo 觸發寫入，
    而 /woow 同一個動作要 owner/admin。
    """

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        owner = _new_user(self.env, f'ha_live_owner_{suffix}')
        self.member = _new_user(self.env, f'ha_live_user_{suffix}')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'HA Live {suffix}', 'owner_id': owner.id})
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': self.member.id, 'role': 'user'})
        self.home = sh_model(self.env).sudo().create(dict(
            _STALE, name='HA Live Home', workspace_id=self.workspace.id,
            state='active', tunnel_id='tun-ha-live',
            subdomain=f'paas-sm-halive{suffix}-01020304'))
        self.sa = self.env[_SA_MODEL].sudo().create(
            {'name': 'HA Live GW', 'workspace_id': self.workspace.id})
        _sa_internal(self.sa.sudo()).write({
            'state': 'active', 'tunnel_id': 'tun-ha-live-sa'})
        self.sa.sudo().write({
            'tunnel_status': 'connected', 'edge_connection_count': 4,
            'connector_id': 'c-stale', 'tunnel_uptime': '2026-09-25T04:09:25Z',
        })
        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    def _token(self, scope):
        return self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': f'ha-live-{uuid.uuid4().hex}',
            'scope': scope,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': self.member.id,
        })

    def _get(self, url, scope):
        token = self._token(scope)
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token.access_token}'})

    def _no_refresh(self, model):
        """把 action_refresh_status 換成會記錄呼叫的 mock（patch registry class）。"""
        return patch.object(type(self.env[model]), 'action_refresh_status',
                            side_effect=AssertionError('must not refresh'))

    def test_home_status_returns_live_without_refresh(self):
        with self._no_refresh(_SH_MODEL) as refresh, \
                patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/smarthome/homes/{self.home.id}/status', 'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(refresh.called)
        home = resp.json()['home']
        self.assertEqual((home['tunnel_status'], home['tunnel_uptime']),
                         ('disconnected', ''))
        # 回應形狀與改動前逐 key 相同：新 key 不外流給 HA component。
        self.assertEqual(set(home), set(self.home.sudo().to_dict()))
        self.home.invalidate_recordset()
        self.assertEqual(self.home.sudo().tunnel_status, 'connected',
                         'status endpoint must not write the DB')

    def test_home_status_operator_failure_returns_db_values(self):
        client = _client(side_effect=PaaSOperatorAPIError('boom', status_code=502))
        with self._no_refresh(_SH_MODEL) as refresh, \
                patch(_SH_CLIENT_FACTORY, return_value=client):
            resp = self._get(
                f'/api/smarthome/homes/{self.home.id}/status', 'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(refresh.called)
        self.assertEqual(resp.json()['home']['tunnel_status'], 'connected')
        self.home.invalidate_recordset()
        self.assertFalse(self.home.sudo().error_message,
                         'failure path must not write error_message either')

    def test_sa_status_returns_live_without_refresh(self):
        with self._no_refresh(_SA_MODEL) as refresh, \
                patch(_SA_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/security-access/accesses/{self.sa.id}/status',
                'security-access:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(refresh.called)
        access = resp.json()['access']
        self.assertEqual((access['tunnel_status'], access['edge_connection_count'],
                          access['tunnel_uptime']), ('disconnected', 0, ''))
        self.assertNotIn('tunnel_status_live', access)
        self.sa.invalidate_recordset()
        self.assertEqual(self.sa.sudo().edge_connection_count, 4,
                         'status endpoint must not write the DB')

    def test_existing_access_checks_still_apply(self):
        """scope 與 workspace 成員資格照舊把關（陽性見上面幾條）。"""
        resp = self._get(
            f'/api/smarthome/homes/{self.home.id}/status', 'workspace:read')
        self.assertEqual(resp.status_code, 403)
        outsider = _new_user(self.env, f'ha_live_out_{uuid.uuid4().hex[:6]}')
        token = self._token('smarthome:read')
        token.sudo().user_id = outsider.id
        resp = self.url_open(
            f'/api/smarthome/homes/{self.home.id}/status',
            headers={'Authorization': f'Bearer {token.access_token}'})
        self.assertEqual(resp.status_code, 403)

    # ---- list／detail 端點：status 改只讀後，stored 值不再被 HA 輪詢刷新 ----

    def test_list_homes_overlays_live_status_without_writing(self):
        with self._no_refresh(_SH_MODEL), \
                patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/smarthome/workspaces/{self.workspace.id}/homes',
                'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        homes = {h['id']: h for h in resp.json()['homes']}
        self.assertEqual(homes[self.home.id]['tunnel_status'], 'disconnected',
                         'stale stored "connected" must not leak to the HA list')
        self.assertEqual(set(homes[self.home.id]),
                         {'id', 'name', 'state', 'subdomain', 'tunnel_status'})
        self.home.invalidate_recordset()
        self.assertEqual(self.home.sudo().tunnel_status, 'connected',
                         'list endpoint must not write the DB')

    def test_list_homes_operator_failure_trips_breaker_and_falls_back(self):
        """operator 掛掉時：第一台拿不到就停，剩下的不再各吃一次逾時；全部退回 DB 值。"""
        second = sh_model(self.env).sudo().create(dict(
            _STALE, name='HA Live Home 2', workspace_id=self.workspace.id,
            state='active', tunnel_id='tun-ha-live-2',
            subdomain=f'paas-sm-halive2{uuid.uuid4().hex[:6]}-01020304'))
        client = _client(side_effect=PaaSOperatorAPIError('boom', status_code=502))
        with patch(_SH_CLIENT_FACTORY, return_value=client):
            resp = self._get(
                f'/api/smarthome/workspaces/{self.workspace.id}/homes',
                'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(client.get_tunnel_status.call_count, 1)
        statuses = {h['id']: h['tunnel_status'] for h in resp.json()['homes']}
        self.assertEqual(statuses[self.home.id], 'connected')
        self.assertEqual(statuses[second.id], 'connected')

    def test_list_homes_skips_operator_for_unprovisioned_home(self):
        """沒有 tunnel 的列不打 operator、也不觸發熔斷（否則會擋掉後面有 tunnel 的列）。"""
        pending = sh_model(self.env).sudo().create({
            'name': 'HA Pending', 'workspace_id': self.workspace.id})
        client = _client(return_value=_DOWN)
        with patch(_SH_CLIENT_FACTORY, return_value=client):
            resp = self._get(
                f'/api/smarthome/workspaces/{self.workspace.id}/homes',
                'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(client.get_tunnel_status.call_count, 1)
        statuses = {h['id']: h['tunnel_status'] for h in resp.json()['homes']}
        self.assertEqual(statuses[self.home.id], 'disconnected')
        self.assertEqual(statuses[pending.id], 'disconnected')

    def test_get_home_detail_overlays_live_status(self):
        with patch(_SH_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/smarthome/homes/{self.home.id}', 'smarthome:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        home = resp.json()['home']
        self.assertEqual((home['tunnel_status'], home['tunnel_uptime']),
                         ('disconnected', ''))
        self.assertEqual(set(home), set(self.home.sudo().to_dict()))

    def test_sa_list_overlays_live_status(self):
        with self._no_refresh(_SA_MODEL), \
                patch(_SA_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/security-access/workspaces/{self.workspace.id}/accesses',
                'security-access:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        accesses = {a['id']: a for a in resp.json()['accesses']}
        self.assertEqual(accesses[self.sa.id]['tunnel_status'], 'disconnected')
        self.sa.invalidate_recordset()
        self.assertEqual(self.sa.sudo().tunnel_status, 'connected',
                         'list endpoint must not write the DB')

    def test_sa_detail_overlays_live_status(self):
        with patch(_SA_CLIENT_FACTORY, return_value=_client(return_value=_DOWN)):
            resp = self._get(
                f'/api/security-access/accesses/{self.sa.id}',
                'security-access:read')
        self.assertEqual(resp.status_code, 200, resp.text)
        access = resp.json()['access']
        self.assertEqual((access['tunnel_status'], access['edge_connection_count']),
                         ('disconnected', 0))
        self.assertNotIn('tunnel_status_live', access)
