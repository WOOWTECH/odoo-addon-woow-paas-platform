"""SA 詳情頁的 tunnel 狀態改用即時值（只讀疊加，不寫 DB）。

2026-09-29 正式站實測：SA 的 tunnel 已經 0 connector 數小時，概覽頁仍顯示
09-25 寫入的「Connector 已連線、Edge 4」——``action_refresh_status`` 只在
``refresh_status`` action 被呼叫時才寫回，而詳情頁從不呼叫、也沒有排程。
同一頁的 route 狀態欄（#1234）卻顯示 Connector offline，兩者互相矛盾。

守的性質：
* ``get`` 回傳的 tunnel 欄位是 operator 的即時值，所有成員（含 role=user）都拿得到；
* 這條路徑**不寫 DB**（寫回要過 owner/admin 的 write ir.rule）；
* operator 拿不到時沿用 DB 值，不會讓整頁失敗；
* ``_tunnel_vals_from_status`` 與重構前的 ``action_refresh_status`` 判讀一致。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)
from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorAPIError

_CLIENT_FACTORY = ('odoo.addons.woow_paas_platform.models.security_access.'
                   'get_paas_operator_client')

_CONNECTED = {
    'tunnel_id': 't', 'status': 'healthy', 'deleted_at': None,
    'connections': [
        {'connector_id': 'c-live', 'type': '', 'client_version': '2026.7.3',
         'opened_at': '2026-09-29T11:29:42Z'},
        {'connector_id': 'c-live', 'type': ''},
    ],
}
_DOWN = {'tunnel_id': 't', 'status': 'down', 'deleted_at': None, 'connections': []}


@tagged('post_install', '-at_install')
class TestTunnelValsFromStatus(TransactionCase):
    """純判讀：與重構前 action_refresh_status 內嵌的邏輯逐格相同。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']

    # #1766：多了 `connector_count`（**機器數**，distinct client_id）與
    # `binding_conflict`。⚠ 別把它跟 `edge_connection_count`（**連線數**，一台
    # cloudflared 預設 4 條）看成同一件事——這裡剛好 2 條連線＝1 台。
    def test_connected(self):
        vals = self.SA._tunnel_vals_from_status(_CONNECTED)
        self.assertEqual(vals, {
            'tunnel_status': 'connected', 'edge_connection_count': 2,
            'connector_id': 'c-live', 'connector_type': '',
            'connector_version': '2026.7.3',
            'tunnel_uptime': '2026-09-29T11:29:42Z',
            'connector_count': 1, 'binding_conflict': False,
        })

    def test_disconnected(self):
        self.assertEqual(self.SA._tunnel_vals_from_status(_DOWN), {
            'tunnel_status': 'disconnected', 'edge_connection_count': 0,
            'connector_version': '',
            'connector_count': 0, 'binding_conflict': False,
        })

    def test_deleted_clears_connector_bookkeeping(self):
        vals = self.SA._tunnel_vals_from_status(
            dict(_CONNECTED, deleted_at='2026-09-29T00:00:00Z'))
        self.assertEqual(vals['tunnel_status'], 'deleted')
        self.assertEqual(vals['edge_connection_count'], 0)
        self.assertEqual(vals['connector_id'], '')


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestSaDetailLiveTunnelStatus(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        owner = _new_user(self.env, f'sa_live_owner_{suffix}')
        self.member = _new_user(self.env, f'sa_live_user_{suffix}')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.workspace = Workspace.create({'name': f'SA Live {suffix}', 'owner_id': owner.id})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.member.id, 'role': 'user'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'Live GW', 'workspace_id': self.workspace.id})
        _internal(self.sa.sudo()).write({
            'tunnel_id': 'ffffffff-0000-0000-0000-00000000live',
            'tunnel_name': 'paas-sa-test-live',
        })
        # DB 裡是「上一次寫入」的舊值——正式站當時的樣子。
        self.sa.sudo().write({
            'tunnel_status': 'connected', 'edge_connection_count': 4,
            'connector_id': 'c-stale', 'tunnel_uptime': '2026-09-25T04:09:25Z',
        })

    def _get(self):
        resp = self.url_open(
            '/api/workspaces/%s/security-accesses/%s' % (self.workspace.id, self.sa.id),
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': {'action': 'get'}}),
            headers={'Content-Type': 'application/json'})
        self._last_body = resp.json()
        return self._last_body.get('result') or {}

    def _client(self, **kw):
        client = MagicMock()
        client.get_tunnel_status = MagicMock(**kw)
        return client

    def test_member_sees_live_disconnected_over_stale_db(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=self._client(return_value=_DOWN)):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        data = res['data']
        self.assertEqual(data['tunnel_status'], 'disconnected')
        self.assertEqual(data['edge_connection_count'], 0)
        self.assertEqual(data['tunnel_uptime'], '', 'a dead connector has no "opened" time')
        self.assertTrue(data['tunnel_status_live'])

    def test_live_path_does_not_write_db(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=self._client(return_value=_DOWN)):
            self._get()
        self.sa.invalidate_recordset()
        self.assertEqual(self.sa.sudo().tunnel_status, 'connected')
        self.assertEqual(self.sa.sudo().edge_connection_count, 4)

    def test_live_connected_values(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=self._client(return_value=_CONNECTED)):
            data = self._get()['data']
        self.assertEqual((data['tunnel_status'], data['edge_connection_count'],
                          data['connector_id']), ('connected', 2, 'c-live'))

    def test_operator_failure_falls_back_to_db_values(self):
        self.authenticate(self.member.login, self.member.login)
        client = self._client(side_effect=PaaSOperatorAPIError('boom', status_code=502))
        with patch(_CLIENT_FACTORY, return_value=client):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(res['data']['tunnel_status'], 'connected')
        self.assertNotIn('tunnel_status_live', res['data'])

    def test_no_operator_configured_falls_back(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=None):
            res = self._get()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(res['data']['edge_connection_count'], 4)
