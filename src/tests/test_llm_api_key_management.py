"""LLM-07 paid API-key mirror contract (DESIGN §2.3, D4, D9, D10).

These tests lock down PaaS-owned mirror semantics and the JSON-RPC boundary:
durable expiry-notice fields, calculated display status, integer total-limit DTO
values, the per-account 20-key invariant, and fake-backed paid-key flows that
never reach a real new-api service.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import PropertyMock, patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from psycopg2.errors import LockNotAvailable, SerializationFailure

from ..models import llm_api_key
from ..models import llm_swr
from ..models.llm_account import REAUTH_SESSION_KEY
from ..services import newapi_client as nac
from ..models.llm_common import (
    API_ERROR_CODES,
    ERR_LLM_INTERNAL,
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
    CommittedLlmFixture,
    env_for,
    publish_test_terms,
    scope_claims,
)
from .test_llm_platform_foundation import _ConfiguredCase


CONTROLLER_LOGGER = 'odoo.addons.woow_paas_platform.controllers.llm'
_DEFAULT_MANAGEMENT_EXPIRY = object()


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

    def _committed_swr_fixture(self, key_specs):
        """Create SWR inputs in a real committed transaction visible to its worker cursor."""
        fixture = CommittedLlmFixture(self).start()
        partner_id = fixture.partner('LLM-07 committed SWR %s' % uuid.uuid4().hex[:8])
        account_id = fixture.activate(partner_id)
        key_ids = {}
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            # activate() intentionally creates a provisioning account; these
            # mirror tests need only its listable state and never run provision.
            _llm_internal(env[ACCOUNT].browse(account_id)).write({'state': 'active'})
            for label, values in key_specs.items():
                vals = {
                    'account_id': account_id,
                    'kind': 'paid',
                    'state': 'active',
                    'key_label': label,
                }
                vals.update(values)
                key_ids[label] = _llm_internal(env[KEY].sudo()).create(vals).id
            cr.commit()
        self.addCleanup(self._purge_committed_swr_cache, account_id)
        return fixture, account_id, key_ids

    def _purge_committed_swr_cache(self, account_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM woow_paas_platform_llm_swr_cache WHERE account_id = %s',
                (account_id,))

    def _committed_swr_store(self, fixture, account_id, items, *, complete=False):
        with self.registry.cursor() as cr:
            env = fixture.env(cr)
            return env['woow_paas_platform.llm_swr_cache']._llm_store_token_usage(
                env[ACCOUNT].browse(account_id), items, complete=complete)

    def _committed_swr_snapshot(self, fixture, key_ids):
        with self.registry.cursor() as cr:
            env = fixture.env(cr)
            keys = env[KEY].browse(list(key_ids.values())).exists()
            return {
                label: {
                    'state': key.state,
                    'disabled_by': key.disabled_by,
                    'used': key.used_quota_cached,
                    'remain': key.remain_quota_cached,
                    'accessed_at': key.accessed_at,
                }
                for label, key_id in key_ids.items()
                for key in keys.filtered(lambda rec: rec.id == key_id)
            }

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

    def test_nullable_quota_limit_roundtrips_without_changing_integer_registry(self):
        empty_false = self._key(label='unlimited false', quota_limit_points=False)
        empty_none = self._key(label='unlimited none', quota_limit_points=None)
        zero = self._key(label='zero limit', quota_limit_points=0)
        positive = self._key(label='positive limit', quota_limit_points=37)

        quota_field = self.Key._fields['quota_limit_points']
        self.assertIs(type(quota_field), llm_api_key.LlmOptionalLimitInteger)
        self.assertIs(fields.MetaField.by_type['integer'], fields.Integer)
        self.env.flush_all()
        ids = (empty_false.id, empty_none.id, zero.id, positive.id)
        self.env.invalidate_all()
        limits = self.env[KEY].browse(ids).read(['quota_limit_points'])

        self.assertEqual([row['quota_limit_points'] for row in limits],
                         [None, None, 0, 37])

    def test_swr_token_sync_updates_usage_reconciles_managed_status_and_deletes_only_absent(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'active': {'newapi_token_id': 801, 'newapi_name': 'wp_801_abcd',
                       'used_quota_cached': 1, 'accessed_at': datetime(2025, 1, 1)},
            'disabled': {'state': 'disabled', 'disabled_by': 'user',
                         'newapi_token_id': 802, 'newapi_name': 'wp_802_abcd'},
            'active_to_disabled': {'newapi_token_id': 806, 'newapi_name': 'wp_806_abcd'},
            'protected': {'state': 'disabled', 'disabled_by': 'suspend',
                          'newapi_token_id': 803, 'newapi_name': 'wp_803_abcd'},
            'absent': {'newapi_token_id': 804, 'newapi_name': 'wp_804_abcd'},
            'creating': {'state': 'creating', 'newapi_token_id': 805,
                         'newapi_name': 'wp_805_abcd'},
        })
        items = [
            {'id': 801, 'name': 'wp_801_abcd', 'remain_quota': 7, 'used_quota': 9,
             'accessed_time': 1_800_000_000, 'status': 1},
            {'id': 802, 'name': 'wp_802_abcd', 'remain_quota': 3, 'used_quota': 5,
             'accessed_time': 0, 'status': 1},
            {'id': 803, 'name': 'wp_803_abcd', 'remain_quota': 1, 'used_quota': 2,
             'status': 2},
            {'id': 806, 'name': 'wp_806_abcd', 'remain_quota': 1,
             'used_quota': 2, 'status': 2},
            {'id': 999, 'name': 'remote only', 'remain_quota': 0, 'used_quota': 0,
             'status': 2},
        ]

        self.assertTrue(self._committed_swr_store(fixture, account_id, items, complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual((rows['active']['used'], rows['active']['remain']), (9, 7))
        self.assertEqual((rows['active']['state'], rows['active']['disabled_by']), ('active', False))
        self.assertEqual(rows['active']['accessed_at'], datetime.fromtimestamp(
            1_800_000_000, tz=timezone.utc).replace(tzinfo=None))
        self.assertEqual((rows['disabled']['state'], rows['disabled']['disabled_by']),
                         ('active', False))
        self.assertEqual((rows['active_to_disabled']['state'],
                          rows['active_to_disabled']['disabled_by']), ('disabled', 'user'))
        self.assertEqual((rows['protected']['state'], rows['protected']['disabled_by']),
                         ('disabled', 'suspend'))
        self.assertEqual(rows['absent']['state'], 'deleted')
        self.assertEqual(rows['creating']['state'], 'creating')

    def test_swr_list_completeness_validation_guards_absent_mirror_deletion(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'guarded': {'newapi_token_id': 811, 'newapi_name': 'wp_811_abcd'},
        })
        incomplete_responses = [
            {'items': [], 'total': 1},
            {'items': [], 'total': None},
            {'items': [], 'total': '0'},
            {'items': [], 'total': True},
            {'items': [{'id': 1, 'name': ''}], 'total': 1},
            {'items': [{'id': 1, 'name': 'dup'}, {'id': 1, 'name': 'dup'}], 'total': 2},
            {'items': [{'id': 1, 'name': 'first'}, {'id': 1, 'name': 'second'}], 'total': 2},
            {'items': [{'id': 1, 'name': 'same'}, {'id': 2, 'name': 'same'}], 'total': 2},
        ]
        for data in incomplete_responses:
            with self.subTest(data=data):
                self.assertFalse(llm_swr._token_list_is_complete(data))
                self.assertTrue(self._committed_swr_store(
                    fixture, account_id, data['items'],
                    complete=llm_swr._token_list_is_complete(data)))
                self.assertEqual(self._committed_swr_snapshot(fixture, keys)['guarded']['state'],
                                 'active')
        # Exact total and unique valid identities prove completeness; absence is now actionable.
        self.assertTrue(llm_swr._token_list_is_complete({'items': [], 'total': 0}))
        self.assertTrue(self._committed_swr_store(fixture, account_id, [], complete=True))
        self.assertEqual(self._committed_swr_snapshot(fixture, keys)['guarded']['state'],
                         'deleted')

    def test_swr_malformed_match_and_identity_conflict_do_not_transition_or_delete(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'guarded': {'newapi_token_id': 812, 'newapi_name': 'wp_812_abcd'},
            'other': {'newapi_token_id': 813, 'newapi_name': 'wp_813_abcd'},
        })
        invalid = {'id': 812, 'name': 'wp_812_abcd', 'remain_quota': -1,
                   'used_quota': 10, 'status': 2}
        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            invalid,
            # This second committed mirror belongs to the later conflict cases;
            # keep it present so this complete snapshot does not delete it.
            {'id': 813, 'name': 'wp_813_abcd', 'remain_quota': 0,
             'used_quota': 0, 'status': 1},
        ], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual(rows['guarded']['state'], 'active')
        self.assertEqual(rows['guarded']['used'], 0)
        # Each conflict direction protects every implicated local mirror from absence deletion.
        for items in (
            [
                {'id': 812, 'name': 'wp_812_b001', 'remain_quota': 0, 'used_quota': 0,
                 'status': 2},
                {'id': 813, 'name': 'wp_813_abcd', 'remain_quota': 0, 'used_quota': 0,
                 'status': 1},
            ],
            [
                {'id': 812, 'name': 'wp_812_abcd', 'remain_quota': 0, 'used_quota': 0,
                 'status': 1},
                {'id': 999, 'name': 'wp_813_abcd', 'remain_quota': 0, 'used_quota': 0,
                 'status': 2},
            ],
        ):
            self.assertTrue(self._committed_swr_store(
                fixture, account_id, items, complete=True))
            rows = self._committed_swr_snapshot(fixture, keys)
            self.assertEqual(rows['guarded']['state'], 'active')
            self.assertEqual(rows['other']['state'], 'active')

    def test_swr_rejects_bool_and_float_remote_ids_without_touching_mirror(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'numeric': {'newapi_token_id': 1, 'newapi_name': 'wp_1_abcd',
                        'used_quota_cached': 4, 'remain_quota_cached': 6,
                        'accessed_at': datetime(2025, 1, 1)},
        })
        for bad_id in (True, 1.0):
            item = {'id': bad_id, 'name': 'wp_1_abcd', 'remain_quota': 0,
                    'used_quota': 99, 'accessed_time': 1_800_000_000, 'status': 2}
            self.assertTrue(self._committed_swr_store(
                fixture, account_id, [item], complete=False))
            row = self._committed_swr_snapshot(fixture, keys)['numeric']
            self.assertEqual((row['used'], row['remain']), (4, 6))
            self.assertEqual((row['state'], row['accessed_at']),
                             ('active', datetime(2025, 1, 1)))

    def test_swr_valid_access_time_updates_with_invalid_usage_and_status(self):
        previous = datetime(2025, 1, 1)
        fixture, account_id, keys = self._committed_swr_fixture({
            'access': {'newapi_token_id': 817, 'newapi_name': 'wp_817_abcd',
                       'used_quota_cached': 4, 'remain_quota_cached': 6,
                       'accessed_at': previous},
        })
        item = {'id': 817, 'name': 'wp_817_abcd', 'remain_quota': -1,
                'used_quota': 99, 'accessed_time': 1_800_000_000, 'status': 7}

        self.assertTrue(self._committed_swr_store(fixture, account_id, [item], complete=False))
        row = self._committed_swr_snapshot(fixture, keys)['access']
        self.assertEqual(row['accessed_at'], datetime.fromtimestamp(
            1_800_000_000, tz=timezone.utc).replace(tzinfo=None))
        self.assertEqual((row['used'], row['remain']), (4, 6))
        self.assertEqual((row['state'], row['disabled_by']), ('active', False))

    def test_swr_local_identity_ambiguity_protects_unmatched_mirrors_from_deletion(self):
        shared_id = 818
        fixture, account_id, keys = self._committed_swr_fixture({
            'id_first': {'newapi_token_id': shared_id, 'newapi_name': 'wp_818_a001'},
            'id_second': {'newapi_token_id': shared_id, 'newapi_name': 'wp_818_a002'},
            'name_first': {'newapi_token_id': 819, 'newapi_name': 'wp_819_a003'},
            'name_second': {'newapi_token_id': 820, 'newapi_name': 'wp_819_a003'},
        })
        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            {'id': shared_id, 'name': 'wp_818_a001', 'remain_quota': 10,
             'used_quota': 2, 'accessed_time': 1_800_000_000, 'status': 1},
        ], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual(rows['id_first']['state'], 'active')
        self.assertEqual(rows['id_second']['state'], 'active')
        self.assertEqual(rows['id_first']['used'], 2)

        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            {'id': 819, 'name': 'wp_819_a003', 'remain_quota': 12, 'used_quota': 3,
             'accessed_time': 1_800_000_001, 'status': 1},
        ], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual(rows['name_first']['state'], 'active')
        self.assertEqual(rows['name_second']['state'], 'active')
        self.assertEqual(rows['name_first']['used'], 3)

    def test_swr_remote_and_local_exact_identity_ambiguity_never_updates_mirror(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'remote_duplicate': {'newapi_token_id': 822, 'newapi_name': 'wp_822_abcd',
                                 'used_quota_cached': 4, 'remain_quota_cached': 6},
        })
        duplicate_item = {'id': 822, 'name': 'wp_822_abcd',
                          'remain_quota': 0, 'used_quota': 99, 'status': 2}
        self.assertTrue(self._committed_swr_store(
            fixture, account_id, [duplicate_item, dict(duplicate_item)], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual((rows['remote_duplicate']['used'], rows['remote_duplicate']['remain'],
                          rows['remote_duplicate']['state']), (4, 6, 'active'))

        # Independent accounts keep a complete snapshot in one scenario from
        # correctly deleting the fixtures intended for the next scenario.
        fixture, account_id, keys = self._committed_swr_fixture({
            'remote_conflict': {'newapi_token_id': 823, 'newapi_name': 'wp_823_abcd',
                                'used_quota_cached': 5, 'remain_quota_cached': 7},
        })
        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            {'id': 823, 'name': 'wp_823_abcd', 'remain_quota': 0,
             'used_quota': 99, 'status': 2},
            {'id': 823, 'name': 'wp_823_b001', 'remain_quota': 0,
             'used_quota': 99, 'status': 2},
        ], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual((rows['remote_conflict']['used'], rows['remote_conflict']['remain'],
                          rows['remote_conflict']['state']), (5, 7, 'active'))

        fixture, account_id, keys = self._committed_swr_fixture({
            'local_first': {'newapi_token_id': 824, 'newapi_name': 'wp_824_a004',
                            'used_quota_cached': 3, 'remain_quota_cached': 8},
            'local_second': {'newapi_token_id': 824, 'newapi_name': 'wp_824_a004',
                             'used_quota_cached': 4, 'remain_quota_cached': 9},
        })
        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            {'id': 824, 'name': 'wp_824_a004', 'remain_quota': 0, 'used_quota': 99,
             'accessed_time': 1_800_000_000, 'status': 2},
        ], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual((rows['local_first']['used'], rows['local_first']['remain'],
                          rows['local_first']['state']), (3, 8, 'active'))
        self.assertEqual((rows['local_second']['used'], rows['local_second']['remain'],
                          rows['local_second']['state']), (4, 9, 'active'))

    def test_swr_full_list_deletes_absent_system_disabled_but_preserves_present_reason(self):
        fixture, account_id, keys = self._committed_swr_fixture({
            'absent': {'state': 'disabled', 'disabled_by': 'suspend',
                       'newapi_token_id': 814, 'newapi_name': 'wp_814_abcd'},
            'present': {'state': 'disabled', 'disabled_by': 'suspend',
                        'newapi_token_id': 815, 'newapi_name': 'wp_815_abcd'},
        })
        self.assertTrue(self._committed_swr_store(fixture, account_id, [
            {'id': 815, 'name': 'wp_815_abcd', 'remain_quota': 1, 'used_quota': 2,
             'status': 1}], complete=True))
        rows = self._committed_swr_snapshot(fixture, keys)
        self.assertEqual(rows['absent']['state'], 'deleted')
        self.assertEqual((rows['present']['state'], rows['present']['disabled_by']),
                         ('disabled', 'suspend'))

    def test_keys_list_is_db_only_and_carries_total_progress_and_data_as_of(self):
        from ..models.llm_swr import KIND_TOKENS

        key = self._key(label='progress', quota_limit_points=20,
                        used_quota_cached=7 * QUOTA_PER_POINT,
                        remain_quota_cached=13 * QUOTA_PER_POINT,
                        newapi_token_id=821, newapi_name='wp_821_abcd')
        fetched = fields.Datetime.now() - timedelta(seconds=31)
        _llm_internal(self.env['woow_paas_platform.llm_swr_cache'].sudo()).create({
            'account_id': self.account.id,
            'kind': KIND_TOKENS,
            'payload': '{}',
            'fetched_at': fetched,
        })
        Account = type(self.env[ACCOUNT])
        cache = self.env['woow_paas_platform.llm_swr_cache']
        Cache = type(cache)
        with patch.object(Account, '_llm_client',
                          side_effect=AssertionError('keys/list performed synchronous new-api I/O')) as client_factory, \
                patch.object(Cache, '_llm_swr_touch', autospec=True,
                             return_value=False) as touch:
            payload = self.Key._llm_keys_payload(self.account.partner_id)
        client_factory.assert_not_called()
        touch.assert_called_once_with(cache, self.account, KIND_TOKENS, fetched)
        row = self.env['woow_paas_platform.llm_swr_cache']._llm_swr_get(self.account, KIND_TOKENS)
        self.assertFalse(row['requested_at'])
        self.assertEqual(payload['data_as_of'], fetched.replace(tzinfo=timezone.utc).isoformat())
        dto = next(item for item in payload['items'] if item['id'] == key.id)
        self.assertEqual((dto['limit_points'], dto['used_points'], dto['remain_points']), (20, 7, 13))

    def test_swr_touch_queues_through_independent_cursor_for_committed_account(self):
        from ..models.llm_swr import KIND_TOKENS

        fixture, account_id, _keys = self._committed_swr_fixture({
            'queued': {'newapi_token_id': 825, 'newapi_name': 'wp_825_abcd'},
        })
        with self.registry.cursor() as cr:
            env = fixture.env(cr)
            account = env[ACCOUNT].browse(account_id)
            Cron = type(env['ir.cron'])
            with patch.object(Cron, '_trigger') as trigger:
                queued = env['woow_paas_platform.llm_swr_cache']._llm_swr_touch(
                    account, KIND_TOKENS, None)
        self.assertTrue(queued)
        trigger.assert_called_once()

        with self.registry.cursor() as cr:
            env = fixture.env(cr)
            row = env['woow_paas_platform.llm_swr_cache']._llm_swr_get(
                env[ACCOUNT].browse(account_id), KIND_TOKENS)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row['requested_at'])

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

    def test_management_stale_snapshot_and_held_lock_fail_before_remote_call(self):
        """Real independent transactions must conflict before any remote side effect."""
        operations = ('update', 'extend', 'status', 'delete')
        remotes = {operation: self._fake_paid_token() for operation in operations}
        fixture, account_id, key_ids = self._committed_swr_fixture({
            operation: {
                'newapi_name': 'wp_101_abcd',
                'newapi_token_id': remotes[operation][-1],
                'quota_limit_points': 100,
                'expires_at': fields.Datetime.now() + timedelta(days=30),
                'model_allowlist': 'model-a,model-b',
                'remain_quota_cached': 100 * QUOTA_PER_POINT,
                'used_quota_cached': 0,
            } for operation in operations
        })
        calls = {
            'update': lambda service, partner, key_id: service._llm_update_key(
                partner, key_id, expires='90d'),
            'extend': lambda service, partner, key_id: service._llm_extend_key(partner, key_id),
            'status': lambda service, partner, key_id: service._llm_set_key_status(
                partner, key_id, False),
            'delete': lambda service, partner, key_id: service._llm_delete_key(
                partner, key_id, 'delete'),
        }
        for operation in operations:
            key_id = key_ids[operation]
            fake, client, ip, up, _other, _token_id = remotes[operation]
            fake.calls.clear()
            with self.subTest(operation=operation):
                with self.registry.cursor() as request_cr:
                    request_env = fixture.env(request_cr)
                    stale_key = request_env[KEY].browse(key_id)
                    self.assertEqual(stale_key.key_label, operation)
                    partner = request_env[ACCOUNT].browse(account_id).partner_id
                    with self.registry.cursor() as writer_cr:
                        writer_cr.execute(
                            'UPDATE woow_paas_platform_llm_api_key '
                            'SET remain_quota_cached = remain_quota_cached + 1 WHERE id = %s',
                            (key_id,))
                    with patch.object(type(request_env[KEY]), '_llm_management_remote',
                                      return_value=(ip, up, client)):
                        with self.assertRaises(SerializationFailure):
                            calls[operation](request_env[KEY], partner, key_id)
                            request_env.flush_all()
                self.assertEqual(fake.calls, [])

        # A held row lock must reject a competitor without waiting on remote I/O.
        held_key_id = key_ids['status']
        fake, client, ip, up, _other, _token_id = self._fake_paid_token()
        fake.calls.clear()
        holder_cr = self.registry.cursor()
        try:
            holder_cr.execute(
                'SELECT id FROM woow_paas_platform_llm_api_key WHERE id = %s FOR UPDATE',
                (held_key_id,))
            with self.registry.cursor() as request_cr:
                request_env = fixture.env(request_cr)
                partner = request_env[ACCOUNT].browse(account_id).partner_id
                request_cr.execute("SET LOCAL lock_timeout = '500ms'")
                with patch.object(type(request_env[KEY]), '_llm_management_remote',
                                  return_value=(ip, up, client)):
                    with self.assertRaises(LockNotAvailable):
                        request_env[KEY]._llm_set_key_status(partner, held_key_id, False)
                        request_env.flush_all()
        finally:
            holder_cr.rollback()
            holder_cr.close()
        self.assertEqual(fake.calls, [])

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
        # Management assertions measure calls caused by the operation under test;
        # provisioning's AP/UP calls are setup, not part of that baseline.
        self.fake.calls.clear()
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

    def _http(self, path, **params):
        response = self.url_open(
            path,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(
            response.status_code, 200,
            'LLM key endpoint returned unexpected HTTP status %s' % response.status_code,
        )
        body = response.json()
        self.assertNotIn(
            'error', body,
            'LLM key endpoint returned a JSON-RPC error',
        )
        return body['result']

    def _http_create(self, **params):
        Cache = type(self.env['woow_paas_platform.llm_swr_cache'])
        with patch.object(Cache, '_llm_models_payload', return_value=self._catalogue()):
            return self._http('/api/llm/keys/create', **params)

    def _error(self, call):
        try:
            call()
        except LlmError as exc:
            return exc.code, exc.params
        self.fail('Expected a fixed LlmError')

    def _management_key(self, *, kind='paid', expires_at=_DEFAULT_MANAGEMENT_EXPIRY,
                        state='active', **values):
        if expires_at is _DEFAULT_MANAGEMENT_EXPIRY:
            expires_at = fields.Datetime.now() + timedelta(days=90)
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
        self.assertFalse(key.expires_at)
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
        self._management_remote(expiry=int(trial.expires_at.replace(tzinfo=timezone.utc).timestamp()))
        self.fake.tokens[501]['group'] = 'free'
        self.service._llm_delete_key(self.user.partner_id, trial.id, 'trial', user=self.user,
                                     real_ip=self.real_ip)
        self.assertEqual(trial.state, 'deleted')
        self.assertNotIn(501, self.fake.tokens)
        replacement = self._management_key(kind='free_trial', key_label='replacement')
        self.assertEqual(replacement.state, 'active')
        self.assertNotEqual(replacement.id, trial.id)
        self.assertEqual([call[0] for call in self.fake.calls], ['user_delete_token'])

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
        self.assertEqual(args[4].uid, self.user.id)
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
        self.assertEqual(result['code'], ERR_LLM_INTERNAL)
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

    def test_http_paid_key_management_success_paths(self):
        """list/update/extend/status/delete use the session user's owned mirror only."""
        self.authenticate(self.user.login, self.password)
        key = self._management_key(
            key_label='HTTP initial', expires_at=fields.Datetime.now() + timedelta(days=30))
        self._management_remote()
        before_calls = len(self.fake.calls)

        listed = self._http('/api/llm/keys/list')
        self.assertTrue(listed['success'], listed)
        self.assertEqual([item['id'] for item in listed['data']['items']], [key.id])

        updated = self._http(
            '/api/llm/keys/update', key_id=key.id, name='HTTP managed', limit_points=120)
        self.assertTrue(updated['success'], updated)
        self.assertEqual((updated['data']['display_name'], updated['data']['remain_after_points']),
                         ('HTTP managed', 90))
        self.assertEqual(self.fake.tokens[501]['expired_time'], 1_900_000_000)
        self.assertEqual((self.fake.tokens[501]['model_limits_enabled'],
                          self.fake.tokens[501]['model_limits']), (True, 'paid-a,paid-b'))

        extended = self._http('/api/llm/keys/extend', key_id=key.id)
        self.assertTrue(extended['success'], extended)
        self.assertEqual(extended['data']['id'], key.id)

        disabled = self._http('/api/llm/keys/set_status', key_id=key.id, enabled=False)
        self.assertTrue(disabled['success'], disabled)
        self.assertEqual(disabled['data']['status'], 'disabled')

        deleted = self._http(
            '/api/llm/keys/delete', key_id=key.id, confirm_name='HTTP managed')
        self.assertTrue(deleted['success'], deleted)
        self.assertEqual(deleted['data'], {'deleted': True})
        self.assertEqual(
            [name for name, _ip in self.fake.calls[before_calls:]],
            ['user_get_token', 'user_update_token', 'user_get_token', 'user_update_token',
             'user_set_token_status', 'user_delete_token'],
        )

    def test_http_update_keeps_shared_omitted_sentinel_distinct_from_json_null(self):
        self.authenticate(self.user.login, self.password)
        injected_ip = '192.0.2.200'
        untrusted = {
            'partner_id': 1, 'account_id': self.account.id + 1, 'user': 'untrusted',
            'session': 'untrusted', 'upstream': {'token': 'untrusted'},
            'body': 'untrusted', 'error': 'untrusted', 'real_ip': injected_ip,
        }
        with patch.object(type(self.env[KEY]), '_llm_update_key',
                          return_value={'id': 71}) as update:
            omitted = self._http('/api/llm/keys/update', key_id=71, **untrusted)
            explicit_null = self._http(
                '/api/llm/keys/update', key_id=72, limit_points=None,
                expires=None, models=None, **untrusted)

        self.assertTrue(omitted['success'], omitted)
        self.assertTrue(explicit_null['success'], explicit_null)
        self.assertEqual(update.call_count, 2)
        omitted_args, omitted_kwargs = update.call_args_list[0]
        self.assertEqual(omitted_args, (self.user.partner_id, 71))
        self.assertEqual(
            set(omitted_kwargs),
            {'user', 'real_ip', 'name', 'limit_points', 'expires', 'models'},
        )
        self.assertEqual(omitted_kwargs['user'], self.user)
        self.assertNotEqual(omitted_kwargs['real_ip'], injected_ip)
        for field in ('name', 'limit_points', 'expires', 'models'):
            self.assertIs(omitted_kwargs[field], llm_api_key._OMITTED)

        null_args, null_kwargs = update.call_args_list[1]
        self.assertEqual(null_args, (self.user.partner_id, 72))
        self.assertIs(null_kwargs['name'], llm_api_key._OMITTED)
        for field in ('limit_points', 'expires', 'models'):
            self.assertIsNone(null_kwargs[field])

    def test_http_management_routes_bind_trusted_context_only(self):
        self.authenticate(self.user.login, self.password)
        injected_ip = '192.0.2.201'
        untrusted = {
            'partner_id': 1, 'account_id': self.account.id + 1, 'user': 'untrusted',
            'session': 'untrusted', 'upstream': {'token': 'untrusted'},
            'body': 'untrusted', 'error': 'untrusted', 'real_ip': injected_ip,
        }
        Key = type(self.env[KEY])
        with patch.object(Key, '_llm_extend_key', return_value={'id': 81}) as extend, \
                patch.object(Key, '_llm_set_key_status', return_value={'id': 82}) as status, \
                patch.object(Key, '_llm_delete_key', return_value={'deleted': True}) as delete:
            extended = self._http('/api/llm/keys/extend', key_id=81, **untrusted)
            changed = self._http(
                '/api/llm/keys/set_status', key_id=82, enabled=False, **untrusted)
            deleted = self._http(
                '/api/llm/keys/delete', key_id=83, confirm_name='confirm', **untrusted)

        self.assertTrue(extended['success'], extended)
        self.assertTrue(changed['success'], changed)
        self.assertTrue(deleted['success'], deleted)
        extend_args, extend_kwargs = extend.call_args
        self.assertEqual(extend_args, (self.user.partner_id, 81))
        self.assertEqual(set(extend_kwargs), {'user', 'real_ip'})
        self.assertEqual(extend_kwargs['user'], self.user)
        self.assertNotEqual(extend_kwargs['real_ip'], injected_ip)

        status_args, status_kwargs = status.call_args
        self.assertEqual(status_args, (self.user.partner_id, 82, False))
        self.assertEqual(set(status_kwargs), {'user', 'real_ip', 'session'})
        self.assertEqual(status_kwargs['user'], self.user)
        self.assertNotEqual(status_kwargs['real_ip'], injected_ip)
        self.assertEqual(status_kwargs['session'].uid, self.user.id)
        self.assertNotEqual(status_kwargs['session'], 'untrusted')

        delete_args, delete_kwargs = delete.call_args
        self.assertEqual(delete_args, (self.user.partner_id, 83, 'confirm'))
        self.assertEqual(set(delete_kwargs), {'user', 'real_ip'})
        self.assertEqual(delete_kwargs['user'], self.user)
        self.assertNotEqual(delete_kwargs['real_ip'], injected_ip)

    def test_http_other_portal_cannot_read_or_manage_foreign_key_without_io(self):
        key = self._management_key(
            key_label='owner only', expires_at=fields.Datetime.now() + timedelta(days=30))
        self._management_remote()
        suffix = uuid.uuid4().hex[:8]
        other_password = 'llm-paid-other-%s' % suffix
        other = new_test_user(
            self.env, login='llm_paid_other_%s' % suffix, password=other_password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        other_account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': other.partner_id.id,
        })
        _llm_internal(other_account).write({
            'state': 'active',
            'newapi_username': nac.newapi_username('wqa', other_account.id),
        })
        self.authenticate(other.login, other_password)
        before_calls = list(self.fake.calls)

        listed = self._http('/api/llm/keys/list')
        self.assertTrue(listed['success'], listed)
        self.assertNotIn(key.id, [item['id'] for item in listed['data']['items']])
        for path, params in (
            ('/api/llm/keys/update', {'key_id': key.id, 'name': 'steal'}),
            ('/api/llm/keys/extend', {'key_id': key.id}),
            ('/api/llm/keys/set_status', {'key_id': key.id, 'enabled': False}),
            ('/api/llm/keys/delete', {'key_id': key.id, 'confirm_name': 'owner only'}),
        ):
            with self.subTest(path=path):
                denied = self._http(path, **params)
                self.assertFalse(denied['success'], denied)
                self.assertEqual(denied['code'], ERR_LLM_KEY_NOT_FOUND)
        self.assertEqual(self.fake.calls, before_calls)

    def test_http_enable_terms_gate_precedes_io_but_other_management_actions_continue(self):
        self.authenticate(self.user.login, self.password)
        key = self._management_key(
            state='disabled', disabled_by='user', key_label='terms key',
            expires_at=fields.Datetime.now() + timedelta(days=30))
        self._management_remote()
        self.fake.tokens[501]['status'] = 2
        publish_test_terms(self.env, 'qa-http-enable-terms-block', requires_reaccept=True)
        self.account.invalidate_recordset()
        before_calls = list(self.fake.calls)

        blocked = self._http('/api/llm/keys/set_status', key_id=key.id, enabled=True)
        self.assertFalse(blocked['success'], blocked)
        self.assertEqual(blocked['code'], 'llm_tos_outdated')
        self.assertEqual(self.fake.calls, before_calls)

        for path, params in (
            ('/api/llm/keys/update', {'key_id': key.id, 'limit_points': 120}),
            ('/api/llm/keys/extend', {'key_id': key.id}),
            ('/api/llm/keys/set_status', {'key_id': key.id, 'enabled': False}),
            ('/api/llm/keys/delete', {'key_id': key.id, 'confirm_name': 'terms key'}),
        ):
            with self.subTest(path=path):
                result = self._http(path, **params)
                self.assertTrue(result['success'], result)

    def test_http_management_catch_all_never_returns_or_logs_token_shaped_exception(self):
        self.authenticate(self.user.login, self.password)
        secret = 'sk-' + 'A' * 48
        with patch.object(type(self.env[KEY]), '_llm_update_key',
                          side_effect=RuntimeError('upstream_body=%s' % secret)):
            with self.assertLogs(CONTROLLER_LOGGER, level=logging.ERROR) as logs:
                result = self._http('/api/llm/keys/update', key_id=91)

        client_output = json.dumps(result)
        log_output = '\n'.join(logs.output)
        self.assertFalse(result['success'], result)
        self.assertEqual(result['code'], ERR_LLM_INTERNAL)
        self.assertNotIn(secret, client_output)
        self.assertNotIn(secret, log_output)
        self.assertIn('keys/update failed', log_output)
        self.assertIn('uid=', log_output)

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

    def test_swr_refresh_rejects_malformed_total_before_store_and_keeps_partial_non_deleting(self):
        from ..models.llm_swr import KIND_TOKENS

        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': self.account.id, 'kind': 'paid', 'state': 'active',
            'key_label': 'refresh guard', 'newapi_token_id': 816, 'newapi_name': 'wp_816_abcd',
        })

        class Client:
            data = None

            def user_list_tokens(self, *_args, **_kwargs):
                return nac.Ok(data=self.data, message='', status=200)

        client = Client()
        cache = self.env['woow_paas_platform.llm_swr_cache']
        for bad_total in (None, '1', True, -1):
            client.data = {'items': [], 'total': bad_total}
            with patch.object(type(cache), '_llm_store_token_usage', autospec=True,
                              side_effect=AssertionError('malformed envelope reached store')) as store:
                result = cache._llm_swr_refresh_one(
                    client, {'identity_ok': True, 'block': 1}, self.account.id, KIND_TOKENS, None)
            self.assertEqual(result, (None, 'shape'))
            store.assert_not_called()
        client.data = {'items': [], 'total': 101}
        with patch.object(type(cache), '_llm_store_token_usage', autospec=True,
                          return_value=True) as store:
            result = cache._llm_swr_refresh_one(
                client, {'identity_ok': True, 'block': 1}, self.account.id, KIND_TOKENS, None)
        self.assertEqual(result, (None, 'ok'))
        store.assert_called_once_with(cache, self.account, [], complete=False)
        self.env.invalidate_all()
        key.invalidate_recordset()
        self.assertEqual(key.state, 'active')
