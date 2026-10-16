# -*- coding: utf-8 -*-
"""#1766：一條 tunnel 只給一台 Home Assistant 接。

## 事故（2026-10-17 正式站實測）

同一個 PaaS 帳號在兩台 HA（192.168.2.34、192.168.2.121）各跑一次設定流程、都選了
同一個家 `woowtech-ha`（id=1）。tunnel-token 端點當時是**無狀態**的，於是兩台都拿
到同一份權杖、各自起了 cloudflared。

Cloudflare 對「一條 tunnel、多個 connector」的語意是**分流**（那是給零停機換版用
的），不是拒絕 ⇒ 對外網址輪流落到其中一台。使用者的症狀是「遠端登入時好時壞」：
請求打到沒有他帳號的那台機器。

operator 實測數據（事故當下）：

| 家 | tunnel 連線數 | 實際機器數 |
| --- | --- | --- |
| id=1 `woowtech-ha` | 8 | 2（各 4 條） |
| id=2 `elmohome` | 4 | 1 |

平台端完全看不到：`_tunnel_vals_from_status` 只讀 `connections[0]`，兩台接著時一樣
回 `connected` ＋ 一個 connector。

## 這個檔釘住的三件事

1. **發權杖前先 claim**：第一台帶 `instance_uid` 來的會被記住；其他 uid 來要同一筆
   資源 ⇒ 409 `already_bound`，**而且回應裡沒有權杖**。
2. **legacy 單向升級**：沒帶 uid 的舊元件照發（否則既有安裝會在下次重啟集體斷
   線），但一旦有自報身分的實例 claim 了，不帶 uid 的請求就會被擋——不然舊版元件
   就是繞過綁定的後門。
3. **機器數要數 distinct client_id**：`connector_id` 是每條連線一個（cloudflared
   預設 4 條），拿它當機器數會把正常的一台看成四台。

SH 與 SA 兩側都測：它們是鏡像端點，只收一側等於把攻擊面搬家
（`two-layer-acl-lockstep`）。
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal,
)
from odoo.addons.woow_paas_platform.models.tunnel_binding_mixin import (
    TunnelBindingConflict,
)

from .smart_home_fixture import sh_model

_SCOPES = ('workspace:read smarthome:read smarthome:tunnel '
           'security-access:read security-access:tunnel')
UID_A = 'a' * 32
UID_B = 'b' * 32


class _BindingFixture:
    """owner ＋ workspace ＋ 一個已開通的 SH／SA。"""

    def _setup_fixture(self):
        import uuid
        suffix = uuid.uuid4().hex[:6]
        self.owner = self.env['res.users'].sudo().create({
            'name': f'Binding Owner {suffix}',
            'login': f'binding_owner_{suffix}',
            'email': f'binding_owner_{suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'Binding WS {suffix}', 'owner_id': self.owner.id,
        })
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        self.home = sh_model(self.env).sudo().create({
            'name': 'Binding HA',
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-binding-sh',
            'tunnel_token': 'tok-binding-sh',
            'subdomain': 'paas-sm-binding1-02030405',
            'tunnel_status': 'connected',
        })
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': f'Binding Gateway {suffix}', 'workspace_id': self.workspace.id,
        })
        _sa_internal(self.sa.sudo()).write({
            'state': 'active', 'tunnel_id': 'tun-binding-sa',
            'tunnel_token': 'tok-binding-sa',
        })


@tagged('post_install', '-at_install')
class TestTunnelBindingModel(_BindingFixture, TransactionCase):
    """claim / release 的狀態機（model 層）。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def test_new_record_starts_unbound(self):
        self.assertEqual(self.home.binding_state, 'unbound')
        self.assertEqual(self.sa.binding_state, 'unbound')
        self.assertFalse(self.home.bound_instance_uid)
        self.assertEqual(self.home.connector_count, 0)
        self.assertFalse(self.home.binding_conflict)

    def test_first_claim_wins_and_is_idempotent(self):
        self.assertEqual(self.home._tunnel_binding_claim(UID_A, '家 (pi)'), 'claimed')
        self.assertEqual(self.home.bound_instance_uid, UID_A)
        self.assertEqual(self.home.bound_instance_label, '家 (pi)')
        bound_at = self.home.bound_at
        self.assertTrue(bound_at)
        # 同一台重啟／reload 會再要一次權杖——必須放行，而且不改綁定時間
        self.assertEqual(self.home._tunnel_binding_claim(UID_A, '家 (pi)'), 'claimed')
        self.assertEqual(self.home.bound_at, bound_at)

    def test_same_instance_renaming_itself_updates_the_label_only(self):
        self.home._tunnel_binding_claim(UID_A, '家 (pi)')
        bound_at = self.home.bound_at
        self.home._tunnel_binding_claim(UID_A, '一樓 (pi)')
        self.assertEqual(self.home.bound_instance_label, '一樓 (pi)')
        self.assertEqual(self.home.bound_instance_uid, UID_A)
        self.assertEqual(self.home.bound_at, bound_at)

    def test_second_instance_is_refused(self):
        self.home._tunnel_binding_claim(UID_A, '家 (pi)')
        with self.assertRaises(TunnelBindingConflict) as caught:
            self.home._tunnel_binding_claim(UID_B, '展示機 (sho)')
        self.assertEqual(caught.exception.bound_to, '家 (pi)')
        self.assertTrue(caught.exception.bound_at)
        # 綁定沒有被第二台改掉
        self.assertEqual(self.home.bound_instance_uid, UID_A)

    def test_guard_holds_for_superuser_too(self):
        """⚠ TransactionCase 本來就是 su env。guard 若用 env.su 當豁免條件，上面
        那條測試會**假綠**（deny 分支根本走不到）。這條把「連 superuser 也擋」講
        明白，避免之後有人用 su 豁免「修」這個 guard。"""
        self.home._tunnel_binding_claim(UID_A, '家 (pi)')
        with self.assertRaises(TunnelBindingConflict):
            self.home.sudo()._tunnel_binding_claim(UID_B, '展示機 (sho)')

    def test_legacy_caller_is_allowed_but_marked(self):
        self.assertEqual(self.home._tunnel_binding_claim('', ''), 'legacy')
        self.assertEqual(self.home.binding_state, 'legacy')
        self.assertFalse(self.home.bound_instance_uid)

    def test_legacy_upgrades_to_claimed_once_somebody_identifies_itself(self):
        self.home._tunnel_binding_claim('', '')
        self.assertEqual(self.home._tunnel_binding_claim(UID_A, '家 (pi)'), 'claimed')
        self.assertEqual(self.home.bound_instance_uid, UID_A)

    def test_claimed_record_refuses_an_anonymous_caller(self):
        """舊版元件不能變成繞過綁定的後門。"""
        self.home._tunnel_binding_claim(UID_A, '家 (pi)')
        with self.assertRaises(TunnelBindingConflict):
            self.home._tunnel_binding_claim('', '')

    def test_release_lets_the_next_machine_take_over(self):
        self.home._tunnel_binding_claim(UID_A, '家 (pi)')
        self.home.action_release_binding()
        self.assertEqual(self.home.binding_state, 'unbound')
        self.assertFalse(self.home.bound_instance_uid)
        self.assertFalse(self.home.bound_at)
        self.assertEqual(self.home._tunnel_binding_claim(UID_B, '展示機 (sho)'),
                         'claimed')

    def test_security_access_has_the_same_state_machine(self):
        """鏡像端點不可只收一側（two-layer-acl-lockstep）。"""
        self.assertEqual(self.sa._tunnel_binding_claim(UID_A, 'sa-host'), 'claimed')
        with self.assertRaises(TunnelBindingConflict):
            self.sa._tunnel_binding_claim(UID_B, 'other-host')
        self.sa.action_release_binding()
        self.assertEqual(self.sa._tunnel_binding_claim(UID_B, 'other-host'), 'claimed')


