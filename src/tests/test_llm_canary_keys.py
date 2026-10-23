"""Canary key service-glue tests with a validator stub.

Runtime claim authority and PostgreSQL SQL-fence behavior are covered by the
canary runtime/PG tests; this fixture does not provide evidence for either.
"""
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import tagged

from ..models import llm_api_key as key_module
from ..models import llm_free_usage as usage_module
from ..models.llm_common import LlmError, _llm_internal
from ..models.llm_notifications import MODEL as NOTIFICATION_MODEL
from ..services import llm_canary as canary_policy
from ..services.llm_canary import CanaryClaim
from ..services import newapi_client as nac
from .llm_test_support import ACCOUNT, KEY, TEST_PREFIX
from .test_llm_platform_foundation import _ConfiguredCase, _new_account


@tagged('post_install', '-at_install', 'llm_canary')
class TestLlmCanaryKeys(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.account = _new_account(self.env, 'Canary key QA', prefix=TEST_PREFIX)
        self.Keys = self.env[KEY].sudo()
        self.Usage = self.env[usage_module.USAGE_MODEL].sudo()
        self.day = datetime(2031, 6, 8).date()
        self._works = {}
        clock = patch.object(usage_module, 'taipei_day', return_value=self.day)
        clock.start()
        self.addCleanup(clock.stop)
        validator = patch.object(
            type(self.env['woow_paas_platform.llm_runtime_state']),
            '_canary_validate_claim_cr',
            side_effect=lambda *args: self._works[args[-1].request_id], create=True)
        validator.start()
        self.addCleanup(validator.stop)

    def _work(self, account):
        return {
            'request_id': str(uuid.uuid4()),
            'account_id': account.id,
            'partner_id': account.partner_id.id,
            'fingerprint': 'f' * 64,
            'expires_at': (datetime.utcnow() + timedelta(minutes=10)).replace(
                microsecond=0).isoformat(),
            'ip': nac.tenant_ip(account.id, 7),
            'proxy_url': nac.PROXY_URL_STG,
        }

    def _claim(self, account=None):
        account = account or self.account
        work = self._work(account)
        self._works[work['request_id']] = work
        claim = canary_policy.issue_claim(7, work)
        assert canary_policy.authorized(claim)
        return claim

    def test_ordinary_fourth_key_is_rejected_even_with_privileged_canary_context(self):
        for _ in range(3):
            self.Usage._llm_reserve_key_cr(self.env.cr, self.account.id, self.day)
        with self.assertRaises(LlmError):
            self.Usage.with_context(canary=True)._llm_reserve_key_cr(
                self.env.cr, self.account.id, self.day)

    def test_canary_does_not_increment_or_refund_ordinary_daily_count(self):
        for _ in range(2):
            self.Usage._llm_reserve_key_cr(self.env.cr, self.account.id, self.day)
        claim = self._claim()
        key_id, _name = self.Keys._llm_insert_creating(
            self.account, 'free_trial', claim.request_id, None, '',
            datetime.utcnow() + timedelta(minutes=10), ','.join(nac.FREE_TRIAL_MODELS),
            canary_claim=claim)
        self.env.cr.execute(
            'SELECT canary_generation FROM %s WHERE id=%%s' % key_module.KEY_TABLE,
            (key_id,))
        self.assertEqual(self.env.cr.fetchone()[0], claim.generation)
        self.assertEqual(self.Usage._llm_today(self.account.id)['keys_created_today'], 2)
        self.Keys._llm_finish(key_id, {'state': 'deleted'})
        self.assertEqual(self.Usage._llm_today(self.account.id)['keys_created_today'], 2)

    def test_canary_reservation_still_rejects_a_capped_day(self):
        _llm_internal(self.Usage).create({
            'account_id': self.account.id, 'day_tpe': self.day,
            'success_count': 3, 'trial_keys_created': 2,
            'capped_at': datetime(2031, 6, 8, 0, 0),
        })
        self.env.flush_all()
        with self.assertRaises(LlmError):
            self.Usage._llm_reserve_canary_key_cr(
                self.env.cr, self.account.id, self.day, self._claim())
        self.assertEqual(self.Usage._llm_today(self.account.id)['keys_created_today'], 2)

    def test_malformed_and_wrong_account_claims_are_rejected(self):
        with self.assertRaises(LlmError):
            self.Usage._llm_reserve_canary_key_cr(
                self.env.cr, self.account.id, self.day, {'generation': 7})
        other = _new_account(self.env, 'Different canary account', prefix=TEST_PREFIX)
        with self.assertRaises(LlmError):
            self.Usage._llm_reserve_canary_key_cr(
                self.env.cr, self.account.id, self.day, self._claim(other))

    def test_unsealed_claim_is_rejected_by_private_create_helper(self):
        sealed = self._claim()
        unsealed = CanaryClaim(
            sealed.generation, sealed.request_id, sealed.account_id,
            sealed.partner_id, sealed.fingerprint)
        with self.assertRaises(LlmError):
            self.Keys._llm_create_canary_key(
                self.account, unsealed, time.monotonic() + 30)

    def test_private_helper_selects_cron_role_and_preserves_deadline(self):
        claim = self._claim()
        deadline = time.monotonic() + 30
        expires_at = self._works[claim.request_id]['expires_at']
        with patch.object(type(self.Keys), '_llm_assert_key_create_allowed'), \
                patch.object(type(self.account), '_llm_require_current_terms'), \
                patch.object(type(self.Keys), '_llm_preflight_create',
                             return_value=self.Keys.browse()), \
                patch.object(type(self.Keys), '_llm_create_with_reveal', return_value={'key_id': 1}) as create:
            result = self.Keys._llm_create_canary_key(self.account, claim, deadline)
        self.assertEqual(result, {'key_id': 1})
        self.assertEqual(create.call_args.args[4]['expires_at'],
                         datetime.fromisoformat(expires_at))
        self.assertEqual(create.call_args.kwargs['role'], nac.ROLE_CRON)
        self.assertIs(create.call_args.kwargs['canary_claim'], claim)
        self.assertEqual(create.call_args.kwargs['deadline'], deadline)

    def test_create_uses_validator_work_ip_and_proxy_for_cron_client(self):
        claim = self._claim()
        work = self._works[claim.request_id]
        client = MagicMock()
        client.base_url = work['proxy_url']
        event_type = type(self.env['woow_paas_platform.llm_event'])
        key_type = type(self.Keys)
        account_type = type(self.account)
        deadline = time.monotonic() + 30
        spec = {
            'kind': 'free_trial', 'key_label': '', 'limit_points': None,
            'expires_at': datetime.fromisoformat(work['expires_at']),
            'model_allowlist': ','.join(nac.FREE_TRIAL_MODELS),
        }
        with patch.object(key_type, '_llm_assert_key_create_allowed'), \
                patch.object(account_type, '_llm_tenant_ip', return_value=work['ip']), \
                patch.object(account_type, '_llm_up_token', return_value='cron-up') as up_token, \
                patch.object(type(self.env[ACCOUNT]), '_llm_client', return_value=client) as client_for, \
                patch.object(event_type, '_llm_throttle_reserve', return_value=901), \
                patch.object(event_type, '_llm_event_finish'), \
                patch.object(key_type, '_llm_insert_creating', return_value=(77, 'wp_77_test')), \
                patch.object(key_type, '_llm_create_remote', return_value={'key_id': 77}) as remote:
            result = self.Keys._llm_create_with_reveal(
                self.account, claim.request_id, None, '', spec, lambda name: {},
                canary_claim=claim, deadline=deadline, role=nac.ROLE_CRON)
        self.assertEqual(result, {'key_id': 77})
        client_for.assert_called_once_with(nac.ROLE_CRON)
        up_token.assert_called_once_with(role=nac.ROLE_CRON)
        self.assertIs(remote.call_args.args[1], client)
        self.assertEqual(remote.call_args.args[2], work['ip'])

    def test_active_canary_key_does_not_send_key_created_notice(self):
        claim = self._claim()
        key_id, _name = self.Keys._llm_insert_creating(
            self.account, 'free_trial', claim.request_id, None, '',
            datetime.fromisoformat(self._works[claim.request_id]['expires_at']),
            ','.join(nac.FREE_TRIAL_MODELS), canary_claim=claim)
        with patch.object(type(self.env[NOTIFICATION_MODEL]), '_llm_notice') as notice:
            self.Keys._llm_finish(key_id, {
                'state': 'active',
                'newapi_token_id': 123456,
                'masked_key': 'sk-abcd…wxyz',
                'revealed_at': datetime.utcnow(),
            })
        key = self.Keys.browse(key_id)
        key.invalidate_recordset(['state', 'created_notice_pending'])
        self.assertEqual(key.state, 'active')
        self.assertFalse(key.created_notice_pending)
        notice.assert_not_called()
