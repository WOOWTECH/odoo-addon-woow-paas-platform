"""REV-01: policy removal closes the origin; a bounded hold follows the provider's
acknowledgement; only the end of the hold plus a re-confirmed Access sync re-opens it.

The provider only *accepts* a token revocation, so acceptance is never proof that an
old cookie is denied: the hold is a bound, and the live same-cookie oracle still has
to observe the denial.  No external network; the operator is the AC1011 fake.
"""
from datetime import timedelta
import logging
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_cs_access_identity
from ..models.security_access import _internal_identity as _sa_identity
from ..models.smart_home_route import _internal_sm_route_identity
from ..services.paas_operator import PaaSOperatorAPIError
from ..services.access_state import (
    ACCESS_REVOKE_HOLD_DEFAULT,
    ACCESS_REVOKE_HOLD_MAX,
    ACCESS_REVOKE_HOLD_MIN,
    ACCESS_REVOKE_HOLD_PARAM,
    access_configuration_ready,
    access_revoke_hold_seconds,
    access_revoke_operator_reports,
    access_revoke_provider_accepted,
    access_revoke_state,
    schedule_access_resync,
)
from .test_access_publish_pending_ac1011 import PendingFixture

SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
             'get_paas_operator_client')
ANCHOR_FIELD = {'sa': 'route_id', 'sh': 'smart_home_route_id',
                'cs': 'cloud_service_id'}
SCHEDULE = {kind: 'odoo.addons.woow_paas_platform.models.%s.schedule_access_resync'
            % module for kind, module in (
                ('sa', 'security_access'), ('sh', 'smart_home_route'),
                ('cs', 'cloud_service'))}
NOW = fields.Datetime.from_string('2026-10-28 00:00:00')


class TestRevokeHoldPure(TransactionCase):
    """The decision functions behind the hold, without any anchor."""

    def test_state_machine(self):
        hold = 120
        self.assertEqual(access_revoke_state(False, NOW, NOW, hold), 'none')
        self.assertEqual(access_revoke_state(True, False, NOW, hold),
                         'await_acceptance')
        self.assertEqual(access_revoke_state(True, NOW, NOW, hold), 'held')
        self.assertEqual(access_revoke_state(
            True, NOW, NOW + timedelta(seconds=hold - 1), hold), 'held')
        self.assertEqual(access_revoke_state(
            True, NOW, NOW + timedelta(seconds=hold), hold), 'releasable')

    def test_only_an_explicit_acceptance_counts(self):
        self.assertTrue(access_revoke_provider_accepted(
            {'token_revocation': 'accepted'}))
        for result in ({'token_revocation': 'failed'},
                       {'token_revocation': 'not_required'},
                       {}, None, 'accepted', {'token_revocation': True}):
            self.assertFalse(access_revoke_provider_accepted(result), result)

    def test_operator_skew_is_detected_by_the_missing_field(self):
        self.assertTrue(access_revoke_operator_reports(
            {'token_revocation': 'not_required'}))
        self.assertTrue(access_revoke_operator_reports(
            {'token_revocation': 'failed'}))
        for result in ({}, {'app_id': 'x'}, None, 'accepted'):
            self.assertFalse(access_revoke_operator_reports(result), result)

    def test_hold_parameter_default_clamp_and_garbage(self):
        params = self.env['ir.config_parameter'].sudo()
        params.search([('key', '=', ACCESS_REVOKE_HOLD_PARAM)]).unlink()
        self.assertEqual(access_revoke_hold_seconds(self.env),
                         ACCESS_REVOKE_HOLD_DEFAULT)
        # An unset parameter is the default, not the clamp floor (int(False) == 0).
        params.set_param(ACCESS_REVOKE_HOLD_PARAM, '')
        self.assertEqual(access_revoke_hold_seconds(self.env),
                         ACCESS_REVOKE_HOLD_DEFAULT)
        for raw, expected in (('90', 90), ('1', ACCESS_REVOKE_HOLD_MIN),
                              ('0', ACCESS_REVOKE_HOLD_MIN),
                              ('-5', ACCESS_REVOKE_HOLD_MIN),
                              ('999999', ACCESS_REVOKE_HOLD_MAX)):
            params.set_param(ACCESS_REVOKE_HOLD_PARAM, raw)
            self.assertEqual(access_revoke_hold_seconds(self.env), expected, raw)
        # A typo must not weaken the barrier: it falls back to the default.
        for raw in ('soon', '', '1.5', 'None'):
            params.set_param(ACCESS_REVOKE_HOLD_PARAM, raw)
            self.assertEqual(access_revoke_hold_seconds(self.env),
                             ACCESS_REVOKE_HOLD_DEFAULT, raw)

    def test_schedule_is_best_effort(self):
        with patch.object(type(self.env['ir.cron']), '_trigger',
                          side_effect=RuntimeError('no worker')):
            self.assertFalse(schedule_access_resync(self.env, NOW))
        with patch.object(self.env.registry['ir.cron'], '_trigger') as trigger:
            self.assertTrue(schedule_access_resync(self.env, NOW))
        trigger.assert_called_once()