@tagged('post_install', '-at_install')
class TestConnectorCounting(_BindingFixture, TransactionCase):
    """機器數＝distinct client_id，不是連線數。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _conns(self, *client_ids):
        """每個 client 開 4 條連線，每條連線一個自己的 connector_id（CF 的形狀）。"""
        out = []
        for client in client_ids:
            for index in range(4):
                out.append({
                    'connector_id': f'{client}-conn-{index}',
                    'client_id': client,
                    'type': 'quic',
                    'origin_ip': '118.169.77.143',
                    'opened_at': '2026-10-17T01:00:0%sZ' % index,
                })
        return out

    def test_one_machine_four_connections_is_not_a_conflict(self):
        vals = self.home._tunnel_vals_from_status(
            {'connections': self._conns('client-a')})
        self.assertEqual(vals['connector_count'], 1)
        self.assertFalse(vals['binding_conflict'])
        self.assertEqual(vals['tunnel_status'], 'connected')

    def test_two_machines_raise_the_conflict_flag(self):
        vals = self.home._tunnel_vals_from_status(
            {'connections': self._conns('client-a', 'client-b')})
        self.assertEqual(vals['connector_count'], 2)
        self.assertTrue(vals['binding_conflict'])
        # ⚠ 仍然是 connected——這正是事故當下「平台看起來一切正常」的原因，
        # 所以衝突必須靠自己的旗標表達，不能期待 tunnel_status 改變。
        self.assertEqual(vals['tunnel_status'], 'connected')

    def test_no_connections_clears_the_flag(self):
        vals = self.home._tunnel_vals_from_status({'connections': []})
        self.assertEqual(vals['connector_count'], 0)
        self.assertFalse(vals['binding_conflict'])
        self.assertEqual(vals['tunnel_status'], 'disconnected')

    def test_deleted_tunnel_clears_the_count(self):
        """#833 的同一個理由：不要留下「已刪除 ＋ 2 台接著」的混合世代。"""
        vals = self.home._tunnel_vals_from_status(
            {'connections': self._conns('client-a', 'client-b'),
             'deleted_at': '2026-10-17T02:00:00Z'})
        self.assertEqual(vals['tunnel_status'], 'deleted')
        self.assertEqual(vals['connector_count'], 0)
        self.assertFalse(vals['binding_conflict'])

    def test_old_operator_without_client_id_falls_back_to_connection_count(self):
        """client_id 是 2026-10-17 才加進 operator 的欄位；舊 operator 回不出來。

        退路是「連線數 / 4」近似值——寧可粗糙也不要把「數不出來」當成「只有一
        台」，否則升版順序一顛倒（Odoo 先上、operator 後上）就會靜默失去偵測。
        """
        conns = [{'connector_id': f'c{i}', 'type': 'quic'} for i in range(8)]
        vals = self.home._tunnel_vals_from_status({'connections': conns})
        self.assertEqual(vals['connector_count'], 2)
        self.assertTrue(vals['binding_conflict'])

    def test_security_access_counts_machines_separately_from_connections(self):
        vals = self.sa._tunnel_vals_from_status(
            {'connections': self._conns('client-a')})
        self.assertEqual(vals['edge_connection_count'], 4)  # 連線數
        self.assertEqual(vals['connector_count'], 1)        # 機器數
        self.assertFalse(vals['binding_conflict'])


