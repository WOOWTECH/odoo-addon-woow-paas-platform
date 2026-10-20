"""Safety contracts for LLM-12a backoffice actions."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.llm_common import _llm_internal
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
        with self.env.cr.savepoint():
            self.env.cr.execute('UPDATE woow_paas_platform_llm_event '
                                'SET target_model = %s, target_id = %s WHERE id = %s',
                                (ORDER, order.id, reservation_id))

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
        self.env.cr.execute('UPDATE woow_paas_platform_llm_event '
                            'SET target_model = %s, target_id = %s WHERE id = %s',
                            (ORDER, order.id, reservation_id))

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
                             '_llm_insert_event_cr', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with self.env.cr.savepoint():
                    order.with_user(self.operator_id)._admin_review(
                        'retry', 'upstream checked no matching log',
                        operator_id=self.operator_id)
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
                'state': 'needs_review',
            })
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
        reason = "<script>alert('not markup')</script> approved manual correction"
        created = self._service_call(
            uuid.uuid4().int % (2 ** 31 - 1) or 1, points=13, reason=reason)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        month_start = now_tw.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_start = (month_start - timedelta(days=1)).replace(day=1)
        prior_month_stamp = previous_start.astimezone(timezone.utc).replace(tzinfo=None)
        period_id = previous_start.strftime('%Y-%m')

        def cleanup_report():
            with self.registry.cursor() as cr:
                cr.execute('SELECT mail_ref FROM woow_paas_platform_llm_manual_credit_report '
                           'WHERE period_id = %s', (period_id,))
                report = cr.fetchone()
                cr.execute('DELETE FROM woow_paas_platform_llm_manual_credit_report '
                           'WHERE period_id = %s', (period_id,))
                if report:
                    cr.execute('DELETE FROM mail_mail WHERE id = %s', (report[0],))

        self.addCleanup(cleanup_report)
        with self.registry.cursor() as cr:
            cr.execute('UPDATE ' + ORDER_TABLE + ' SET create_date = %s WHERE id = %s',
                       (prior_month_stamp, created['id']))
        with patch.object(llm_backoffice, 'alert_targets', return_value={
                'emails': ('authorized-reports@example.test',), 'webhook': None}):
            cron = self.env['woow_paas_platform.llm_manual_credit_cron']
            self.assertTrue(cron._queue_monthly_report())
            self.assertFalse(cron._queue_monthly_report())
        with self.registry.cursor() as cr:
            cr.execute('SELECT period_id, start_utc, end_utc, order_count, points_total, mail_ref '
                       'FROM woow_paas_platform_llm_manual_credit_report '
                       'WHERE period_id = %s', (period_id,))
            row = cr.fetchone()
            self.assertTrue(row)
            self.assertEqual(row[1], previous_start.astimezone(timezone.utc).replace(tzinfo=None))
            self.assertEqual(row[2], month_start.astimezone(timezone.utc).replace(tzinfo=None))
            cr.execute('SELECT email_to, body_html, auto_delete FROM mail_mail WHERE id = %s',
                       (row[5],))
            email_to, body_html, auto_delete = cr.fetchone()
            self.assertEqual(email_to, 'authorized-reports@example.test')
            self.assertIn('&lt;script&gt;', body_html)
            self.assertNotIn('<script>', body_html)
            self.assertTrue(auto_delete)

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

    def test_batch_disable_is_queued_bounded_and_uses_cron_client(self):
        from ..models import llm_backoffice_operation
        Key = self.env['woow_paas_platform.llm_api_key']
        keys = _llm_internal(Key).create([{
            'account_id': self.account_id, 'kind': 'paid',
            'client_request_id': 'batch-' + uuid.uuid4().hex,
            'newapi_token_id': index, 'state': 'active',
        } for index in range(11)])
        op = self.env['woow_paas_platform.llm_backoffice_operation']._enqueue(
            operation='disable_all_keys', request_id='disable-test-' + uuid.uuid4().hex,
            operator=self.env['res.users'].browse(self.operator_id),
            reason='disable the mirrored account keys for a safety test')

        class Client:
            def __init__(self):
                self.calls = []
                self.failed_once = False

            def user_set_token_status(self, ip, up, token_id, enabled, **kwargs):
                self.calls.append((token_id, enabled, kwargs.get('timeout')))
                if not self.failed_once:
                    self.failed_once = True
                    return nac.Definite(status=503, code='TEMPORARY_TEST_FAILURE')
                return nac.Ok(data={})

        client = Client()
        account_class = type(self.env[ACCOUNT])
        with patch.object(account_class, '_llm_tenant_ip', return_value='203.0.113.1'), \
                patch.object(account_class, '_llm_up_token', return_value='test-up'), \
                patch.object(account_class, '_llm_client', return_value=client):
            worker = self.env['woow_paas_platform.llm_backoffice_operation']
            worker._process_disable_keys(self.env, op, self.env['res.users'].browse(self.operator_id))
            self.assertEqual(len(client.calls), 10)
            self.assertEqual(op.state, 'pending')
            self.assertEqual(op.key_failed_count, 1)
            self.assertTrue(op.key_retry_ids_json)
            worker._process_disable_keys(self.env, op, self.env['res.users'].browse(self.operator_id))
        self.assertEqual(len(client.calls), 12)
        self.assertTrue(all(call[1] is False for call in client.calls))
        self.assertTrue(all(call[2] == nac.TIMEOUT_CRON_WRITE for call in client.calls))
        self.assertEqual(op.state, 'done')
        self.assertEqual(op.key_failed_count, 0)
        self.assertTrue(all(key.state == 'disabled' for key in keys))

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
        before_mail = self.env['mail.mail'].sudo().search_count([])
        before_periods = self.env[
            'woow_paas_platform.llm_manual_credit_report'].sudo().search_count([])
        with patch.object(llm_backoffice, 'llm_unconfigured', return_value=False), \
                patch.object(llm_backoffice, 'alert_targets', return_value={
                    'emails': (), 'webhook': None}):
            self.assertFalse(self.env[
                'woow_paas_platform.llm_manual_credit_cron']._queue_monthly_report())
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), before_mail)
        self.assertEqual(self.env[
            'woow_paas_platform.llm_manual_credit_report'].sudo().search_count([]), before_periods)