@tagged('post_install', '-at_install')
class TestRevokeHoldRev01(PendingFixture, TransactionCase):
    """One scenario set, run for each of the three anchor kinds."""

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        patcher = patch(SA_CLIENT, return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.revoke_result = 'accepted'
        original = self.original_sync_access_app = self.client.sync_access_app

        def sync_access_app(payload):
            result = original(payload)
            result['token_revocation'] = (
                self.revoke_result if payload.get('revoke_tokens')
                else 'not_required')
            return result
        self.client.sync_access_app = self.good_sync = sync_access_app
        self.env['ir.config_parameter'].sudo().set_param(
            ACCESS_REVOKE_HOLD_PARAM, '60')
        self.hold = access_revoke_hold_seconds(self.env)

    # ---- helpers -----------------------------------------------------------

    def identity(self, kind, anchor):
        return {'sa': _sa_identity, 'sh': _internal_sm_route_identity,
                'cs': _internal_cs_access_identity}[kind](anchor)

    def protected_with_two_policies(self, kind):
        anchor = self.anchor(kind)
        self.client.origin = True
        self.assertTrue(anchor.write({'is_protected': True}))
        policies = self.env['woow_paas_platform.security_access_access_policy'
                            ].create([{
                                ANCHOR_FIELD[kind]: anchor.id,
                                'name': 'allow-%d' % index, 'decision': 'allow',
                                'rule_type': 'email_list',
                                'rule_value': 'user%d@example.test' % index,
                            } for index in (1, 2)])
        anchor.invalidate_recordset()
        self.assertTrue(self.client.origin)
        self.assertFalse(anchor.access_publish_pending)
        self.assertFalse(anchor.access_token_revoke_pending)
        return anchor, policies

    def revocations(self):
        return [event for event in self.client.events
                if event[0] == 'access' and event[1].get('revoke_tokens')]

    def end_the_hold(self, kind, anchor):
        self.identity(kind, anchor).access_token_revoke_accepted_at = (
            fields.Datetime.now() - timedelta(seconds=self.hold + 1))
        anchor.invalidate_recordset()

    def assert_closed_in_hold(self, anchor):
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertTrue(anchor.access_token_revoke_pending)
        self.assertTrue(anchor.access_publish_pending)
        self.assertFalse(access_configuration_ready(anchor))

    def assert_reopened(self, anchor):
        anchor.invalidate_recordset()
        self.assertTrue(self.client.origin)
        self.assertFalse(anchor.access_token_revoke_pending)
        self.assertFalse(anchor.access_token_revoke_accepted_at)
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(access_configuration_ready(anchor))

    # ---- scenarios ---------------------------------------------------------

    def removal_closes_origin_and_hold_starts_at_acknowledgement(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        self.client.events.clear()
        with patch(SCHEDULE[kind]) as schedule:
            policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.assertTrue(anchor.access_token_revoke_accepted_at)
        self.assertEqual(len(self.revocations()), 1)
        # The wake-up lands just after the hold would end.
        schedule.assert_called_once()
        wake_at = schedule.call_args.args[1]
        self.assertGreater(wake_at, anchor.access_token_revoke_accepted_at
                           + timedelta(seconds=self.hold))

    def sync_inside_the_hold_stays_closed_and_does_not_revoke_again(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        self.client.events.clear()
        policies[1].unlink()
        accepted_at = anchor.access_token_revoke_accepted_at
        for _ in range(3):
            self.assertFalse(anchor._sync_access_to_operator())
        self.assert_closed_in_hold(anchor)
        self.assertEqual(anchor.access_token_revoke_accepted_at, accepted_at)
        self.assertEqual(len(self.revocations()), 1)

    def sync_after_the_hold_reconfirms_access_then_republishes(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.end_the_hold(kind, anchor)
        self.client.events.clear()
        self.assertTrue(anchor._sync_access_to_operator())
        self.assert_reopened(anchor)
        names = self.names()
        # Access is re-confirmed before the origin is re-published, and no
        # additional revocation is requested at release.
        self.assertIn('access', names)
        publish = [index for index, event in enumerate(self.client.events)
                   if event[0] in ('configure', 'routes', 'unpark')
                   and not str(event[1]).startswith('http_status:')]
        self.assertTrue(publish)
        self.assertLess(names.index('access'), publish[0])
        self.assertEqual(len(self.revocations()), 0)

    def failed_release_keeps_the_markers_for_the_cron(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.end_the_hold(kind, anchor)
        self.fail_publish(kind)
        self.assertFalse(anchor._sync_access_to_operator())
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)
        # Markers are only cleared after a confirmed re-publication.
        self.assertTrue(anchor.access_token_revoke_pending)
        self.assertTrue(anchor.access_publish_pending)
        self.fail_publish(kind, False)
        self.env['woow_paas_platform.access_resync_cron'
                 ]._cron_resync_access_protection()
        self.assert_reopened(anchor)

    def tenant_edit_during_the_hold_does_not_release(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        anchor.write({'session_duration': '8h'})
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 1)

    def second_removal_restarts_the_hold(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        first = anchor.access_token_revoke_accepted_at
        self.identity(kind, anchor).access_token_revoke_accepted_at = (
            fields.Datetime.now() - timedelta(seconds=self.hold - 10))
        policies[0].unlink()
        anchor.invalidate_recordset()
        self.assertTrue(anchor.access_token_revoke_pending)
        self.assertTrue(anchor.access_token_revoke_accepted_at)
        self.assertGreater(anchor.access_token_revoke_accepted_at,
                           first - timedelta(seconds=self.hold))
        self.assertEqual(len(self.revocations()), 2)
        self.assert_closed_in_hold(anchor)

    def unacknowledged_revocation_stays_closed_and_is_retried(self, kind):
        anchor, policies = self.protected_with_two_policies(kind)
        self.revoke_result = 'failed'
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.assertFalse(anchor.access_token_revoke_accepted_at)
        # Even long after, nothing was acknowledged, so nothing may re-open.
        self.assertFalse(anchor._sync_access_to_operator())
        self.assert_closed_in_hold(anchor)
        self.revoke_result = 'accepted'
        self.env['woow_paas_platform.access_resync_cron'
                 ]._cron_resync_access_protection()
        anchor.invalidate_recordset()
        self.assertTrue(anchor.access_token_revoke_accepted_at)
        self.assert_closed_in_hold(anchor)
        self.assertEqual(len(self.revocations()), 3)

    def teardown_during_the_hold_releases_the_origin(self, kind):
        """Protection turned off while the revocation is open: the app is gone, so
        there is nothing left to revoke or wait for; the public origin is
        re-published and the markers are cleared (not held forever)."""
        anchor, policies = self.protected_with_two_policies(kind)
        policies[1].unlink()
        self.assert_closed_in_hold(anchor)
        self.assertTrue(anchor.write({'is_protected': False}))
        anchor.invalidate_recordset()
        self.assertFalse(anchor.is_protected)
        self.assertFalse(anchor.access_app_id)
        self.assert_reopened(anchor)

    def provider_failure_at_removal_reopens_through_the_cron(self, kind):
        """The origin is parked before the provider call.  If the call fails the
        deletion rolls back (the policy stays) - but the parked edge cannot roll
        back, so a durable closure marker must remain for the cron to re-open it."""
        anchor, policies = self.protected_with_two_policies(kind)

        def provider_down(payload):
            raise PaaSOperatorAPIError('provider down', status_code=502)
        self.client.sync_access_app = provider_down
        with self.assertRaises(PaaSOperatorAPIError):
            policies[1].unlink()
        self.assertTrue(policies[1].exists())
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)  # parked before the failing call
        self.assertTrue(anchor.access_publish_pending)
        self.assertFalse(anchor.access_token_revoke_pending)  # nothing was deleted
        # The provider is back: the cron re-confirms Access and re-opens it.
        self.client.sync_access_app = self.good_sync
        self.env['woow_paas_platform.access_resync_cron'
                 ]._cron_resync_access_protection()
        anchor.invalidate_recordset()
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(self.client.origin)
        self.assertEqual(len(self.revocations()), 0)

    def test_sa_teardown_during_the_hold_releases_the_origin(self):
        self.teardown_during_the_hold_releases_the_origin('sa')

    def test_sh_teardown_during_the_hold_releases_the_origin(self):
        self.teardown_during_the_hold_releases_the_origin('sh')

    def test_cs_teardown_during_the_hold_releases_the_origin(self):
        self.teardown_during_the_hold_releases_the_origin('cs')

    def test_sa_provider_failure_at_removal_reopens_through_the_cron(self):
        self.provider_failure_at_removal_reopens_through_the_cron('sa')

    def test_sh_provider_failure_at_removal_reopens_through_the_cron(self):
        self.provider_failure_at_removal_reopens_through_the_cron('sh')

    def test_cs_provider_failure_at_removal_reopens_through_the_cron(self):
        self.provider_failure_at_removal_reopens_through_the_cron('cs')

    def test_a_refused_revocation_is_logged_with_its_reason(self):
        anchor, policies = self.protected_with_two_policies('sa')
        self.revoke_result = 'failed'
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.security_access',
                level='ERROR') as logs:
            policies[1].unlink()
        self.assertTrue(any('did not accept the token revocation' in line
                            for line in logs.output), logs.output)
        self.assert_closed_in_hold(anchor)

    def test_sa_removal_closes_origin_and_hold_starts_at_acknowledgement(self):
        self.removal_closes_origin_and_hold_starts_at_acknowledgement('sa')

    def test_sh_removal_closes_origin_and_hold_starts_at_acknowledgement(self):
        self.removal_closes_origin_and_hold_starts_at_acknowledgement('sh')

    def test_cs_removal_closes_origin_and_hold_starts_at_acknowledgement(self):
        self.removal_closes_origin_and_hold_starts_at_acknowledgement('cs')

    def test_sa_sync_inside_the_hold_stays_closed(self):
        self.sync_inside_the_hold_stays_closed_and_does_not_revoke_again('sa')

    def test_sh_sync_inside_the_hold_stays_closed(self):
        self.sync_inside_the_hold_stays_closed_and_does_not_revoke_again('sh')

    def test_cs_sync_inside_the_hold_stays_closed(self):
        self.sync_inside_the_hold_stays_closed_and_does_not_revoke_again('cs')

    def test_sa_sync_after_the_hold_reconfirms_then_republishes(self):
        self.sync_after_the_hold_reconfirms_access_then_republishes('sa')

    def test_sh_sync_after_the_hold_reconfirms_then_republishes(self):
        self.sync_after_the_hold_reconfirms_access_then_republishes('sh')

    def test_cs_sync_after_the_hold_reconfirms_then_republishes(self):
        self.sync_after_the_hold_reconfirms_access_then_republishes('cs')

    def test_sa_failed_release_keeps_the_markers_for_the_cron(self):
        self.failed_release_keeps_the_markers_for_the_cron('sa')

    def test_sh_failed_release_keeps_the_markers_for_the_cron(self):
        self.failed_release_keeps_the_markers_for_the_cron('sh')

    def test_cs_failed_release_keeps_the_markers_for_the_cron(self):
        self.failed_release_keeps_the_markers_for_the_cron('cs')

    def test_sa_tenant_edit_during_the_hold_does_not_release(self):
        self.tenant_edit_during_the_hold_does_not_release('sa')

    def test_sh_tenant_edit_during_the_hold_does_not_release(self):
        self.tenant_edit_during_the_hold_does_not_release('sh')

    def test_cs_tenant_edit_during_the_hold_does_not_release(self):
        self.tenant_edit_during_the_hold_does_not_release('cs')

    def test_sa_second_removal_restarts_the_hold(self):
        self.second_removal_restarts_the_hold('sa')

    def test_sh_second_removal_restarts_the_hold(self):
        self.second_removal_restarts_the_hold('sh')

    def test_cs_second_removal_restarts_the_hold(self):
        self.second_removal_restarts_the_hold('cs')

    def test_sa_unacknowledged_revocation_stays_closed_and_is_retried(self):
        self.unacknowledged_revocation_stays_closed_and_is_retried('sa')

    def test_sh_unacknowledged_revocation_stays_closed_and_is_retried(self):
        self.unacknowledged_revocation_stays_closed_and_is_retried('sh')

    def test_cs_unacknowledged_revocation_stays_closed_and_is_retried(self):
        self.unacknowledged_revocation_stays_closed_and_is_retried('cs')

    def test_operator_without_the_revocation_contract_stays_closed_and_logs(self):
        """An operator deployed after this module cannot acknowledge anything:
        stay closed (fail-closed) and say why instead of waiting silently."""
        anchor, policies = self.protected_with_two_policies('sa')
        # The plain fake never reports ``token_revocation`` (an old operator).
        self.client.sync_access_app = self.original_sync_access_app
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.security_access',
                level='ERROR') as logs:
            policies[1].unlink()
        self.assertTrue(any('did not report token_revocation' in line
                            for line in logs.output), logs.output)
        self.assert_closed_in_hold(anchor)
        self.assertFalse(anchor.access_token_revoke_accepted_at)

    # ---- cron scan shape ---------------------------------------------------

    def test_cron_scans_only_rows_with_an_action_due(self):
        cron = self.env['woow_paas_platform.access_resync_cron']
        anchor, policies = self.protected_with_two_policies('sa')
        Route = self.env['woow_paas_platform.security_access_route']
        domain = cron._resync_domain('woow_paas_platform.security_access_route')
        policies[1].unlink()
        # Inside the hold: nothing is due, a scan would only burn operator calls.
        self.assertNotIn(anchor, Route.search(domain))
        self.end_the_hold('sa', anchor)
        self.assertIn(anchor, Route.search(
            cron._resync_domain('woow_paas_platform.security_access_route')))

    def test_every_leg_recovers_plain_pending_but_not_rows_in_a_hold(self):
        cron = self.env['woow_paas_platform.access_resync_cron']
        for model in ('woow_paas_platform.security_access_route',
                      'woow_paas_platform.smart_home_route',
                      'woow_paas_platform.cloud_service'):
            domain = repr(cron._resync_domain(model))
            self.assertIn('access_publish_pending', domain, model)
            # A row inside a revocation hold is also pending; its own clause
            # decides when it is due, so the plain clause excludes it.
            self.assertIn("('access_token_revoke_pending', '=', False)", domain, model)


@tagged('post_install', '-at_install')
class TestFault02CronRecovery(PendingFixture, TransactionCase):
    """FAULT-02: a closed Security Access route recovers through the resync cron,
    and only after Access was re-confirmed against a live connector."""

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        patcher = patch(SA_CLIENT, return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cron = self.env['woow_paas_platform.access_resync_cron']

    def closed_sa_route(self):
        sa, route = self.sa_resource()
        self.client.origin = True
        self.assertTrue(route.write({'is_protected': True}))
        self.assertTrue(self.client.origin)
        # What the fault probe does: durable pending, then park at the edge.
        _sa_identity(route).access_publish_pending = True
        sa._sync_routes_to_operator()
        self.assertFalse(self.client.origin)
        self.client.events.clear()
        return sa, route

    def test_cron_recovers_after_access_is_reconfirmed_on_a_live_connector(self):
        sa, route = self.closed_sa_route()
        self.cron._cron_resync_access_protection()
        route.invalidate_recordset()
        self.assertFalse(route.access_publish_pending)
        self.assertTrue(self.client.origin)
        names = self.names()
        self.assertIn('access', names)
        first_publish = next(
            index for index, event in enumerate(self.client.events)
            if event[0] in ('configure', 'routes')
            and not str(event[1]).startswith('http_status:'))
        self.assertLess(names.index('access'), first_publish)

    def test_cron_keeps_the_route_closed_while_the_connector_is_down(self):
        sa, route = self.closed_sa_route()
        live = self.client.get_tunnel_status
        self.client.get_tunnel_status = lambda tunnel_id: {
            'tunnel_id': tunnel_id, 'name': sa.tunnel_name,
            'status': 'inactive', 'connections': [], 'deleted_at': None}
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.access_resync_cron',
                level=logging.INFO) as logs:
            self.cron._cron_resync_access_protection()
        # The sync spent operator calls without releasing: that is an attempt
        # against the leg's budget, not a gated (call-free) skip.
        self.assertTrue(any('pending=1' in line and 'gated=0' in line
                            for line in logs.output), logs.output)
        route.invalidate_recordset()
        self.assertTrue(route.access_publish_pending)
        self.assertFalse(self.client.origin)
        # Staying down is not a failure to retry harder; coming back recovers.
        self.client.get_tunnel_status = live
        self.cron._cron_resync_access_protection()
        route.invalidate_recordset()
        self.assertFalse(route.access_publish_pending)
        self.assertTrue(self.client.origin)

    def plain_pending_recovers_through_the_cron(self, kind):
        anchor = self.anchor(kind)
        self.client.origin = True
        self.assertTrue(anchor.write({'is_protected': True}))
        {'sh': _internal_sm_route_identity, 'cs': _internal_cs_access_identity
         }[kind](anchor).access_publish_pending = True
        self.client.origin = False  # parked while pending
        self.client.events.clear()
        self.cron._cron_resync_access_protection()
        anchor.invalidate_recordset()
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(self.client.origin)
        self.assertIn('access', self.names())

    def test_sh_plain_pending_recovers_through_the_cron(self):
        self.plain_pending_recovers_through_the_cron('sh')

    def test_cs_plain_pending_recovers_through_the_cron(self):
        self.plain_pending_recovers_through_the_cron('cs')
