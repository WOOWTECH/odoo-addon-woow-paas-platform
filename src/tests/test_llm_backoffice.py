"""Safety contracts for LLM-12a backoffice actions."""
import threading
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.llm_common import _llm_internal
from ..models.llm_common import LlmError
from ..models.llm_credit_order import ORDER_TABLE, _credit_internal
from ..services import newapi_client as nac
from .llm_test_support import CommittedLlmFixture, env_for

BACKOFFICE = 'woow_paas_platform.llm_manual_credit_service'
ACCOUNT = 'woow_paas_platform.llm_account'
ORDER = 'woow_paas_platform.llm_credit_order'


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmBackofficeCommitted(TransactionCase):
    """Real cursors prove idempotency and the daily cap at the DB boundary."""

    def setUp(self):
        super().setUp()
        self.fixture = CommittedLlmFixture(self).start()
        self.addCleanup(self._cleanup)
        tag = uuid.uuid4().hex[:10]
        self.partner_id = self.fixture.partner('LLM backoffice ' + tag)
        self.account_id = self.fixture.activate(self.partner_id)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env[ACCOUNT].browse(self.account_id)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': 4811020})
            operator = new_test_user(
                env, login='llm_backoffice_' + tag,
                groups='base.group_user,base.group_system',
                email='llm-backoffice-%s@example.com' % tag)
            operator._llm_set_named_human_eligible(True)
            self.operator_id = operator.id
        # Patch the imported symbols in the implementation module. Patches are
        # process-local and never alter shared database configuration.
        from ..models import llm_backoffice
        begin = llm_backoffice._begin

        def begin_read_committed_in_test(env, cr, read_committed=False):
            # _begin intentionally avoids changing isolation in Odoo's shared
            # HttpCase test cursor. These are independent committed cursors;
            # give them the same RC isolation they receive in production.
            if read_committed and env.registry.in_test_mode():
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            return begin(env, cr, read_committed=read_committed)

        self.alert_sink = patch.object(
            type(self.env['woow_paas_platform.llm_event']), '_llm_alert', return_value=1)
        self.incident_sink = patch.object(
            type(self.env['woow_paas_platform.llm_event']), '_llm_incident', return_value=1)
        self.alert_mock = self.alert_sink.start()
        self.incident_mock = self.incident_sink.start()
        self.addCleanup(self.alert_sink.stop)
        self.addCleanup(self.incident_sink.stop)
        self._active_patches = [
            patch.object(llm_backoffice, 'llm_config', return_value={
                'orders_enabled': True, 'service_admin_id': None}),
            patch.object(llm_backoffice, 'llm_unconfigured', return_value=False),
            patch.object(llm_backoffice, '_begin', begin_read_committed_in_test),
            patch.object(type(self.env['woow_paas_platform.llm_runtime_state']),
                         '_llm_read', return_value={'guard_tripped': False}),
            patch.object(type(self.env['woow_paas_platform.llm_event']),
                         '_llm_record', return_value=1),
            patch.object(type(self.env['ir.cron']), '_trigger', return_value=False),
        ]
        for patcher in self._active_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _cleanup(self):
        if not getattr(self, 'account_id', False):
            return
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_paas_platform_llm_backoffice_operation '
                       'WHERE account_id = %s', (self.account_id,))
            cr.execute('DELETE FROM woow_paas_platform_llm_event WHERE account_id = %s',
                       (self.account_id,))
            cr.execute('DELETE FROM woow_paas_platform_llm_credit_order WHERE account_id = %s',
                       (self.account_id,))
        self.fixture.purge()
        if getattr(self, 'operator_id', False):
            with self.registry.cursor() as cr:
                env_for(self.registry, cr)['res.users'].sudo().browse(
                    self.operator_id).unlink()

    def _cleanup_operation(self, operation_id):
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_paas_platform_llm_backoffice_operation WHERE id = %s',
                       (operation_id,))

    def _service_call(self, request_id, points=900, reason='approved manual backoffice credit',
                      account_id=None):
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)
            env = api.Environment(cr, self.operator_id, {})
            return env[BACKOFFICE]._create_manual(
                account_id or self.account_id, points, reason, request_id)

    def test_old_request_replay_returns_same_result_and_rejects_fingerprint_change(self):
        request_id = uuid.uuid4().int % (2 ** 31 - 1) or 1
        first = self._service_call(request_id)
        replay = self._service_call(request_id)
        self.assertEqual(replay, first)
        with self.assertRaises(ValidationError):
            self._service_call(request_id, points=899)
        with self.assertRaises(ValidationError):
            self._service_call(request_id, reason='different approved manual reason')
        second_partner = self.fixture.partner('LLM backoffice replay mismatch')
        second_account_id = self.fixture.activate(second_partner)
        with self.registry.cursor() as cr:
            _llm_internal(env_for(self.registry, cr)[ACCOUNT].browse(second_account_id)).write({
                'state': 'active', 'newapi_user_id': 4811022})
        with self.assertRaises(ValidationError):
            self._service_call(request_id, account_id=second_account_id)

    def test_manual_notice_marker_survives_failed_alert_and_replay_repairs_it(self):
        request_id = uuid.uuid4().int % (2 ** 31 - 1) or 1
        self.alert_mock.return_value = False
        first = self._service_call(request_id, points=11)
        self.assertFalse(first['notice_accepted'])
        with self.registry.cursor() as cr:
            cr.execute('SELECT manual_notice_accepted FROM ' + ORDER_TABLE + ' WHERE id = %s',
                       (first['id'],))
            self.assertFalse(cr.fetchone()[0])
        self.alert_mock.return_value = 1
        replay = self._service_call(request_id, points=11)
        self.assertTrue(replay['notice_accepted'])
        with self.registry.cursor() as cr:
            cr.execute('SELECT manual_notice_accepted FROM ' + ORDER_TABLE + ' WHERE id = %s',
                       (first['id'],))
            self.assertTrue(cr.fetchone()[0])

    def _queue_manual_operation(self, request_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, self.operator_id, {})
            operation = env['woow_paas_platform.llm_backoffice_operation']._enqueue(
                operation='manual_credit', request_id=request_id,
                operator=env['res.users'].browse(self.operator_id),
                account=env[ACCOUNT].browse(self.account_id), points=19,
                reason='approved manual grant recovery test')
            return operation.id

    def test_worker_commits_manual_order_and_result_fk_with_read_committed(self):
        from ..models.llm_common import LlmCronTick
        from ..models import llm_backoffice_operation
        operation_id = self._queue_manual_operation('manual-full-' + uuid.uuid4().hex)
        worker = self.env['woow_paas_platform.llm_backoffice_operation']
        tick = LlmCronTick(seconds=20, items=10)
        with patch.object(llm_backoffice_operation, 'llm_unconfigured', return_value=False):
            self.assertFalse(worker._process_batch(tick))
        self.assertEqual(tick.done, 1)
        with self.registry.cursor() as cr:
            cr.execute('SELECT state, order_id FROM woow_paas_platform_llm_backoffice_operation '
                       'WHERE id = %s', (operation_id,))
            state, order_id = cr.fetchone()
            self.assertEqual(state, 'done')
            cr.execute('SELECT source, points FROM ' + ORDER_TABLE + ' WHERE id = %s',
                       (order_id,))
            self.assertEqual(cr.fetchone(), ('manual', 19))

    def test_manual_notice_repair_survives_operator_revocation(self):
        from ..models.llm_common import LlmCronTick
        from ..models import llm_backoffice_operation
        operation_id = self._queue_manual_operation('manual-repair-' + uuid.uuid4().hex)
        worker = self.env['woow_paas_platform.llm_backoffice_operation']
        self.alert_mock.return_value = False
        with patch.object(llm_backoffice_operation, 'llm_unconfigured', return_value=False):
            worker._process_batch(LlmCronTick(seconds=20, items=10))
        with self.registry.cursor() as cr:
            cr.execute('SELECT id FROM ' + ORDER_TABLE +
                       " WHERE client_request_id = %s AND manual_notice_accepted = FALSE",
                       ('manual-wizard-%d' % operation_id,))
            self.assertTrue(cr.fetchone())
            env = api.Environment(cr, self.operator_id, {})
            env['res.users'].browse(self.operator_id)._llm_set_named_human_eligible(False)
        self.alert_mock.return_value = 1
        with patch.object(llm_backoffice_operation, 'llm_unconfigured', return_value=False):
            worker._process_batch(LlmCronTick(seconds=20, items=10))
        with self.registry.cursor() as cr:
            cr.execute('SELECT state FROM woow_paas_platform_llm_backoffice_operation '
                       'WHERE id = %s', (operation_id,))
            self.assertEqual(cr.fetchone()[0], 'done')
            cr.execute('SELECT manual_notice_accepted FROM ' + ORDER_TABLE +
                       ' WHERE client_request_id = %s', ('manual-wizard-%d' % operation_id,))
            self.assertTrue(cr.fetchone()[0])
        with self.registry.cursor() as cr:
            api.Environment(cr, self.operator_id, {})['res.users'].browse(
                self.operator_id)._llm_set_named_human_eligible(True)

    def test_retry_uses_owned_reservation_and_audits_in_same_transaction(self):
        from ..models import llm_backoffice
        from odoo import fields
        account = self.env[ACCOUNT].browse(self.account_id)
        reservation_id = self.env['woow_paas_platform.llm_event']._llm_throttle_reserve(
            account, 'convert', uid=self.operator_id)
        stamp = fields.Datetime.now()
        order = _credit_internal(self.env[ORDER].sudo()).create({
            'account_id': account.id, 'partner_id': account.partner_id.id,
            'newapi_user_id': account.newapi_user_id, 'source': 'user', 'points': 10,
            'quota': 156250, 'client_request_id': 'retry-' + uuid.uuid4().hex,
            'convert_event_id': reservation_id,
        })
        _credit_internal(order)._transition(
            'sending', sent_at=stamp, quota_before=1000, used_quota_before=100,
            request_count_before=4, before_snapshot_present=True, before_sampled_at=stamp)
        _credit_internal(order)._transition('needs_review')
        self.env['woow_paas_platform.llm_event']._llm_event_finish(
            reservation_id, 'not_executed', target_model=ORDER, target_id=order.id)

        class Client:
            def ap_log(self, *args, **kwargs):
                return nac.Ok(data={'page': 1, 'page_size': 100, 'total': 0, 'items': []})

            def ap_get_user(self, user_id, **kwargs):
                return nac.Ok(data={'id': user_id, 'quota': 1000,
                                    'used_quota': 100, 'request_count': 4})

        with patch.object(type(account), '_llm_client', return_value=Client()), \
                patch.object(llm_backoffice, 'llm_unconfigured', return_value=False), \
                patch.object(llm_backoffice, 'llm_config', return_value={
                    'service_admin_id': None, 'orders_enabled': True}), \
                patch.object(type(self.env['woow_paas_platform.llm_runtime_state']),
                             '_llm_read', return_value={'guard_tripped': False}):
            order.with_user(self.operator_id)._admin_review(
                'retry', 'upstream was checked and no credit occurred',
                operator_id=self.operator_id)
        self.assertEqual(order.state, 'paid')
        self.env.cr.execute('SELECT result FROM woow_paas_platform_llm_event WHERE id = %s',
                            (reservation_id,))
        self.assertEqual(self.env.cr.fetchone()[0], 'not_executed')
        self.env.cr.execute('SELECT id, uid FROM woow_paas_platform_llm_event '
                            "WHERE action = 'order_transition' AND target_id = %s", (order.id,))
        audit = self.env.cr.fetchone()
        self.assertTrue(audit)
        self.assertEqual(audit[1], self.operator_id)

    def test_review_audit_insert_failure_rolls_back_transition(self):
        from ..models import llm_backoffice
        from odoo import fields
        account = self.env[ACCOUNT].browse(self.account_id)
        stamp = fields.Datetime.now()
        reservation_id = self.env['woow_paas_platform.llm_event']._llm_throttle_reserve(
            account, 'convert', uid=self.operator_id)
        order = _credit_internal(self.env[ORDER].sudo()).create({
            'account_id': account.id, 'partner_id': account.partner_id.id,
            'newapi_user_id': account.newapi_user_id, 'source': 'user', 'points': 10,
            'quota': 156250, 'client_request_id': 'audit-fail-' + uuid.uuid4().hex,
            'convert_event_id': reservation_id,
        })
        _credit_internal(order)._transition(
            'sending', sent_at=stamp, quota_before=1000, used_quota_before=100,
            request_count_before=4, before_snapshot_present=True, before_sampled_at=stamp)
        _credit_internal(order)._transition('needs_review')
        self.env['woow_paas_platform.llm_event']._llm_event_finish(
            reservation_id, 'not_executed', target_model=ORDER, target_id=order.id)

        class Client:
            def ap_log(self, *args, **kwargs):
                return nac.Ok(data={'page': 1, 'page_size': 100, 'total': 0, 'items': []})

            def ap_get_user(self, user_id, **kwargs):
                return nac.Ok(data={'id': user_id, 'quota': 1000,
                                    'used_quota': 100, 'request_count': 4})

        with patch.object(type(account), '_llm_client', return_value=Client()), \
                patch.object(llm_backoffice, 'llm_unconfigured', return_value=False), \
                patch.object(llm_backoffice, 'llm_config', return_value={
                    'service_admin_id': None, 'orders_enabled': True}), \
                patch.object(type(self.env['woow_paas_platform.llm_runtime_state']),
                             '_llm_read', return_value={'guard_tripped': False}), \
                patch.object(type(self.env['woow_paas_platform.llm_event']),
                             '_llm_insert_event_cr', side_effect=RuntimeError('injected')) as audit_insert:
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with self.env.cr.savepoint():
                    order.with_user(self.operator_id)._admin_review(
                        'retry', 'upstream checked no matching log',
                        operator_id=self.operator_id)
        self.assertEqual(audit_insert.call_count, 1, 'financial audit failure injection was reached')
        order.invalidate_recordset()
        self.assertEqual(order.state, 'needs_review')

    def test_worker_passes_shared_deadline_and_leaves_unclaimed_work_pending(self):
        import time
        from ..models import llm_backoffice_operation
        from ..models.llm_common import LlmCronTick
        account_id = self.account_id
        operator_id = self.operator_id
        with self.registry.cursor() as cr:
            env = api.Environment(cr, operator_id, {})
            account = env[ACCOUNT].browse(account_id)
            order = _credit_internal(env[ORDER].sudo()).create({
                'account_id': account.id, 'partner_id': account.partner_id.id,
                'newapi_user_id': account.newapi_user_id, 'source': 'user', 'points': 10,
                'quota': 156250, 'client_request_id': 'deadline-' + uuid.uuid4().hex,
            })
            _credit_internal(order)._transition('needs_review')
            operation = env['woow_paas_platform.llm_backoffice_operation']._enqueue(
                operation='review', request_id='deadline-op-' + uuid.uuid4().hex,
                operator=env['res.users'].browse(operator_id), account=account,
                order=order, decision='credited', reason='reviewed in bounded worker')
            operation_id = operation.id
        deadlines = []

        def fake_review(order, decision, reason, operator_id=None, deadline=None):
            deadlines.append(deadline)
            return True

        tick = LlmCronTick(seconds=12, items=10)
        with patch.object(llm_backoffice_operation, 'llm_unconfigured', return_value=False), \
                patch.object(type(self.env[ORDER]), '_admin_review', fake_review):
            self.env['woow_paas_platform.llm_backoffice_operation']._process_batch(tick)
        self.assertEqual(len(deadlines), 1)
        self.assertGreater(deadlines[0], time.monotonic())
        self.assertLessEqual(deadlines[0], tick.deadline)

        pending_id = self._queue_manual_operation('deadline-pending-' + uuid.uuid4().hex)
        exhausted = LlmCronTick(seconds=1, items=10)
        exhausted.deadline = time.monotonic() - 1
        with patch.object(llm_backoffice_operation, 'llm_unconfigured', return_value=False):
            self.env['woow_paas_platform.llm_backoffice_operation']._process_batch(exhausted)
        self.assertTrue(exhausted.more)
        with self.registry.cursor() as cr:
            cr.execute('SELECT state FROM woow_paas_platform_llm_backoffice_operation '
                       'WHERE id = %s', (pending_id,))
            self.assertEqual(cr.fetchone()[0], 'pending')

    def test_monthly_report_queues_once_for_configured_recipients_with_escaped_reasons(self):
        from datetime import datetime, timedelta, timezone
        from ..models import llm_backoffice
        taipei = timezone(timedelta(hours=8))
        fixed_now = datetime(2099, 9, 15, 12, tzinfo=taipei)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        reason = "<script>alert('not markup')</script> approved manual correction"
        created = self._service_call(
            uuid.uuid4().int % (2 ** 31 - 1) or 1, points=13, reason=reason)
        now_tw = fixed_now
        month_start = now_tw.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_start = (month_start - timedelta(days=1)).replace(day=1)
        prior_month_stamp = previous_start.astimezone(timezone.utc).replace(tzinfo=None)
        period_id = previous_start.strftime('%Y-%m')
        with self.registry.cursor() as cr:
            cr.execute('UPDATE ' + ORDER_TABLE + ' SET create_date = %s WHERE id = %s',
                       (prior_month_stamp, created['id']))
        with self.registry.cursor() as cr:
            cr.execute('SELECT id FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', (period_id,))
            if cr.fetchone():
                self.skipTest('isolated synthetic report period is already occupied')

        cursor_key = 'monthly-test-' + uuid.uuid4().hex
        owned = {'report_id': None, 'mail_id': None, 'cursor_id': None}
        def cleanup_report():
            with self.registry.cursor() as cr:
                if owned['report_id'] and owned['mail_id']:
                    cr.execute('SELECT mail_ref FROM woow_paas_platform_llm_manual_credit_report '
                               'WHERE id = %s AND period_id = %s',
                               (owned['report_id'], period_id))
                    report = cr.fetchone()
                    if report and report[0] == owned['mail_id']:
                        cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report '
                                   'WHERE id = %s AND period_id = %s AND mail_ref = %s',
                                   (owned['report_id'], period_id, owned['mail_id']))
                        if cr.rowcount == 1:
                            cr.execute('DELETE FROM mail_mail WHERE id = %s', (owned['mail_id'],))
                if owned['cursor_id']:
                    cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report_cursor '
                               'WHERE id = %s AND cursor_key = %s',
                               (owned['cursor_id'], cursor_key))

        self.addCleanup(cleanup_report)
        with patch.object(llm_backoffice, 'alert_targets', return_value={
                'emails': ('authorized-reports@example.test',), 'webhook': None}):
            cron = self.env['woow_paas_platform.llm_manual_credit_cron']
            with patch.object(llm_backoffice, 'datetime', FrozenDateTime):
                self.assertTrue(cron._queue_monthly_report(
                    cursor_key=cursor_key, initial_period=period_id))
                self.assertFalse(cron._queue_monthly_report(
                    cursor_key=cursor_key, initial_period=period_id))
        with self.registry.cursor() as cr:
            cr.execute('SELECT id FROM woow_paas_platform_llm_manual_credit_report_cursor '
                       'WHERE cursor_key = %s', (cursor_key,))
            cursor_row = cr.fetchone()
            owned['cursor_id'] = cursor_row[0]
            cr.execute('SELECT id, period_id, start_utc, end_utc, order_count, points_total, mail_ref '
                       'FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', (period_id,))
            row = cr.fetchone()
            self.assertTrue(row)
            owned['report_id'] = row[0]
            owned['mail_id'] = row[6]
            self.assertEqual(row[2], previous_start.astimezone(timezone.utc).replace(tzinfo=None))
            self.assertEqual(row[3], month_start.astimezone(timezone.utc).replace(tzinfo=None))
            self.assertEqual(row[4], 1)
            self.assertEqual(row[5], 13)
            cr.execute('SELECT email_to, body_html, auto_delete FROM mail_mail WHERE id = %s',
                       (row[6],))
            email_to, body_html, auto_delete = cr.fetchone()
            self.assertEqual(email_to, 'authorized-reports@example.test')
            self.assertIn('&lt;script&gt;', body_html)
            self.assertNotIn('<script>', body_html)
            self.assertTrue(auto_delete)

    def test_monthly_report_catches_up_failed_period_after_month_rollover(self):
        from datetime import datetime, timedelta, timezone
        from ..models import llm_backoffice
        taipei = timezone(timedelta(hours=8))
        first_now = datetime(2099, 9, 15, 12, tzinfo=taipei)
        rolled_now = datetime(2099, 10, 2, 12, tzinfo=taipei)
        cursor_key = 'monthly-catchup-' + uuid.uuid4().hex
        created = self._service_call(
            uuid.uuid4().int % (2 ** 31 - 1) or 1, points=23,
            reason='durable monthly report retry fixture')
        start_local = datetime(2099, 8, 1, tzinfo=taipei)
        period_id = '2099-08'
        with self.registry.cursor() as cr:
            cr.execute('SELECT id FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', (period_id,))
            if cr.fetchone():
                self.skipTest('isolated synthetic catch-up period is already occupied')
            cr.execute('UPDATE ' + ORDER_TABLE + ' SET create_date = %s WHERE id = %s',
                       (start_local.astimezone(timezone.utc).replace(tzinfo=None), created['id']))
        owned = {'cursor_id': None, 'report_id': None, 'mail_id': None}

        def cleanup_report():
            with self.registry.cursor() as cr:
                if owned['report_id'] and owned['mail_id']:
                    cr.execute('SELECT mail_ref FROM woow_paas_platform_llm_manual_credit_report '
                               'WHERE id = %s AND period_id = %s',
                               (owned['report_id'], period_id))
                    report = cr.fetchone()
                    if report and report[0] == owned['mail_id']:
                        cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report '
                                   'WHERE id = %s AND period_id = %s AND mail_ref = %s',
                                   (owned['report_id'], period_id, owned['mail_id']))
                        if cr.rowcount == 1:
                            cr.execute('DELETE FROM mail_mail WHERE id = %s', (owned['mail_id'],))
                if owned['cursor_id']:
                    cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report_cursor '
                               'WHERE id = %s AND cursor_key = %s',
                               (owned['cursor_id'], cursor_key))

        self.addCleanup(cleanup_report)

        def frozen_datetime(now):
            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return now.astimezone(tz) if tz else now.replace(tzinfo=None)
            return FrozenDateTime

        cron = self.env['woow_paas_platform.llm_manual_credit_cron']
        with patch.object(llm_backoffice, 'alert_targets', return_value={
                'emails': ('authorized-reports@example.test',), 'webhook': None}), \
                patch.object(llm_backoffice, 'datetime', frozen_datetime(first_now)), \
                patch.object(type(self.env['mail.mail']), 'create',
                             side_effect=RuntimeError('injected queue failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected queue failure'):
                cron._queue_monthly_report(
                    cursor_key=cursor_key, initial_period=period_id)
        with self.registry.cursor() as cr:
            cr.execute('SELECT id, next_period FROM woow_paas_platform_llm_manual_credit_report_cursor '
                       'WHERE cursor_key = %s', (cursor_key,))
            cursor_row = cr.fetchone()
            self.assertEqual(cursor_row[1], period_id)
            owned['cursor_id'] = cursor_row[0]

        with patch.object(llm_backoffice, 'alert_targets', return_value={
                'emails': ('authorized-reports@example.test',), 'webhook': None}), \
                patch.object(llm_backoffice, 'datetime', frozen_datetime(rolled_now)):
            self.assertTrue(cron._queue_monthly_report(
                cursor_key=cursor_key, initial_period=period_id))
        with self.registry.cursor() as cr:
            cr.execute('SELECT id, order_count, points_total, mail_ref '
                       'FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', (period_id,))
            report = cr.fetchone()
            self.assertTrue(report)
            owned['report_id'], owned['mail_id'] = report[0], report[3]
            self.assertEqual(report[1:3], (1, 23))
            cr.execute('SELECT next_period FROM woow_paas_platform_llm_manual_credit_report_cursor '
                       'WHERE id = %s AND cursor_key = %s', (owned['cursor_id'], cursor_key))
            self.assertEqual(cr.fetchone()[0], '2099-09')

    def test_budget_alert_and_incident_inputs_contain_no_reason(self):
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)
            env = api.Environment(cr, self.operator_id, {})
            env[BACKOFFICE]._report_manual_cap(self.account_id)
        self.assertNotIn('reason', repr(self.alert_mock.call_args.kwargs))
        self.assertNotIn('reason', repr(self.incident_mock.call_args.kwargs))
        self.assertEqual(self.alert_mock.call_args.kwargs['numbers'], {
            'account_id': self.account_id,
        })

    def test_manual_rpc_only_queues_stable_operation(self):
        wizard = self.env['woow_paas_platform.llm_manual_credit_wizard'].with_user(
            self.operator_id).create({
                'account_id': self.account_id, 'points': 17,
                'reason': 'manual adjustment approved for testing',
            })
        action = wizard.action_create()
        operation = self.env['woow_paas_platform.llm_backoffice_operation'].browse(
            action['res_id'])
        self.assertEqual(operation.state, 'pending')
        self.assertEqual(operation.operator_id.id, self.operator_id)
        with self.registry.cursor() as cr:
            cr.execute("SELECT count(*) FROM " + ORDER_TABLE +
                       " WHERE source = 'manual' AND client_request_id = %s",
                       ('manual-wizard-%d' % operation.id,))
            self.assertEqual(cr.fetchone()[0], 0)
        replay = wizard.action_create()
        self.assertEqual(replay['res_id'], operation.id)
        wizard.write({'reason': 'a changed reason that must not replay'})
        with self.assertRaises(ValidationError):
            wizard.action_create()

    def test_token_recovery_alternates_scan_and_disable_and_requires_complete_proof(self):
        from ..services import llm_token_recovery as recovery

        self.assertEqual(recovery.next_action(
            scan_required=True, scan_complete=False,
            pending={'10': {'retry_at': 999}}, prefer_scan=False, now=100), 'scan')
        self.assertEqual(recovery.next_action(
            scan_required=True, scan_complete=False,
            pending={'10': {'retry_at': 0}}, prefer_scan=True, now=100), 'scan')
        self.assertEqual(recovery.next_action(
            scan_required=True, scan_complete=False,
            pending={'10': {'retry_at': 0}}, prefer_scan=False, now=100), 'disable')
        self.assertFalse(recovery.valid_page([], 101, 3, 100, nac.INT32_MAX),
                         'a page past the reported end cannot prove a full scan')
        first_page = [{'id': 10, 'name': 'same'}]
        head = recovery.page_fingerprint(first_page)
        proof = dict(pending={}, disabled={10}, total=1, baseline_total=1,
                     head=head, verify_items=first_page, verify_total=1,
                     page_size=100, max_id=nac.INT32_MAX)
        self.assertTrue(recovery.stable_scan_complete(**proof))
        self.assertFalse(recovery.stable_scan_complete(
            **dict(proof, pending={'11': {'retry_at': 0}})),
            'the head and total proof is invalid until every discovered ID settles')

    def test_batch_disable_is_queued_bounded_and_uses_cron_client(self):
        Key = self.env['woow_paas_platform.llm_api_key']
        vals_list = [{
            'account_id': self.account_id, 'kind': 'paid',
            'client_request_id': 'batch-' + uuid.uuid4().hex,
            'newapi_token_id': index, 'state': 'active', 'newapi_name': 'batch-%s' % index,
        } for index in range(501, 512)]
        vals_list[0].update({'kind': 'free_trial', 'state': 'disabled',
                             'disabled_by': 'free_daily_cap'})
        keys = _llm_internal(Key).create(vals_list)
        op = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='disable_all_keys', request_id='disable-test-' + uuid.uuid4().hex,
            operator=self.env['res.users'].browse(self.operator_id),
            reason='disable the mirrored account keys for a safety test')
        self.addCleanup(self._cleanup_operation, op.id)

        class Client:
            def __init__(self):
                self.calls = []
                self.failed_501 = False

            def user_set_token_status(self, ip, up, token_id, enabled, **kwargs):
                self.calls.append((token_id, enabled, kwargs.get('timeout')))
                if token_id == 501 and not self.failed_501:
                    self.failed_501 = True
                    return nac.Definite(status=503, code='TEMPORARY_TEST_FAILURE')
                return nac.Ok(data={})

            def user_list_tokens(self, *args, **kwargs):
                return nac.Ok(data={'items': [], 'total': 0})

        client = Client()
        account_class = type(self.env[ACCOUNT])
        with patch.object(account_class, '_llm_tenant_ip', return_value='203.0.113.1'), \
                patch.object(account_class, '_llm_up_token', return_value='test-up'), \
                patch.object(account_class, '_llm_client', return_value=client):
            worker = self.env['woow_paas_platform.llm_backoffice_operation']
            actor = self.env['res.users'].browse(self.operator_id)
            for _ in range(100):
                worker._process_disable_keys(self.env, op, actor)
                if set(range(501, 512)).issubset({call[0] for call in client.calls}):
                    break
            self.assertTrue(set(range(501, 512)).issubset(
                {call[0] for call in client.calls}),
                'a failing first key must not starve later obligations')
            self.assertEqual(op.state, 'pending')
            failed_obligation = op.key_obligation_ids.filtered(
                lambda item: item.key_id.id == keys[0].id)
            self.assertEqual(failed_obligation.state, 'pending')
            self.env.cr.execute(
                'UPDATE woow_paas_platform_llm_backoffice_key_obligation '
                'SET next_work_at = NULL WHERE id = %s', (failed_obligation.id,))
            for _ in range(100):
                worker._process_disable_keys(self.env, op, actor)
                if all(row.state == 'done' for row in op.key_obligation_ids.filtered(
                        lambda item: item.key_id.id in keys.ids)):
                    break
        self.assertTrue(all(call[1] is False for call in client.calls))
        self.assertTrue(all(call[2] == nac.TIMEOUT_CRON_WRITE for call in client.calls))
        self.assertTrue(all(key.state == 'disabled' and key.disabled_by == 'ops' for key in keys))
        class DisableClient:
            def user_set_token_status(self, *args, **kwargs):
                return nac.Ok(data={})

        with patch.object(type(Key), '_llm_management_remote',
                          return_value=('203.0.113.1', 'test-up', DisableClient())):
            Key._llm_set_key_status(keys[1].partner_id, keys[1].id, False)
        keys[1].invalidate_recordset(['state', 'disabled_by'])
        self.assertEqual(keys[1].disabled_by, 'ops', 'user disable must preserve ops restriction')
        with patch.object(type(Key), '_llm_management_remote',
                          side_effect=AssertionError('ops key enable must stop before remote I/O')):
            with self.assertRaises(LlmError):
                Key._llm_set_key_status(keys[1].partner_id, keys[1].id, True)
        own_obligations = op.key_obligation_ids.filtered(lambda item: item.key_id.id in keys.ids)
        self.assertEqual(len(own_obligations), len(keys))
        self.assertTrue(all(row.state == 'done' for row in own_obligations))
        pending = op.key_obligation_ids.filtered(lambda item: item.state == 'pending')
        self.assertEqual(op.state, 'pending' if pending else 'done')
        self.assertEqual(op.key_failed_count, len(pending))
        self.assertEqual(op.key_target_count, len(op.key_obligation_ids))

    def test_ops_disable_marker_wins_over_late_create_finalize(self):
        request_id = 'late-finalize-' + uuid.uuid4().hex
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            key = _llm_internal(env['woow_paas_platform.llm_api_key']).create({
                'account_id': self.account_id, 'kind': 'paid',
                'client_request_id': request_id,
                'newapi_name': 'late_finalize_' + uuid.uuid4().hex[:10],
                'state': 'creating',
            })
            key_id = key.id
            cr.commit()
        with self.registry.cursor() as cr:
            cr.execute('UPDATE woow_paas_platform_llm_api_key '
                       "SET disabled_by = 'ops' WHERE id = %s AND state = 'creating'",
                       (key_id,))
            self.assertEqual(cr.rowcount, 1)
            cr.commit()

        finish = self.env['woow_paas_platform.llm_api_key']._llm_finish(
            key_id, {'state': 'active', 'newapi_token_id': 991122})
        self.assertEqual(finish, {'state': 'disabled', 'disabled_by': 'ops'})
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            key = env['woow_paas_platform.llm_api_key'].browse(key_id)
            self.assertEqual(key.state, 'disabled')
            self.assertEqual(key.disabled_by, 'ops')
            self.assertEqual(key.newapi_token_id, 991122)

    def test_batch_disable_recovers_every_same_name_token_before_completion(self):
        Key = self.env['woow_paas_platform.llm_api_key']
        name = 'wp_recovery_' + uuid.uuid4().hex[:12]
        key = _llm_internal(Key).create({
            'account_id': self.account_id, 'kind': 'free_trial',
            'client_request_id': 'batch-recovery-' + uuid.uuid4().hex,
            'newapi_name': name, 'state': 'failed_reveal',
        })
        _llm_internal(Key).create({
            'account_id': self.account_id, 'kind': 'free_trial',
            'client_request_id': 'batch-replacement-' + uuid.uuid4().hex,
            'newapi_name': 'wp_replacement_' + uuid.uuid4().hex[:12],
            'state': 'creating',
        })
        op = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='disable_all_keys', request_id='disable-recovery-' + uuid.uuid4().hex,
            operator=self.env['res.users'].browse(self.operator_id),
            reason='recover and disable all same-name token ids')
        self.addCleanup(self._cleanup_operation, op.id)
        obligation = op.key_obligation_ids.filtered(lambda row: row.key_id.id == key.id)
        self.assertTrue(obligation.scan_required)
        matching = [{'id': 2000 + i, 'name': name if i in (10, 20) else 'other-%d' % i}
                    for i in range(100)]

        class Client:
            def __init__(self):
                self.disabled = []
                self.pages = []
                self.events = []
                self.failed_2010 = False

            def user_list_tokens(self, ip, up, page=1, page_size=100, **kwargs):
                self.pages.append(page)
                self.events.append(('list', page))
                items = matching if page == 1 else [{'id': 3000, 'name': name}]
                return nac.Ok(data={'items': items, 'total': 101})

            def user_set_token_status(self, ip, up, token_id, enabled, **kwargs):
                self.events.append(('disable', token_id))
                if token_id == 2010 and not self.failed_2010:
                    self.failed_2010 = True
                    return nac.Definite(status=503, code='TEMPORARY_TEST_FAILURE')
                self.disabled.append(token_id)
                return nac.Ok(data={})

        client = Client()
        account_class = type(self.env[ACCOUNT])
        with patch.object(account_class, '_llm_tenant_ip', return_value='203.0.113.1'), \
                patch.object(account_class, '_llm_up_token', return_value='test-up'), \
                patch.object(account_class, '_llm_client', return_value=client):
            worker = self.env['woow_paas_platform.llm_backoffice_operation']
            actor = self.env['res.users'].browse(self.operator_id)
            for _ in range(200):
                if {2020, 3000}.issubset(set(client.disabled)):
                    break
                worker._process_disable_keys(self.env, op, actor)
            self.assertEqual(set(client.disabled), {2020, 3000},
                             'a backed-off token must not block healthy same-name siblings')
            self.assertTrue(obligation.pending_tokens_json.get('2010'))
            self.assertTrue(obligation.result_code != 'ok')
            from ..services import llm_token_recovery
            self.assertFalse(llm_token_recovery.stable_scan_complete(
                pending={'2010': {'attempts': 1, 'retry_at': 123}},
                disabled={2020, 3000}, total=101, baseline_total=101,
                head=llm_token_recovery.page_fingerprint(matching[:100]),
                verify_items=matching, verify_total=101, page_size=100,
                max_id=nac.INT32_MAX),
                'the final proof must refuse completion while one token remains pending')
            self.env.cr.execute(
                'SELECT pending_tokens_json FROM '
                'woow_paas_platform_llm_backoffice_key_obligation WHERE id = %s',
                (obligation.id,))
            pending = self.env.cr.fetchone()[0]
            pending['2010']['retry_at'] = 0
            self.env.cr.execute(
                'UPDATE woow_paas_platform_llm_backoffice_key_obligation '
                'SET pending_tokens_json = %s::jsonb, next_work_at = NULL WHERE id = %s',
                (json.dumps(pending), obligation.id))
            for _ in range(200):
                if obligation.state == 'done':
                    break
                worker._process_disable_keys(self.env, op, actor)
        self.assertEqual(obligation.state, 'done')
        key.invalidate_recordset(['state'])
        self.assertEqual(key.state, 'failed_reveal', 'recovery must preserve free-trial quarantine')
        self.assertEqual(set(client.disabled), {2010, 2020, 3000})
        self.assertTrue(obligation.scan_complete)
        self.assertIn(2, client.pages, 'the worker must traverse every page')
        self.assertGreaterEqual(client.pages.count(1), 2,
                                'the head page is rechecked after pending token IDs clear')

    def test_batch_disable_does_not_complete_an_unresolved_missing_token_id(self):
        Key = self.env['woow_paas_platform.llm_api_key']
        name = 'wp_unresolved_' + uuid.uuid4().hex[:12]
        key = _llm_internal(Key).create({
            'account_id': self.account_id, 'kind': 'paid',
            'client_request_id': 'batch-unresolved-' + uuid.uuid4().hex,
            'newapi_name': name, 'state': 'failed_reveal',
        })
        op = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='disable_all_keys', request_id='disable-unresolved-' + uuid.uuid4().hex,
            operator=self.env['res.users'].browse(self.operator_id),
            reason='keep unresolved create identity pending')
        self.addCleanup(self._cleanup_operation, op.id)
        obligation = op.key_obligation_ids.filtered(lambda row: row.key_id.id == key.id)

        class Client:
            def user_list_tokens(self, *args, **kwargs):
                return nac.Ok(data={'items': [], 'total': 0})

            def user_set_token_status(self, ip, up, token_id, enabled, **kwargs):
                return nac.Ok(data={})

        account_class = type(self.env[ACCOUNT])
        with patch.object(account_class, '_llm_tenant_ip', return_value='203.0.113.1'), \
                patch.object(account_class, '_llm_up_token', return_value='test-up'), \
                patch.object(account_class, '_llm_client', return_value=Client()):
            worker = self.env['woow_paas_platform.llm_backoffice_operation']
            actor = self.env['res.users'].browse(self.operator_id)
            for _ in range(200):
                if obligation.result_code == 'identity_unresolved':
                    break
                worker._process_disable_keys(self.env, op, actor)
        self.assertEqual(obligation.state, 'pending')
        self.assertEqual(obligation.result_code, 'identity_unresolved')
        self.assertEqual(op.state, 'pending')
        self.assertFalse(obligation.disabled_tokens_json)

    def test_daily_cap_serializes_concurrent_read_committed_requests(self):
        with self.registry.cursor() as cr:
            cr.execute("SELECT COALESCE(SUM(points), 0) FROM " + ORDER_TABLE +
                       " WHERE source = 'manual' AND state <> 'cancelled' "
                       "AND create_date >= date_trunc('day', NOW() AT TIME ZONE 'Asia/Taipei') "
                       "AT TIME ZONE 'Asia/Taipei' "
                       "AND create_date < (date_trunc('day', NOW() AT TIME ZONE 'Asia/Taipei') "
                       "+ interval '1 day') AT TIME ZONE 'Asia/Taipei'")
            already_used = cr.fetchone()[0]
        if already_used > 2800:
            self.skipTest('shared database has less than 200 points of daily cap headroom')
        initial = 2800 - already_used
        while initial:
            points = min(initial, 1000)
            self._service_call(uuid.uuid4().int % (2 ** 31 - 1) or 1, points)
            initial -= points
        barrier = threading.Barrier(2)

        def request():
            barrier.wait(timeout=10)
            try:
                return self._service_call(uuid.uuid4().int % (2 ** 31 - 1) or 1, 200)
            except UserError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _n: request(), range(2)))
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        with self.registry.cursor() as cr:
            cr.execute("SELECT SUM(points) FROM woow_paas_platform_llm_credit_order "
                       "WHERE source = 'manual' AND state <> 'cancelled' "
                       "AND create_date >= date_trunc('day', NOW() AT TIME ZONE 'Asia/Taipei') "
                       "AT TIME ZONE 'Asia/Taipei' "
                       "AND create_date < (date_trunc('day', NOW() AT TIME ZONE 'Asia/Taipei') "
                       "+ interval '1 day') AT TIME ZONE 'Asia/Taipei'")
            self.assertEqual(cr.fetchone()[0], 3000)
        with self.assertRaises(UserError):
            self._service_call(uuid.uuid4().int % (2 ** 31 - 1) or 1, 1,
                               'SENSITIVE reason must never appear in alerts')
        self.assertNotIn('SENSITIVE reason', repr(self.alert_mock.mock_calls))
        self.assertNotIn('SENSITIVE reason', repr(self.incident_mock.mock_calls))


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmBackofficeGuards(TransactionCase):

    def test_named_operator_rejects_public_and_builtin_actors(self):
        service = self.env[BACKOFFICE]
        ordinary = new_test_user(
            self.env, login='llm_ordinary_' + uuid.uuid4().hex, groups='base.group_user',
            email='llm-ordinary@example.com')
        for uid in (SUPERUSER_ID, self.env.ref('base.user_admin').id,
                    self.env.ref('base.user_root').id, self.env.ref('base.public_user').id,
                    ordinary.id):
            with self.subTest(uid=uid), self.assertRaises(AccessError):
                service.with_user(uid)._create_manual(1, 1, 'valid manual credit reason', 1)

    def test_named_human_flag_is_default_false_and_not_rpc_provisionable(self):
        from ..models.llm_backoffice import _named_system_operator
        system = new_test_user(
            self.env, login='llm_flag_system_' + uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='operator@example.test')
        shared = new_test_user(
            self.env, login='llm_flag_shared_' + uuid.uuid4().hex,
            groups='base.group_portal',
            email='shared@example.test')
        service = new_test_user(
            self.env, login='llm_flag_service_' + uuid.uuid4().hex,
            groups='base.group_system,base.group_user', email='service@example.test')
        self.assertFalse(system.llm_named_human_eligible)
        self.assertFalse(shared.llm_named_human_eligible)
        self.assertFalse(service.llm_named_human_eligible)
        self.assertTrue(shared.share)
        for actor in (system, shared, service):
            with self.subTest(user=actor.login), self.assertRaises(AccessError):
                _named_system_operator(actor.env(user=actor))
        # Even system+sudo and a caller-forged JSON-like context cannot use the
        # server-only object-identity capability to provision eligibility.
        for record in (system, system.sudo().with_context(woow_llm_named_human_provision=True)):
            with self.assertRaises(AccessError):
                record.write({'llm_named_human_eligible': True})
        with self.assertRaises(ValidationError):
            shared._llm_set_named_human_eligible(True)
        for xmlid in ('base.user_root', 'base.user_admin'):
            with self.subTest(xmlid=xmlid), self.assertRaises(ValidationError):
                self.env.ref(xmlid)._llm_set_named_human_eligible(True)
        with self.assertRaises(AccessError):
            self.env['res.users'].sudo().create({
                'name': 'RPC forged operator', 'login': 'forged_' + uuid.uuid4().hex,
                'llm_named_human_eligible': True,
            })
        with self.assertRaises(AccessError):
            self.env[ORDER].sudo().create({
                'account_id': 1, 'partner_id': 1, 'newapi_user_id': 1,
                'source': 'manual', 'points': 1, 'quota': 15625,
                'client_request_id': 'forged-' + uuid.uuid4().hex,
                'reason': 'forged manual ledger creation', 'requested_by_uid': system.id,
            })

    def test_named_human_eligibility_ignores_context_and_ir_default_on_create(self):
        Users = self.env['res.users'].sudo()
        system = new_test_user(
            self.env, login='llm_default_owner_' + uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='default-owner@example.test')
        context_user = Users.with_user(system).with_context(
            default_llm_named_human_eligible=True).create({
                'name': 'Context default must not provision',
                'login': 'llm_context_default_' + uuid.uuid4().hex,
            })
        self.assertFalse(context_user.llm_named_human_eligible)

        defaults = self.env['ir.default'].sudo()
        defaults.set('res.users', 'llm_named_human_eligible', True, user_id=system.id)
        try:
            ir_default_user = Users.with_user(system).create({
                'name': 'ir.default must not provision',
                'login': 'llm_ir_default_' + uuid.uuid4().hex,
            })
            self.assertFalse(ir_default_user.llm_named_human_eligible)
            ordinary = Users.with_user(system).create({
                'name': 'Ordinary user control',
                'login': 'llm_ordinary_control_' + uuid.uuid4().hex,
            })
            self.assertFalse(ordinary.llm_named_human_eligible)
        finally:
            defaults.set('res.users', 'llm_named_human_eligible', False, user_id=system.id)
        self.assertFalse(system.llm_named_human_eligible)

    def test_wizard_public_action_and_direct_ledger_mutations_are_guarded(self):
        Wizard = self.env['woow_paas_platform.llm_manual_credit_wizard']
        partner = self.env['res.partner'].create({'name': 'LLM RPC mutation fixture'})
        account = _llm_internal(self.env[ACCOUNT]).create({'partner_id': partner.id})
        _llm_internal(account).write({'state': 'active', 'newapi_user_id': 4811023})
        wizard = Wizard.sudo().create({
            'account_id': account.id, 'points': 1, 'reason': 'valid wizard reason text',
        })
        for uid in (SUPERUSER_ID, self.env.ref('base.user_admin').id,
                    self.env.ref('base.public_user').id):
            with self.subTest(uid=uid), self.assertRaises(AccessError):
                wizard.with_user(uid).action_create()
        order = _credit_internal(self.env[ORDER].sudo()).create({
            'account_id': account.id, 'partner_id': partner.id,
            'newapi_user_id': account.newapi_user_id, 'source': 'user', 'points': 10,
            'quota': 156250, 'client_request_id': 'rpc-' + uuid.uuid4().hex,
        })
        with self.assertRaises(AccessError):
            order.sudo().write({'points': order.points + 1})
        with self.assertRaises(AccessError):
            self.env[ORDER].sudo().create({})
        operations = self.env['woow_paas_platform.llm_backoffice_operation'].sudo()
        with self.assertRaises(AccessError):
            operations.create({'request_id': 'rpc-' + uuid.uuid4().hex,
                               'fingerprint': 'x', 'operation': 'review',
                               'operator_id': SUPERUSER_ID})
        with self.assertRaises(AccessError):
            operations.browse(1).write({'state': 'done'})
        accountant = new_test_user(
            self.env, login='llm_accountant_' + uuid.uuid4().hex,
            groups='account.group_account_manager')
        with self.assertRaises(AccessError):
            order.with_user(accountant).read(['reason'])

    def test_manual_creation_fails_closed_when_database_is_unconfigured(self):
        from ..models import llm_backoffice
        operator = new_test_user(
            self.env, login='llm_unconfigured_' + uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='llm-unconfigured@example.com')
        operator._llm_set_named_human_eligible(True)
        service = self.env[BACKOFFICE].with_user(operator)
        with patch.object(llm_backoffice, 'llm_unconfigured', return_value=True), \
                patch.object(type(self.env['woow_paas_platform.llm_runtime_state']),
                             '_llm_read', return_value={'guard_tripped': False}):
            with self.assertRaisesRegex(UserError, 'not configured'):
                service._create_manual(1, 1, 'valid manual credit reason', 22)

    def test_review_requires_original_snapshot_and_never_accepts_missing_sample(self):
        from ..models import llm_backoffice
        operator = new_test_user(
            self.env, login='llm_review_' + uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='llm-review@example.com')
        operator._llm_set_named_human_eligible(True)
        partner = self.env['res.partner'].create({'name': 'LLM review evidence fixture'})
        account = _llm_internal(self.env[ACCOUNT]).create({'partner_id': partner.id})
        _llm_internal(account).write({'state': 'active', 'newapi_user_id': 4811021,
                                      'newapi_username': 'wqa1234567890'})
        order = _credit_internal(self.env[ORDER].sudo()).create({
            'account_id': account.id, 'partner_id': partner.id,
            'newapi_user_id': account.newapi_user_id, 'source': 'user', 'points': 10,
            'quota': 156250, 'client_request_id': 'review-' + uuid.uuid4().hex,
            'newapi_request_id': 'upstream-review-' + uuid.uuid4().hex,
        })
        from odoo import fields
        stamp = fields.Datetime.now()
        _credit_internal(order)._transition(
            'sending', sent_at=stamp, quota_before=1000, used_quota_before=100,
            request_count_before=4, before_snapshot_present=True, before_sampled_at=stamp)
        _credit_internal(order)._transition('needs_review')

        class Client:
            def ap_log(self, *args, **kwargs):
                return nac.Ok(data={'page': 1, 'page_size': 100,
                                    'total': 1, 'items': [{
                    'type': 1, 'user_id': 4811021,
                    'created_at': int(fields.Datetime.to_datetime(stamp).timestamp()),
                    'request_id': order.newapi_request_id,
                    'other': {'op': {'action': 'user.quota_add', 'params': {
                        'mode': 'add', 'target_user_id': 4811021,
                        'requested_quota': 156250, 'from': 1000, 'to': 157250}},
                        'admin_info': {'admin_id': 12}},
                }]})

            def ap_get_user(self, *args, **kwargs):
                return nac.Ok(data={'id': 4811021, 'quota': 157250,
                                    'used_quota': 100, 'request_count': 4})

        with patch.object(type(account), '_llm_client', return_value=Client()), \
                patch.object(llm_backoffice, 'llm_unconfigured', return_value=False), \
                patch.object(llm_backoffice, 'llm_config', return_value={
                    'service_admin_id': 12, 'orders_enabled': True}), \
                patch.object(type(self.env['woow_paas_platform.llm_runtime_state']),
                             '_llm_read', return_value={'guard_tripped': False}):
            with self.assertRaisesRegex(UserError, 'Matching upstream credit evidence'):
                order.with_user(operator)._admin_review('retry', 'reviewed upstream evidence')
        self.assertEqual(order.state, 'needs_review')

    def test_monthly_report_cron_is_noop_when_runtime_is_unconfigured(self):
        from ..models import llm_common
        with patch.object(llm_common, 'llm_unconfigured', return_value=True):
            before = self.env['mail.mail'].search_count([])
            self.env['woow_paas_platform.llm_manual_credit_cron']._cron_manual_credit_monthly_report()
            self.assertEqual(self.env['mail.mail'].search_count([]), before)

    def test_monthly_report_is_noop_without_valid_configured_recipients(self):
        from ..models import llm_backoffice
        cursor_key = 'monthly-no-recipient-' + uuid.uuid4().hex
        before_mail = self.env['mail.mail'].sudo().search_count([])
        before_periods = self.env[
            'woow_paas_platform.llm_manual_credit_report'].sudo().search_count([])
        cursor_id = None
        with patch.object(llm_backoffice, 'llm_unconfigured', return_value=False), \
                patch.object(llm_backoffice, 'alert_targets', return_value={
                    'emails': (), 'webhook': None}):
            self.assertFalse(self.env[
                'woow_paas_platform.llm_manual_credit_cron']._queue_monthly_report(
                    cursor_key=cursor_key, initial_period='2099-01'))
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), before_mail)
        self.assertEqual(self.env[
            'woow_paas_platform.llm_manual_credit_report'].sudo().search_count([]), before_periods)
        with self.registry.cursor() as cr:
            cr.execute('SELECT id, next_period FROM '
                       'woow_paas_platform_llm_manual_credit_report_cursor '
                       'WHERE cursor_key = %s', (cursor_key,))
            row = cr.fetchone()
            self.assertTrue(row, 'missing recipients must preserve a durable retry cursor')
            cursor_id = row[0]
            self.assertEqual(row[1], '2099-01')
            cr.execute('SELECT count(*) FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', ('2099-01',))
            self.assertEqual(cr.fetchone()[0], 0, 'no success receipt is written without recipients')
            cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report_cursor '
                       'WHERE id = %s AND cursor_key = %s', (cursor_id, cursor_key))
