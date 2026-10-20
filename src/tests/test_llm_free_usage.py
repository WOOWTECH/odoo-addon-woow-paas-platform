"""LLM-10: real SQL receipts/counters; fake only the HTTP client boundary.

HttpCase supplies TestCursor rollback for service helpers' independent cursors.
Every case uses its own accounts and receipt IDs; it never changes live settings.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.tests.common import new_test_user, tagged

from ..models import llm_free_usage as free_module
from ..models import llm_api_key as key_module
from ..models.llm_common import LlmCronTick, LlmError, _llm_internal
from ..models.llm_free_usage import (
    RECEIPT_TABLE, USAGE_MODEL, meter_window, taipei_day,
)
from ..services import newapi_client as nac
from .llm_test_support import ACCOUNT, KEY, RUNTIME, TEST_PREFIX, reset_llm_config
from .test_llm_platform_foundation import _ConfiguredCase, _new_account, _raises

NOW = datetime(2031, 6, 8, 4, 0)
STAMP = int(NOW.replace(tzinfo=timezone.utc).timestamp())
DAY = taipei_day(STAMP)


@tagged('post_install', '-at_install')
class TestLlmFreeUsage(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.account = _new_account(self.env, 'Free cap QA', user_id=1234)
        self.Usage = self.env[USAGE_MODEL].sudo().with_context(llm_free_daily_limit=5)
        self.State = self.env[RUNTIME]
        self.State._llm_write(free_meter_last_ts=STAMP - 600, free_meter_last_log_id=0,
                              free_meter_scan_start=0, free_meter_scan_end=0,
                              free_meter_scan_page=0, free_meter_scan_max_id=0)
        self.log_base = 9_000_000_000_000 + self.account.id * 1000
        for target, attribute, value in (
            (free_module, 'taipei_day', lambda ts=None: taipei_day(STAMP if ts is None else ts)),
            (key_module, '_utcnow', lambda: NOW),
        ):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = MagicMock()
        self.client.user_set_token_status.return_value = nac.Ok({})
        for name, value in (('_llm_client', self.client), ('_llm_up_token', 'qa-up'),
                            ('_llm_tenant_ip', '198.18.2.1')):
            patcher = patch.object(type(self.env[ACCOUNT]), name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _log(self, seq, timestamp=STAMP, username=None):
        return {'id': self.log_base + seq, 'created_at': timestamp, 'type': 2,
                'group': 'free', 'username': username or self.account.newapi_username}

    def _page(self, items, total=None, page=1):
        return self.Usage._llm_apply_page(self.State._llm_read(), {
            'items': items, 'total': len(items) if total is None else total,
        }, STAMP - 3600, STAMP, page, TEST_PREFIX)

    def _key(self, account=None, kind='free_trial', state='active', reason=None, token_id=123):
        account = account or self.account
        key = _llm_internal(self.env[KEY].sudo()).create({
            'account_id': account.id, 'kind': kind, 'state': state,
            'disabled_by': reason, 'newapi_token_id': token_id,
            'newapi_name': 'wp_%s_qa' % token_id,
            'free_capped_day': DAY - timedelta(days=1) if reason == 'free_daily_cap' else False,
        })
        self.env.flush_all()
        return key

    def _usage(self, account=None, day=DAY, count=5, capped=True):
        return _llm_internal(self.env[USAGE_MODEL].sudo()).create({
            'account_id': (account or self.account).id, 'day_tpe': day,
            'success_count': count, 'capped_at': NOW if capped else False,
        })

    def test_overlap_catches_ten_minute_gap_and_caps_recovery_at_one_day(self):
        self.assertEqual(self.Usage._llm_today(self.account.id)['day_tpe'], DAY.isoformat())
        self.assertEqual(meter_window(STAMP, STAMP - 600), STAMP - 660)
        self.assertEqual(meter_window(STAMP, STAMP - 60), STAMP - 300)
        self.assertEqual(meter_window(STAMP, STAMP - 172800), STAMP - 86400)
        midnight = int(datetime(2031, 6, 7, 16, tzinfo=timezone.utc).timestamp())
        self.assertEqual(taipei_day(midnight), DAY)
        self.assertEqual(taipei_day(midnight - 1), DAY - timedelta(days=1))

    def test_descending_pages_resume_without_skipping_or_double_counting(self):
        rows = [self._log(i) for i in range(101, 1, -1)]
        before = self.State._llm_read()
        self.assertFalse(self._page(rows, total=101))
        state = self.State._llm_read()
        self.assertEqual(state['free_meter_last_log_id'], 0)
        self.assertEqual(state['free_meter_scan_page'], 2)
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 100)
        # Simulate response/page replay after worker loss; receipts, count and cursor
        # are one transaction. Replaying any committed page cannot count twice.
        self.Usage._llm_apply_page(before, {'items': rows, 'total': 101},
                                   STAMP - 3600, STAMP, 1, TEST_PREFIX)
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 100)
        self.assertTrue(self._page([self._log(1)], total=101, page=2))
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 101)
        self.assertEqual(self.State._llm_read()['free_meter_last_log_id'], self.log_base + 101)
        self._page(rows, total=100)
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 101)

    def test_invalid_page_does_not_advance_cursor_or_count(self):
        before = self.State._llm_read()
        _raises(self, ValueError, lambda: self._page([self._log(1), {'id': 2}]))
        self.assertEqual(self.State._llm_read(), before)
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 0)

    def test_only_local_environment_success_logs_count(self):
        self._page([self._log(1), self._log(2, username='wp0000000001'),
                    self._log(3, username=TEST_PREFIX + '9999999999')])
        self.assertEqual(self.Usage._llm_today(self.account.id)['today_count'], 1)

    def test_cap_disables_trial_and_catches_bypass_on_next_tick(self):
        trial = self._key()
        paid = self._key(kind='paid', token_id=456)
        self._page([self._log(i) for i in range(1, 6)])
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        trial.invalidate_recordset()
        self.assertEqual((trial.state, trial.disabled_by), ('disabled', 'free_daily_cap'))
        self.client.user_set_token_status.assert_called_once()
        self.assertFalse(self.client.user_set_token_status.call_args.args[3])
        self.assertEqual(paid.state, 'active')
        exc = _raises(self, LlmError, lambda: self.Usage._llm_assert_can_create(self.account.id))
        self.assertEqual(exc.code, 'llm_free_capped_today')
        # Remove the disabled mirror and deliberately bypass creation guard.
        _llm_internal(trial).write({'state': 'deleted'})
        # Release the partial unique index before the replacement INSERT; ORM
        # writes are deferred, while _key() only flushes after creating its row.
        trial.flush_recordset(['state'])
        bypass = self._key(token_id=789)
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        bypass.invalidate_recordset()
        self.assertEqual((bypass.state, bypass.disabled_by), ('disabled', 'free_daily_cap'))
        self.assertEqual(self.client.user_set_token_status.call_count, 2)

    def test_interrupted_trial_is_disabled_without_conflicting_with_replacement(self):
        failed = self._key(state='failed_reveal', token_id=789)
        replacement = self._key(token_id=790)
        self._usage()
        self.env.flush_all()
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        failed.invalidate_recordset()
        replacement.invalidate_recordset()
        self.assertEqual((failed.state, failed.disabled_by), ('failed_reveal', 'free_daily_cap'))
        self.assertEqual(replacement.state, 'disabled')
        self.assertEqual(self.client.user_set_token_status.call_count, 2)
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        self.assertEqual(self.client.user_set_token_status.call_count, 2)

    def test_failed_up_rotates_so_other_capped_accounts_are_processed(self):
        self._usage()
        self._key()
        other = _new_account(self.env, 'Next cap account')
        self._usage(account=other)
        self._key(account=other, token_id=456)
        self.env.flush_all()
        self.client.user_set_token_status.return_value = nac.NotExecuted('connect')
        with patch.object(type(self.Usage), '_llm_alert'):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 2), enabled=False)
            self.Usage._llm_enforce_caps(LlmCronTick(30, 2), enabled=False)
        self.assertEqual([call.args[2] for call in self.client.user_set_token_status.call_args_list],
                         [123, 456])

    def test_daily_create_budget_is_atomic_and_definite_non_creation_releases_slot(self):
        keys = self.env[KEY]
        for i in range(3):
            key_id, _name = keys._llm_insert_creating(
                self.account, 'free_trial', str(uuid.uuid4()), self.env.user, '', NOW, '')
            keys._llm_finish(key_id, {'state': 'active'})
            # User deletion must not give the daily slot back.
            self.env.cr.execute('UPDATE woow_paas_platform_llm_api_key SET state=\'deleted\' '
                                'WHERE id=%s', (key_id,))
        error = _raises(self, LlmError, lambda: keys._llm_insert_creating(
            self.account, 'free_trial', str(uuid.uuid4()), self.env.user, '', NOW, ''))
        self.assertEqual(error.code, 'llm_free_key_daily_limit')
        self.assertTrue(error.not_executed)
        self.assertEqual(self.Usage._llm_today(self.account.id)['keys_created_today'], 3)
        other = _new_account(self.env, 'Definite failure')
        key_id, _name = keys._llm_insert_creating(
            other, 'free_trial', str(uuid.uuid4()), self.env.user, '', NOW, '')
        keys._llm_finish(key_id, {'state': 'deleted'})
        keys._llm_finish(key_id, {'state': 'deleted'})
        self.assertEqual(self.Usage._llm_today(other.id)['keys_created_today'], 0)

    def test_reset_restores_only_cap_keys_on_later_day_and_is_idempotent(self):
        old = self._key(state='disabled', reason='free_daily_cap')
        suspended = _new_account(self.env, 'Suspended cap', state='suspended')
        suspended_key = self._key(account=suspended, state='disabled', reason='free_daily_cap')
        manual = _new_account(self.env, 'Manual suspension')
        manual_key = self._key(account=manual, state='disabled', reason='suspend')
        today = _new_account(self.env, 'Capped today')
        self._usage(account=today)
        today_key = self._key(account=today, state='disabled', reason='free_daily_cap')
        expired_account = _new_account(self.env, 'Expired cap')
        expired = self._key(account=expired_account, state='disabled', reason='free_daily_cap')
        _llm_internal(expired).write({'expires_at': datetime(2000, 1, 1)})
        self.env.flush_all()
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)
        for key in old + suspended_key + manual_key + today_key:
            key.invalidate_recordset()
        self.assertEqual(old.state, 'active')
        self.assertFalse(old.disabled_by)
        self.assertEqual(suspended_key.state, 'disabled')
        self.assertEqual(manual_key.disabled_by, 'suspend')
        self.assertEqual(today_key.state, 'disabled')
        expired.invalidate_recordset()
        self.assertEqual(expired.state, 'disabled')
        self.client.user_set_token_status.assert_called_once()
        self.assertTrue(self.client.user_set_token_status.call_args.args[3])

    def test_ops_restriction_survives_free_cap_enforcement_and_reset(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            key = _llm_internal(env[KEY]).create({
                'account_id': self.account.id, 'kind': 'free_trial', 'state': 'active',
                'disabled_by': 'ops', 'free_cap_pending': True, 'free_capped_day': DAY,
                'newapi_token_id': 123, 'newapi_name': 'wp_123_qa',
            })
            _llm_internal(env[USAGE_MODEL]).create({
                'account_id': self.account.id, 'day_tpe': DAY,
                'success_count': 5, 'capped_at': NOW,
            })
            key_id = key.id
            cr.commit()
        key = self.env[KEY].browse(key_id)

        self.assertFalse(any(row['id'] == key.id
                             for row in self.Usage._llm_cap_candidates(enabled=False)))
        self.assertFalse(any(row['id'] == key.id
                             for row in self.Usage._llm_cap_candidates(enabled=True)))
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        with patch.object(free_module, 'taipei_day', return_value=DAY + timedelta(days=1)):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)

        key.invalidate_recordset(['state', 'disabled_by', 'free_cap_pending', 'free_capped_day'])
        self.assertEqual((key.state, key.disabled_by, key.free_cap_pending, key.free_capped_day),
                         ('active', 'ops', True, DAY))
        self.client.user_set_token_status.assert_not_called()

    def test_stale_cap_candidate_cannot_replace_concurrent_ops_restriction(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            key = _llm_internal(env[KEY]).create({
                'account_id': self.account.id, 'kind': 'free_trial', 'state': 'active',
                'newapi_token_id': 124, 'newapi_name': 'wp_124_qa',
            })
            _llm_internal(env[USAGE_MODEL]).create({
                'account_id': self.account.id, 'day_tpe': DAY,
                'success_count': 5, 'capped_at': NOW,
            })
            key_id = key.id
            cr.commit()
        key = self.env[KEY].browse(key_id)
        stale_row = next(row for row in self.Usage._llm_cap_candidates(enabled=False)
                         if row['id'] == key.id)
        with self.registry.cursor() as cr:
            cr.execute('UPDATE woow_paas_platform_llm_api_key '
                       "SET disabled_by = 'ops' WHERE id = %s AND state = 'active'",
                       (key.id,))
            self.assertEqual(cr.rowcount, 1)

        with patch.object(type(self.Usage), '_llm_cap_candidates',
                          return_value=[stale_row]):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)

        key.invalidate_recordset(['state', 'disabled_by', 'free_cap_pending'])
        self.assertEqual((key.state, key.disabled_by, key.free_cap_pending),
                         ('active', 'ops', False))
        self.client.user_set_token_status.assert_not_called()

    def test_unknown_disable_can_be_reset_after_midnight(self):
        key = self._key()
        self._usage()
        self.env.flush_all()
        self.client.user_set_token_status.return_value = nac.Unknown('read')
        with patch.object(type(self.Usage), '_llm_alert'):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        key.invalidate_recordset()
        self.assertEqual(key.state, 'active')
        self.assertTrue(key.free_cap_pending)
        self.assertEqual(key.free_capped_day, DAY)
        self.client.user_set_token_status.return_value = nac.Ok({})
        with patch.object(free_module, 'taipei_day', return_value=DAY + timedelta(days=1)):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)
        key.invalidate_recordset()
        self.assertEqual(key.state, 'active')
        self.assertFalse(key.free_cap_pending)
        self.assertFalse(key.disabled_by)
        self.assertEqual([call.args[3] for call in self.client.user_set_token_status.call_args_list],
                         [False, True])

    def test_disable_finishing_after_midnight_keeps_original_cap_day(self):
        key = self._key()
        self._usage()
        self.env.flush_all()
        clock = [DAY]
        def completed(*args, **kwargs):
            clock[0] = DAY + timedelta(days=1)
            return nac.Ok({})
        self.client.user_set_token_status.side_effect = completed
        with patch.object(free_module, 'taipei_day', side_effect=lambda *args: clock[0]):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        key.invalidate_recordset()
        self.assertEqual(key.free_capped_day, DAY)

    def test_unknown_reset_is_disabled_again_when_new_day_reaches_cap(self):
        key = self._key(state='disabled', reason='free_daily_cap')
        # Upstream may have enabled the token even though its response was lost.
        self.client.user_set_token_status.return_value = nac.Unknown('read')
        with patch.object(type(self.Usage), '_llm_alert'):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)
        key.invalidate_recordset()
        self.assertEqual(key.free_capped_day, DAY - timedelta(days=1))
        self._usage()
        self.env.flush_all()
        self.client.user_set_token_status.return_value = nac.Ok({})
        self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        key.invalidate_recordset()
        self.assertEqual(key.free_capped_day, DAY)
        self.assertEqual([call.args[3] for call in self.client.user_set_token_status.call_args_list],
                         [True, False])

    def test_reset_rechecks_suspension_after_selecting_candidates(self):
        key = self._key(state='disabled', reason='free_daily_cap')
        rows = self.Usage._llm_cap_candidates(enabled=True)
        _llm_internal(self.account).write({'state': 'suspended'})
        self.env.flush_all()
        with patch.object(type(self.Usage), '_llm_cap_candidates', return_value=rows):
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=True)
        self.client.user_set_token_status.assert_not_called()
        key.invalidate_recordset()
        self.assertEqual(key.state, 'disabled')

    def test_failed_up_keeps_cap_for_retry_and_alerts(self):
        self._usage()
        key = self._key()
        self.client.user_set_token_status.return_value = nac.NotExecuted('connect')
        with patch.object(type(self.Usage), '_llm_alert') as alert:
            self.Usage._llm_enforce_caps(LlmCronTick(30, 20), enabled=False)
        key.invalidate_recordset()
        self.assertEqual(key.state, 'active')
        self.assertTrue(self.Usage._llm_today(self.account.id)['capped'])
        self.assertEqual(alert.call_args.args[1], 'llm_free_up_failed')

    def test_lag_alert_runs_even_if_circuit_prevents_outbound(self):
        with patch.object(type(self.Usage), '_llm_alert') as alert, \
                patch.object(free_module, 'run_llm_cron') as run, \
                patch.object(free_module.time, 'time', return_value=STAMP):
            self.Usage._cron_llm_free_tier_meter()
        alert.assert_called_once()
        run.assert_called_once()

    def test_cleanup_retains_ninety_days_and_removes_older_rows(self):
        old = self._usage(day=DAY - timedelta(days=91))
        retained = self._usage(day=DAY - timedelta(days=90))
        self.env.flush_all()
        self.env.cr.execute('INSERT INTO %s (newapi_log_id, day_tpe) VALUES (%%s,%%s)' %
                            RECEIPT_TABLE, (self.log_base, DAY - timedelta(days=91)))
        self.Usage._llm_cleanup_tick({}, LlmCronTick(30, 4))
        self.env.invalidate_all()
        self.assertFalse(old.exists())
        self.assertTrue(retained.exists())
        self.env.cr.execute('SELECT id FROM %s WHERE newapi_log_id=%%s' % RECEIPT_TABLE,
                            (self.log_base,))
        self.assertFalse(self.env.cr.fetchone())

    def test_unconfigured_crons_have_zero_outbound_and_zero_alerts(self):
        reset_llm_config(self.env)
        with patch.object(type(self.Usage), '_llm_alert') as alert:
            self.Usage._cron_llm_free_tier_meter()
            self.Usage._cron_llm_free_tier_reset()
            self.Usage._cron_llm_free_usage_cleanup()
            self.Usage._cron_llm_free_group_action()
        self.assertFalse(self.client.mock_calls)
        alert.assert_not_called()

    def test_bg_client_exception_consumes_request_without_delayed_replay(self):
        with patch.object(type(self.env['ir.cron']), '_trigger'):
            self.account.action_llm_block_free_trial()
        with patch.object(type(self.env[ACCOUNT]), '_llm_client', side_effect=RuntimeError('test')), \
                patch.object(type(self.Usage), '_llm_alert') as alert:
            self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        alert.assert_called_once()
        self.account.invalidate_recordset()
        self.assertFalse(self.account.free_group_requested)
        self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        self.client.bg_set_user_group.assert_not_called()

    def test_bg_disabled_consumes_request_and_requires_manual_retry(self):
        with patch.object(type(self.env['ir.cron']), '_trigger'):
            self.account.action_llm_block_free_trial()
        self.client.bg_set_user_group.return_value = nac.Definite(
            proxy_error='policy', reason='break_glass_off')
        with patch.object(type(self.Usage), '_llm_alert') as alert:
            self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        alert.assert_called_once_with(
            free_module.LlmAlert.BREAK_GLASS_USED, 'llm_break_glass_failed', self.account.id)
        self.account.invalidate_recordset()
        self.assertFalse(self.account.free_group_requested)
        self.assertEqual(self.client.bg_set_user_group.call_count, 1)

        # Enabling bg later cannot replay the consumed request.
        self.client.bg_set_user_group.return_value = nac.Ok({})
        self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        self.assertEqual(self.client.bg_set_user_group.call_count, 1)

        with patch.object(type(self.env['ir.cron']), '_trigger'):
            self.account.action_llm_block_free_trial()
        self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        self.assertEqual(self.client.bg_set_user_group.call_count, 2)
        self.account.invalidate_recordset()
        self.assertFalse(self.account.free_group_requested)

    def test_bg_is_explicit_queue_and_changes_only_group(self):
        self.client.bg_set_user_group.return_value = nac.Ok({})
        with patch.object(type(self.env['ir.cron']), '_trigger') as trigger:
            self.account.action_llm_block_free_trial()
        trigger.assert_called_once()
        self.client.bg_set_user_group.assert_not_called()
        self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        self.client.bg_set_user_group.assert_called_once_with(
            1234, 'free_blocked', deadline=self.client.bg_set_user_group.call_args.kwargs['deadline'])
        self.env[ACCOUNT]._llm_free_group_tick({}, LlmCronTick(30, 10))
        self.client.bg_set_user_group.assert_called_once()
        self.client.op_options.assert_not_called()


@tagged('post_install', '-at_install')
class TestLlmFreeUsageSecurity(_ConfiguredCase):

    def test_direct_writes_and_tenant_bg_are_denied(self):
        account = _new_account(self.env, 'Free cap ACL')
        user = new_test_user(self.env, login='freecap-portal-qa', groups='base.group_portal')
        _raises(self, AccessError, lambda: self.env[USAGE_MODEL].sudo().create({
            'account_id': account.id, 'day_tpe': DAY}))
        _raises(self, AccessError, lambda: account.with_user(user).action_llm_block_free_trial())
        self.assertFalse(self.env[USAGE_MODEL].with_user(user).has_access('read'))

    def test_test_cap_override_is_staging_superuser_only(self):
        usage = self.env[USAGE_MODEL].sudo().with_context(llm_free_daily_limit=5)
        self.assertEqual(usage._llm_daily_limit(), 5)
        with patch.object(free_module, 'llm_config', return_value={'proxy_url': nac.PROXY_URL_PROD}):
            self.assertEqual(usage._llm_daily_limit(), 200)
        self.assertEqual(usage.with_context(llm_free_daily_limit=999)._llm_daily_limit(), 200)
        user = new_test_user(self.env, login='freecap-context-qa', groups='base.group_portal')
        self.assertEqual(usage.with_user(user)._llm_daily_limit(), 200)
