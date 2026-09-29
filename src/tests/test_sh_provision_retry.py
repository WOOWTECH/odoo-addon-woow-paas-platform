# -*- coding: utf-8 -*-
"""Smart Home 開通失敗後可以重試（在同一條 tunnel 上收斂，不重建）。

修前：``action_provision`` 只要 ``tunnel_id`` 已存在就一律 ``raise UserError``，而
#484 的 ``dns_failed`` 分支正好會留下「``error`` ＋ 活著的 tunnel」。詳情頁也沒有
Provision／Retry 鈕 ⇒ 租戶卡在 ``error`` 之後沒有任何自助出路，只能刪掉重建或等
人工補 DNS。SA 早就用「有 tunnel 就只重跑宣告式 route sync」解掉同一個形狀。

守的性質（每一條都對應 ``SmartHome.action_provision`` guard 的一格）：
* ``error``／``pending`` ＋ 既有 tunnel ⇒ 走 ``sync_tunnel_routes`` 收斂，
  ``create_tunnel`` **一次都不能再被呼叫**（那是一條新的計費孤兒）；
* 沒有 tunnel 的 ``pending`` ⇒ 照舊完整開通；
* ``active``（與 ``suspended``／``provisioning``）＋ tunnel ⇒ 照舊拒絕，而且不打
  operator——``suspended`` 若被放行，重試會把 503 的 ingress 推回 localhost；
  同一個理由，訂閱不在 ``in_progress`` 時 ``error`` 也不得收斂；
* 收斂失敗 ⇒ 維持 ``error`` ＋ ``error_message``，tunnel 錨點原封不動（下一次
  重試還走得到這一格）。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorConnectionError,
    PaaSOperatorError,
)
from .smart_home_fixture import sh_model  # #524：建立閘 ＋ 身分錨點閘

# 見 test_named_failure_fields_484：patch 使用者所在的 module namespace，不是定義處。
_SH_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')


def _client():
    client = MagicMock()
    client.create_tunnel.return_value = {
        'tunnel_id': 'tun-new',
        'tunnel_name': 'paas-prod-sm-new',
        'tunnel_token': 'tok-new',
        'dns_failed': False,
    }
    client.sync_tunnel_routes.return_value = {
        'tunnel_id': 'tun-retry', 'routes': [],
    }
    return client


class _RetryCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS SH retry'})

    def _home(self, **vals):
        """一台已經「開通到一半」的 SH：tunnel 與 subdomain 都在，state 由呼叫端給。

        tunnel 欄位是身分錨點，fixture 走 `sh_model`（連身分閘一起開）才擺得進去，
        否則 `_strip_identity_anchors` 會靜默把它們寫成空，測試跑在錯的前提上。
        """
        base = {
            'name': 'SH retry',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-retry',
            'tunnel_name': 'paas-prod-sm-retry',
            'tunnel_token': 'tok-retry',
            'subdomain': 'paas-sm-retry',
            'tunnel_route': 'https://paas-sm-retry.woowtech.io',
            'state': 'error',
            'error_message': 'Tunnel created but its DNS record could not be created',
        }
        base.update(vals)
        return sh_model(self.env).create(base)

    def _provision(self, home, client):
        with patch(_SH_CLIENT, return_value=client):
            return home.action_provision()


@tagged('post_install', '-at_install')
class TestShProvisionRetryConverges(_RetryCase):

    def test_retry_on_error_with_tunnel_converges_without_create_tunnel(self):
        home = self._home(ha_port=9123)
        client = _client()

        self._provision(home, client)

        client.create_tunnel.assert_not_called()
        client.sync_tunnel_routes.assert_called_once_with(
            'tun-retry',
            routes=[{
                'subdomain': home.subdomain,
                # 與 create_tunnel 當初烙進 ingress 的同一個值（含自訂 port）。
                'service_url': 'http://localhost:9123',
                'http_host_header': False,
            }],
            comment='paas-prod-sm-retry',
            tunnel_name='paas-prod-sm-retry',  # #531 歸屬主張
        )
        self.assertEqual(home.state, 'active')
        self.assertFalse(home.error_message)
        self.assertTrue(home.deployed_at)
        # 錨點一格都不能動
        self.assertEqual(home.tunnel_id, 'tun-retry')
        self.assertEqual(home.tunnel_name, 'paas-prod-sm-retry')
        self.assertEqual(home.sudo().tunnel_token, 'tok-retry')
        # #768：primary route 在收斂時也要存在（冪等補建）
        primary = self.env['woow_paas_platform.smart_home_route'].sudo().search([
            ('smart_home_id', '=', home.id), ('is_primary', '=', True)])
        self.assertEqual(len(primary), 1)

    def test_retry_is_idempotent_for_primary_route(self):
        """`dns_failed` 分支已經建過 primary route ⇒ 收斂時不得再長出第二條，
        而且中間夾一次失敗的重試也一樣。"""
        home = self._home()
        home._ensure_primary_route()  # 模擬 dns_failed 分支當下建好的那條
        failing = _client()
        failing.sync_tunnel_routes.side_effect = PaaSOperatorError(
            message='CF 500')
        self._provision(home, failing)
        self.assertEqual(home.state, 'error')

        self._provision(home, _client())

        self.assertEqual(home.state, 'active')
        primary = self.env['woow_paas_platform.smart_home_route'].sudo().search([
            ('smart_home_id', '=', home.id), ('is_primary', '=', True)])
        self.assertEqual(len(primary), 1)

    def test_retry_on_pending_with_tunnel_also_converges(self):
        home = self._home(state='pending', error_message=False)
        client = _client()

        self._provision(home, client)

        client.create_tunnel.assert_not_called()
        client.sync_tunnel_routes.assert_called_once()
        self.assertEqual(home.state, 'active')

    def test_dns_failed_then_retry_end_to_end(self):
        """真實路徑：第一次開通 dns_failed ⇒ error；重試 ⇒ 同一條 tunnel 收斂到 active。"""
        home = sh_model(self.env).create({
            'name': 'SH e2e', 'workspace_id': self.workspace.id})
        client = _client()
        client.create_tunnel.return_value = dict(
            client.create_tunnel.return_value, dns_failed=True)

        self._provision(home, client)
        self.assertEqual(home.state, 'error')
        self.assertEqual(home.tunnel_id, 'tun-new')
        self.assertIn('Retry setup', home.error_message,
                      'dns_failed 的訊息要指向新的自助出路')

        self._provision(home, client)

        self.assertEqual(client.create_tunnel.call_count, 1,
                         '重試又建了一條 tunnel＝第一條變成計費孤兒')
        self.assertEqual(client.sync_tunnel_routes.call_count, 1)
        self.assertEqual(client.sync_tunnel_routes.call_args.args[0], 'tun-new')
        self.assertEqual(home.state, 'active')
        self.assertEqual(home.tunnel_id, 'tun-new')

    def test_existing_deployed_at_is_not_overwritten(self):
        home = self._home(deployed_at='2026-09-01 00:00:00')
        self._provision(home, _client())
        self.assertEqual(str(home.deployed_at), '2026-09-01 00:00:00')


@tagged('post_install', '-at_install')
class TestShProvisionWithoutTunnelUnchanged(_RetryCase):

    def test_retry_on_pending_without_tunnel_does_full_provision(self):
        home = sh_model(self.env).create({
            'name': 'SH fresh', 'workspace_id': self.workspace.id})
        self.assertEqual(home.state, 'pending')
        client = _client()

        self._provision(home, client)

        client.create_tunnel.assert_called_once()
        client.sync_tunnel_routes.assert_not_called()
        self.assertEqual(home.state, 'active')
        self.assertEqual(home.tunnel_id, 'tun-new')


@tagged('post_install', '-at_install')
class TestShProvisionGuardStillRefuses(_RetryCase):

    def test_active_home_with_tunnel_still_refuses(self):
        home = self._home(state='active', error_message=False)
        client = _client()

        with self.assertRaises(UserError) as cm:
            self._provision(home, client)

        self.assertIn('already has a Cloudflare tunnel', str(cm.exception))
        client.create_tunnel.assert_not_called()
        client.sync_tunnel_routes.assert_not_called()
        self.assertEqual(home.state, 'active')
        self.assertEqual(home.tunnel_id, 'tun-retry')

    def test_suspended_and_provisioning_homes_also_refuse(self):
        """suspended：ingress 刻意是 503，重試推回 localhost 等於繞過停權。
        provisioning：另一個請求正在跑。兩者都要在打 operator 之前擋下。"""
        for state in ('suspended', 'provisioning'):
            with self.subTest(state=state):
                home = self._home(
                    state=state, error_message=False,
                    subdomain='paas-sm-refuse-%s' % state)
                client = _client()
                with self.assertRaises(UserError):
                    self._provision(home, client)
                client.create_tunnel.assert_not_called()
                client.sync_tunnel_routes.assert_not_called()
                self.assertEqual(home.state, state)

    def test_inactive_subscription_refuses_before_any_write(self):
        """`_do_suspend` 推 503 失敗會落 `error`（不是 `suspended`）⇒「error ＋ 訂閱
        已停」真的會出現；放行重試＝把 ingress 推回 localhost、繞過停權。"""
        home = self._home()
        client = _client()
        # patch registry 組出來的 class（type(env[model])），不是 models/ 裡的定義
        # class——後者不在 MRO 的最前面，patch 了也不會被呼叫到。
        with patch.object(type(self.env['woow_paas_platform.smart_home']),
                          '_is_subscription_active', return_value=False):
            with self.assertRaises(UserError):
                self._provision(home, client)

        client.sync_tunnel_routes.assert_not_called()
        client.create_tunnel.assert_not_called()
        self.assertEqual(home.state, 'error')

    def test_tunnel_without_subdomain_refuses_before_any_write(self):
        """沒有 stored hostname 就沒有 desired state；不得自己猜一個（＝替租戶換網址）。"""
        home = self._home(subdomain=False)
        client = _client()

        with self.assertRaises(UserError):
            self._provision(home, client)

        client.sync_tunnel_routes.assert_not_called()
        client.create_tunnel.assert_not_called()
        self.assertEqual(home.state, 'error', '被拒的呼叫不得把記錄推離原本的 state')


@tagged('post_install', '-at_install')
class TestShProvisionRetryFailureKeepsError(_RetryCase):

    def test_operator_error_keeps_error_and_tunnel(self):
        home = self._home()
        client = _client()
        client.sync_tunnel_routes.side_effect = PaaSOperatorError(
            message='DNS record already exists')

        self._provision(home, client)

        self.assertEqual(home.state, 'error')
        self.assertIn('DNS record already exists', home.error_message)
        self.assertEqual(home.tunnel_id, 'tun-retry',
                         '失敗時保留 tunnel_id，下一次重試才走得回收斂分支')
        client.create_tunnel.assert_not_called()

    def test_connection_error_keeps_error(self):
        home = self._home()
        client = _client()
        client.sync_tunnel_routes.side_effect = PaaSOperatorConnectionError(
            message='Connection refused')

        self._provision(home, client)

        self.assertEqual(home.state, 'error')
        self.assertIn('Cannot connect', home.error_message)
        self.assertEqual(home.tunnel_id, 'tun-retry')

    def test_unexpected_error_does_not_strand_provisioning(self):
        """非 operator 類例外也要落 error：controller 會把例外收成 200，
        記錄若停在 provisioning，詳情頁的重試鈕（只在 pending／error 出現）就消失了。"""
        home = self._home()
        client = _client()
        client.sync_tunnel_routes.side_effect = RuntimeError('boom')

        self._provision(home, client)

        self.assertEqual(home.state, 'error')
        self.assertIn('boom', home.error_message)
        self.assertEqual(home.tunnel_id, 'tun-retry')