@tagged('post_install', '-at_install')
class TestTunnelTokenEndpointBinding(_BindingFixture, HttpCase):
    """端點層：第二台拿不到權杖（真 HTTP）。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    def _get(self, url):
        token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': f'ha-binding-{self.owner.id}-{len(url)}',
            'scope': _SCOPES,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': self.owner.id,
        })
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token.access_token}'})

    def _sh_url(self, uid=None, label=None):
        url = f'/api/smarthome/homes/{self.home.id}/tunnel-token'
        if uid is not None:
            url += f'?instance_uid={uid}'
            if label:
                url += f'&instance_label={label}'
        return url

    def _sa_url(self, uid=None):
        url = f'/api/security-access/accesses/{self.sa.id}/tunnel-token'
        if uid is not None:
            url += f'?instance_uid={uid}'
        return url

    def test_first_machine_gets_the_token_and_is_recorded(self):
        resp = self._get(self._sh_url(UID_A, 'pi'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['tunnel_token'], 'tok-binding-sh')
        self.home.invalidate_recordset()
        self.assertEqual(self.home.binding_state, 'claimed')
        self.assertEqual(self.home.bound_instance_uid, UID_A)

    def test_second_machine_gets_409_and_no_token(self):
        self.assertEqual(self._get(self._sh_url(UID_A, 'pi')).status_code, 200)
        resp = self._get(self._sh_url(UID_B, 'sho'))
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body['error'], 'already_bound')
        self.assertEqual(body['bound_to'], 'pi')
        self.assertTrue(body['bound_at'])
        # 關鍵：權杖不可以出現在回應裡的任何地方
        self.assertNotIn('tok-binding-sh', resp.text)

    def test_same_machine_can_ask_again(self):
        self.assertEqual(self._get(self._sh_url(UID_A, 'pi')).status_code, 200)
        self.assertEqual(self._get(self._sh_url(UID_A, 'pi')).status_code, 200)

    def test_old_component_without_identity_still_works(self):
        resp = self._get(self._sh_url())
        self.assertEqual(resp.status_code, 200)
        self.home.invalidate_recordset()
        self.assertEqual(self.home.binding_state, 'legacy')

    def test_security_access_endpoint_is_gated_the_same_way(self):
        self.assertEqual(self._get(self._sa_url(UID_A)).status_code, 200)
        resp = self._get(self._sa_url(UID_B))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['error'], 'already_bound')
        self.assertNotIn('tok-binding-sa', resp.text)

    def test_release_then_the_other_machine_can_bind(self):
        self.assertEqual(self._get(self._sh_url(UID_A, 'pi')).status_code, 200)
        self.home.action_release_binding()
        self.assertEqual(self._get(self._sh_url(UID_B, 'sho')).status_code, 200)
