"""Real ORM/PG queues; every fixture stays in an owned rollback transaction."""
import os
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from threading import RLock
from unittest.mock import patch

from odoo import fields
from odoo.sql_db import TestCursor
from odoo.tests import TransactionCase, tagged

from ..models import llm_event
from ..models.llm_common import ERR_LLM_INTERNAL, run_llm_cron
from ..models.llm_event import _env_on
from ..services.llm_sql_budget import deadline_cursor, trigger_cron


@tagged('post_install', '-at_install', 'llm_foundation')
class TestLlmBoundedCron(TransactionCase):

    @contextmanager
    def _rollback_env(self, *, bounded=False):
        deadline = time.monotonic() + 15 if bounded else None
        with deadline_cursor(self.registry._db.cursor, deadline) as cr:
            try:
                yield _env_on(self.registry, cr)
            finally:
                # Also discard mail and cron callbacks before the context exits.
                cr.rollback()

    def _new_cron(self, env, active):
        return env['ir.cron'].create({
            'name': 'LLM bounded cron test ' + uuid.uuid4().hex,
            'model_id': env['ir.model']._get_id('ir.cron'),
            'state': 'code',
            'code': 'pass',
            'active': active,
            'interval_number': 1,
            'interval_type': 'days',
            'nextcall': fields.Datetime.now() + timedelta(days=1),
        })

    def _no_notify(self):
        return patch.object(
            type(self.env['ir.cron']), '_notifydb',
            side_effect=AssertionError('unexpected_cron_notify'),
        )

    def test_bounded_trigger_preserves_active_and_future_semantics(self):
        with self._no_notify() as notify, self._rollback_env(bounded=True) as env:
            active = self._new_cron(env, True)
            inactive = self._new_cron(env, False)
            env.flush_all()
            # Cron creation itself may register callbacks via the environment
            # flag; only callbacks added by the actual trigger calls are tested.
            env.cr.postcommit.clear()
            now = fields.Datetime.now()
            future = now + timedelta(days=1)

            immediate = trigger_cron(active)
            self.assertEqual(immediate.cron_id, active)
            self.assertGreaterEqual(immediate.call_at, now)
            self.assertLessEqual(immediate.call_at, fields.Datetime.now())
            self.assertFalse(trigger_cron(inactive))
            self.assertFalse(trigger_cron(inactive, at=now - timedelta(seconds=1)))
            for cron in (active, inactive):
                future_trigger = trigger_cron(cron, at=future)
                self.assertEqual(future_trigger.cron_id, cron)
                self.assertEqual(future_trigger.call_at, future)
            env.flush_all()
            self.assertEqual(env['ir.cron.trigger'].search_count([
                ('cron_id', 'in', (active.id, inactive.id)),
            ]), 3)
            env.cr.postcommit.run()
            notify.assert_not_called()

    def test_run_llm_cron_only_queues_continuation_with_remaining_budget(self):
        # Configuration is passed as a value; no settings or cron flags are
        # committed. outbound=False also keeps the actual framework off-network.
        config = {'proxy_state': 'usable', 'show': True, 'pilot': False, 'canary': False}
        xmlid = 'woow_paas_platform.ir_cron_llm_event_cleanup'
        with self._rollback_env() as env, self._no_notify() as notify:
            cron = env.ref(xmlid)
            lock = RLock()
            opened = []

            def cursor_factory():
                child = TestCursor(env.cr, lock, readonly=False)
                opened.append(child)
                native_commit = child.commit

                def commit_with_callbacks():
                    # Odoo TestCursor.commit normally discards these hooks.
                    # Exercise them so an accidental native _trigger cannot
                    # appear safe merely because the test shim hid _notifydb.
                    child.flush()
                    child.postcommit.run()
                    return native_commit()

                child.commit = commit_with_callbacks
                return child

            def trigger_rows():
                env.cr.execute(
                    'SELECT id, call_at FROM ir_cron_trigger WHERE cron_id = %s',
                    (cron.id,),
                )
                return dict(env.cr.fetchall())

            before = trigger_rows()
            expired_ticks = []

            def expired_work(_config, tick):
                expired_ticks.append(tick)
                tick.delay = 60
                time.sleep(max(0, tick.remaining()) + 0.02)
                return True

            def more_work(_config, tick):
                tick.delay = 60
                tick.more = True
                return False

            # Future native triggers only notify when this process-local flag
            # is set. Force that path to expose accidental native delegation.
            with patch.dict(os.environ, {'ODOO_NOTIFY_CRON_CHANGES': '1'}), \
                    patch.object(self.registry, 'cursor', new=cursor_factory):
                self.assertTrue(run_llm_cron(
                    env, xmlid, expired_work, seconds=0.08, items=1,
                    config=config, outbound=False,
                ))
                self.assertEqual(len(expired_ticks), 1)
                self.assertLess(expired_ticks[0].remaining(), 0)
                self.assertFalse(opened, 'expired continuation opened a cursor')
                self.assertEqual(trigger_rows(), before)

                earliest = fields.Datetime.now() + timedelta(seconds=60)
                self.assertTrue(run_llm_cron(
                    env, xmlid, more_work, seconds=5, items=1,
                    config=config, outbound=False,
                ))
                self.assertEqual(len(opened), 1)
                added = trigger_rows().keys() - before.keys()
                self.assertEqual(len(added), 1)
                call_at = trigger_rows()[added.pop()]
                self.assertGreaterEqual(call_at, earliest)
                self.assertLessEqual(
                    call_at, fields.Datetime.now() + timedelta(seconds=60),
                )
                notify.assert_not_called()
            # Child commit only released a savepoint; _rollback_env removes
            # the continuation as well as every other fixture in this test.

    def test_alert_email_and_webhook_use_bounded_trigger_queue(self):
        with self._no_notify() as notify, self._rollback_env(bounded=True) as env:
            mail_cron = env.ref(llm_event.MAIL_CRON_XMLID)
            delivery_cron = env.ref(llm_event.DELIVERY_CRON_XMLID)
            # These changes exist only on this raw cursor and are rolled back.
            (mail_cron | delivery_cron).write({'active': True})
            env.flush_all()
            env.cr.postcommit.clear()
            Trigger = env['ir.cron.trigger']
            before = {
                cron.id: set(Trigger.search([('cron_id', '=', cron.id)]).ids)
                for cron in (mail_cron, delivery_cron)
            }
            service = env['woow_paas_platform.llm_event']
            recipient = 'llm-budget-%s@example.invalid' % uuid.uuid4().hex
            payload = {
                'alert': llm_event.LlmAlert.TEST.value,
                'event_id': 1,
                'database': env.cr.dbname,
                'at': fields.Datetime.now().isoformat(),
                'account_id': None,
                'target_model': None,
                'target_id': None,
                'error_code': ERR_LLM_INTERNAL,
                'numbers': {},
            }
            result = service._llm_alert_queue_email(
                env.cr, (recipient,), payload, 0,
            )
            self.assertEqual(result['email_state'], 'queued')
            mail = env['mail.mail'].browse(result['mail_id']).exists()
            self.assertTrue(mail)
            self.assertEqual(mail.email_to, recipient)
            self.assertEqual(mail.state, 'outgoing')
            self.assertEqual(service._llm_alert_queue_webhook(
                env.cr, 'https://example.invalid/llm-budget',
            ), 'pending')
            env.flush_all()
            for cron in (mail_cron, delivery_cron):
                added = set(Trigger.search([('cron_id', '=', cron.id)]).ids) - before[cron.id]
                self.assertEqual(len(added), 1)
            env.cr.postcommit.run()
            notify.assert_not_called()
