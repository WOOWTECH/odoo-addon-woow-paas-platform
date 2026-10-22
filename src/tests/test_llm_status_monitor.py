"""Rollback-scoped SQL/adapter tests, NOT independent-cursor/commit evidence.

No ICP configuration, cron activation, mail or event is committed. The monitor's
module-local deadline_cursor is routed to this test's rollback transaction;
_environment and _now are deterministic fixtures, and _begin is bypassed only
here because isolation cannot be changed after test queries. Production code
and global registry.cursor are unchanged. Independent cursors/real commits,
concurrency, restart and fresh raw-ICP reads require an owned scratch PG runner.
These eight Odoo tests have not been executed during this revision.
"""
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models import llm_status_monitor as module
from ..models.llm_runtime_state import STATE_TABLE


@tagged('post_install', '-at_install', 'llm_status_monitor')
class TestLlmStatusMonitor(TransactionCase):

    def setUp(self):
        super().setUp()
        self.State = self.env['woow_paas_platform.llm_runtime_state'].sudo()
        self.clock = datetime(2026, 10, 21, 12)
        self.fingerprint = 'test-environment-a'
        self.State._llm_ensure_row(self.env.cr)
        self.env.cr.execute('UPDATE %s SET status_armed = false, status_generation = 0, '
                            'status_started_at = NULL, status_completed_at = NULL, '
                            'status_success_at = NULL, status_result = NULL, '
                            'status_fingerprint = NULL, guard_tripped = false, '
                            'guard_reasons = NULL, guard_since = NULL' % STATE_TABLE)

        @contextmanager
        def rollback_cursor(_factory, _deadline):
            # Never enter/exit registry.cursor(): that would commit a different
            # transaction and cannot see the test fixture. Savepoint != commit.
            with self.env.cr.savepoint(flush=False):
                yield self.env.cr

        for name, replacement in (
                ('deadline_cursor', rollback_cursor),
                ('_begin', lambda *args, **kwargs: None),
                ('_environment', lambda cr: (self.fingerprint, module.nac.PROXY_URL_STG, True)),
                ('_now', lambda cr: self.clock)):
            handle = patch.object(module, name, replacement)
            handle.start()
            self.addCleanup(handle.stop)

    def deadline(self):
        return time.monotonic() + 20

    def _read_receipt(self):
        # Explicit same-test-cursor read; do not claim this exercises _llm_read's
        # independent transaction or its visibility/commit guarantees.
        columns = module._STATUS_COLUMNS + ('guard_tripped', 'guard_reasons', 'guard_since')
        self.env.cr.execute('SELECT %s FROM %s' % (', '.join(columns), STATE_TABLE))
        return self.State._llm_status_guard(self.env.cr, self.env.cr.dictfetchone())

    def test_inactive_install_and_active_without_receipt(self):
        self.assertFalse(self.env.ref(module.STATUS_CRON_XMLID).active)
        self.assertFalse(self._read_receipt()['guard_tripped'])
        # Emulate only the active lookup, without changing the real cron row.
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        row = self.State._llm_status_guard(cursor, {'status_armed': False, 'guard_tripped': False})
        self.assertTrue(row['guard_tripped'])
        self.assertIn('SELECT c.active', cursor.execute.call_args.args[0])

    def test_claim_pending_and_single_owner_in_rollback_transaction(self):
        claim = self.State._status_claim(self.deadline())
        self.assertTrue(claim)
        self.assertTrue(self._read_receipt()['guard_tripped'])
        self.assertFalse(self.State._status_claim(self.deadline()))
        self.assertEqual(self._read_receipt()['status_generation'], claim['status_generation'])

    def test_success_receipt_and_raw_guard_preservation(self):
        self.env.cr.execute("UPDATE %s SET guard_tripped = true, guard_reasons = 'other_i7'" % STATE_TABLE)
        claim = self.State._status_claim(self.deadline())
        self.clock += timedelta(seconds=1)
        self.assertEqual(self.State._status_finish(claim, 'ok', self.deadline()), 'ok')
        row = self._read_receipt()
        self.assertTrue(row['guard_tripped'])
        self.assertEqual(row['guard_reasons'], 'other_i7')
        self.assertEqual(row['status_completed_at'], self.clock)
        self.assertEqual(row['status_success_at'], self.clock)

    def test_stale_receipt_guards_without_cron_tick(self):
        claim = self.State._status_claim(self.deadline())
        self.State._status_finish(claim, 'ok', self.deadline())
        self.assertFalse(self._read_receipt()['guard_tripped'])
        self.clock += timedelta(minutes=7)
        self.assertTrue(self._read_receipt()['guard_tripped'])
        self.env.cr.execute('SELECT guard_tripped FROM %s' % STATE_TABLE)
        self.assertFalse(self.env.cr.fetchone()[0], 'overlay must not overwrite raw guard')
        with patch.object(type(self.State), '_llm_read', side_effect=self._read_receipt):
            payload = self.env['woow_paas_platform.llm_swr_cache']._llm_models_payload()
        self.assertEqual(payload['state'], 'maintenance')

    def test_lease_expiry_fences_old_worker_after_new_claim(self):
        first = self.State._status_claim(self.deadline())
        self.clock += timedelta(seconds=31)
        second = self.State._status_claim(self.deadline())
        self.assertGreater(second['status_generation'], first['status_generation'])
        self.assertFalse(self.State._status_finish(first, 'ok', self.deadline()))
        self.assertEqual(self._read_receipt()['status_result'], 'pending')
        self.assertEqual(self.State._status_finish(second, 'invalid', self.deadline()), 'invalid')
        self.assertTrue(self._read_receipt()['guard_tripped'])

    def test_changed_environment_rejects_old_ok_receipt(self):
        claim = self.State._status_claim(self.deadline())
        self.fingerprint = 'test-environment-b'
        self.assertEqual(self.State._status_finish(claim, 'ok', self.deadline()), 'config_changed')
        self.assertTrue(self._read_receipt()['guard_tripped'])

    def test_future_timestamp_and_expired_completion_are_rejected(self):
        claim = self.State._status_claim(self.deadline())
        self.clock -= timedelta(minutes=1)
        self.assertFalse(self.State._status_finish(claim, 'ok', self.deadline()))
        self.assertTrue(self._read_receipt()['guard_tripped'])
        self.clock += timedelta(minutes=2)
        self.assertFalse(self.State._status_finish(claim, 'ok', self.deadline()))

    def test_unconfigured_status_does_not_create_client_receipt_or_alert(self):
        Account = type(self.env['woow_paas_platform.llm_account'])
        Event = type(self.env['woow_paas_platform.llm_event'])
        config = {'proxy_state': 'unset', 'show': False, 'pilot': frozenset(), 'canary': ''}
        with patch.object(module, 'llm_config', return_value=config), \
                patch.object(Account, '_llm_client') as client, \
                patch.object(Event, '_llm_alert') as alert, \
                patch.object(type(self.State), '_status_claim') as claim:
            self.assertFalse(self.State._cron_llm_status_monitor())
        client.assert_not_called()
        alert.assert_not_called()
        claim.assert_not_called()
