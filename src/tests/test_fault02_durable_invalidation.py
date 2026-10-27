from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.addons.woow_paas_platform.models.security_access import _internal_identity

MOCK_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
               'get_paas_operator_client')


class TestFault02DurableInvalidation(TransactionCase):
    """Native decision contract for connector and Operator fault invalidation."""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']

    def decide(self, old='epoch-a', new='epoch-a', connections=None):
        if connections is None:
            connections = [{'connector_id': 'connector-a'}]
        return self.SA._fault02_should_invalidate(old, new, connections)

    def test_healthy_same_operator_stays_ready(self):
        self.assertFalse(self.decide())

    def test_connector_down_invalidates(self):
        self.assertTrue(self.decide(connections=[]))

    def test_malformed_connector_inventory_invalidates(self):
        self.assertTrue(self.decide(connections=[{}]))

    def test_blank_connector_identity_invalidates(self):
        self.assertTrue(self.decide(connections=[{'connector_id': '  '}]))

    def test_operator_restart_invalidates(self):
        self.assertTrue(self.decide(new='epoch-b'))

    def test_first_operator_observation_is_not_restart(self):
        self.assertFalse(self.decide(old=False, new='epoch-a'))

    def owned_route(self):
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'FAULT02 invalidation workspace'})
        sa = self.SA.create({'name': 'FAULT02 owned',
                             'workspace_id': workspace.id})
        route = self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': sa.id, 'subdomain_prefix': 'fault02-owned',
            'service_url': 'http://owned-origin:8124', 'is_protected': True,
        })
        _internal_identity(sa).write({'state': 'active',
                                      'tunnel_id': 'tunnel-owned'})
        _internal_identity(route).write({'access_app_id': 'app-owned',
                                         'access_publish_pending': False})
        return sa, route

    @patch(MOCK_CLIENT)
    def test_refresh_connector_down_persists_pending_and_parks(self, get_client):
        sa, route = self.owned_route()
        client = MagicMock()
        client.health_check.return_value = {'instance_epoch': 'epoch-a'}
        client.get_tunnel_status.return_value = {'connections': []}
        get_client.return_value = client
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)
        self.assertEqual(sa.operator_instance_epoch, 'epoch-a')
        park.assert_called_once()

    @patch(MOCK_CLIENT)
    def test_refresh_operator_restart_persists_pending_and_parks(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(sa).operator_instance_epoch = 'epoch-a'
        client = MagicMock()
        client.health_check.return_value = {'instance_epoch': 'epoch-b'}
        client.get_tunnel_status.return_value = {
            'connections': [{'connector_id': 'connector-a'}]}
        get_client.return_value = client
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)
        self.assertEqual(sa.operator_instance_epoch, 'epoch-b')
        park.assert_called_once()

    @patch(MOCK_CLIENT)
    def test_refresh_healthy_same_epoch_does_not_close(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(sa).operator_instance_epoch = 'epoch-a'
        client = MagicMock()
        client.health_check.return_value = {'instance_epoch': 'epoch-a'}
        client.get_tunnel_status.return_value = {
            'connections': [{'connector_id': 'connector-a'}]}
        get_client.return_value = client
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertFalse(route.access_publish_pending)
        park.assert_not_called()
