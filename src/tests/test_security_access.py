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
