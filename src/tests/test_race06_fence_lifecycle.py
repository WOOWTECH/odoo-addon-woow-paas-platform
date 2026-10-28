"""RACE-06 fence lifecycle: the fence must never turn one failure into a
permanent outage.

A create that the provider definitively refused can be retried; a create whose
outcome is uncertain stays fenced; and an identity whose earlier create finished
(app later torn down) starts a new cycle instead of adopting a dead app id.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models.smart_home_route import _internal_sm_route_identity
from ..services.paas_operator import (
    PaaSOperatorAPIError,
    PaaSOperatorConnectionError,
    PaaSOperatorError,
    PaaSOperatorTimeoutError,
)
from .test_cs_sh_fail_closed_ac1011 import Fixture

MODEL = 'woow_paas_platform.access_dispatch_fence'
ROUTE_MODEL = 'woow_paas_platform.smart_home_route'
ROUTE_LOG = 'odoo.addons.woow_paas_platform.models.smart_home_route'


class TestFenceRejectionClassification(TransactionCase):
    """Only a provider refusal may reopen the retry window."""

    def test_definitive_statuses(self):
        Fence = self.env[MODEL]
        for status in (400, 401, 403, 404, 405, 409, 415, 422, 429):
            self.assertTrue(Fence._is_definitive_rejection(
                PaaSOperatorAPIError('refused', status_code=status)), status)

    def test_uncertain_outcomes_stay_fenced(self):
        Fence = self.env[MODEL]
        for error in (
                PaaSOperatorAPIError('bad gateway', status_code=502),
                PaaSOperatorAPIError('timeout upstream', status_code=504),
                PaaSOperatorAPIError('crashed', status_code=500),
                PaaSOperatorError('no status'),
                PaaSOperatorConnectionError('reset'),
                PaaSOperatorTimeoutError('slow'),
                RuntimeError('anything else')):
            self.assertFalse(Fence._is_definitive_rejection(error), error)


@tagged('post_install', '-at_install')
class TestFenceLifecycle(Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        self.sh_active()
        self.sync_access_app = self.client.sync_access_app
        self.addCleanup(self.delete_fence_rows)

    # The fence works on its own connections and really commits, so the test's
    # REPEATABLE READ transaction cannot see (or lock) its rows.  Read and change
    # them through independent cursors too, and remove what the test left behind.
    _COLUMNS = ('id', 'state', 'attempt', 'provider_app_id', 'effect_started_at')

    def fence(self):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id, state, attempt, provider_app_id, effect_started_at '
                'FROM woow_paas_platform_access_dispatch_fence '
                'WHERE anchor_model=%s AND anchor_id=%s',
                (ROUTE_MODEL, self.route.id))
            row = cr.fetchone()
        return dict(zip(self._COLUMNS, row)) if row else {}

    def set_fence_state(self, state):
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_paas_platform_access_dispatch_fence SET state=%s '
                'WHERE anchor_model=%s AND anchor_id=%s',
                (state, ROUTE_MODEL, self.route.id))
            self.assertEqual(cr.rowcount, 1)
            cr.commit()

    def delete_fence_rows(self):
        with self.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM woow_paas_platform_access_dispatch_fence '
                'WHERE anchor_model=%s AND anchor_id=%s',
                (ROUTE_MODEL, self.route.id))
            cr.commit()

    def refuse(self, status):
        def sync_access_app(payload):
            raise PaaSOperatorAPIError('provider refused', status_code=status)
        self.client.sync_access_app = sync_access_app

    def test_refused_create_can_be_retried(self):
        self.refuse(400)  # e.g. the selected login method is not configured
        with self.assertRaises(PaaSOperatorAPIError):
            self.route._sync_access_to_operator()
        fence = self.fence()
        self.assertEqual(fence['state'], 'prepared')
        self.assertFalse(fence['effect_started_at'])
        # The cause is fixed: the very next attempt creates the app.
        self.client.sync_access_app = self.sync_access_app
        self.assertTrue(self.route._sync_access_to_operator())
        self.route.invalidate_recordset()
        self.assertTrue(self.route.access_app_id)
        self.assertEqual(self.fence()['attempt'], 2)

    def test_uncertain_create_is_not_repeated_inside_the_settle_window(self):
        self.refuse(502)
        with self.assertRaises(PaaSOperatorAPIError):
            self.route._sync_access_to_operator()
        fence = self.fence()
        self.assertEqual(fence['state'], 'dispatching')
        self.assertTrue(fence['effect_started_at'])
        # Back to normal, but the earlier outcome may still be finishing at the
        # provider: inside the settle window nothing is repeated.
        self.client.sync_access_app = self.sync_access_app
        self.client.events.clear()
        with self.assertLogs(ROUTE_LOG, level='ERROR') as logs:
            self.assertFalse(self.route._sync_access_to_operator())
        self.assertTrue(any('provider review required' in line
                            for line in logs.output), logs.output)
        self.assertNotIn('access', self.names())

    def test_recreate_after_teardown_starts_a_new_cycle(self):
        """Disable protection, enable it again: same app name and hostname, so
        the same fence identity.  A resolved row names an app that no longer
        exists; adopting it would PUT a dead id and fence the route for good."""
        self.assertTrue(self.route._sync_access_to_operator())
        self.route.invalidate_recordset()
        first_app = self.route.access_app_id
        self.assertTrue(first_app)
        self.assertEqual(self.fence()['provider_app_id'], first_app)
        self.set_fence_state('resolved')  # what the post-commit settlement does
        # Teardown: the provider app is gone and the platform forgot its id.
        _internal_sm_route_identity(self.route).access_app_id = False
        self.client.app = False
        self.client.events.clear()
        self.assertTrue(self.route._sync_access_to_operator())
        self.route.invalidate_recordset()
        self.assertTrue(self.route.access_app_id)
        enabled = [event[1] for event in self.client.events
                   if event[0] == 'access' and event[1].get('enabled')]
        self.assertTrue(enabled)
        # A create, not an adoption PUT of the old app id.
        self.assertFalse(enabled[-1].get('existing_app_id'))
        fence = self.fence()
        self.assertEqual(fence['attempt'], 2)
        self.assertEqual(fence['provider_app_id'], self.route.access_app_id)

    def age_fence(self, seconds):
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_paas_platform_access_dispatch_fence '
                'SET effect_started_at = effect_started_at - make_interval(secs => %s) '
                'WHERE anchor_model=%s AND anchor_id=%s',
                (seconds, ROUTE_MODEL, self.route.id))
            cr.commit()

    def test_uncertain_create_is_repeated_once_it_has_settled(self):
        self.refuse(502)
        with self.assertRaises(PaaSOperatorAPIError):
            self.route._sync_access_to_operator()
        self.assertEqual(self.fence()['state'], 'dispatching')
        self.client.sync_access_app = self.sync_access_app
        self.age_fence(3600)
        self.assertTrue(self.route._sync_access_to_operator())
        self.route.invalidate_recordset()
        self.assertTrue(self.route.access_app_id)
        self.assertEqual(self.fence()['attempt'], 2)

    def test_a_domain_held_by_a_foreign_app_is_refused_and_reopens_the_fence(self):
        """409 with no exact match of ours: not adoptable, and no app of ours exists."""
        self.client.unique_domains = True
        host = '%s.example.test' % self.sh.subdomain
        self.client.domains[host] = {'app_id': 'foreign-app', 'name': 'someone-elses-app'}
        with self.assertRaises(PaaSOperatorAPIError) as raised:
            self.route._sync_access_to_operator()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.fence()['state'], 'prepared')   # reopened, not stuck
        self.assertEqual(self.client.domains[host]['app_id'], 'foreign-app')
