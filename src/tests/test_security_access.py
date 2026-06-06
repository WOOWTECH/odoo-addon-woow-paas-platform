from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestSecurityAccess(TransactionCase):
    """woow_paas_platform.security_access model behaviour."""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.workspace = self.Workspace.create({'name': 'SA Test WS'})

    def _make_sa(self, name='My Gateway'):
        return self.SecurityAccess.create({
            'name': name,
            'workspace_id': self.workspace.id,
        })

    # ---- identity / naming ----

    def test_canonical_name_set_once_on_create(self):
        sa = self._make_sa()
        self.assertTrue(sa.canonical_name.startswith('paas-sa-'))
        self.assertTrue(sa.reference_id)

    def test_canonical_name_stable_after_nickname_change(self):
        """nickname 可改，canonical_name 不得漂移（hash salt 含 name）。"""
        sa = self._make_sa()
        original = sa.canonical_name
        sa.write({'name': 'Renamed Gateway'})
        self.assertEqual(sa.canonical_name, original)

    def test_canonical_name_write_guard_fires_under_su(self):
        """write() guard 對 su env 也生效（無 env.su 豁免，否則 su-env 測試假綠）。

        TransactionCase 預設就是 su env；若 guard 仍給 su 豁免，這條會假綠
        （沒打到 guard 分支）。改用 context escape hatch 後，su 也須被擋。
        """
        sa = self._make_sa()
        with self.assertRaises(UserError):
            sa.write({'canonical_name': 'paas-sa-hijack-su'})
        with self.assertRaises(UserError):
            sa.write({'reference_id': 'hijack-ref-su'})

    def test_canonical_name_injection_stripped_under_su(self):
        """su env 直接 create 注入 canonical_name 也須被剝離（改自動生成）。"""
        injected = 'paas-sa-victim-su'
        sa = self.SecurityAccess.create({
            'name': 'SU Injected GW',
            'workspace_id': self.workspace.id,
            'canonical_name': injected,
        })
        self.assertNotEqual(sa.canonical_name, injected)
        self.assertTrue(sa.canonical_name.startswith('paas-sa-'))

    def test_primary_hostname_combines_domain(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'stg.woowtech.io')
        sa = self._make_sa()
        self.assertEqual(
            sa.primary_hostname, f"{sa.canonical_name}.stg.woowtech.io")

    # ---- provision ----

    @patch(MOCK_GET_CLIENT)
    def test_provision_creates_tunnel_and_primary_route(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-sa-1',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'paas-sa-x-y',
            'dns_record_id': 'rec-primary',
        }
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertEqual(sa.tunnel_id, 'tun-sa-1')
        # tunnel name = canonical（反推性需求）
        create_kwargs = mock_client.create_tunnel.call_args.kwargs
        self.assertEqual(create_kwargs['name'], sa.canonical_name)
        # primary route 自動建立、prefix = canonical、不可刪
        primary = sa.route_ids.filtered('is_primary')
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary.subdomain_prefix, sa.canonical_name)
        # provision 只對 operator sync 一次（sa_skip_route_sync 略過
        # route-create override 的多餘 sync；只留帶 http_host_header 的顯式 sync）
        self.assertEqual(mock_client.sync_tunnel_routes.call_count, 1)

    @patch(MOCK_GET_CLIENT)
    def test_provision_error_sets_error_state(self, mock_get_client):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mock_client = MagicMock()
        mock_client.create_tunnel.side_effect = PaaSOperatorError('boom')
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.action_provision()
        self.assertEqual(sa.state, 'error')
        self.assertIn('boom', sa.error_message)

    @patch(MOCK_GET_CLIENT)
    def test_provision_retry_after_tunnel_exists_does_not_recreate(
            self, mock_get_client):
        """Idempotency guard: re-provision with a live tunnel_id must only
        re-sync routes, never call create_tunnel again (else orphan CF tunnel).
        """
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-sa-1',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'paas-sa-x-y',
            'dns_record_id': 'rec-primary',
        }
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        # First provision succeeds end-to-end.
        sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertEqual(mock_client.create_tunnel.call_count, 1)
        original_tunnel = sa.tunnel_id

        # Simulate a stale 'error' state on the same record, then retry.
        sa.write({'state': 'error'})
        mock_client.create_tunnel.reset_mock()
        sa.action_provision()
        # No second tunnel created; tunnel_id unchanged; back to active.
        mock_client.create_tunnel.assert_not_called()
        self.assertEqual(sa.tunnel_id, original_tunnel)
        self.assertEqual(sa.state, 'active')

    @patch(MOCK_GET_CLIENT)
    def test_provision_resync_failure_keeps_tunnel_for_retry(
            self, mock_get_client):
        """Stage-2 re-sync failure must land in 'error' WITHOUT orphaning:
        tunnel_id stays so the next retry finishes via the idempotent path."""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-sa-2',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'paas-sa-x-y',
            'dns_record_id': 'rec-primary',
        }
        mock_client.sync_tunnel_routes.side_effect = PaaSOperatorError('cf 503')
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.action_provision()
        self.assertEqual(sa.state, 'error')
        # tunnel was created and its id retained (no orphan, retry-able)
        self.assertEqual(sa.tunnel_id, 'tun-sa-2')
        # retry succeeds without recreating the tunnel
        mock_client.sync_tunnel_routes.side_effect = None
        mock_client.create_tunnel.reset_mock()
        sa.action_provision()
        mock_client.create_tunnel.assert_not_called()
        self.assertEqual(sa.state, 'active')

    @patch(MOCK_GET_CLIENT)
    def test_provision_stage2_non_operator_error_fails_loud(
            self, mock_get_client):
        """Stage-2 非 operator 例外（route create UNIQUE 撞庫）也須 fail-loud
        到 'error'，不得留在假性 'active' 而 CF 有 live tunnel、DB 無對齊。

        重現：先用 sa1 佔走 canonical_name 對應的 primary prefix，再 provision
        sa2 但讓它算出同一個 canonical_name → primary route create 撞 UNIQUE →
        非 PaaSOperatorError 例外。修前窄 except 會放行例外、state 卡在
        'active'；修後 broad except 寫 'error'。
        """
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-stage2',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'paas-sa-x-y',
            'dns_record_id': 'rec-primary',
        }
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        # Squat the SA's own canonical_name as a route prefix so the primary
        # route create inside action_provision hits the UNIQUE constraint.
        self.Route.with_context(sa_skip_route_sync=True).create({
            'security_access_id': sa.id,
            'subdomain_prefix': sa.canonical_name,
            'is_primary': True,
        })
        # Drop the primary so the re-sync self-heal path isn't the one tested;
        # we want the FIRST-provision branch's UNIQUE clash. Re-create a
        # NON-primary squatter on the canonical prefix on a sibling SA.
        sa.route_ids.with_context(sa_force_delete_primary=True).unlink()
        sibling = self.SecurityAccess.create({
            'name': 'Squatter', 'workspace_id': self.workspace.id,
        })
        self.Route.with_context(sa_skip_route_sync=True).create({
            'security_access_id': sibling.id,
            'subdomain_prefix': sa.canonical_name,
            'is_primary': True,
        })
        sa.action_provision()
        # tunnel was created (id stored) but primary route create clashed →
        # must NOT remain 'active'; must fail loud to 'error'.
        self.assertEqual(sa.state, 'error')
        self.assertTrue(sa.error_message)
        self.assertEqual(sa.tunnel_id, 'tun-stage2')

    @patch(MOCK_GET_CLIENT)
    def test_provision_retry_rebuilds_missing_primary_route(
            self, mock_get_client):
        """active+tunnel_id 但無 primary route 的不可自癒態：retry 須補建 primary。

        模擬 stage-2 失敗後落入「tunnel 已存、primary route 沒建成」的狀態，
        再次 provision（走 re-sync 分支）必須偵測並補建 primary route，而非
        推一份只有 catch-all 404 的 ingress。
        """
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        # Simulate the broken state: live tunnel, no primary route.
        sa.write({'state': 'error', 'tunnel_id': 'tun-orphan'})
        self.assertFalse(sa.route_ids.filtered('is_primary'))
        sa.action_provision()
        # primary route rebuilt from canonical_name + sync pushed it.
        primary = sa.route_ids.filtered('is_primary')
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary.subdomain_prefix, sa.canonical_name)
        self.assertEqual(sa.state, 'active')
        sync_kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        subs = [r['subdomain'] for r in sync_kwargs['routes']]
        self.assertIn(sa.canonical_name, subs)

    # ---- refresh status ----

    @patch(MOCK_GET_CLIENT)
    def test_refresh_status_connected(self, mock_get_client):
        """connections 非空 → connected + 解析 connector_*/uptime（鏡像 SmartHome）。"""
        mock_client = MagicMock()
        mock_client.get_tunnel_status.return_value = {
            'connections': [{
                'connector_id': 'conn-123',
                'type': 'cloudflared',
                'opened_at': '2026-06-06T08:30:00Z',
            }],
        }
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.write({'state': 'active', 'tunnel_id': 'tun-refresh'})
        sa.action_refresh_status()
        self.assertEqual(sa.tunnel_status, 'connected')
        self.assertEqual(sa.connector_id, 'conn-123')
        self.assertEqual(sa.connector_type, 'cloudflared')
        self.assertEqual(sa.tunnel_uptime, '2026-06-06T08:30:00Z')
        mock_client.get_tunnel_status.assert_called_once_with('tun-refresh')

    @patch(MOCK_GET_CLIENT)
    def test_refresh_status_disconnected(self, mock_get_client):
        """connections 空 → disconnected。"""
        mock_client = MagicMock()
        mock_client.get_tunnel_status.return_value = {'connections': []}
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.write({'state': 'active', 'tunnel_id': 'tun-disc'})
        sa.action_refresh_status()
        self.assertEqual(sa.tunnel_status, 'disconnected')

    @patch(MOCK_GET_CLIENT)
    def test_refresh_status_operator_error_sets_error_status(
            self, mock_get_client):
        """get_tunnel_status 拋 PaaSOperatorError → tunnel_status='error'。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mock_client = MagicMock()
        mock_client.get_tunnel_status.side_effect = PaaSOperatorError('cf down')
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.write({'state': 'active', 'tunnel_id': 'tun-err'})
        sa.action_refresh_status()
        self.assertEqual(sa.tunnel_status, 'error')
        self.assertIn('cf down', sa.error_message)

    def test_refresh_status_noop_without_tunnel(self):
        """無 tunnel_id → 直接 return，不呼叫 operator。"""
        sa = self._make_sa()
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            sa.action_refresh_status()
            mock_get_client.assert_not_called()

    # ---- routes ----

    def _make_active_sa(self):
        sa = self._make_sa()
        sa.write({'state': 'active', 'tunnel_id': 'tun-sa-1'})
        # route create 會觸發 operator sync（sa 已 active）→ 必須 mock
        with patch(MOCK_GET_CLIENT) as mock_gc:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_gc.return_value = mock_client
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True,
            })
        return sa

    @patch(MOCK_GET_CLIENT)
    def test_add_route_syncs_to_operator(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {
            'tunnel_id': 'tun-sa-1',
            'routes': [
                {'subdomain': 'x', 'hostname': 'x.io', 'dns_record_id': 'r1'},
            ],
        }
        mock_get_client.return_value = mock_client
        sa = self._make_active_sa()
        self.Route.create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'waltek-ha',
        })
        sync_kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        # 宣告式整份：primary + 新 route 都在 payload
        subs = [r['subdomain'] for r in sync_kwargs['routes']]
        self.assertIn(sa.canonical_name, subs)
        self.assertIn('waltek-ha', subs)
        self.assertEqual(sync_kwargs['comment'], sa.canonical_name)

    def test_route_prefix_validation(self):
        sa = self._make_active_sa()
        for bad in ['-bad', 'bad-', 'Bad', 'a b', 'paas-evil', 'svc-evil', 'ab']:
            with self.assertRaises(ValidationError, msg=bad):
                with self.env.cr.savepoint():
                    self.Route.create({
                        'security_access_id': sa.id,
                        'subdomain_prefix': bad,
                    })

    def test_route_prefix_unique_across_platform(self):
        sa = self._make_active_sa()
        sa2 = self.SecurityAccess.create({
            'name': 'Other SA',
            'workspace_id': self.workspace.id,
        })
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_get_client.return_value = MagicMock()
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'taken-prefix',
            })
            with self.assertRaises(Exception):  # psycopg2 UniqueViolation
                with self.env.cr.savepoint():
                    self.Route.create({
                        'security_access_id': sa2.id,
                        'subdomain_prefix': 'taken-prefix',
                    })

    def test_primary_route_cannot_be_deleted(self):
        sa = self._make_active_sa()
        primary = sa.route_ids.filtered('is_primary')
        with self.assertRaises(UserError):
            primary.unlink()

    def test_route_prefix_immutable(self):
        sa = self._make_active_sa()
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_get_client.return_value = MagicMock()
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'fixed-prefix',
            })
            with self.assertRaises(UserError):
                route.write({'subdomain_prefix': 'renamed'})

    def test_route_create_sync_failure_rolls_back_db_row(self):
        """create sync 失敗 → 整筆 route 不落 DB（無幽靈 route）。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        sa = self._make_active_sa()
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.side_effect = PaaSOperatorError('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            with self.assertRaises(PaaSOperatorError):
                self.Route.create({
                    'security_access_id': sa.id,
                    'subdomain_prefix': 'ghost-route',
                })
        # DB row must NOT survive the failed sync.
        self.assertFalse(self.Route.search(
            [('subdomain_prefix', '=', 'ghost-route')]))

    def test_route_unlink_sync_failure_rolls_back_keeps_row(self):
        """unlink sync 失敗（最嚴重方向）→ DB row 保留，不留對外服務孤兒。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        sa = self._make_active_sa()
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'doomed-route',
            })
            route_id = route.id
            mock_client.sync_tunnel_routes.side_effect = PaaSOperatorError(
                'cf down')
            with self.assertRaises(PaaSOperatorError):
                route.unlink()
        # Row must survive — CF teardown failed, so the DB must still reference
        # it so a later sync can finish (no public orphan stranded).
        self.assertTrue(self.Route.browse(route_id).exists())

    def test_route_write_sync_failure_rolls_back_service_url(self):
        """write sync 失敗 → service_url 回滾，DB 與 CF 不分歧。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        sa = self._make_active_sa()
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'rewrite-route',
                'service_url': 'http://localhost:80',
            })
            mock_client.sync_tunnel_routes.side_effect = PaaSOperatorError(
                'cf down')
            with self.assertRaises(PaaSOperatorError):
                route.write({'service_url': 'http://localhost:9999'})
        self.assertEqual(route.service_url, 'http://localhost:80')


