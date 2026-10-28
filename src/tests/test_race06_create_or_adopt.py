"""RACE-06: a create whose outcome is unknown is simply repeated.

Cloudflare keeps exactly one Access application per domain: a repeated POST is
refused with 409 (error 11010), also for simultaneous POSTs -- measured on the live
account.  So after a worker death around the provider call the next sync either
creates the app (the first attempt never landed) or is told it exists and adopts the
app that matches our exact name and domain.  No administrator is needed, and there is
never a second app.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..services.access_state import (
    ACCESS_CREATE_SETTLE_DEFAULT,
    ACCESS_CREATE_SETTLE_MAX,
    ACCESS_CREATE_SETTLE_MIN,
    ACCESS_CREATE_SETTLE_PARAM,
    access_create_settle_seconds,
    sync_access_app_create_or_adopt,
)
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorError
from .test_access_publish_pending_ac1011 import PendingFixture

SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
             'get_paas_operator_client')
SLEEP = 'odoo.addons.woow_paas_platform.services.access_state.time.sleep'
FENCE_TABLE = 'woow_paas_platform_access_dispatch_fence'


class StubClient:
    """Scripted operator: every step is a result dict or an exception to raise."""

    def __init__(self, sync, review=()):
        self._sync, self._review = list(sync), list(review)
        self.sync_calls, self.review_calls = [], []

    def sync_access_app(self, payload):
        self.sync_calls.append(dict(payload))
        step = self._sync.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def review_access_app(self, app_name, primary_hostname):
        self.review_calls.append((app_name, primary_hostname))
        return self._review.pop(0)


def create_payload(**extra):
    return dict({'enabled': True, 'app_name': 'app-x', 'primary_hostname': 'x.example.test',
                 'existing_app_id': None, 'policies': []}, **extra)


CONFLICT = PaaSOperatorAPIError('application_already_exists', status_code=409)
UNIQUE = {'status': 'unique', 'app_id': 'app-existing', 'match_count': 1}
ABSENT = {'status': 'absent', 'app_id': None, 'match_count': 0}
AMBIGUOUS = {'status': 'ambiguous', 'app_id': None, 'match_count': 2}


class TestCreateOrAdoptHelper(TransactionCase):

    def test_a_normal_create_is_a_single_call(self):
        client = StubClient([{'app_id': 'new'}])
        result, used = sync_access_app_create_or_adopt(client, create_payload())
        self.assertEqual(result, {'app_id': 'new'})
        self.assertEqual(len(client.sync_calls), 1)
        self.assertFalse(client.review_calls)
        self.assertFalse(used['existing_app_id'])

    def test_a_conflict_adopts_the_exact_match_and_updates_it(self):
        client = StubClient([CONFLICT, {'app_id': 'app-existing'}], [UNIQUE])
        result, used = sync_access_app_create_or_adopt(client, create_payload())
        self.assertEqual(result['app_id'], 'app-existing')
        self.assertEqual(client.review_calls, [('app-x', 'x.example.test')])
        self.assertIsNone(client.sync_calls[0]['existing_app_id'])
        self.assertEqual(client.sync_calls[1]['existing_app_id'], 'app-existing')
        self.assertEqual(used['existing_app_id'], 'app-existing')

    def test_review_lag_after_the_conflict_is_waited_out(self):
        client = StubClient([CONFLICT, {'app_id': 'app-existing'}], [ABSENT, ABSENT, UNIQUE])
        with patch(SLEEP) as sleep:
            result, _used = sync_access_app_create_or_adopt(client, create_payload())
        self.assertEqual(result['app_id'], 'app-existing')
        self.assertEqual(len(client.review_calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_a_foreign_app_holding_the_domain_is_not_adopted(self):
        client = StubClient([CONFLICT], [ABSENT, ABSENT, ABSENT])
        with patch(SLEEP), self.assertRaises(PaaSOperatorAPIError) as raised:
            sync_access_app_create_or_adopt(client, create_payload())
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(client.sync_calls), 1)  # never repointed

    def test_ambiguous_matches_are_not_adopted(self):
        client = StubClient([CONFLICT], [AMBIGUOUS] * 3)
        with patch(SLEEP), self.assertRaises(PaaSOperatorAPIError):
            sync_access_app_create_or_adopt(client, create_payload())
        self.assertEqual(len(client.sync_calls), 1)

    def test_only_a_409_is_ever_reviewed(self):
        for status in (400, 404, 429, 500, 502, 504):
            client = StubClient([PaaSOperatorAPIError('x', status_code=status)])
            with self.assertRaises(PaaSOperatorAPIError):
                sync_access_app_create_or_adopt(client, create_payload())
            self.assertFalse(client.review_calls, status)
        client = StubClient([PaaSOperatorError('no status')])
        with self.assertRaises(PaaSOperatorError):
            sync_access_app_create_or_adopt(client, create_payload())

    def test_updates_and_teardowns_are_never_adopted(self):
        for payload in (create_payload(existing_app_id='app-known'), create_payload(enabled=False)):
            client = StubClient([CONFLICT])
            with self.assertRaises(PaaSOperatorAPIError):
                sync_access_app_create_or_adopt(client, payload)
            self.assertFalse(client.review_calls)

    def test_settle_parameter_default_clamp_and_garbage(self):
        params = self.env['ir.config_parameter'].sudo()
        params.search([('key', '=', ACCESS_CREATE_SETTLE_PARAM)]).unlink()
        self.assertEqual(access_create_settle_seconds(self.env), ACCESS_CREATE_SETTLE_DEFAULT)
        for raw, expected in (('', ACCESS_CREATE_SETTLE_DEFAULT), ('90', 90),
                              ('1', ACCESS_CREATE_SETTLE_MIN), ('-4', ACCESS_CREATE_SETTLE_MIN),
                              ('99999999', ACCESS_CREATE_SETTLE_MAX),
                              ('soon', ACCESS_CREATE_SETTLE_DEFAULT), ('1.5', ACCESS_CREATE_SETTLE_DEFAULT)):
            params.set_param(ACCESS_CREATE_SETTLE_PARAM, raw)
            self.assertEqual(access_create_settle_seconds(self.env), expected, raw)


@tagged('post_install', '-at_install')
class TestCrashAroundTheProviderCall(PendingFixture, TransactionCase):
    """A worker dies around the provider call; the next sync converges on one app."""

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        patcher = patch(SA_CLIENT, return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.unique_domains = True
        self.addCleanup(self.delete_fence_rows)

    def delete_fence_rows(self):
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM %s WHERE anchor_model = %%s' % FENCE_TABLE,
                       ('woow_paas_platform.smart_home_route',))
            cr.commit()

    def age_fence(self, seconds):
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE %s SET effect_started_at = effect_started_at - make_interval(secs => %%s) '
                'WHERE effect_started_at IS NOT NULL' % FENCE_TABLE, (seconds,))
            cr.commit()

    def creates(self):
        return [event[1] for event in self.client.events
                if event[0] == 'access' and event[1].get('enabled')
                and not event[1].get('existing_app_id')]

    def crash_after_landing_then_retry(self, kind):
        anchor = self.anchor(kind)
        self.client.die_after_landing = True
        with self.assertRaises(RuntimeError):  # rolls the DB write back, like a dead worker
            anchor.write({'is_protected': True})
        anchor.invalidate_recordset()
        self.assertFalse(anchor.access_app_id)             # the platform lost the id ...
        self.assertEqual(len(self.client.domains), 1)      # ... the provider has the app
        provider_app = next(iter(self.client.domains.values()))['app_id']
        if kind == 'sh':
            self.age_fence(ACCESS_CREATE_SETTLE_DEFAULT + 60)  # past the settle window
        self.assertTrue(anchor.write({'is_protected': True}))
        anchor.invalidate_recordset()
        self.assertEqual(len(self.client.domains), 1)      # never a second app
        self.assertEqual(anchor.access_app_id, provider_app)  # the existing one was adopted
        self.assertFalse(anchor.access_publish_pending)
        self.assertEqual(len(self.creates()), 2)           # the repeat was refused, then adopted

    def crash_before_the_provider_then_retry(self, kind):
        anchor = self.anchor(kind)
        self.client.die_before_provider = True
        with self.assertRaises(RuntimeError):
            anchor.write({'is_protected': True})
        anchor.invalidate_recordset()
        self.assertFalse(anchor.access_app_id)
        self.assertEqual(len(self.client.domains), 0)      # nothing reached the provider
        if kind == 'sh':
            self.age_fence(ACCESS_CREATE_SETTLE_DEFAULT + 60)
        self.assertTrue(anchor.write({'is_protected': True}))
        anchor.invalidate_recordset()
        self.assertEqual(len(self.client.domains), 1)
        self.assertEqual(anchor.access_app_id, next(iter(self.client.domains.values()))['app_id'])

    def test_sa_crash_after_landing_converges(self):
        self.crash_after_landing_then_retry('sa')

    def test_cs_crash_after_landing_converges(self):
        self.crash_after_landing_then_retry('cs')

    def test_sh_crash_after_landing_converges_after_the_settle_window(self):
        self.crash_after_landing_then_retry('sh')

    def test_sa_crash_before_the_provider_converges(self):
        self.crash_before_the_provider_then_retry('sa')

    def test_cs_crash_before_the_provider_converges(self):
        self.crash_before_the_provider_then_retry('cs')

    def test_sh_crash_before_the_provider_converges_after_the_settle_window(self):
        self.crash_before_the_provider_then_retry('sh')

    def test_sh_unknown_outcome_is_left_alone_inside_the_settle_window(self):
        anchor = self.anchor('sh')
        self.client.die_after_landing = True
        with self.assertRaises(RuntimeError):
            anchor.write({'is_protected': True})
        before = len(self.creates())
        anchor.write({'is_protected': True})  # too soon: the first call may still be finishing
        anchor.invalidate_recordset()
        self.assertEqual(len(self.creates()), before)
        self.assertFalse(anchor.access_app_id)
        self.assertEqual(len(self.client.domains), 1)
