"""LLM-07 paid API-key mirror contract (DESIGN §2.3, D4, D9, D10).

These tests deliberately stay below the HTTP boundary.  They lock down the
PaaS-owned mirror semantics first: durable expiry-notice fields, calculated display
status, integer total-limit DTO values, the per-account 20-key invariant, and the
fake-backed one-time paid-key creation transaction.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import PropertyMock, patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models import llm_api_key
from ..models.llm_account import REAUTH_SESSION_KEY
from ..services import newapi_client as nac
from ..models.llm_common import (
    API_ERROR_CODES,
    ERR_LLM_KEY_CANNOT_ENABLE,
    ERR_LLM_KEY_LIMIT_BELOW_USED,
    ERR_LLM_KEY_NOT_FOUND,
    MAX_KEYS_PER_ACCOUNT,
    QUOTA_PER_POINT,
    LlmError,
    _llm_internal,
)
from ..controllers.llm import _fail
from .llm_fake_newapi import FakeNewApi
from .llm_test_support import (
    ACCOUNT,
    EVENT,
    KEY,
    TEST_TOS,
    LlmClientAdapter,
    publish_test_terms,
    scope_claims,
)
from .test_llm_platform_foundation import _ConfiguredCase


CONTROLLER_LOGGER = 'odoo.addons.woow_paas_platform.controllers.llm'


class _TestSession(dict):
    """Minimal server-side session shape for service-level N25 tests."""

    def __init__(self, uid):
        super().__init__()
        self.uid = uid
        self.sid = uuid.uuid4().hex


@tagged('post_install', '-at_install', 'llm_key_management')
class TestLlmApiKeyMirror(TransactionCase):
    """The key mirror is authoritative for user-facing status, never new-api status 3/4."""

    def setUp(self):
        super().setUp()
        partner = self.env['res.partner'].create({'name': 'LLM key mirror owner'})
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': partner.id,
        })
        _llm_internal(self.account).write({'state': 'active'})
        self.Key = self.env[KEY]

    def _key(self, *, kind='paid', state='active', label='Key', **values):
        defaults = {
            'account_id': self.account.id,
            'kind': kind,
            'state': state,
            'key_label': label,
        }
        defaults.update(values)
        return _llm_internal(self.Key.sudo()).create(defaults)

    def test_mirror_has_expiry_notice_deduplication_fields(self):
        self.assertIn('expiry_notice_7d_at', self.Key._fields)
        self.assertIn('expiry_notice_1d_at', self.Key._fields)

    def test_key_management_errors_and_response_params_are_closed(self):
        self.assertLessEqual({
            ERR_LLM_KEY_NOT_FOUND,
            ERR_LLM_KEY_LIMIT_BELOW_USED,
            ERR_LLM_KEY_CANNOT_ENABLE,
        }, API_ERROR_CODES)
        response = _fail(ERR_LLM_KEY_LIMIT_BELOW_USED, used=30, reason='exhausted',
                         upstream_body='must never leave the server')
        self.assertEqual(response['used'], 30)
        self.assertEqual(response['reason'], 'exhausted')
        self.assertNotIn('upstream_body', response)

    def test_dto_exposes_integer_total_limit_usage_and_remaining_points(self):
        now = fields.Datetime.now()
        key = self._key(
            label='ci total-limit',
            quota_limit_points=100,
            used_quota_cached=30 * QUOTA_PER_POINT,
            remain_quota_cached=70 * QUOTA_PER_POINT,
            accessed_at=now,
        )

        dto = key._llm_dto()

        self.assertEqual(dto['limit_points'], 100)
        self.assertEqual(dto['used_points'], 30)
        self.assertEqual(dto['remain_points'], 70)
        self.assertEqual(dto['last_used_at'], now.replace(tzinfo=timezone.utc).isoformat())

    def test_quota_point_conversion_never_uses_a_false_or_float_cache_value(self):
        convert = self.Key._llm_points_from_quota

        self.assertEqual(convert(0), 0)
        self.assertEqual(convert(30 * QUOTA_PER_POINT + QUOTA_PER_POINT - 1), 30)
        self.assertIsNone(convert(None))
        self.assertIsNone(convert(False))
        self.assertIsNone(convert(1.5))
        self.assertIsNone(convert(-1))

    def test_status_is_calculated_from_mirror_not_newapi_runtime_status(self):
        expired = self._key(expires_at=fields.Datetime.now() - timedelta(seconds=1))
        exhausted = self._key(
            quota_limit_points=1,
            used_quota_cached=QUOTA_PER_POINT,
        )
        free_capped = self._key(
            kind='free_trial', state='disabled', disabled_by='free_daily_cap', label='Free cap')
        suspended = self._key(state='disabled', disabled_by='suspend', label='Suspended')
        overdraft = self._key(state='disabled', disabled_by='overdraft', label='Overdraft')
        user_disabled = self._key(state='disabled', disabled_by='user', label='User disabled')

        self.assertEqual(expired._llm_status(), 'expired')
        self.assertEqual(exhausted._llm_status(), 'exhausted')
        self.assertEqual(free_capped._llm_status(), 'free_capped')
        self.assertEqual(suspended._llm_status(), 'suspended')
        self.assertEqual(overdraft._llm_status(), 'overdraft')
        self.assertEqual(user_disabled._llm_status(), 'disabled')

    def test_stale_deleted_and_lost_keys_keep_mirror_derived_statuses(self):
        stale = self._key(state='creating', label='Stale creation')
        deleted = self._key(state='deleted', label='Deleted key')
        lost = self._key(state='lost', label='Lost key')

        with patch.object(
                llm_api_key, '_utcnow',
                return_value=stale.create_date + timedelta(seconds=301)):
            self.assertEqual(stale._llm_status(), 'failed_reveal')
        self.assertEqual(deleted._llm_status(), 'deleted')
        self.assertEqual(lost._llm_status(), 'lost')

    def test_twenty_non_deleted_non_lost_keys_are_the_account_limit(self):
        for index in range(MAX_KEYS_PER_ACCOUNT - 2):
            self._key(label='Live %s' % index)
        disabled = self._key(state='disabled', label='Disabled key')
        expired = self._key(
            expires_at=fields.Datetime.now() - timedelta(seconds=1), label='Expired key')
        self._key(state='deleted', label='Deleted key')
        self._key(state='lost', label='Lost key')

        with self.assertRaises(ValidationError):
            self._key(label='Key over limit')

        # Disabled/expired records retain their slots; only deleted/lost release one.
        _llm_internal(disabled).write({'state': 'deleted'})
        _llm_internal(expired).write({'state': 'lost'})
        self._key(label='Reclaimed one')
        self._key(label='Reclaimed two')
        with self.assertRaises(ValidationError):
            self._key(label='Over limit again')

    def _fake_paid_token(self):
        fake = FakeNewApi(clock=lambda: 1_800_000_000)
        ip = '198.18.17.9'
        owner_up = 'nap_' + 'a' * 40
        other_up = 'nap_' + 'b' * 40
        fake.users[101] = {'id': 101, 'username': 'wqa0000000101', 'status': 1}
        fake.users[102] = {'id': 102, 'username': 'wqa0000000102', 'status': 1}
        fake.pats[owner_up] = {'id': 501, 'user_id': 101, 'name': 'owner',
                               'expires_at': None, 'last_used_ip': ''}
        fake.pats[other_up] = {'id': 502, 'user_id': 102, 'name': 'other',
                               'expires_at': None, 'last_used_ip': ''}
        client = fake.client(nac.ROLE_WEB)
        created = client.user_create_token(
            ip, owner_up,
            nac.paid_token_body('wp_101_abcd', 1_900_000_000,
                                 100 * QUOTA_PER_POINT, False, True,
                                 'model-a,model-b'))
        self.assertTrue(created.ok)
        return fake, client, ip, owner_up, other_up, next(iter(fake.tokens))

    def test_fake_paid_token_management_authenticates_ownership_and_preserves_fields(self):
        fake, client, ip, owner_up, other_up, token_id = self._fake_paid_token()
        before = dict(fake.tokens[token_id])
        update = nac.paid_token_update_body(
            before, token_id, 'wp_101_abcd', remain_quota=70 * QUOTA_PER_POINT)

        self.assertTrue(client.user_update_token(ip, owner_up, update).ok)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], 70 * QUOTA_PER_POINT)
        for field in ('id', 'name', 'key', 'user_id', 'created_time', 'status', 'used_quota',
                      'allow_ips', 'group', 'cross_group_retry', 'allow_user_quota_query'):
            self.assertEqual(fake.tokens[token_id][field], before[field], field)

        foreign_update = dict(update, remain_quota=60 * QUOTA_PER_POINT)
        foreign_update_result = client.user_update_token(ip, other_up, foreign_update)
        foreign_status_result = client.user_set_token_status(ip, other_up, token_id, False)
        foreign_delete_result = client.user_delete_token(ip, other_up, token_id)
        self.assertIsInstance(foreign_update_result, nac.Unknown)
        self.assertIsInstance(foreign_status_result, nac.Unknown)
        self.assertIsInstance(foreign_delete_result, nac.Unknown)
        self.assertIsInstance(client.user_get_token(ip, other_up, token_id), nac.Definite)
        missing_get = client.user_get_token(ip, owner_up, token_id + 1000)
        self.assertIsInstance(missing_get, nac.Definite)
        self.assertEqual(missing_get.reason, 'no_message_key')
        missing_update = dict(update, id=token_id + 1000)
        self.assertIsInstance(client.user_update_token(ip, owner_up, missing_update), nac.Unknown)
        self.assertIsInstance(client.user_set_token_status(ip, owner_up, token_id + 1000, False),
                              nac.Unknown)
        self.assertIsInstance(client.user_delete_token(ip, owner_up, token_id + 1000), nac.Unknown)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], 70 * QUOTA_PER_POINT)

        self.assertTrue(client.user_set_token_status(ip, owner_up, token_id, False).ok)
        self.assertEqual(fake.tokens[token_id]['status'], 2)
        self.assertTrue(client.user_delete_token(ip, owner_up, token_id).ok)
        self.assertNotIn(token_id, fake.tokens)
        self.assertEqual([name for name, _client_ip in fake.calls], [
            'user_create_token', 'user_update_token', 'user_update_token',
            'user_set_token_status', 'user_delete_token', 'user_get_token', 'user_get_token',
            'user_update_token', 'user_set_token_status', 'user_delete_token',
            'user_set_token_status', 'user_delete_token',
        ])

    def test_fake_management_write_failure_injection_keeps_ambiguous_remote_side_effect(self):
        fake, client, ip, owner_up, _other_up, token_id = self._fake_paid_token()
        before = dict(fake.tokens[token_id])
        update = nac.paid_token_update_body(
            before, token_id, 'wp_101_abcd', remain_quota=70 * QUOTA_PER_POINT)

        fake.fail_before['user_update_token'] = nac.Definite(reason='rejected', status=400)
        self.assertFalse(client.user_update_token(ip, owner_up, update).ok)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], before['remain_quota'])

        fake.fail_before['user_update_token'] = nac.NotExecuted('proxy_connect', status=502)
        self.assertFalse(client.user_update_token(ip, owner_up, update).ok)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], before['remain_quota'])

        fake.fail_next['user_update_token'] = nac.Unknown('proxy_timeout', status=504)
        self.assertFalse(client.user_update_token(ip, owner_up, update).ok)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], 70 * QUOTA_PER_POINT,
                         'Unknown may mean new-api already committed the write')

    def test_fake_reveal_window_retains_the_120_second_boundary(self):
        fake, client, ip, owner_up, _other_up, token_id = self._fake_paid_token()

        fake.clock = lambda: 1_800_000_120
        self.assertTrue(client.user_reveal_token(ip, owner_up, token_id).ok)
        fake.clock = lambda: 1_800_000_121
        denied = client.user_reveal_token(ip, owner_up, token_id)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.reason, 'reveal_window')


@tagged('post_install', '-at_install', 'llm_key_management')
class TestLlmPaidKeyCreate(_ConfiguredCase):
    """Paid creation is a one-time reveal transaction with local gates first."""

    real_ip = '203.0.113.70'

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'llm-paid-key-%s' % suffix
        self.user = new_test_user(
            self.env, login='llm_paid_key_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.fake = FakeNewApi()
        self.client = LlmClientAdapter(self, self.fake, full_factory=True)
        scope_claims(self, lambda env: env[ACCOUNT].sudo().search(
            [('partner_id', '=', self.user.partner_id.id)]).ids)
        Account = self.env[ACCOUNT]
        Account._llm_activate(self.user.partner_id, TEST_TOS, self.user, self.real_ip)
        Account._cron_llm_provision()
        self.env.invalidate_all()
        self.account = Account._llm_account_for_partner(self.user.partner_id)
        self.assertEqual(self.account.state, 'active', self.account.last_error_code)
        # Keep the original portal uid while granting the service its narrowly scoped sudo path.
        self.service = self.env[KEY].with_user(self.user).sudo()

    def _catalogue(self):
        return {
            'state': 'ok',
            'items': [
                {'model': 'paid-a', 'group': 'default'},
                {'model': 'paid-b', 'group': 'default'},
                {'model': 'free-trial-model', 'group': 'free'},
            ],
        }

    def _session(self, granted=False):
        session = _TestSession(self.user.id)
        if granted:
            session[REAUTH_SESSION_KEY] = {
                'uid': self.user.id,
                'account_id': self.account.id,
                'sid': session.sid,
                'at': fields.Datetime.to_string(fields.Datetime.now()),
            }
        return session

    def _create(self, **overrides):
        values = {
            'name': 'n8n production',
            'limit_points': 100,
            'expires': '90d',
            'models': ['paid-a'],
        }
        values.update(overrides)
        request_id = values.pop('client_request_id', str(uuid.uuid4()))
        session = values.pop('session', self._session())
        catalogue = values.pop('catalogue', self._catalogue())
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=catalogue):
            return self.service._llm_create_paid_key(
                self.user.partner_id, request_id, self.user, self.real_ip, session, **values)

    def _http_create(self, **params):
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            response = self.url_open(
                '/api/llm/keys/create',
                data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                headers={'Content-Type': 'application/json'},
            )
        self.assertEqual(
            response.status_code, 200,
            'LLM key create endpoint returned unexpected HTTP status %s' % response.status_code,
        )
        body = response.json()
        self.assertNotIn(
            'error', body,
            'LLM key create endpoint returned a JSON-RPC error',
        )
        return body['result']

    def _error(self, call):
        try:
            call()
        except LlmError as exc:
            return exc.code, exc.params
        self.fail('Expected a fixed LlmError')

    def _management_key(self, *, kind='paid', expires_at=None, state='active', **values):
        defaults = {
            'account_id': self.account.id, 'kind': kind, 'state': state,
            'key_label': 'private label', 'newapi_name': 'wp_501_abcd',
            'newapi_token_id': 501, 'quota_limit_points': 100,
            'expires_at': expires_at, 'model_allowlist': 'paid-a,paid-b',
            'remain_quota_cached': 100 * QUOTA_PER_POINT,
            'used_quota_cached': 0,
        }
        defaults.update(values)
        return _llm_internal(self.env[KEY].sudo()).create(defaults)

    def _management_remote(self, *, expiry=1_900_000_000):
        fake = self.fake
        token = nac.paid_token_body('wp_501_abcd', expiry, 100 * QUOTA_PER_POINT,
                                    False, True, 'paid-a,paid-b')
        token.update({'id': 501, 'user_id': 101, 'key': 'never-read-plaintext',
                      'created_time': 1_800_000_000, 'used_quota': 30 * QUOTA_PER_POINT,
                      'status': 1, 'allow_ips': '', 'group': 'default',
                      'cross_group_retry': False, 'allow_user_quota_query': False})
        fake.tokens[501] = token

    def _management_call(self, key, method, *args, **kwargs):
        return getattr(self.service, method)(self.user.partner_id, key.id,
                                             user=self.user, real_ip=self.real_ip,
                                             **kwargs) if not args else getattr(
                                                 self.service, method)(self.user.partner_id,
                                                                       key.id, *args,
                                                                       user=self.user,
                                                                       real_ip=self.real_ip,
                                                                       **kwargs)

    def test_management_update_total_limit_safe_put_and_unknown_mirror(self):
        key = self._management_key()
        self._management_remote()
        result = self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            limit_points=100)
        self.assertEqual(result['remain_after_points'], 70)
        self.assertEqual(self.fake.tokens[501]['remain_quota'], 70 * QUOTA_PER_POINT)
        self.assertEqual(self.fake.tokens[501]['allow_ips'], '')
        self.assertEqual(key.newapi_name, 'wp_501_abcd')
        unlimited = self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            limit_points=None)
        self.assertEqual(self.fake.tokens[501]['remain_quota'], 0)
        self.assertTrue(self.fake.tokens[501]['unlimited_quota'])
        self.assertFalse(key.quota_limit_points)
        self.assertIsNone(unlimited['limit_points'])
        before = (key.quota_limit_points, key.remain_quota_cached)
        self.fake.fail_next['user_update_token'] = nac.Unknown('proxy_timeout', status=504)
        self._error(lambda: self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            limit_points=90))
        self.assertEqual((key.quota_limit_points, key.remain_quota_cached), before)

    def test_management_models_null_means_all_defaults_and_lists_are_canonical(self):
        key = self._management_key()
        self._management_remote()
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        catalogue = {'state': 'ok', 'items': [
            {'model': 'paid-b', 'group': 'default'},
            {'model': 'paid-a', 'group': 'default'},
            {'model': 'free-only', 'group': 'free'},
        ]}
        with patch.object(Cache, '_llm_models_payload', return_value=catalogue):
            self.service._llm_update_key(
                self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
                models=None)
        self.assertFalse(self.fake.tokens[501]['model_limits_enabled'])
        self.assertEqual(self.fake.tokens[501]['model_limits'], '')
        self.assertFalse(key.model_allowlist)

        with patch.object(Cache, '_llm_models_payload', return_value=catalogue):
            self.service._llm_update_key(
                self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
                models=['paid-a', 'paid-b'])
        self.assertTrue(self.fake.tokens[501]['model_limits_enabled'])
        self.assertEqual(self.fake.tokens[501]['model_limits'], 'paid-b,paid-a')
        self.assertEqual(key.model_allowlist, 'paid-b,paid-a')

    def test_management_model_catalogue_invalidity_fails_before_external_calls(self):
        key = self._management_key()
        before = (key.model_allowlist, key.key_label, key.state)
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        invalid_payloads = (
            {'state': 'stale', 'items': []},
            {'state': 'ok', 'items': ()},
            {'state': 'ok', 'items': [{'model': 'bad name', 'group': 'default'}]},
            {'state': 'ok', 'items': [
                {'model': 'paid-a', 'group': 'default'},
                {'model': 'paid-a', 'group': 'default'},
            ]},
            {'state': 'ok', 'items': [{'model': 'free-only', 'group': 'free'}]},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), patch.object(
                    Cache, '_llm_models_payload', return_value=payload):
                self.assertEqual(self._error(lambda: self.service._llm_update_key(
                    self.user.partner_id, key.id, user=self.user,
                    real_ip=self.real_ip, models=None))[0], 'llm_unavailable')
                self.assertEqual(self.fake.calls, [])
                self.assertEqual((key.model_allowlist, key.key_label, key.state), before)

    def test_management_update_explicit_null_expiry_and_cached_false_limit(self):
        key = self._management_key(quota_limit_points=False)
        self._management_remote()
        self.fake.tokens[501]['unlimited_quota'] = True
        self.fake.tokens[501]['remain_quota'] = 0
        result = self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            expires=None)
        self.assertEqual(self.fake.tokens[501]['expired_time'], -1)
        self.assertIsNone(key.expires_at)
        self.assertFalse(key.quota_limit_points)
        self.assertIsNone(result['limit_points'])
        self.assertTrue(self.fake.tokens[501]['unlimited_quota'])
        self.assertEqual(self.fake.tokens[501]['remain_quota'], 0)

    def test_management_total_limit_rejects_malformed_remote_used_quota(self):
        key = self._management_key()
        self._management_remote()
        before = (key.quota_limit_points, key.remain_quota_cached,
                  key.used_quota_cached, key.expires_at, key.model_allowlist,
                  key.key_label, key.state)
        for malformed in (True, None, -1):
            for requested_limit in (50, None):
                self.fake.calls[:] = []
                self.fake.tokens[501].pop('used_quota', None)
                if malformed is not None:
                    self.fake.tokens[501]['used_quota'] = malformed
                with self.subTest(used_quota=malformed, limit_points=requested_limit):
                    self.assertEqual(self._error(lambda: self.service._llm_update_key(
                        self.user.partner_id, key.id, user=self.user,
                        real_ip=self.real_ip, limit_points=requested_limit))[0],
                        'llm_unavailable')
                    self.assertEqual([call[0] for call in self.fake.calls], ['user_get_token'])
                    self.assertEqual((key.quota_limit_points, key.remain_quota_cached,
                                      key.used_quota_cached, key.expires_at, key.model_allowlist,
                                      key.key_label, key.state), before)

    def test_management_update_rejects_corrupt_local_integer_limits_before_external_calls(self):
        for invalid_limit in (0, -1):
            key = self._management_key(quota_limit_points=invalid_limit)
            before = (key.quota_limit_points, key.expires_at, key.model_allowlist)
            with self.subTest(limit_points=invalid_limit):
                self.assertEqual(self._error(lambda: self.service._llm_update_key(
                    self.user.partner_id, key.id, user=self.user,
                    real_ip=self.real_ip, expires='90d'))[0], 'llm_key_invalid')
                self.assertEqual(self.fake.calls, [])
                self.assertEqual((key.quota_limit_points, key.expires_at,
                                  key.model_allowlist), before)

    def test_management_untrusted_remote_shapes_fail_unavailable_without_mutation(self):
        key = self._management_key()
        self._management_remote()
        before = (key.state, key.key_label, key.quota_limit_points, key.expires_at,
                  key.model_allowlist, key.remain_quota_cached, key.used_quota_cached,
                  key.expiry_notice_7d_at, key.expiry_notice_1d_at)
        for operation in ('update', 'extend'):
            self.fake.calls[:] = []
            self.fake.tokens[501]['allow_ips'] = '198.51.100.8'
            with self.subTest(operation=operation):
                call = (lambda: self.service._llm_update_key(
                    self.user.partner_id, key.id, user=self.user,
                    real_ip=self.real_ip, expires='90d')) if operation == 'update' else (
                    lambda: self.service._llm_extend_key(
                        self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip))
                self.assertEqual(self._error(call)[0], 'llm_unavailable')
                self.assertEqual([call[0] for call in self.fake.calls], ['user_get_token'])
                self.assertEqual((key.state, key.key_label, key.quota_limit_points,
                                  key.expires_at, key.model_allowlist, key.remain_quota_cached,
                                  key.used_quota_cached, key.expiry_notice_7d_at,
                                  key.expiry_notice_1d_at), before)
            self.fake.tokens[501]['allow_ips'] = ''

    def test_management_extend_never_exact_expiry_and_unknown_mirror(self):
        never = self._management_key(expires_at=None)
        dto = self.service._llm_extend_key(
            self.user.partner_id, never.id, user=self.user, real_ip=self.real_ip)
        self.assertEqual(dto['id'], never.id)
        self.assertEqual(self.fake.calls, [])

        now = datetime(2026, 10, 19, 12, 0, 0)
        now_epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
        for remote_expiry in (1_700_000_000, 1_900_000_000):
            key = self._management_key(
                expires_at=now - timedelta(days=5),
                expiry_notice_7d_at=now - timedelta(days=1),
                expiry_notice_1d_at=now - timedelta(hours=1))
            self._management_remote(expiry=remote_expiry)
            with patch.object(llm_api_key, '_utcnow', return_value=now):
                self.service._llm_extend_key(
                    self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip)
            self.assertEqual(self.fake.tokens[501]['expired_time'],
                             max(now_epoch, remote_expiry) + 90 * 86400)
            self.assertFalse(key.expiry_notice_7d_at)
            self.assertFalse(key.expiry_notice_1d_at)

        key = self._management_key(
            expires_at=now - timedelta(days=5), expiry_notice_7d_at=now,
            expiry_notice_1d_at=now)
        self._management_remote(expiry=1_900_000_000)
        before = (key.expires_at, key.expiry_notice_7d_at, key.expiry_notice_1d_at)
        self.fake.fail_before['user_update_token'] = nac.Unknown('proxy_timeout', status=504)
        self._error(lambda: self.service._llm_extend_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip))
        self.assertEqual((key.expires_at, key.expiry_notice_7d_at,
                          key.expiry_notice_1d_at), before)

    def test_management_status_successful_enable_and_actual_boolean_validation(self):
        key = self._management_key(state='disabled', disabled_by='user')
        self._management_remote()
        self.fake.tokens[501]['status'] = 2
        for enabled in (1, 0, None):
            self.assertEqual(self._error(lambda: self.service._llm_set_key_status(
                self.user.partner_id, key.id, enabled, user=self.user,
                real_ip=self.real_ip))[0], 'llm_key_invalid')
        self.assertEqual(self.fake.calls, [])
        result = self.service._llm_set_key_status(
            self.user.partner_id, key.id, True, user=self.user, real_ip=self.real_ip)
        self.assertEqual(self.fake.tokens[501]['status'], 1)
        self.assertEqual((key.state, key.disabled_by), ('active', False))
        self.assertEqual(result['status'], 'active')

    def test_management_delete_requires_remote_id_except_failed_reveal_and_absence_converges(self):
        self.fake.tokens.pop(501, None)
        for operation in ('update', 'extend'):
            key = self._management_key(
                expires_at=fields.Datetime.now() + timedelta(days=30))
            self.fake.calls[:] = []
            with self.subTest(missing_remote_operation=operation):
                if operation == 'update':
                    call = lambda: self.service._llm_update_key(
                        self.user.partner_id, key.id, user=self.user,
                        real_ip=self.real_ip, expires='90d')
                else:
                    call = lambda: self.service._llm_extend_key(
                        self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip)
                self.assertEqual(self._error(call)[0], ERR_LLM_KEY_NOT_FOUND)
                self.assertEqual(key.state, 'deleted')
                self.assertEqual([call[0] for call in self.fake.calls], ['user_get_token'])

        for state, token_id in (('active', False), ('disabled', 0)):
            self.fake.calls[:] = []
            key = self._management_key(state=state, newapi_token_id=token_id)
            with self.subTest(state=state):
                self.assertEqual(self._error(lambda: self.service._llm_delete_key(
                    self.user.partner_id, key.id, 'private label', user=self.user,
                    real_ip=self.real_ip))[0], 'llm_unavailable')
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(key.state, state)

        key = self._management_key()
        self.fake.tokens.pop(501, None)
        self.fake.calls[:] = []
        self.assertEqual(self._error(lambda: self.service._llm_delete_key(
            self.user.partner_id, key.id, 'private label', user=self.user,
            real_ip=self.real_ip))[0], 'llm_unavailable')
        self.assertEqual(key.state, 'active')
        self.assertNotIn(501, self.fake.tokens)
        self.assertEqual([call[0] for call in self.fake.calls], ['user_delete_token'])

    def test_management_own_unavailable_states_are_zero_external_calls(self):
        for state in ('deleted', 'lost', 'creating', 'failed_reveal'):
            key = self._management_key(state=state)
            with self.subTest(state=state):
                self.assertEqual(self._error(lambda: self.service._llm_update_key(
                    self.user.partner_id, key.id, user=self.user,
                    real_ip=self.real_ip, name='changed'))[0], ERR_LLM_KEY_NOT_FOUND)
                self.assertEqual(self.fake.calls, [])

    def test_management_remote_operations_reject_invalid_mirror_token_ids_pre_io(self):
        invalid_ids = (False, 0, -1, nac.INT32_MAX + 1)
        for state in ('active', 'disabled'):
            for invalid_id in invalid_ids:
                key = self._management_key(
                    state=state, newapi_token_id=(501 if invalid_id == nac.INT32_MAX + 1
                                                  else invalid_id),
                    expires_at=fields.Datetime.now() + timedelta(days=30))
                before_state = key.state
                before_label = key.key_label
                before_expiry = key.expires_at
                for operation in ('update', 'extend', 'status', 'delete'):
                    self.fake.calls[:] = []
                    with self.subTest(state=state, token_id=invalid_id, operation=operation):
                        token_patch = patch.object(
                            type(key), 'newapi_token_id', new_callable=PropertyMock,
                            return_value=invalid_id)
                        with token_patch:
                            if operation == 'update':
                                call = lambda: self.service._llm_update_key(
                                    self.user.partner_id, key.id, user=self.user,
                                    real_ip=self.real_ip, expires='90d')
                            elif operation == 'extend':
                                call = lambda: self.service._llm_extend_key(
                                    self.user.partner_id, key.id, user=self.user,
                                    real_ip=self.real_ip)
                            elif operation == 'status':
                                call = lambda: self.service._llm_set_key_status(
                                    self.user.partner_id, key.id, False, user=self.user,
                                    real_ip=self.real_ip)
                            else:
                                call = lambda: self.service._llm_delete_key(
                                    self.user.partner_id, key.id, 'private label',
                                    user=self.user, real_ip=self.real_ip)
                            self.assertEqual(self._error(call)[0], 'llm_unavailable')
                        self.assertEqual(self.fake.calls, [])
                        self.assertEqual((key.state, key.key_label, key.expires_at),
                                         (before_state, before_label, before_expiry))

        local_key = self._management_key(
            state='active', newapi_token_id=False, expires_at=None)
        self.service._llm_update_key(
            self.user.partner_id, local_key.id, user=self.user, real_ip=self.real_ip,
            name='local label only')
        self.service._llm_extend_key(
            self.user.partner_id, local_key.id, user=self.user, real_ip=self.real_ip)
        self.assertEqual(local_key.key_label, 'local label only')
        self.assertEqual(self.fake.calls, [])

    def test_management_update_omitted_false_limit_remains_unlimited(self):
        key = self._management_key(quota_limit_points=False)
        self._management_remote()
        self.fake.tokens[501]['unlimited_quota'] = True
        self.fake.tokens[501]['remain_quota'] = 0
        self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            expires='90d')
        self.assertTrue(self.fake.tokens[501]['unlimited_quota'])
        self.assertEqual(self.fake.tokens[501]['remain_quota'], 0)
        self.assertFalse(key.quota_limit_points)

    def test_management_update_rejects_total_below_remote_used_before_write(self):
        key = self._management_key()
        self._management_remote()
        error = self._error(lambda: self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            limit_points=20))
        self.assertEqual(error[0], ERR_LLM_KEY_LIMIT_BELOW_USED)
        self.assertEqual(error[1].get('used'), 30)
        self.assertEqual([c[0] for c in self.fake.calls], ['user_get_token'])

    def test_management_extend_paid_expired_and_never_and_status_delete(self):
        key = self._management_key(expires_at=fields.Datetime.now() - timedelta(days=2),
                                   expiry_notice_7d_at=fields.Datetime.now(),
                                   expiry_notice_1d_at=fields.Datetime.now())
        self._management_remote(expiry=1_700_000_000)
        result = self.service._llm_extend_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip)
        self.assertEqual(result['id'], key.id)
        self.assertFalse(key.expiry_notice_7d_at)
        self.assertFalse(key.expiry_notice_1d_at)

        self.service._llm_set_key_status(
            self.user.partner_id, key.id, False, user=self.user, real_ip=self.real_ip,
            session=self._session())
        self.assertEqual((key.state, key.disabled_by), ('disabled', 'user'))
        result = self.service._llm_delete_key(
            self.user.partner_id, key.id, 'private label', user=self.user,
            real_ip=self.real_ip)
        self.assertEqual(result, {'deleted': True})
        self.assertEqual(key.state, 'deleted')
        self.assertNotIn(501, self.fake.tokens)

    def test_management_foreign_key_is_never_looked_up_or_called(self):
        other = self.env['res.partner'].create({'name': 'other management owner'})
        key = self._management_key()
        calls = (
            lambda: self.service._llm_update_key(other, key.id, user=self.user,
                                                 real_ip=self.real_ip, name='changed'),
            lambda: self.service._llm_extend_key(other, key.id, user=self.user,
                                                 real_ip=self.real_ip),
            lambda: self.service._llm_set_key_status(other, key.id, True, user=self.user,
                                                      real_ip=self.real_ip),
            lambda: self.service._llm_set_key_status(other, key.id, False, user=self.user,
                                                      real_ip=self.real_ip),
            lambda: self.service._llm_delete_key(other, key.id, 'private label', user=self.user,
                                                  real_ip=self.real_ip),
        )
        for call in calls:
            self.assertEqual(self._error(call)[0], ERR_LLM_KEY_NOT_FOUND)
        self.assertEqual(self.fake.calls, [])

    def test_management_enable_local_blocks_terms_and_unknown_are_fail_closed(self):
        blocked = ('expired', 'exhausted', 'free_capped', 'suspended', 'overdraft')
        for status in blocked:
            if status == 'expired':
                key = self._management_key(expires_at=fields.Datetime.now() - timedelta(days=1))
            elif status == 'exhausted':
                key = self._management_key(quota_limit_points=1,
                                           used_quota_cached=QUOTA_PER_POINT)
            elif status == 'free_capped':
                key = self._management_key(kind='free_trial', state='disabled',
                                           disabled_by='free_daily_cap')
            else:
                reason = 'suspend' if status == 'suspended' else 'overdraft'
                key = self._management_key(state='disabled', disabled_by=reason)
            with self.subTest(status=status):
                self.assertEqual(self._error(lambda: self.service._llm_set_key_status(
                    self.user.partner_id, key.id, True, user=self.user,
                    real_ip=self.real_ip))[0], 'llm_key_cannot_enable')
        key = self._management_key()
        with patch.object(type(self.account), '_llm_require_current_terms',
                          side_effect=LlmError('llm_tos_outdated')):
            self.assertEqual(self._error(lambda: self.service._llm_set_key_status(
                self.user.partner_id, key.id, True, user=self.user,
                real_ip=self.real_ip))[0], 'llm_tos_outdated')
        self.assertEqual(self.fake.calls, [])
        self._management_remote()
        self.fake.fail_before['user_set_token_status'] = nac.Unknown('proxy_timeout', status=504)
        self._error(lambda: self.service._llm_set_key_status(
            self.user.partner_id, key.id, False, user=self.user, real_ip=self.real_ip))
        self.assertEqual((key.state, key.disabled_by), ('active', False))

    def test_management_delete_confirmation_unknown_and_failed_reveal_local_cleanup(self):
        key = self._management_key()
        self._management_remote()
        self.assertEqual(self._error(lambda: self.service._llm_delete_key(
            self.user.partner_id, key.id, 'wrong', user=self.user,
            real_ip=self.real_ip))[0], 'llm_key_invalid')
        self.assertEqual(self.fake.calls, [])
        self.fake.fail_before['user_delete_token'] = nac.Unknown('proxy_timeout', status=504)
        self._error(lambda: self.service._llm_delete_key(
            self.user.partner_id, key.id, 'private label', user=self.user,
            real_ip=self.real_ip))
        self.assertEqual(key.state, 'active')
        failed = self._management_key(state='failed_reveal', key_label='interrupted',
                                      newapi_token_id=False, newapi_name=False)
        self.service._llm_delete_key(self.user.partner_id, failed.id, 'interrupted',
                                     user=self.user, real_ip=self.real_ip)
        self.assertEqual(failed.state, 'deleted')
        self.assertEqual([c[0] for c in self.fake.calls], ['user_delete_token'])

    def test_management_delete_expired_trial_releases_unique_slot(self):
        trial = self._management_key(kind='free_trial', expires_at=fields.Datetime.now() -
                                     timedelta(days=1), key_label='trial')
        self.service._llm_delete_key(self.user.partner_id, trial.id, 'trial', user=self.user,
                                     real_ip=self.real_ip)
        replacement = self._management_key(kind='free_trial', key_label='replacement')
        self.assertEqual(replacement.state, 'active')
        self.assertEqual(self.fake.calls, [])

    def test_management_success_events_use_closed_actions_and_safe_targets(self):
        key = self._management_key()
        self._management_remote()
        self.service._llm_update_key(self.user.partner_id, key.id, user=self.user,
                                     real_ip=self.real_ip, name='renamed locally')
        self.service._llm_set_key_status(self.user.partner_id, key.id, False, user=self.user,
                                         real_ip=self.real_ip)
        self.service._llm_delete_key(self.user.partner_id, key.id, 'renamed locally',
                                     user=self.user, real_ip=self.real_ip)
        rows = self.env[EVENT].sudo().search([
            ('account_id', '=', self.account.id), ('target_model', '=', 'woow_paas_platform.llm_api_key'),
            ('target_id', '=', key.id), ('action', 'in', ('key_update', 'key_status', 'key_delete')),
        ])
        self.assertEqual(set(rows.mapped('action')), {'key_update', 'key_status', 'key_delete'})
        for row in rows:
            self.assertEqual(row.partner_id.id, self.account.partner_id.id)
            self.assertEqual(row.uid.id, self.user.id)
            self.assertEqual(row.real_ip, self.real_ip)
            self.assertFalse(row.details_json)
            self.assertNotIn('renamed locally', row.details_json or '')
            self.assertNotIn('wp_501_abcd', row.details_json or '')

    def test_management_free_trial_edit_and_extend_reject_without_io(self):
        key = self._management_key(kind='free_trial')
        self.assertEqual(self._error(lambda: self.service._llm_update_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip,
            limit_points=20))[0], 'llm_key_invalid')
        self.assertEqual(self._error(lambda: self.service._llm_extend_key(
            self.user.partner_id, key.id, user=self.user, real_ip=self.real_ip))[0],
            'llm_key_invalid')
        self.assertEqual(self.fake.calls, [])

    def test_http_paid_create_reveals_once_and_ignores_untrusted_context_fields(self):
        self.authenticate(self.user.login, self.password)
        other_partner = self.env['res.partner'].create({'name': 'Untrusted create partner'})
        request_id = str(uuid.uuid4())
        before_calls = len(self.fake.calls)

        result = self._http_create(
            kind='paid', client_request_id=request_id, name='HTTP paid key',
            limit_points=100, expires='90d', models=['paid-a'],
            partner_id=other_partner.id, account_id=self.account.id + 1,
            user_id=1, session={'uid': 1}, real_ip='198.51.100.99',
            up_token='must-not-be-forwarded', arbitrary='ignored',
        )

        self.assertTrue(result['success'], result)
        self.assertRegex(result['data']['key'], r'^sk-[A-Za-z0-9]{48}$')
        mirror = self.env[KEY].sudo().browse(result['data']['key_id'])
        self.assertEqual(mirror.account_id.partner_id, self.user.partner_id)
        self.assertEqual(mirror.key_label, 'HTTP paid key')
        self.assertGreater(len(self.fake.calls), before_calls)

    def test_http_paid_create_replay_never_reveals_or_calls_upstream_again(self):
        self.authenticate(self.user.login, self.password)
        params = {
            'kind': 'paid', 'client_request_id': str(uuid.uuid4()),
            'name': 'HTTP replay key', 'limit_points': 25,
            'expires': '30d', 'models': ['paid-a'],
        }

        first = self._http_create(**params)
        calls_after_first = list(self.fake.calls)
        replay = self._http_create(**params)

        self.assertTrue(first['success'], first)
        self.assertFalse(replay['success'], replay)
        self.assertEqual(replay['code'], 'llm_key_already_shown')
        self.assertNotIn('key', replay.get('data', {}))
        self.assertEqual(self.fake.calls, calls_after_first)
        self.assertEqual(len(self.fake.tokens), 1)

    def test_http_paid_terms_gate_rejects_before_throttle_and_upstream(self):
        self.authenticate(self.user.login, self.password)
        publish_test_terms(self.env, 'qa-http-paid-terms-block', requires_reaccept=True)
        self.account.invalidate_recordset()
        before_calls = list(self.fake.calls)
        before_events = len(self._key_events())

        result = self._http_create(
            kind='paid', client_request_id=str(uuid.uuid4()), name='Blocked key',
            limit_points=10, expires='30d', models=['paid-a'],
        )

        self.assertFalse(result['success'], result)
        self.assertEqual(result['code'], 'llm_tos_outdated')
        self.assertEqual(self.fake.calls, before_calls)
        self.assertEqual(len(self._key_events()), before_events)

    def test_http_paid_reauth_gate_rejects_unlimited_never_expiring_key_before_upstream(self):
        self.authenticate(self.user.login, self.password)
        before_calls = list(self.fake.calls)
        before_events = len(self._key_events())

        result = self._http_create(
            kind='paid', client_request_id=str(uuid.uuid4()), name='HTTP permanent key',
            limit_points=None, expires='never', models=None,
        )

        self.assertFalse(result['success'], result)
        self.assertEqual(result['code'], 'llm_reauth_required')
        self.assertEqual(self.fake.calls, before_calls)
        self.assertEqual(len(self._key_events()), before_events)

    def test_http_free_trial_create_regression(self):
        self.authenticate(self.user.login, self.password)

        result = self._http_create(
            kind='free_trial', client_request_id=str(uuid.uuid4()),
            arbitrary='ignored', partner_id=self.account.id + 100,
        )

        self.assertTrue(result['success'], result)
        mirror = self.env[KEY].sudo().browse(result['data']['key_id'])
        self.assertEqual((mirror.kind, mirror.account_id.partner_id),
                         ('free_trial', self.user.partner_id))
        self.assertRegex(result['data']['key'], r'^sk-[A-Za-z0-9]{48}$')

    def test_http_paid_route_forwards_only_named_fields_and_trusted_context(self):
        self.authenticate(self.user.login, self.password)
        request_id = str(uuid.uuid4())
        with patch.object(type(self.env[KEY]), '_llm_create_paid_key',
                          return_value={'key': 'sk-' + 'A' * 48}) as create:
            result = self._http_create(
                kind='paid', client_request_id=request_id, name='Allowlist key',
                limit_points=40, expires='90d', models=['paid-a'],
                partner_id=1, user_id=1, session='untrusted', real_ip='192.0.2.200',
                arbitrary='untrusted',
            )

        self.assertTrue(result['success'], result)
        create.assert_called_once()
        args, kwargs = create.call_args
        self.assertEqual(args[0], self.user.partner_id)
        self.assertEqual(args[1], request_id)
        self.assertEqual(args[2], self.user)
        self.assertNotEqual(args[3], '192.0.2.200')
        self.assertIsInstance(args[4], dict)
        self.assertNotEqual(args[4], 'untrusted')
        self.assertEqual(set(kwargs), {'name', 'limit_points', 'expires', 'models'})
        self.assertEqual(kwargs, {
            'name': 'Allowlist key', 'limit_points': 40,
            'expires': '90d', 'models': ['paid-a'],
        })

    def test_http_paid_service_exception_is_not_returned_to_client(self):
        self.authenticate(self.user.login, self.password)
        secret = 'upstream secret response body'
        with patch.object(type(self.env[KEY]), '_llm_create_paid_key',
                          side_effect=RuntimeError(secret)):
            with self.assertLogs(CONTROLLER_LOGGER, level=logging.ERROR) as logs:
                result = self._http_create(
                    kind='paid', client_request_id=str(uuid.uuid4()), name='Error key',
                    limit_points=10, expires='30d', models=['paid-a'],
                )

        client_output = json.dumps(result)
        log_output = '\n'.join(logs.output)
        self.assertFalse(result['success'], 'paid service exception must fail closed')
        self.assertEqual(result['code'], 'llm_internal')
        self.assertFalse(secret in client_output, 'client response contained exception text')
        self.assertFalse(secret in log_output, 'controller log contained exception text')
        self.assertFalse('Traceback' in log_output, 'controller log contained a traceback')
        self.assertTrue('endpoint=keys/create' in log_output,
                        'controller log lacked endpoint context')
        self.assertTrue('uid=' in log_output, 'controller log lacked uid context')

    def test_http_unknown_kind_is_stably_rejected_without_upstream_call(self):
        self.authenticate(self.user.login, self.password)
        before_calls = list(self.fake.calls)

        result = self._http_create(kind='future-kind', client_request_id=str(uuid.uuid4()))

        self.assertFalse(result['success'], result)
        self.assertEqual(result['code'], 'llm_key_invalid')
        self.assertEqual(self.fake.calls, before_calls)

    def _key_events(self):
        return self.env[EVENT].sudo().search([
            ('account_id', '=', self.account.id), ('action', '=', 'key_create'),
        ], order='id')

    def _service_account(self):
        return self.env[ACCOUNT].with_user(self.user).sudo()._llm_account_for_partner(
            self.user.partner_id)

    def _mirror(self, **values):
        defaults = {
            'account_id': self.account.id,
            'kind': 'paid',
            'state': 'active',
        }
        defaults.update(values)
        return _llm_internal(self.env[KEY].sudo()).create(defaults)

    def test_limited_paid_creation_uses_closed_payload_and_never_persists_plaintext(self):
        now = datetime(2032, 3, 4, 5, 6, 7)
        with patch.object(llm_api_key, '_utcnow', return_value=now):
            created = self._create(
                name='N8N — 正式', limit_points=100, expires='90d',
                models=['paid-b', 'paid-a'])

        self.assertRegex(created['key'], r'^sk-[A-Za-z0-9]{48}$')
        mirror = self.env[KEY].sudo().browse(created['key_id'])
        self.assertEqual((mirror.kind, mirror.state, mirror.key_label),
                         ('paid', 'active', 'N8N — 正式'))
        self.assertEqual(mirror.quota_limit_points, 100)
        self.assertEqual(mirror.expires_at, now + timedelta(days=90))
        self.assertEqual(mirror.model_allowlist, 'paid-a,paid-b')
        [token] = self.fake.tokens.values()
        self.assertRegex(token['name'], r'^wp_[0-9]+_[a-z0-9]{4}$')
        self.assertNotEqual(token['name'], mirror.key_label)
        self.assertEqual({
            'expired_time': token['expired_time'],
            'remain_quota': token['remain_quota'],
            'unlimited_quota': token['unlimited_quota'],
            'model_limits_enabled': token['model_limits_enabled'],
            'model_limits': token['model_limits'],
            'allow_ips': token['allow_ips'],
            'group': token['group'],
            'cross_group_retry': token['cross_group_retry'],
            'allow_user_quota_query': token['allow_user_quota_query'],
        }, {
            'expired_time': int((now + timedelta(days=90)).replace(
                tzinfo=timezone.utc).timestamp()),
            'remain_quota': 100 * QUOTA_PER_POINT,
            'unlimited_quota': False,
            'model_limits_enabled': True,
            'model_limits': 'paid-a,paid-b',
            'allow_ips': '',
            'group': 'default',
            'cross_group_retry': False,
            'allow_user_quota_query': False,
        })
        raw = created['key'][3:]
        self.assertNotIn(raw, json.dumps(mirror.read()[0], default=str))
        self.assertNotIn(raw, json.dumps(self.fake.calls))
        self.assertNotIn(raw, json.dumps(self._key_events().read(), default=str))

    def test_never_unlimited_requires_session_reauth_before_any_write_then_creates(self):
        calls_before = len(self.fake.calls)
        events_before = len(self._key_events())
        blocked = self._error(lambda: self._create(
            name='long-lived automation', limit_points=None, expires='never', models=None,
            session=self._session()))
        self.assertEqual(blocked, ('llm_reauth_required', {}))
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(len(self._key_events()), events_before)

        created = self._create(
            name='long-lived automation', limit_points=None, expires='never', models=None,
            session=self._session(granted=True))
        [token] = self.fake.tokens.values()
        mirror = self.env[KEY].sudo().browse(created['key_id'])
        self.assertEqual((token['expired_time'], token['remain_quota'], token['unlimited_quota'],
                          token['model_limits_enabled'], token['model_limits']),
                         (-1, 0, True, False, ''))
        self.assertFalse(mirror.expires_at)
        self.assertFalse(mirror.quota_limit_points)
        self.assertFalse(mirror.model_allowlist)

    def test_ordinary_paid_creation_never_checks_reauth(self):
        Account = type(self.env[ACCOUNT])
        with patch.object(Account, '_llm_require_reauth',
                          side_effect=AssertionError('ordinary key requested reauth')):
            created = self._create(limit_points=1, expires='30d')
        self.assertTrue(created['key'])

    def test_replayed_client_request_is_not_resent_or_reshown(self):
        request_id = str(uuid.uuid4())
        first = self._create(client_request_id=request_id)
        calls = list(self.fake.calls)
        events = len(self._key_events())

        replay = self._error(lambda: self._create(client_request_id=request_id))

        self.assertEqual(replay, ('llm_key_already_shown', {}))
        self.assertEqual(self.fake.calls, calls)
        self.assertEqual(len(self._key_events()), events)
        self.assertEqual(len(self.fake.tokens), 1)
        self.assertTrue(first['key'])

    def test_unknown_create_with_incomplete_token_list_never_retries_post(self):
        incomplete_responses = (
            ('items_missing', {'total': 0}),
            ('items_not_list', {'items': {}, 'total': 0}),
            ('total_missing', {'items': []}),
            ('total_not_int', {'items': [], 'total': '0'}),
            ('total_bool', {'items': [], 'total': True}),
            ('total_negative', {'items': [], 'total': -1}),
            ('total_mismatch', {
                'items': [
                    {'id': index + 1, 'name': 'unrelated-%s' % index}
                    for index in range(10)
                ],
                'total': 11,
            }),
            ('invalid_item', {'items': [{'id': '1', 'name': 'unrelated'}], 'total': 1}),
        )
        for response_shape, response_data in incomplete_responses:
            with self.subTest(response_shape=response_shape):
                before_calls = len(self.fake.calls)
                before_tokens = len(self.fake.tokens)
                request_id = str(uuid.uuid4())
                self.fake.fail_next['user_create_token'] = nac.Unknown(
                    'proxy_timeout', status=504)
                self.fake.fail_next['user_list_tokens'] = nac.Ok(
                    data=response_data, message='', status=200)

                failed = self._error(lambda: self._create(client_request_id=request_id))

                calls = self.fake.calls[before_calls:]
                self.assertEqual(failed, ('llm_unavailable', {}))
                self.assertEqual(
                    sum(name == 'user_create_token' for name, _ip in calls), 1)
                self.assertEqual(
                    sum(name == 'user_list_tokens' for name, _ip in calls), 1)
                self.assertEqual(
                    sum(name == 'user_reveal_token' for name, _ip in calls), 0)
                self.assertEqual(len(self.fake.tokens), before_tokens + 1)
                self.env.invalidate_all()
                mirror = self.env[KEY].sudo().search([
                    ('account_id', '=', self.account.id),
                    ('client_request_id', '=', request_id),
                ])
                self.assertEqual(len(mirror), 1)
                self.assertEqual(mirror.state, 'failed_reveal')

    def test_unknown_reveal_fails_closed_without_retry_or_plaintext(self):
        before_calls = len(self.fake.calls)
        request_id = str(uuid.uuid4())
        self.fake.fail_next['user_reveal_token'] = nac.Unknown('proxy_timeout', status=504)

        failed = self._error(lambda: self._create(client_request_id=request_id))

        calls = self.fake.calls[before_calls:]
        self.assertEqual(failed, ('llm_unavailable', {}))
        self.assertEqual(sum(name == 'user_create_token' for name, _ip in calls), 1)
        self.assertEqual(sum(name == 'user_list_tokens' for name, _ip in calls), 1)
        self.assertEqual(sum(name == 'user_reveal_token' for name, _ip in calls), 1)
        self.env.invalidate_all()
        mirror = self.env[KEY].sudo().search([
            ('account_id', '=', self.account.id),
            ('client_request_id', '=', request_id),
        ])
        self.assertEqual(len(mirror), 1)
        self.assertEqual(mirror.state, 'failed_reveal')
        self.assertGreater(mirror.newapi_token_id, 0)
        [token] = self.fake.tokens.values()
        plaintext = token['key']
        for output in (
                json.dumps(failed, default=str),
                json.dumps(mirror.read(), default=str),
                json.dumps(self._key_events().read(), default=str)):
            self.assertFalse(plaintext in output,
                             'plaintext must not be returned or persisted')

    def test_twenty_live_keys_rejects_the_twenty_first_before_throttle_or_external_call(self):
        for index in range(MAX_KEYS_PER_ACCOUNT):
            self._mirror(key_label='Existing %s' % index)
        calls_before = len(self.fake.calls)
        events_before = len(self._key_events())

        limited = self._error(self._create)

        self.assertEqual(limited, ('llm_key_limit', {'max_keys': MAX_KEYS_PER_ACCOUNT}))
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(len(self._key_events()), events_before)

    def test_allowlist_accepts_only_cached_default_models_and_all_default_is_explicit(self):
        calls_before = len(self.fake.calls)
        events_before = len(self._key_events())
        for models in (['free-trial-model'], ['missing-model'], [], 'paid-a'):
            with self.subTest(models=models):
                invalid = self._error(lambda: self._create(models=models))
                self.assertEqual(invalid, ('llm_key_invalid', {'field': 'models'}))
        malformed = self._catalogue()
        malformed['items'][0]['model'] = 'malformed+model'
        self.assertEqual(self._error(lambda: self._create(catalogue=malformed)),
                         ('llm_unavailable', {}))
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(len(self._key_events()), events_before)

        created = self._create(models=None)
        [token] = self.fake.tokens.values()
        mirror = self.env[KEY].sudo().browse(created['key_id'])
        self.assertEqual((token['model_limits_enabled'], token['model_limits']), (False, ''))
        self.assertFalse(mirror.model_allowlist)

    def test_stale_creating_paid_key_is_settled_before_a_replacement_is_created(self):
        key_id, _name = self.service._llm_insert_creating(
            self._service_account(), 'paid', str(uuid.uuid4()), self.user, self.real_ip,
            None, '', key_label='interrupted paid creation', quota_limit_points=None)
        self.env.cr.execute(
            'UPDATE %s SET create_date = %%s WHERE id = %%s' % llm_api_key.KEY_TABLE,
            (fields.Datetime.now() - timedelta(seconds=llm_api_key._STALE_CREATING_SECONDS + 1),
             key_id))
        self.env[KEY].invalidate_model()

        created = self._create()

        stale = self.env[KEY].sudo().browse(key_id)
        stale.invalidate_recordset()
        self.assertEqual(stale.state, 'failed_reveal')
        self.assertTrue(created['key'])

    def test_nonblocking_terms_revision_allows_paid_creation(self):
        publish_test_terms(self.env, 'qa-terms-paid-banner', requires_reaccept=False)
        self.account.invalidate_recordset()

        self.assertTrue(self._create()['key'])

    def test_terms_reacceptance_blocks_before_throttle_and_later_banner_does_not_clear_block(self):
        publish_test_terms(self.env, 'qa-terms-paid-block', requires_reaccept=True)
        self.account.invalidate_recordset()
        calls_before = len(self.fake.calls)
        events_before = len(self._key_events())

        blocked = self._error(self._create)

        self.assertEqual(blocked, ('llm_tos_outdated', {'current_version': 'qa-terms-paid-block'}))
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(len(self._key_events()), events_before)
        publish_test_terms(self.env, 'qa-terms-paid-banner', requires_reaccept=False)
        self.account.invalidate_recordset()
        blocked_again = self._error(self._create)
        self.assertEqual(blocked_again,
                         ('llm_tos_outdated', {'current_version': 'qa-terms-paid-banner'}))
        self.assertEqual(len(self.fake.calls), calls_before)
        self.assertEqual(len(self._key_events()), events_before)