class TestSecurityAccessLifecycleHooks(TransactionCase):
    """_do_suspend / _do_resume / _do_cleanup / unlink 清理鏈."""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SA LC WS'})

    def _make_active_sa_with_routes(self, mock_client):
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SecurityAccess.create({
                'name': 'LC Gateway',
                'workspace_id': self.workspace.id,
            })
            sa.write({'state': 'active', 'tunnel_id': 'tun-lc'})
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True,
            })
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'lc-custom',
                'service_url': 'http://localhost:80',
            })
        return sa

    def _sync_mock(self):
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        return mock_client

    def test_suspend_overrides_all_routes_to_503(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.sync_tunnel_routes.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
        self.assertEqual(sa.state, 'suspended')
        self.assertEqual(sa.pre_suspend_state, 'active')
        kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        # 全部 route（含 primary）都 503；service_url 原值保留在 DB
        self.assertTrue(all(
            r['service_url'] == 'http_status:503' for r in kwargs['routes']))
        self.assertEqual(len(kwargs['routes']), 2)
        self.assertEqual(
            sa.route_ids.filtered(lambda r: not r.is_primary).service_url,
            'http://localhost:80')

    def test_suspend_idempotent(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            calls_after_first = mock_client.sync_tunnel_routes.call_count
            sa._do_suspend()  # second call must early-return
        self.assertEqual(
            mock_client.sync_tunnel_routes.call_count, calls_after_first)

    def test_suspend_failure_sets_error_no_silent_retry(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.sync_tunnel_routes.side_effect = Exception('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
        self.assertEqual(sa.state, 'error')

    def test_resume_restores_service_urls(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            mock_client.sync_tunnel_routes.reset_mock()
            sa._do_resume()
        self.assertEqual(sa.state, 'active')
        kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        urls = {r['subdomain']: r['service_url'] for r in kwargs['routes']}
        self.assertEqual(urls['lc-custom'], 'http://localhost:80')
        self.assertNotIn('http_status:503', urls.values())

    def test_resume_from_error_heals_to_active(self):
        """在 'error' 態被 suspend（pre_suspend_state='error'），resume 成功
        re-sync 後 tunnel 已健康 → 須還原成 'active' 並清空舊 error_message，
        不得永久卡 'error'（finding：tunnel 正常但 state 誤導 error）。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        sa.write({'state': 'error', 'error_message': 'stale pre-suspend error'})
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            self.assertEqual(sa.pre_suspend_state, 'error')
            sa._do_resume()
        self.assertEqual(sa.state, 'active')
        self.assertFalse(sa.error_message)

    def test_resume_without_tunnel_restores_pending_not_active(self):
        """從未 provision（無 tunnel_id）的 SA 被 suspend→resume，不得標成
        'active'（會顯示「運作中」卻無 tunnel），須還原成 'pending'。"""
        mock_client = self._sync_mock()
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SecurityAccess.create({
                'name': 'Unprovisioned GW',
                'workspace_id': self.workspace.id,
            })
        self.assertFalse(sa.tunnel_id)
        self.assertEqual(sa.state, 'pending')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            self.assertEqual(sa.state, 'suspended')
            # no tunnel → suspend issued no 503 sync
            mock_client.sync_tunnel_routes.assert_not_called()
            sa._do_resume()
        self.assertEqual(sa.state, 'pending')
        # resume on a tunnel-less SA must not push an operator sync either
        mock_client.sync_tunnel_routes.assert_not_called()

    def test_cleanup_deletes_tunnel(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()
        mock_client.delete_tunnel.assert_called_once_with('tun-lc')
        self.assertEqual(sa.state, 'deleting')
        self.assertFalse(sa.tunnel_id)

    def test_cleanup_reentrant_on_failure(self):
        """cleanup 失敗 → tunnel_id 保留，重跑可續刪（冪等重入）。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.delete_tunnel.side_effect = Exception('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()
        self.assertEqual(sa.tunnel_id, 'tun-lc')  # kept for retry
        mock_client.delete_tunnel.side_effect = None
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._cleanup_cloudflare()
        self.assertFalse(sa.tunnel_id)

    def test_cleanup_failure_state_is_error_not_deleting(self):
        """cleanup 失敗（tunnel 仍 live）不得寫 'deleting' 假性成功，須 'error'。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.delete_tunnel.side_effect = Exception('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()
        self.assertEqual(sa.state, 'error')
        self.assertTrue(sa.error_message)
        self.assertEqual(sa.tunnel_id, 'tun-lc')

    def test_suspend_early_returns_on_deleting(self):
        """deleting 是 terminal-ish：_do_suspend 不得把它翻回 suspended。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()  # tunnel cleared, state -> deleting
        self.assertEqual(sa.state, 'deleting')
        mock_client.sync_tunnel_routes.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
        # state stays 'deleting'; no reverse transition to 'suspended'
        self.assertEqual(sa.state, 'deleting')
        mock_client.sync_tunnel_routes.assert_not_called()

    def test_direct_unlink_triggers_cf_cleanup(self):
        """direct-unlink-bypass-cleanup-hook 守線：直接 unlink 也要清 CF。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa.with_context(sa_force_delete_primary=True).unlink()
        mock_client.delete_tunnel.assert_called_once_with('tun-lc')


class TestSecurityAccessAcl(TransactionCase):
    """workspace-scoped ir.rule：非 member 看不到、member 看得到。"""

    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        self.alice = User.create({
            'name': 'SA Alice', 'login': 'sa_acl_alice',
            'email': 'sa_acl_alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.mallory = User.create({
            'name': 'SA Mallory', 'login': 'sa_acl_mallory',
            'email': 'sa_acl_mallory@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA ACL WS', 'owner_id': self.alice.id,
        })
        # workspace owner 是否自動有 access record 依 workspace model 實作；
        # 保險起見明確建 alice 的 access（若已存在改用 search 確認）
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.alice.id, 'role': 'owner',
            })
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'ACL Gateway', 'workspace_id': self.workspace.id,
        })
        self.route = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': self.sa.canonical_name,
                'is_primary': True,
            })

    def test_member_can_read(self):
        visible = self.env['woow_paas_platform.security_access'].with_user(
            self.alice).search([('id', '=', self.sa.id)])
        self.assertEqual(len(visible), 1)

    def test_non_member_cannot_read(self):
        visible = self.env['woow_paas_platform.security_access'].with_user(
            self.mallory).search([('id', '=', self.sa.id)])
        self.assertEqual(len(visible), 0)

    def test_non_member_cannot_read_routes(self):
        """子模型 walk security_access_id.workspace_id 的 rule 也要生效。"""
        visible = self.env[
            'woow_paas_platform.security_access_route'].with_user(
                self.mallory).search([('id', '=', self.route.id)])
        self.assertEqual(len(visible), 0)

    def _add_user_member(self):
        """role='user' 一般 member（非 owner/admin），用來測 read/write 拆分。"""
        carol = self.env['res.users'].create({
            'name': 'SA Carol', 'login': 'sa_acl_carol',
            'email': 'sa_acl_carol@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id,
            'user_id': carol.id, 'role': 'user',
        })
        return carol

    def test_user_role_member_can_read_but_not_write_sa(self):
        """ir.rule read/write 拆分：role='user' member 可讀 SA，但 backend
        ORM write 必須被 ir.rule 擋（與 BFF owner/admin gate lock-step，封住
        後台旁路面）。"""
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        # read OK（read rule 對所有 member 開放）
        visible = self.env['woow_paas_platform.security_access'].with_user(
            carol).search([('id', '=', self.sa.id)])
        self.assertEqual(len(visible), 1)
        # write DENY（manage rule 只給 owner/admin）
        with self.assertRaises(AccessError):
            self.sa.with_user(carol).write({'name': 'Hijacked Name'})

    def test_user_role_member_cannot_create_or_unlink_sa(self):
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        with self.assertRaises(AccessError):
            self.env['woow_paas_platform.security_access'].with_user(
                carol).create({
                    'name': 'Carol GW', 'workspace_id': self.workspace.id,
                })
        with self.assertRaises(AccessError):
            self.sa.with_user(carol).unlink()

    def test_user_role_member_cannot_mutate_routes(self):
        """backend SA form 的 editable route list 旁路面：role='user' member
        不得 create/write/unlink route。"""
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        # read OK
        visible = self.env[
            'woow_paas_platform.security_access_route'].with_user(
                carol).search([('id', '=', self.route.id)])
        self.assertEqual(len(visible), 1)
        # create DENY
        with self.assertRaises(AccessError):
            self.env[
                'woow_paas_platform.security_access_route'].with_user(
                    carol).create({
                        'security_access_id': self.sa.id,
                        'subdomain_prefix': 'carol-route',
                    })
        # write DENY (non-primary write attempt; even prefix-immutable guard
        # is after ACL, so AccessError fires first on a plain field)
        with self.assertRaises(AccessError):
            self.route.with_user(carol).write(
                {'service_url': 'http://evil:80'})

    def test_owner_can_mutate_sa_and_routes(self):
        """正向：owner（alice）可 write SA + create route（不被新 manage rule 誤擋）。
        sa 為 pending 無 tunnel → route create 不觸發 operator sync。"""
        self.sa.with_user(self.alice).write({'name': 'Owner Renamed'})
        self.assertEqual(self.sa.name, 'Owner Renamed')
        new_route = self.env[
            'woow_paas_platform.security_access_route'].with_user(
                self.alice).create({
                    'security_access_id': self.sa.id,
                    'subdomain_prefix': 'owner-route',
                })
        self.assertTrue(new_route.exists())

    def test_tunnel_token_field_masked_for_non_system(self):
        """tunnel_token groups='base.group_system' field mask。"""
        self.sa.sudo().write({'tunnel_token': 'super-secret'})
        sa_as_alice = self.env[
            'woow_paas_platform.security_access'].with_user(
                self.alice).browse(self.sa.id)
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            _ = sa_as_alice.tunnel_token

    def test_canonical_name_injection_stripped_on_create(self):
        """非 su/system user 直接 ORM create 注入的 canonical_name 必須被忽略
        （改用自動生成值），不得搶註受害者命名空間。"""
        injected = 'paas-sa-victimwshash-deadbeef'
        sa = self.env['woow_paas_platform.security_access'].with_user(
            self.alice).create({
                'name': 'Injected Gateway',
                'workspace_id': self.workspace.id,
                'canonical_name': injected,
            })
        self.assertNotEqual(sa.canonical_name, injected)
        self.assertTrue(sa.canonical_name.startswith('paas-sa-'))

    def test_reference_id_injection_stripped_on_create(self):
        """注入的 reference_id 也必須被剝離，改用 uuid4 default。"""
        sa = self.env['woow_paas_platform.security_access'].with_user(
            self.alice).create({
                'name': 'Injected Ref Gateway',
                'workspace_id': self.workspace.id,
                'reference_id': 'attacker-controlled-ref',
            })
        self.assertNotEqual(sa.reference_id, 'attacker-controlled-ref')
        self.assertTrue(sa.reference_id)

    def test_canonical_name_write_blocked_for_non_system_user(self):
        """write() guard：非 su/system user 改 canonical_name 應 UserError。"""
        with self.assertRaises(UserError):
            self.sa.with_user(self.alice).write(
                {'canonical_name': 'paas-sa-hijack-0000'})

    def test_provision_writes_token_under_system_context(self):
        """system/admin context 下 provision 可寫 group_system 的 tunnel_token。"""
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-acl-1', 'tunnel_token': 'tok-acl-secret',
            'tunnel_name': 'paas-sa-x', 'dns_record_id': 'rec-1',
        }
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.sa.action_provision()  # default TransactionCase = su/admin
        self.assertEqual(self.sa.state, 'active')
        self.assertEqual(self.sa.sudo().tunnel_token, 'tok-acl-secret')

    def test_provision_token_write_fails_closed_for_non_system_owner(self):
        """field-group 語意鎖：非 system 的 workspace owner 觸發 provision，
        tunnel_token（groups='base.group_system'）寫入應 fail-closed（AccessError），
        而非被某人改 sudo 後悄悄放行。鏡像 smart_home 的非 sudo provision 行為。"""
        from odoo.exceptions import AccessError
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-acl-2', 'tunnel_token': 'tok-acl-secret-2',
            'tunnel_name': 'paas-sa-y', 'dns_record_id': 'rec-2',
        }
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        sa_as_alice = self.env[
            'woow_paas_platform.security_access'].with_user(self.alice).browse(
                self.sa.id)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            with self.assertRaises(AccessError):
                sa_as_alice.action_provision()
