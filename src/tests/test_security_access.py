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
