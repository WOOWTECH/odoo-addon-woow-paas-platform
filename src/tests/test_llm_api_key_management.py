"""LLM-07 paid API-key mirror contract (DESIGN §2.3, D4, D9, D10).

These tests deliberately stay below the HTTP boundary.  They lock down the
PaaS-owned mirror semantics first: durable expiry-notice fields, calculated display
status, integer total-limit DTO values, the per-account 20-key invariant, and the
fake-backed one-time paid-key creation transaction.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
        self.assertFalse(client.user_update_token(ip, other_up, foreign_update).ok)
        self.assertFalse(client.user_set_token_status(ip, other_up, token_id, False).ok)
        self.assertFalse(client.user_delete_token(ip, other_up, token_id).ok)
        self.assertEqual(fake.tokens[token_id]['remain_quota'], 70 * QUOTA_PER_POINT)

        self.assertTrue(client.user_set_token_status(ip, owner_up, token_id, False).ok)
        self.assertEqual(fake.tokens[token_id]['status'], 2)
        self.assertTrue(client.user_delete_token(ip, owner_up, token_id).ok)
        self.assertNotIn(token_id, fake.tokens)
        self.assertEqual([name for name, _client_ip in fake.calls], [
            'user_create_token', 'user_update_token', 'user_update_token',
            'user_set_token_status', 'user_delete_token', 'user_set_token_status',
            'user_delete_token',
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

    def _error(self, call):
        try:
            call()
        except LlmError as exc:
            return exc.code, exc.params
        self.fail('Expected a fixed LlmError')

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
