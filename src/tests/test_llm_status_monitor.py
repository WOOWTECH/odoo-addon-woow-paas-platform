"""Status receipts and guard integration on owned Odoo test transactions.

Prepared for later isolated execution only; no shared sandbox run in this PR.
HttpCase test cursors roll back all state/config changes, as existing LLM suites.
"""
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from ..models import llm_common
from ..models import llm_status_monitor as module
from ..models.llm_runtime_state import STATE_TABLE
from .llm_test_support import configure, reset_llm_config


@tagged('post_install', '-at_install', 'llm_status_monitor')
class TestLlmStatusMonitor(HttpCase):

    def setUp(self):
        super().setUp()
        configure(self.env)
        self.State = self.env['woow_paas_platform.llm_runtime_state'].sudo()
        self.env.cr.execute('UPDATE %s SET status_armed = false, status_generation = 0, '
                            'status_started_at = NULL, status_completed_at = NULL, '
                            'status_success_at = NULL, status_result = NULL, '
                            'status_fingerprint = NULL, guard_tripped = false, '
                            'guard_reasons = NULL, guard_since = NULL' % STATE_TABLE)

    def deadline(self):
        return time.monotonic() + 20

    def test_inactive_install_and_active_without_receipt(self):
        cron = self.env.ref(module.STATUS_CRON_XMLID).sudo()
        self.assertFalse(cron.active)
        self.assertFalse(self.State._llm_read()['guard_tripped'])
        cron.write({'active': True})  # test transaction only, never committed
        self.assertTrue(self.State._llm_read()['guard_tripped'])

    def test_claim_is_durable_pending_and_single_owner(self):
        claim = self.State._status_claim(self.deadline())
        self.assertTrue(claim)
        self.assertTrue(self.State._llm_read()['guard_tripped'])
        self.assertFalse(self.State._status_claim(self.deadline()))
        self.assertEqual(self.State._llm_read()['status_generation'], claim['status_generation'])

    def test_success_receipt_and_raw_guard_preservation(self):
        self.env.cr.execute("UPDATE %s SET guard_tripped = true, guard_reasons = 'other_i7'" % STATE_TABLE)
        claim = self.State._status_claim(self.deadline())
        self.assertEqual(self.State._status_finish(claim, 'ok', self.deadline()), 'ok')
        row = self.State._llm_read()
        self.assertTrue(row['guard_tripped'])
        self.assertEqual(row['guard_reasons'], 'other_i7')
        self.assertTrue(row['status_completed_at'])
        self.assertTrue(row['status_success_at'])

    def test_stale_receipt_guards_without_cron_tick(self):
        claim = self.State._status_claim(self.deadline())
        self.State._status_finish(claim, 'ok', self.deadline())
        self.assertFalse(self.State._llm_read()['guard_tripped'])
        self.env.cr.execute("UPDATE %s SET status_started_at = status_started_at - interval '7 minutes', "
                            "status_completed_at = status_completed_at - interval '7 minutes'" % STATE_TABLE)
        self.assertTrue(self.State._llm_read()['guard_tripped'])
        self.env.cr.execute('SELECT guard_tripped FROM %s' % STATE_TABLE)
        self.assertFalse(self.env.cr.fetchone()[0], 'status overlay must not overwrite raw guard')
        payload = self.env['woow_paas_platform.llm_swr_cache']._llm_models_payload()
        self.assertEqual(payload['state'], 'maintenance')

    def test_lease_expiry_fences_old_worker_after_new_claim(self):
        first = self.State._status_claim(self.deadline())
        self.env.cr.execute("UPDATE %s SET status_started_at = status_started_at - interval '31 seconds'"
                            % STATE_TABLE)
        second = self.State._status_claim(self.deadline())
        self.assertGreater(second['status_generation'], first['status_generation'])
        self.assertFalse(self.State._status_finish(first, 'ok', self.deadline()))
        self.assertEqual(self.State._llm_read()['status_result'], 'pending')
        self.assertEqual(self.State._status_finish(second, 'invalid', self.deadline()), 'invalid')
        self.assertTrue(self.State._llm_read()['guard_tripped'])

    def test_changed_db_config_rejects_old_ok_receipt(self):
        claim = self.State._status_claim(self.deadline())
        self.env.cr.execute('UPDATE ir_config_parameter SET value = %s WHERE key = %s',
                            ('wqb', llm_common.PARAM_USERNAME_PREFIX))
        self.assertEqual(self.State._status_finish(claim, 'ok', self.deadline()), 'config_changed')
        self.assertTrue(self.State._llm_read()['guard_tripped'])

    def test_future_timestamp_and_expired_completion_are_rejected(self):
        claim = self.State._status_claim(self.deadline())
        self.env.cr.execute('UPDATE %s SET status_started_at = %%s' % STATE_TABLE,
                            (fields.Datetime.now() + timedelta(minutes=1),))
        self.assertFalse(self.State._status_finish(claim, 'ok', self.deadline()))
        self.assertTrue(self.State._llm_read()['guard_tripped'])

    def test_unconfigured_status_does_not_create_client_receipt_or_alert(self):
        reset_llm_config(self.env)
        Account = type(self.env['woow_paas_platform.llm_account'])
        Event = type(self.env['woow_paas_platform.llm_event'])
        State = type(self.State)
        with patch.object(Account, '_llm_client') as client, \
                patch.object(Event, '_llm_alert') as alert, \
                patch.object(State, '_status_claim') as claim:
            self.assertFalse(self.State._cron_llm_status_monitor())
        client.assert_not_called()
        alert.assert_not_called()
        claim.assert_not_called()
