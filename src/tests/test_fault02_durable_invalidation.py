import json
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.addons.woow_paas_platform.models.security_access import _internal_identity
from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError

MOCK_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
               'get_paas_operator_client')
CRON_SCHEDULE = ('odoo.addons.woow_paas_platform.models.access_resync_cron.'
                 'schedule_access_resync')
LIVE = [{'connector_id': 'connector-a'}]


class TestFault02DurableInvalidation(TransactionCase):
    """Native decision contract for connector and Operator fault invalidation.

    The Operator runs several replicas behind one Service and every process has
    its own epoch, so a restart is only the *same* replica reporting a new epoch.
    """

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']

    def decide(self, restarted=False, connections=None):
        if connections is None:
            connections = list(LIVE)
        return self.SA._fault02_should_invalidate(restarted, connections)

    def epochs(self, stored, instance_id, epoch):
        return self.SA._fault02_epoch_decision(stored, instance_id, epoch)

    # ---- invalidation decision -------------------------------------------------

    def test_healthy_same_operator_stays_ready(self):
        self.assertFalse(self.decide())

    def test_connector_down_invalidates(self):
        self.assertTrue(self.decide(connections=[]))

    def test_malformed_connector_inventory_invalidates(self):
        self.assertTrue(self.decide(connections=[{}]))

    def test_blank_connector_identity_invalidates(self):
        self.assertTrue(self.decide(connections=[{'connector_id': '  '}]))

    def test_operator_restart_invalidates(self):
        self.assertTrue(self.decide(restarted=True))

    # ---- per-replica epoch tracking --------------------------------------------

    def test_first_observation_of_a_replica_is_only_learned(self):
        restarted, stored = self.epochs(False, 'pod-1', 'epoch-a')
        self.assertFalse(restarted)
        self.assertEqual(json.loads(stored), {'pod-1': 'epoch-a'})

    def test_same_replica_with_a_new_epoch_is_a_restart(self):
        _, stored = self.epochs(False, 'pod-1', 'epoch-a')
        restarted, stored = self.epochs(stored, 'pod-1', 'epoch-b')
        self.assertTrue(restarted)
        self.assertEqual(json.loads(stored), {'pod-1': 'epoch-b'})

    def test_alternating_replicas_are_never_restarts(self):
        """Two replicas answering alternately behind one Service (production)."""
        stored = False
        for instance_id, epoch in [('pod-1', 'epoch-a'), ('pod-2', 'epoch-b')] * 6:
            restarted, stored = self.epochs(stored, instance_id, epoch)
            self.assertFalse(restarted, (instance_id, epoch))
        self.assertEqual(json.loads(stored),
                         {'pod-1': 'epoch-a', 'pod-2': 'epoch-b'})

    def test_one_replica_restarting_among_alternating_replicas_is_detected(self):
        stored = False
        for instance_id, epoch in [('pod-1', 'epoch-a'), ('pod-2', 'epoch-b')]:
            _, stored = self.epochs(stored, instance_id, epoch)
        restarted, stored = self.epochs(stored, 'pod-2', 'epoch-c')
        self.assertTrue(restarted)
        restarted, stored = self.epochs(stored, 'pod-1', 'epoch-a')
        self.assertFalse(restarted)

    def test_replacement_pod_is_learned_not_a_restart(self):
        _, stored = self.epochs(False, 'pod-1', 'epoch-a')
        restarted, stored = self.epochs(stored, 'pod-9', 'epoch-z')
        self.assertFalse(restarted)
        self.assertIn('pod-9', json.loads(stored))

    def test_health_without_instance_id_never_restarts(self):
        for stored in (False, '{"pod-1":"epoch-a"}'):
            restarted, kept = self.epochs(stored, None, 'epoch-b')
            self.assertFalse(restarted)
            self.assertEqual(kept, stored)

    def test_legacy_bare_epoch_is_treated_as_empty(self):
        for legacy in ('epoch-a', '["epoch-a"]', '{"pod-1": 7}', '{broken'):
            restarted, stored = self.epochs(legacy, 'pod-1', 'epoch-b')
            self.assertFalse(restarted, legacy)
            self.assertEqual(json.loads(stored), {'pod-1': 'epoch-b'})

    def test_replica_map_is_bounded(self):
        stored = False
        for index in range(self.SA._FAULT02_MAX_INSTANCES + 5):
            _, stored = self.epochs(stored, 'pod-%d' % index, 'epoch-%d' % index)
        known = json.loads(stored)
        self.assertEqual(len(known), self.SA._FAULT02_MAX_INSTANCES)
        self.assertIn('pod-%d' % (self.SA._FAULT02_MAX_INSTANCES + 4), known)
        self.assertNotIn('pod-0', known)

    # ---- action_refresh_status wiring ------------------------------------------

    def owned_route(self, prefix='fault02-owned'):
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'FAULT02 invalidation workspace %s' % prefix})
        sa = self.SA.create({'name': 'FAULT02 %s' % prefix,
                             'workspace_id': workspace.id})
        route = self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': sa.id, 'subdomain_prefix': prefix,
            'service_url': 'http://owned-origin:8124', 'is_protected': True,
        })
        _internal_identity(sa).write({'state': 'active',
                                      'tunnel_id': 'tunnel-%s' % prefix})
        _internal_identity(route).write({'access_app_id': 'app-owned',
                                         'access_publish_pending': False})
        return sa, route

    def client(self, get_client, epoch='epoch-a', instance_id='pod-1',
               connections=None):
        client = MagicMock()
        health = {'instance_epoch': epoch}
        if instance_id:
            health['instance_id'] = instance_id
        client.health_check.return_value = health
        client.get_tunnel_status.return_value = {
            'connections': list(LIVE) if connections is None else connections}
        get_client.return_value = client
        return client

    @patch(MOCK_CLIENT)
    def test_refresh_connector_down_persists_pending_and_parks(self, get_client):
        sa, route = self.owned_route()
        self.client(get_client, connections=[])
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)
        self.assertEqual(json.loads(sa.operator_instance_epoch),
                         {'pod-1': 'epoch-a'})
        park.assert_called_once()

    @patch(MOCK_CLIENT)
    def test_refresh_same_replica_restart_persists_pending_and_parks(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(sa).operator_instance_epoch = '{"pod-1":"epoch-a"}'
        self.client(get_client, epoch='epoch-b')
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)
        self.assertEqual(json.loads(sa.operator_instance_epoch),
                         {'pod-1': 'epoch-b'})
        park.assert_called_once()

    @patch(MOCK_CLIENT)
    def test_refresh_healthy_same_epoch_does_not_close(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(sa).operator_instance_epoch = '{"pod-1":"epoch-a"}'
        self.client(get_client)
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertFalse(route.access_publish_pending)
        park.assert_not_called()

    @patch(MOCK_CLIENT)
    def test_refresh_two_alternating_replicas_never_closes(self, get_client):
        """Regression: production runs two Operator replicas behind one Service."""
        sa, route = self.owned_route()
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            for instance_id, epoch in [('pod-1', 'epoch-a'), ('pod-2', 'epoch-b')] * 5:
                self.client(get_client, epoch=epoch, instance_id=instance_id)
                sa.action_refresh_status()
                self.assertFalse(route.access_publish_pending, (instance_id, epoch))
        park.assert_not_called()
        self.assertEqual(json.loads(sa.operator_instance_epoch),
                         {'pod-1': 'epoch-a', 'pod-2': 'epoch-b'})

    @patch(MOCK_CLIENT)
    def test_refresh_without_instance_id_never_closes_on_epoch_change(self, get_client):
        sa, route = self.owned_route()
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            for epoch in ('epoch-a', 'epoch-b', 'epoch-a'):
                self.client(get_client, epoch=epoch, instance_id=None)
                sa.action_refresh_status()
        self.assertFalse(route.access_publish_pending)
        park.assert_not_called()

    # ---- probe cron follow-up --------------------------------------------------

    @patch(MOCK_CLIENT)
    def test_probe_wakes_the_resync_cron_once_for_new_closures(self, get_client):
        sa, route = self.owned_route()
        self.client(get_client, connections=[])
        cron = self.env['woow_paas_platform.access_resync_cron']
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True), \
                patch(CRON_SCHEDULE) as schedule:
            first = cron._cron_probe_security_access_faults()
            second = cron._cron_probe_security_access_faults()
        self.assertGreaterEqual(first['closed'], 1)
        # Already closed: staying down must not turn the cron into a tight loop.
        self.assertEqual(second['closed'], 0)
        schedule.assert_called_once()
        self.assertTrue(route.access_publish_pending)

    def test_probe_and_resync_are_separate_cron_records(self):
        """The resync record sits in a noupdate file, so an upgraded install keeps
        whatever code it already has.  The probe therefore needs its own record
        (a new xml id is created on upgrade); chaining it onto the old record's
        code would silently never run in production."""
        probe = self.env.ref('woow_paas_platform.ir_cron_access_fault_probe')
        resync = self.env.ref('woow_paas_platform.ir_cron_access_protection_resync')
        self.assertEqual(probe.code.strip(),
                         'model._cron_probe_security_access_faults()')
        self.assertTrue(probe.active)
        self.assertEqual(resync.code.strip(),
                         'model._cron_resync_access_protection()')

    # ---- operators that predate the epoch contract ----------------------------

    @patch(MOCK_CLIENT)
    def test_refresh_against_an_operator_without_epoch_is_not_a_fault(self, get_client):
        """Odoo may be deployed before the Operator: a health payload without the
        epoch fields must neither raise nor close every tenant."""
        sa, route = self.owned_route()
        client = self.client(get_client)
        client.health_check.return_value = {'status': 'healthy'}
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertFalse(route.access_publish_pending)
        self.assertNotEqual(sa.tunnel_status, 'error')
        park.assert_not_called()

    @patch(MOCK_CLIENT)
    def test_refresh_against_an_operator_without_epoch_still_sees_connector_loss(self, get_client):
        sa, route = self.owned_route()
        client = self.client(get_client, connections=[])
        client.health_check.return_value = {'status': 'healthy'}
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park:
            sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)
        park.assert_called_once()

    @patch(MOCK_CLIENT)
    def test_refresh_works_for_a_workspace_admin_without_the_system_group(self, get_client):
        """The epoch map lives in a system-only field; the refresh endpoint runs as
        the workspace owner/admin, who is not a system administrator."""
        sa, route = self.owned_route()
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'FAULT02 workspace admin',
            'login': 'fault02-ws-admin@example.invalid',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': sa.workspace_id.id, 'user_id': user.id,
            'role': 'admin'})
        self.client(get_client)
        sa.with_user(user).action_refresh_status()
        self.assertEqual(json.loads(sa.sudo().operator_instance_epoch),
                         {'pod-1': 'epoch-a'})

    # ---- the probe only marks; the resync leg parks and recovers --------------

    @patch(MOCK_CLIENT)
    def test_probe_marks_a_closure_without_parking(self, get_client):
        sa, route = self.owned_route()
        self.client(get_client, connections=[])
        cron = self.env['woow_paas_platform.access_resync_cron']
        with patch.object(type(sa), '_sync_routes_to_operator', autospec=True) as park, \
                patch(CRON_SCHEDULE):
            cron._cron_probe_security_access_faults()
        self.assertTrue(route.access_publish_pending)
        # Parking happens in the resync leg, in a transaction that also keeps the
        # marker; parking here would strand a parked edge if this one rolled back.
        park.assert_not_called()

    @patch(MOCK_CLIENT)
    def test_probe_does_not_treat_an_unreachable_operator_as_a_fault(self, get_client):
        sa, route = self.owned_route()
        client = self.client(get_client)
        client.health_check.side_effect = PaaSOperatorError('operator down')
        cron = self.env['woow_paas_platform.access_resync_cron']
        with patch(CRON_SCHEDULE) as schedule:
            counts = cron._cron_probe_security_access_faults()
        self.assertFalse(route.access_publish_pending)
        self.assertNotEqual(sa.tunnel_status, 'error')
        self.assertEqual(counts['closed'], 0)
        schedule.assert_not_called()
        # A user-initiated refresh still fails closed.
        sa.action_refresh_status()
        self.assertTrue(route.access_publish_pending)

    @patch(MOCK_CLIENT)
    def test_probe_rotates_through_every_security_access(self, get_client):
        sas = [self.owned_route('fault02-rot-%d' % index)[0] for index in range(3)]
        self.client(get_client)
        cron = self.env['woow_paas_platform.access_resync_cron']
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.access_probe_cursor', '0')
        mine = set(sas[0].ids + sas[1].ids + sas[2].ids)

        def probed():
            return {sa.id for sa in sas if sa.sudo().operator_instance_epoch}

        with patch(CRON_SCHEDULE):
            first = cron._cron_probe_security_access_faults(limit=2)
            self.assertEqual(first['probed'], 2)
            self.assertEqual(len(probed()), 2)
            self.assertNotEqual(params.get_param(
                'woow_paas_platform.access_probe_cursor'), '0')
            second = cron._cron_probe_security_access_faults(limit=2)
        # The next run continues after the last id and wraps at the end.
        self.assertEqual(probed(), mine)
        self.assertEqual(second['probed'], 1)
        self.assertEqual(params.get_param(
            'woow_paas_platform.access_probe_cursor'), '0')

    # ---- recovery wake-ups: a short connector blip must not mean a long outage --

    @patch(MOCK_CLIENT)
    def test_probe_wakes_the_resync_when_a_pending_sa_has_its_connector_back(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(route).access_publish_pending = True  # closed earlier
        self.client(get_client)  # inventory valid again
        cron = self.env['woow_paas_platform.access_resync_cron']
        with patch(CRON_SCHEDULE) as schedule:
            counts = cron._cron_probe_security_access_faults()
        self.assertEqual((counts['closed'], counts['recoverable']), (0, 1))
        schedule.assert_called_once()
        # Now (it only becomes visible after this transaction commits), not a
        # whole period later.
        wake_in = (schedule.call_args.args[1] - fields.Datetime.now()).total_seconds()
        self.assertLessEqual(abs(wake_in), 3)
        # The probe itself never clears the marker: recovery is the resync's job.
        self.assertTrue(route.access_publish_pending)

    @patch(MOCK_CLIENT)
    def test_probe_does_not_wake_for_a_pending_sa_whose_connector_is_still_down(self, get_client):
        sa, route = self.owned_route()
        _internal_identity(route).access_publish_pending = True
        self.client(get_client, connections=[])
        cron = self.env['woow_paas_platform.access_resync_cron']
        with patch(CRON_SCHEDULE) as schedule:
            counts = cron._cron_probe_security_access_faults()
        self.assertEqual((counts['closed'], counts['recoverable']), (0, 0))
        schedule.assert_not_called()
