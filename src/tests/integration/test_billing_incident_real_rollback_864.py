"""#864 PR-D 真跨連線交易探針。

`TransactionCase` 的 cursor 會用 savepoint 模擬 commit/rollback，無法證明
incident 活過整筆 request rollback。本檔沿 #794 的 BaseCase 形狀，使用真
registry cursors、真 PostgreSQL 40001、第三條連線驗終態。

執行：

    odoo -u woow_paas_platform --test-enable \
      --test-tags billing_incident_864 --stop-after-init ...

本 tag 刻意 `-standard`；PR-D 不改 CI workflow，須在 sandbox 明確選 tag。
"""

import logging
import os
import sys
import threading
import time
import traceback
import uuid
from unittest.mock import patch

import odoo
from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure

from odoo import api, fields
from odoo.exceptions import AccessError
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged


_logger = logging.getLogger(__name__)
_MODEL = 'woow.billing.incident'
_TABLE = 'woow_billing_incident'


@tagged(
    'post_install', '-at_install', '-standard', 'billing_incident_864',
)
class TestBillingIncidentRealTransactions864(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        run_uuid = os.environ.get('BILLING_INCIDENT_TEST_RUN_UUID')
        if run_uuid:
            try:
                run_uuid = uuid.UUID(run_uuid).hex
            except ValueError:
                self.fail('BILLING_INCIDENT_TEST_RUN_UUID must be a UUID')
            self.marker = 'b864%s-%s' % (
                run_uuid, uuid.uuid4().hex[:8])
        else:
            self.marker = 'b864%s' % uuid.uuid4().hex
        self.addCleanup(self._purge)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            conflict = env['res.partner'].create({
                'name': '%s-conflict' % self.marker,
            })
            control = env['res.partner'].create({
                'name': '%s-control-before' % self.marker,
            })
            invoice_partner = env['res.partner'].create({
                'name': '%s-invoice-partner' % self.marker,
            })
            move = env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': invoice_partner.id,
                'invoice_date': fields.Date.today(),
                'ref': self.marker,
            })
            self.conflict_partner_id = conflict.id
            self.control_partner_id = control.id
            self.invoice_partner_id = invoice_partner.id
            manager = env.ref('account.group_account_manager').users.filtered(
                lambda user: user.id != odoo.SUPERUSER_ID
            )[:1]
            self.assertTrue(
                manager,
                'integration 前置：至少一位非 superuser account manager',
            )
            self.manager_user_id = manager.id
            self.move_id = move.id

    def _purge(self):
        """只刪本 test UUID marker 建出的資料。"""
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                external_prefix = 'test:%s%%' % self.marker
                cr.execute(
                    "DELETE FROM mail_activity WHERE res_model = %s AND res_id IN "
                    "(SELECT id FROM woow_billing_incident "
                    " WHERE external_ref LIKE %s)",
                    (_MODEL, external_prefix),
                )
                for table, model_column in (
                        ('mail_followers', 'res_model'),
                        ('mail_message', 'model')):
                    cr.execute(
                        'DELETE FROM %s WHERE %s = %%s AND res_id IN '
                        '(SELECT id FROM woow_billing_incident '
                        ' WHERE external_ref LIKE %%s)'
                        % (table, model_column),
                        (_MODEL, external_prefix),
                    )
                # Creating/removing roster users converges every unresolved
                # incident, including unrelated rows already in this test DB.
                # Odoo's assignment notification body names the marker user,
                # so purge those marker-owned side effects even when res_id
                # belongs to an older incident fixture.
                cr.execute(
                    'DELETE FROM mail_message WHERE model = %s '
                    'AND (COALESCE(subject, \'\') LIKE %s '
                    'OR COALESCE(body, \'\') LIKE %s)',
                    (_MODEL, '%' + self.marker + '%',
                     '%' + self.marker + '%'),
                )
                cr.execute(
                    'DELETE FROM ir_attachment WHERE name LIKE %s',
                    ('%' + self.marker + '%',),
                )
                cr.execute(
                    'DELETE FROM mail_activity WHERE COALESCE(summary, \'\') '
                    'LIKE %s',
                    ('%' + self.marker + '%',),
                )
                cr.execute(
                    'DELETE FROM mail_message WHERE COALESCE(subject, \'\') '
                    'LIKE %s OR COALESCE(body, \'\') LIKE %s',
                    ('%' + self.marker + '%', '%' + self.marker + '%'),
                )
                cr.execute(
                    'DELETE FROM woow_billing_incident '
                    'WHERE external_ref LIKE %s',
                    (external_prefix,),
                )
                env['account.move'].sudo().search([
                    ('ref', '=', self.marker),
                ]).unlink()
                env['res.users'].sudo().search([
                    ('login', 'like', self.marker + '-roster-%'),
                ]).unlink()
                env['res.partner'].sudo().search([
                    ('name', 'like', self.marker),
                ]).unlink()
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 billing incident integration fixture cleanup failed '
                '(marker=%s)', self.marker,
            )

    def _snapshot(self, suffix, **overrides):
        values = {
            'external_ref': 'test:%s-%s' % (self.marker, suffix),
            'kind': 'charged_not_credited',
            'partner_ref': self.invoice_partner_id,
            'partner_email': '%s@example.test' % self.marker,
            'payment_token_ref': 818,
            'amount': 600.0,
            'credits': 600.0,
            'bonus': 60.0,
            'currency': 'TWD',
            'trade_no': '%s-trade' % self.marker,
            'rtn_code': '1',
            'failure_code': 'SERIALIZATION_FAILURE',
            'source_model': 'account.move',
            'source_ref': self.move_id,
            'detail': 'frozen before the failure region',
        }
        values.update(overrides)
        return values

    def _call_log(self, snapshot):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return env[_MODEL]._log(snapshot)

    def _call_resolve(self, external_ref, note):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return env[_MODEL]._resolve(
                external_ref,
                resolution_note=note,
                resolved_by_ref=odoo.SUPERUSER_ID,
            )

    def _read_incident(self, external_ref):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id, state, occurrences, detail, resolution_note '
                'FROM %s WHERE external_ref = %%s' % _TABLE,
                (external_ref,),
            )
            return cr.fetchone()

    def _partner_name(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute('SELECT name FROM res_partner WHERE id = %s',
                       (partner_id,))
            row = cr.fetchone()
            return row and row[0]

    def _set_incident_state(self, incident_id, state):
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_billing_incident SET state = %s WHERE id = %s',
                (state, incident_id),
            )
            cr.commit()

    def _review_activity_count(self, incident_id):
        return sum(self._review_activity_counts_by_user(incident_id).values())

    def _review_activity_counts_by_user(self, incident_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            cr.execute(
                'SELECT user_id, COUNT(*) FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s AND active = true '
                'GROUP BY user_id ORDER BY user_id',
                (_MODEL, incident_id, review_type.id),
            )
            return dict(cr.fetchall())

    def _expected_review_activity_counts_by_user(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            users = env[_MODEL]._account_manager_users(env)
            return {user_id: 1 for user_id in users.ids}

    def test_real_40001_outer_rollback_keeps_incident_but_loses_control_write(self):
        snapshot = self._snapshot('real-40001')
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            # Freeze transaction A's REPEATABLE READ snapshot, then make an
            # unrelated same-transaction control write which must disappear.
            cr.execute('SELECT name FROM res_partner WHERE id = %s',
                       (self.conflict_partner_id,))
            self.assertTrue(cr.fetchone())
            cr.execute(
                'UPDATE res_partner SET name = %s WHERE id = %s',
                ('%s-control-after' % self.marker, self.control_partner_id),
            )

            # Transaction B commits a concurrent update after A's snapshot.
            with self.registry.cursor() as concurrent:
                concurrent.execute(
                    'UPDATE res_partner SET name = %s WHERE id = %s',
                    ('%s-conflict-b' % self.marker,
                     self.conflict_partner_id),
                )

            try:
                cr.execute(
                    'UPDATE res_partner SET name = %s WHERE id = %s',
                    ('%s-conflict-a' % self.marker,
                     self.conflict_partner_id),
                )
            except SerializationFailure as exc:
                self.assertEqual(
                    exc.pgcode, errorcodes.SERIALIZATION_FAILURE,
                    'must be a real PostgreSQL 40001, not a stand-in ValueError',
                )
            else:
                self.fail('concurrent RR update must raise real 40001')

            # Outer SQL transaction is aborted here.  Only the pre-frozen dict
            # may cross into `_log`; no ORM read from partner/invoice is legal.
            incident_id = env[_MODEL]._log(snapshot)
            self.assertIsInstance(incident_id, int)
            cr.rollback()
        finally:
            cr.close()

        incident = self._read_incident(snapshot['external_ref'])
        self.assertTrue(incident, 'third connection must see durable incident')
        self.assertEqual(incident[1], 'open')
        self.assertEqual(incident[2], 1)
        self.assertEqual(
            self._partner_name(self.control_partner_id),
            '%s-control-before' % self.marker,
            'same-transaction control write must disappear on outer rollback',
        )

    def test_outer_rollback_leaves_durable_dangling_scalar_source_ref(self):
        snapshot = self._snapshot(
            'dangling-source-ref',
            source_model='res.partner',
        )
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            source = env['res.partner'].create({
                'name': '%s-dangling-source' % self.marker,
            })
            snapshot['source_ref'] = source.id
            incident_id = env[_MODEL]._log(snapshot)
            self.assertIsInstance(incident_id, int)
            cr.rollback()
        finally:
            cr.close()

        with self.registry.cursor() as verify_cr:
            verify_cr.execute(
                'SELECT 1 FROM res_partner WHERE id = %s',
                (snapshot['source_ref'],),
            )
            self.assertFalse(
                verify_cr.fetchone(),
                'outer transaction source row must disappear after real rollback',
            )
            verify_cr.execute(
                'SELECT id, source_model, source_ref FROM '
                'woow_billing_incident WHERE external_ref = %s',
                (snapshot['external_ref'],),
            )
            row = verify_cr.fetchone()
        self.assertEqual(
            row,
            (incident_id, 'res.partner', snapshot['source_ref']),
            'durable incident must retain only the deliberately dangling scalar',
        )

    def test_duplicate_log_atomically_increments_occurrences(self):
        first = self._snapshot('duplicate', detail='first')
        second = self._snapshot('duplicate', detail='second')
        first_id = self._call_log(first)
        second_id = self._call_log(second)

        self.assertEqual(second_id, first_id)
        incident = self._read_incident(first['external_ref'])
        self.assertEqual(incident[2], 2)
        self.assertEqual(incident[3], 'second')

    def test_resolve_survives_outer_rollback_while_control_write_disappears(self):
        snapshot = self._snapshot('resolve')
        incident_id = self._call_log(snapshot)

        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            cr.execute(
                'UPDATE res_partner SET name = %s WHERE id = %s',
                ('%s-control-after-resolve' % self.marker,
                 self.control_partner_id),
            )
            resolved_id = env[_MODEL]._resolve(
                snapshot['external_ref'],
                resolution_note='independent resolve committed',
                resolved_by_ref=odoo.SUPERUSER_ID,
            )
            self.assertEqual(resolved_id, incident_id)
            cr.rollback()
        finally:
            cr.close()

        incident = self._read_incident(snapshot['external_ref'])
        self.assertEqual(incident[1], 'resolved')
        self.assertEqual(incident[4], 'independent resolve committed')
        self.assertEqual(
            self._partner_name(self.control_partner_id),
            '%s-control-before' % self.marker,
        )

    def test_two_cursor_late_manager_write_cannot_overwrite_resolution_audit(self):
        snapshot = self._snapshot('resolved-manager-write-race')
        incident_id = self._call_log(snapshot)
        manager_cr = self.registry.cursor()
        try:
            manager_env = api.Environment(manager_cr, self.manager_user_id, {})
            manager_record = manager_env[_MODEL].browse(incident_id)
            # A real SQL read fixes cursor A's REPEATABLE READ snapshot.  A
            # bare browse() would not, and would leave this race untested.
            manager_cr.execute(
                'SELECT state FROM woow_billing_incident WHERE id = %s',
                (incident_id,),
            )
            self.assertEqual(manager_cr.fetchone()[0], 'open')

            # Cursor B resolves and commits after cursor A's snapshot is fixed.
            # A's subsequent SELECT FOR UPDATE must fail with a real 40001,
            # never overwrite the first committed audit.
            self.assertEqual(
                self._call_resolve(
                    snapshot['external_ref'],
                    'first committed race audit',
                ),
                incident_id,
            )
            try:
                manager_record.write({
                    'manual_check_result': 'ecpay_not_found',
                    'resolution_note': 'late cursor must be rejected',
                })
            except SerializationFailure as exc:
                self.assertEqual(
                    exc.pgcode, errorcodes.SERIALIZATION_FAILURE,
                    'stale RR ledger write must fail as real PostgreSQL 40001',
                )
                manager_cr.rollback()
            else:
                self.fail(
                    'late manager write unexpectedly overwrote a resolved '
                    'incident from another cursor'
                )
        finally:
            manager_cr.close()

        incident = self._read_incident(snapshot['external_ref'])
        self.assertEqual(incident[1], 'resolved')
        self.assertEqual(incident[4], 'first committed race audit')

    def test_resolve_and_manager_action_orderings_preserve_first_audit(self):
        resolve_first = self._snapshot(
            'resolve-before-manager-action',
            state='needs_human',
        )
        resolve_first_id = self._call_log(resolve_first)
        manager_cr = self.registry.cursor()
        try:
            manager_env = api.Environment(
                manager_cr, self.manager_user_id, {})
            stale_action_record = manager_env[_MODEL].browse(resolve_first_id)
            manager_cr.execute(
                'SELECT state FROM woow_billing_incident WHERE id = %s',
                (resolve_first_id,),
            )
            self.assertEqual(manager_cr.fetchone()[0], 'needs_human')
            self.assertEqual(
                self._call_resolve(
                    resolve_first['external_ref'],
                    'resolver won the first transition',
                ),
                resolve_first_id,
            )
            try:
                stale_action_record.action_mark_manually_resolved()
            except SerializationFailure as exc:
                self.assertEqual(
                    exc.pgcode, errorcodes.SERIALIZATION_FAILURE,
                    'stale RR manager action must fail as real 40001',
                )
            else:
                self.fail('stale RR manager action unexpectedly resolved twice')
            manager_cr.rollback()
        finally:
            manager_cr.close()
        resolved = self._read_incident(resolve_first['external_ref'])
        self.assertEqual(resolved[1], 'resolved')
        self.assertEqual(resolved[4], 'resolver won the first transition')

        action_first = self._snapshot(
            'manager-action-before-resolve',
            state='needs_human',
        )
        action_first_id = self._call_log(action_first)
        with self.registry.cursor() as cr:
            manager_env = api.Environment(cr, self.manager_user_id, {})
            manager_record = manager_env[_MODEL].browse(action_first_id)
            manager_record.write({
                'manual_check_result': 'ecpay_found',
                'resolution_note': 'manager action won the first transition',
            })
            self.assertTrue(manager_record.action_mark_manually_resolved())
            cr.commit()
        self.assertEqual(
            self._call_resolve(
                action_first['external_ref'],
                'late resolver must not overwrite first audit',
            ),
            action_first_id,
        )
        resolved = self._read_incident(action_first['external_ref'])
        self.assertEqual(resolved[1], 'resolved')
        self.assertEqual(
            resolved[4], 'manager action won the first transition')

    def test_outer_partner_and_invoice_locks_do_not_block_log(self):
        snapshot = self._snapshot('unrelated-locks')
        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            cr.execute('SELECT id FROM res_partner WHERE id = %s FOR UPDATE',
                       (self.invoice_partner_id,))
            self.assertTrue(cr.fetchone())
            cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE',
                       (self.move_id,))
            self.assertTrue(cr.fetchone())

            started = time.monotonic()
            incident_id = env[_MODEL]._log(snapshot)
            elapsed = time.monotonic() - started
            self.assertIsInstance(incident_id, int)
            self.assertLess(
                elapsed, 0.9,
                'helper must not browse/update locked outer partner/invoice',
            )
            cr.rollback()
        finally:
            cr.close()
        self.assertTrue(self._read_incident(snapshot['external_ref']))

    def test_same_incident_lock_times_out_fail_soft_without_hanging(self):
        snapshot = self._snapshot('same-row-lock')
        incident_id = self._call_log(snapshot)
        self.assertIsInstance(incident_id, int)

        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            cr.execute(
                'SELECT id FROM woow_billing_incident '
                'WHERE external_ref = %s FOR UPDATE',
                (snapshot['external_ref'],),
            )
            self.assertEqual(cr.fetchone()[0], incident_id)
            started = time.monotonic()
            with self.assertLogs(level='ERROR') as logs:
                result = env[_MODEL]._log(snapshot)
            elapsed = time.monotonic() - started
            self.assertFalse(result)
            self.assertNotIn(
                snapshot['external_ref'],
                '\n'.join(logs.output),
                'SQL/application error logs must not render raw external_ref',
            )
            self.assertLess(
                elapsed, 3.0,
                'lock_timeout must bound failure instead of hanging',
            )
            cr.rollback()
        finally:
            cr.close()

        incident = self._read_incident(snapshot['external_ref'])
        self.assertEqual(incident[2], 1)

    def test_concurrent_schedule_and_done_end_with_active_dedicated_alert(self):
        snapshot = self._snapshot(
            'activity-schedule-done-race',
            state='needs_human',
        )
        incident_id = self._call_log(snapshot)
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id),
            self._expected_review_activity_counts_by_user(),
            'scheduler must start with exactly one active alert per manager',
        )
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            activity_id = env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ], limit=1).id
        self.assertTrue(activity_id)

        start_barrier = threading.Barrier(2)
        errors = []
        # Construct each Environment on the main test thread before worker
        # start.  During Odoo test loading the main thread owns Registry._lock;
        # constructing Environment inside workers would test that harness lock,
        # not PostgreSQL concurrency.  The cursors themselves are two real
        # pool connections and all ORM work below still runs in each worker.
        schedule_cr = self.registry._db.cursor()
        done_cr = self.registry._db.cursor()
        worker_context = {'mail_notify_force_send': False}
        schedule_env = api.Environment(
            schedule_cr, odoo.SUPERUSER_ID, worker_context)
        done_env = api.Environment(
            done_cr, odoo.SUPERUSER_ID, worker_context)

        def schedule():
            try:
                start_barrier.wait(timeout=5)
                schedule_cr.execute(
                    'SET LOCAL lock_timeout = %s', ('5s',))
                schedule_env[_MODEL] \
                    ._ensure_review_activities_current_cursor(incident_id)
                schedule_cr.commit()
            except Exception as exc:  # surfaced below in the test thread
                errors.append(('schedule', exc))

        def done():
            try:
                start_barrier.wait(timeout=5)
                done_cr.execute('SET LOCAL lock_timeout = %s', ('5s',))
                done_env['mail.activity'].sudo().browse(
                    activity_id).action_done()
                done_cr.commit()
            except Exception as exc:  # surfaced below in the test thread
                errors.append(('done', exc))

        try:
            threads = [
                threading.Thread(target=schedule, name='b864-schedule'),
                threading.Thread(target=done, name='b864-done'),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            alive = [thread for thread in threads if thread.is_alive()]
            stacks = {
                thread.name: ''.join(traceback.format_stack(
                    sys._current_frames()[thread.ident]))
                for thread in alive
            }
            self.assertFalse(
                alive,
                'schedule/done advisory-lock race threads must terminate: %r'
                % stacks,
            )
        finally:
            if not schedule_cr.closed:
                schedule_cr.close()
            if not done_cr.closed:
                done_cr.close()
        self.assertFalse(errors, 'concurrent workers failed: %r' % errors)
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id),
            self._expected_review_activity_counts_by_user(),
            'schedule/done race must retain exactly one active alert per manager',
        )

    def test_concurrent_future_activity_attachment_is_serialized_and_removed(self):
        """Attachment-first overlap must end with the alert, never its payload."""
        snapshot = self._snapshot(
            'future-activity-attachment-race',
            state='open',
        )
        incident_id = self._call_log(snapshot)
        self._set_incident_state(incident_id, 'needs_human')
        marker = '%s-future-activity' % self.marker

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            generic_activity = env['mail.activity'].sudo().create({
                'res_model_id': env['ir.model']._get('res.partner').id,
                'res_id': self.control_partner_id,
                'activity_type_id': env.ref(
                    'mail.mail_activity_data_todo').id,
                'user_id': self.manager_user_id,
                'summary': '%s-generic-control' % marker,
                'note': 'generic target control',
                'date_deadline': fields.Date.today(),
            })
            generic_attachment = env['ir.attachment'].sudo().create({
                'name': '%s-generic-control.bin' % marker,
                'datas': b'Z2VuZXJpYyBjb250cm9s',
                'res_model': 'mail.activity',
                'res_id': generic_activity.id,
            })
            generic_activity_id = generic_activity.id
            generic_attachment_id = generic_attachment.id
            cr.commit()

        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT nextval(pg_get_serial_sequence(%s, 'id'))",
                ('mail_activity',),
            )
            future_activity_id = cr.fetchone()[0] + 1

        attachment_cr = self.registry._db.cursor()
        target_cr = self.registry._db.cursor()
        worker_context = {'mail_notify_force_send': False}
        attachment_env = api.Environment(
            attachment_cr, odoo.SUPERUSER_ID, worker_context)
        target_env = api.Environment(
            target_cr, odoo.SUPERUSER_ID, worker_context)
        attachment_inserted = threading.Event()
        errors = []

        def attach_future_id():
            try:
                attachment_cr.execute(
                    'SET LOCAL lock_timeout = %s', ('5s',))
                attachment_env['ir.attachment'].sudo().create({
                    'name': '%s-racing-payload.bin' % marker,
                    'datas': b'Zm9yYmlkZGVuIGFjdGl2aXR5IHBheWxvYWQ=',
                    'res_model': 'mail.activity',
                    'res_id': future_activity_id,
                })
                attachment_inserted.set()
                # Keep this transaction open so target-first code misses the
                # uncommitted row.  The advisory protocol must instead wait,
                # then remove it after this commit.
                time.sleep(0.3)
                attachment_cr.commit()
            except Exception as exc:
                errors.append(('attachment', exc))
                attachment_inserted.set()
                attachment_cr.rollback()

        def create_alert():
            try:
                self.assertTrue(attachment_inserted.wait(timeout=5))
                target_cr.execute('SET LOCAL lock_timeout = %s', ('5s',))
                target_env[_MODEL] \
                    ._ensure_review_activities_current_cursor(incident_id)
                target_cr.commit()
            except Exception as exc:
                errors.append(('target', exc))
                target_cr.rollback()

        try:
            threads = [
                threading.Thread(
                    target=attach_future_id,
                    name='b864-future-activity-attachment',
                ),
                threading.Thread(
                    target=create_alert,
                    name='b864-future-activity-target',
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(
                [thread.name for thread in threads if thread.is_alive()],
                'future activity carrier race threads must terminate',
            )
        finally:
            if not attachment_cr.closed:
                attachment_cr.close()
            if not target_cr.closed:
                target_cr.close()
        self.assertFalse(errors, 'concurrent workers failed: %r' % errors)

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            alert = env['mail.activity'].sudo().browse(
                future_activity_id).exists()
            self.assertTrue(alert, 'attack must not roll back the review alert')
            self.assertEqual(alert.res_model, _MODEL)
            self.assertEqual(alert.res_id, incident_id)
            self.assertEqual(alert.activity_type_id, review_type)
            incident_activity_ids = env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
            ]).ids
            self.assertFalse(env['ir.attachment'].sudo().search([
                ('res_model', '=', 'mail.activity'),
                ('res_id', 'in', incident_activity_ids),
            ]), 'incident activity graph must contain no attachment linkage')
            generic_activity = env['mail.activity'].sudo().browse(
                generic_activity_id).exists()
            generic_attachment = env['ir.attachment'].sudo().browse(
                generic_attachment_id).exists()
            self.assertTrue(generic_activity and generic_attachment)
            self.assertEqual(generic_activity.res_model, 'res.partner')
            self.assertEqual(generic_attachment.res_model, 'mail.activity')
            self.assertEqual(
                generic_attachment.res_id, generic_activity_id,
                'generic target control must remain unaffected',
            )

    def test_concurrent_future_message_attachment_is_serialized_and_removed(self):
        """Guarded message creation wins while its payload is purged."""
        snapshot = self._snapshot(
            'future-message-attachment-race',
            state='needs_human',
        )
        incident_id = self._call_log(snapshot)
        marker = '%s-future-message' % self.marker

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            activity = env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.manager_user_id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(activity)
            activity_id = activity.id
            generic_message = env['mail.message'].sudo().create({
                'model': 'res.partner',
                'res_id': self.control_partner_id,
                'message_type': 'comment',
                'subject': '%s-generic-control' % marker,
                'body': 'generic message target control',
            })
            generic_attachment = env['ir.attachment'].sudo().create({
                'name': '%s-generic-control.bin' % marker,
                'datas': b'Z2VuZXJpYyBjb250cm9s',
                'res_model': 'mail.message',
                'res_id': generic_message.id,
            })
            generic_message_id = generic_message.id
            generic_attachment_id = generic_attachment.id
            cr.commit()

        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT nextval(pg_get_serial_sequence(%s, 'id'))",
                ('mail_message',),
            )
            future_message_id = cr.fetchone()[0] + 1

        attachment_cr = self.registry._db.cursor()
        target_cr = self.registry._db.cursor()
        worker_context = {'mail_notify_force_send': False}
        attachment_env = api.Environment(
            attachment_cr, odoo.SUPERUSER_ID, worker_context)
        target_env = api.Environment(
            target_cr, odoo.SUPERUSER_ID, worker_context)
        attachment_inserted = threading.Event()
        errors = []

        def attach_future_id():
            try:
                attachment_cr.execute(
                    'SET LOCAL lock_timeout = %s', ('5s',))
                attachment_env['ir.attachment'].sudo().create({
                    'name': '%s-racing-payload.bin' % marker,
                    'datas': b'Zm9yYmlkZGVuIG1lc3NhZ2UgcGF5bG9hZA==',
                    'res_model': 'mail.message',
                    'res_id': future_message_id,
                })
                attachment_inserted.set()
                time.sleep(0.3)
                attachment_cr.commit()
            except Exception as exc:
                errors.append(('attachment', exc))
                attachment_inserted.set()
                attachment_cr.rollback()

        def create_controlled_message():
            try:
                self.assertTrue(attachment_inserted.wait(timeout=5))
                target_cr.execute('SET LOCAL lock_timeout = %s', ('5s',))
                activity = target_env['mail.activity'].sudo().browse(
                    activity_id).with_user(self.manager_user_id)
                incident = target_env[_MODEL].with_user(
                    self.manager_user_id).browse(incident_id)
                message = incident.message_post_with_source(
                    'mail.message_activity_done',
                    author_id=activity.env.user.partner_id.id,
                    render_values={
                        'activity': activity,
                        'feedback': '%s safe verification' % marker,
                        'display_assignee': False,
                    },
                    mail_activity_type_id=activity.activity_type_id.id,
                    subtype_xmlid='mail.mt_activities',
                )
                self.assertEqual(message.id, future_message_id)
                target_cr.commit()
            except Exception as exc:
                errors.append(('target', exc))
                target_cr.rollback()

        try:
            threads = [
                threading.Thread(
                    target=attach_future_id,
                    name='b864-future-message-attachment',
                ),
                threading.Thread(
                    target=create_controlled_message,
                    name='b864-future-message-target',
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(
                [thread.name for thread in threads if thread.is_alive()],
                'future message carrier race threads must terminate',
            )
        finally:
            if not attachment_cr.closed:
                attachment_cr.close()
            if not target_cr.closed:
                target_cr.close()
        self.assertFalse(errors, 'concurrent workers failed: %r' % errors)

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            message = env['mail.message'].sudo().browse(
                future_message_id).exists()
            self.assertTrue(
                message, 'attack must not roll back the completion message')
            self.assertEqual(message.model, _MODEL)
            self.assertEqual(message.res_id, incident_id)
            incident_message_ids = env['mail.message'].sudo().search([
                ('model', '=', _MODEL),
                ('res_id', '=', incident_id),
            ]).ids
            self.assertFalse(env['ir.attachment'].sudo().search([
                ('res_model', '=', 'mail.message'),
                ('res_id', 'in', incident_message_ids),
            ]), 'incident message graph must contain no attachment linkage')
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            self.assertTrue(env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.manager_user_id),
                ('active', '=', True),
            ]), 'guarded message probe must leave the manager alert active')
            generic_message = env['mail.message'].sudo().browse(
                generic_message_id).exists()
            generic_attachment = env['ir.attachment'].sudo().browse(
                generic_attachment_id).exists()
            self.assertTrue(generic_message and generic_attachment)
            self.assertEqual(generic_message.model, 'res.partner')
            self.assertEqual(generic_attachment.res_model, 'mail.message')
            self.assertEqual(
                generic_attachment.res_id, generic_message_id,
                'generic target control must remain unaffected',
            )

    def test_schedule_subscription_and_revocation_barrier_converge(self):
        from odoo.addons.woow_paas_platform.models import billing_incident

        # The schedule-before-revocation ordering is covered by the true RPC
        # revocation test; this barrier forces the inverse overlap where an
        # already-running revocation owns the global lock and the RC scheduler
        # must refresh after waiting.
        for ordering in ('revocation_first',):
            with self.subTest(ordering=ordering):
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    manager_group = env.ref('account.group_account_manager')
                    former = env['res.users'].sudo().with_context(
                        no_reset_password=True).create({
                            'name': '%s-roster-race-%s' % (
                                self.marker, ordering),
                            'login': '%s-roster-race-%s' % (
                                self.marker, ordering),
                            'email': '%s-roster-race-%s@example.test' % (
                                self.marker, ordering),
                            'groups_id': [(6, 0, [manager_group.id])],
                        })
                    former_id = former.id
                    former_partner_id = former.partner_id.id
                    cr.commit()

                snapshot = self._snapshot(
                    'roster-race-%s' % ordering,
                    state='open',
                )
                incident_id = self._call_log(snapshot)
                self._set_incident_state(incident_id, 'needs_human')

                schedule_cr = self.registry._db.cursor()
                revoke_cr = self.registry._db.cursor()
                worker_context = {'mail_notify_force_send': False}
                schedule_env = api.Environment(
                    schedule_cr, odoo.SUPERUSER_ID, worker_context)
                revoke_env = api.Environment(
                    revoke_cr, odoo.SUPERUSER_ID, worker_context)
                start_barrier = threading.Barrier(2)
                gate_guard = threading.Lock()
                gated_cursors = set()
                acquired = []
                errors = []
                original_lock = billing_incident._acquire_billing_roster_lock

                def barrier_roster_lock(cr):
                    cursor_id = id(cr)
                    with gate_guard:
                        first_call = cursor_id not in gated_cursors
                        gated_cursors.add(cursor_id)
                    if first_call:
                        start_barrier.wait(timeout=5)
                        should_delay = (
                            (ordering == 'schedule_first'
                             and cr is revoke_cr)
                            or (ordering == 'revocation_first'
                                and cr is schedule_cr)
                        )
                        if should_delay:
                            time.sleep(0.1)
                    result = original_lock(cr)
                    with gate_guard:
                        if first_call:
                            acquired.append(
                                'schedule' if cr is schedule_cr else 'revoke')
                    return result

                def schedule():
                    try:
                        schedule_cr.rollback()
                        schedule_cr.execute(
                            'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                        schedule_cr.execute(
                            'SET LOCAL lock_timeout = %s', ('5s',))
                        schedule_env[_MODEL] \
                            ._ensure_review_activities_current_cursor(
                                incident_id)
                        schedule_cr.commit()
                    except Exception as exc:
                        errors.append(('schedule', exc))

                def revoke():
                    try:
                        manager_group = revoke_env.ref(
                            'account.group_account_manager')
                        revoke_env['res.users'].sudo().browse(former_id).write({
                            'groups_id': [(3, manager_group.id)],
                        })
                        revoke_cr.commit()
                    except Exception as exc:
                        errors.append(('revoke', exc))

                try:
                    with patch.object(
                            billing_incident,
                            '_acquire_billing_roster_lock',
                            barrier_roster_lock):
                        threads = [
                            threading.Thread(
                                target=schedule,
                                name='b864-roster-schedule-%s' % ordering,
                            ),
                            threading.Thread(
                                target=revoke,
                                name='b864-roster-revoke-%s' % ordering,
                            ),
                        ]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=10)
                    alive = [
                        thread for thread in threads if thread.is_alive()]
                    self.assertFalse(
                        alive,
                        'schedule/revocation barrier threads must terminate: %r'
                        % [thread.name for thread in alive],
                    )
                finally:
                    if not schedule_cr.closed:
                        schedule_cr.close()
                    if not revoke_cr.closed:
                        revoke_cr.close()
                self.assertFalse(
                    errors,
                    'schedule/revocation workers failed: %r' % errors,
                )
                expected_first = (
                    'schedule' if ordering == 'schedule_first' else 'revoke')
                self.assertEqual(
                    acquired[0], expected_first,
                    'barrier probe must exercise the requested lock ordering',
                )

                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {
                        'mail_notify_force_send': False,
                    })
                    review_type = env.ref(
                        'woow_paas_platform.'
                        'mail_activity_type_billing_incident_review')
                    self.assertFalse(env['mail.followers'].sudo().search([
                        ('res_model', '=', _MODEL),
                        ('res_id', '=', incident_id),
                        ('partner_id', '=', former_partner_id),
                    ]), 'former manager follower must converge to zero')
                    self.assertFalse(env['mail.activity'].sudo().search([
                        ('res_model', '=', _MODEL),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                        ('user_id', '=', former_id),
                        ('active', '=', True),
                    ]), 'former manager activity must converge to zero')
                    current_activity = env['mail.activity'].sudo().search([
                        ('res_model', '=', _MODEL),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                        ('user_id', '=', self.manager_user_id),
                        ('active', '=', True),
                    ], limit=1)
                    self.assertTrue(current_activity)
                    current_partner_id = env['res.users'].sudo().browse(
                        self.manager_user_id).partner_id.id
                    message_floor = env['mail.message'].sudo().search(
                        [], order='id desc', limit=1).id
                    notification_floor = env['mail.notification'].sudo().search(
                        [], order='id desc', limit=1).id
                    mail_floor = env['mail.mail'].sudo().search(
                        [], order='id desc', limit=1).id
                    feedback = 'post-race terminal feedback must stay ephemeral'
                    self.assertFalse(current_activity.with_user(
                        self.manager_user_id).action_feedback(
                            feedback=feedback))
                    self.assertFalse(env['mail.message'].sudo().search([
                        ('id', '>', message_floor),
                    ]), 'dedicated terminal must create no mail.message')
                    self.assertFalse(env['mail.notification'].sudo().search([
                        ('id', '>', notification_floor),
                        ('res_partner_id', 'in', [
                            former_partner_id, current_partner_id,
                        ]),
                    ]), 'former/current notification rows must both stay zero')
                    self.assertFalse(env['mail.mail'].sudo().search([
                        ('id', '>', mail_floor),
                        ('recipient_ids', 'in', [
                            former_partner_id, current_partner_id,
                        ]),
                    ]), 'former/current outgoing mail must both stay zero')
                    self.assertFalse(env['mail.message'].sudo().search([
                        ('body', 'ilike', feedback),
                    ]), 'dedicated terminal feedback must not persist')
                    self.assertEqual(env['mail.activity'].sudo().search_count([
                        ('res_model', '=', _MODEL),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                        ('user_id', '=', self.manager_user_id),
                        ('active', '=', True),
                    ]), 1, 'completed current user must have one active alert')
                    cr.commit()

    def test_scheduler_second_cursor_is_read_committed_before_roster_read(self):
        from odoo.addons.woow_paas_platform.models import billing_incident

        snapshot = self._snapshot(
            'scheduler-read-committed',
            state='open',
        )
        incident_id = self._call_log(snapshot)
        self._set_incident_state(incident_id, 'needs_human')
        observed_isolation = []
        original_lock = billing_incident._acquire_billing_roster_lock

        def observe_isolation_before_roster_lock(cr):
            cr.execute('SHOW transaction_isolation')
            observed_isolation.append(cr.fetchone()[0])
            return original_lock(cr)

        original_test_enable = billing_incident.config['test_enable']
        try:
            billing_incident.config['test_enable'] = False
            with patch.object(
                    billing_incident,
                    '_acquire_billing_roster_lock',
                    observe_isolation_before_roster_lock):
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    self.assertTrue(
                        env[_MODEL]._schedule_review_activities(incident_id))
        finally:
            billing_incident.config['test_enable'] = original_test_enable
        self.assertTrue(observed_isolation)
        self.assertEqual(
            set(observed_isolation),
            {'read committed'},
            'scheduler second cursor must switch isolation before its first '
            'roster/advisory-lock snapshot path',
        )
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id),
            self._expected_review_activity_counts_by_user(),
        )

    def test_duplicate_log_converges_current_manager_roster(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            manager_group = env.ref('account.group_account_manager')
            base_group = env.ref('base.group_user')
            removed = env['res.users'].sudo().with_context(
                no_reset_password=True).create({
                    'name': '%s-roster-removed' % self.marker,
                    'login': '%s-roster-removed' % self.marker,
                    'email': '%s-roster-removed@example.test' % self.marker,
                    'groups_id': [(6, 0, [manager_group.id])],
                })
            added = env['res.users'].sudo().with_context(
                no_reset_password=True).create({
                    'name': '%s-roster-added' % self.marker,
                    'login': '%s-roster-added' % self.marker,
                    'email': '%s-roster-added@example.test' % self.marker,
                    'groups_id': [(6, 0, [base_group.id])],
                })
            removed_id = removed.id
            added_id = added.id
            cr.commit()

        snapshot = self._snapshot(
            'manager-roster-convergence',
            state='needs_human',
        )
        incident_id = self._call_log(snapshot)
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id).get(removed_id),
            1,
            'initial schedule must include every manager in the Odoo group',
        )

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            manager_group = env.ref('account.group_account_manager')
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            # Seed a duplicate before the standard res.users API mutation.  The
            # user.write hook itself must remove both the stale former-manager
            # row and this duplicate, then add the new manager immediately.
            cr.execute(
                'INSERT INTO mail_activity '
                '(res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, summary, note, date_deadline, '
                'automated, active, create_uid, write_uid, create_date, '
                'write_date) '
                'SELECT res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, %s, note, date_deadline, '
                'automated, active, create_uid, write_uid, NOW(), NOW() '
                'FROM mail_activity WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s AND user_id = %s '
                'AND active = true ORDER BY id LIMIT 1',
                (
                    'integration duplicate before user write',
                    _MODEL, incident_id, review_type.id,
                    self.manager_user_id,
                ),
            )
            env['mail.activity'].invalidate_model()
            env['res.users'].sudo().browse(removed_id).write({
                'groups_id': [(3, manager_group.id)],
            })
            env['res.users'].sudo().browse(added_id).write({
                'groups_id': [(4, manager_group.id)],
            })
            immediate_counts = env['mail.activity'].sudo().read_group(
                domain=[
                    ('res_model', '=', _MODEL),
                    ('res_id', '=', incident_id),
                    ('activity_type_id', '=', review_type.id),
                    ('active', '=', True),
                ],
                fields=['user_id'],
                groupby=['user_id'],
            )
            self.assertEqual(
                {row['user_id'][0]: row['user_id_count']
                 for row in immediate_counts},
                {
                    user_id: 1
                    for user_id in manager_group.users.filtered('active').ids
                },
                'res.users.write(groups_id) must converge in the same cursor',
            )
            added = env['res.users'].sudo().browse(added_id)
            added.write({'active': False})
            self.assertFalse(env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', added_id),
                ('active', '=', True),
            ]), 'deactivation must remove the active manager alert immediately')
            added.write({'active': True})
            self.assertEqual(env['mail.activity'].sudo().search_count([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', added_id),
                ('active', '=', True),
            ]), 1, 'reactivation must restore exactly one alert immediately')
            cr.commit()
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id),
            self._expected_review_activity_counts_by_user(),
            'user.write group/active paths must commit an exact current roster',
        )
        self.assertNotIn(
            removed_id, self._review_activity_counts_by_user(incident_id),
            'former manager stale activity must be deleted by user.write',
        )

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            cr.execute(
                'INSERT INTO mail_activity '
                '(res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, summary, note, date_deadline, '
                'automated, active, create_uid, write_uid, create_date, '
                'write_date) '
                'SELECT res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, %s, note, date_deadline, '
                'automated, active, create_uid, write_uid, NOW(), NOW() '
                'FROM mail_activity WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s AND user_id = %s '
                'AND active = true ORDER BY id LIMIT 1',
                (
                    'integration duplicate roster fixture',
                    _MODEL, incident_id, review_type.id,
                    self.manager_user_id,
                ),
            )
            cr.commit()
        self.assertEqual(
            self._review_activity_counts_by_user(incident_id).get(
                self.manager_user_id),
            2,
            '前置：現任 manager 必須先有兩筆 active duplicate',
        )

        logger_target = (
            'odoo.addons.woow_paas_platform.models.billing_incident._logger')
        with patch(logger_target) as logger:
            self.assertEqual(self._call_log(snapshot), incident_id)
        counts = self._review_activity_counts_by_user(incident_id)
        self.assertEqual(
            counts,
            self._expected_review_activity_counts_by_user(),
            'duplicate _log must remove former managers/duplicates and add '
            'new managers before returning',
        )
        self.assertNotIn(removed_id, counts)
        self.assertEqual(counts.get(added_id), 1)
        self.assertNotIn(
            snapshot['external_ref'], repr(logger.mock_calls),
            'roster convergence logging must never disclose raw MTN',
        )

    def test_postcheck_denials_survive_caller_catch_commit_for_all_carriers(self):
        """A swallowed postcheck exception must still roll its ORM write back."""
        snapshot = self._snapshot('postcheck-savepoint')
        incident_id = self._call_log(snapshot)
        marker = '%s-postcheck' % self.marker
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            Message = env['mail.message'].sudo()
            Attachment = env['ir.attachment'].sudo()
            Activity = env['mail.activity'].sudo()
            message = Message.create({
                'model': 'res.partner',
                'res_id': self.control_partner_id,
                'message_type': 'comment',
                'subject': '%s-message-control' % marker,
                'body': 'original message body',
            })
            attachment = Attachment.create({
                'name': '%s-attachment-control.bin' % marker,
                'datas': b'b3JpZ2luYWwgYXR0YWNobWVudA==',
                'res_model': 'res.partner',
                'res_id': self.control_partner_id,
            })
            activity = Activity.create({
                'res_model_id': env['ir.model']._get('res.partner').id,
                'res_id': self.control_partner_id,
                'activity_type_id': env.ref(
                    'mail.mail_activity_data_todo').id,
                'user_id': self.manager_user_id,
                'summary': '%s-activity-control' % marker,
                'note': 'original activity note',
                'date_deadline': fields.Date.today(),
            })
            message_id = message.id
            attachment_id = attachment.id
            activity_id = activity.id
            cr.commit()

        create_message_subject = '%s-message-create' % marker
        create_attachment_name = '%s-attachment-create.bin' % marker
        create_activity_summary = '%s-activity-create' % marker
        denials = {}

        def catch_and_commit(name, callback):
            cr = self.registry.cursor()
            try:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                try:
                    callback(env)
                except AccessError:
                    denials[name] = True
                else:
                    denials[name] = False
                # Deliberately no outer savepoint/rollback: this models a
                # caller swallowing AccessError and completing its request.
                cr.commit()
            finally:
                cr.close()

        def create_message(env):
            Message = env['mail.message'].sudo()
            MessageClass = type(Message)
            with patch.object(
                    MessageClass,
                    '_targets_billing_incident_values',
                    return_value=False):
                Message.create({
                    'model': _MODEL,
                    'res_id': incident_id,
                    'message_type': 'comment',
                    'subject': create_message_subject,
                    'body': 'postcheck-only forbidden create',
                })

        def write_message(env):
            Message = env['mail.message'].sudo()
            MessageClass = type(Message)
            with patch.object(
                    MessageClass,
                    '_targets_billing_incident_values',
                    return_value=False):
                Message.browse(message_id).write({
                    'model': _MODEL,
                    'res_id': incident_id,
                    'body': 'postcheck-only forbidden write',
                })

        def create_attachment(env):
            Attachment = env['ir.attachment'].sudo()
            AttachmentClass = type(Attachment)
            with patch.object(
                    AttachmentClass,
                    '_values_billing_owned_target',
                    return_value=False):
                Attachment.create({
                    'name': create_attachment_name,
                    'datas': b'Zm9yYmlkZGVuIGNyZWF0ZQ==',
                    'res_model': 'woow.billing.incident',
                    'res_id': incident_id,
                })

        def write_attachment(env):
            Attachment = env['ir.attachment'].sudo()
            AttachmentClass = type(Attachment)
            original = AttachmentClass._billing_owned_target
            protected_calls = 0

            def bypass_prospective_once(records, res_model, res_id):
                nonlocal protected_calls
                if res_model == 'woow.billing.incident':
                    protected_calls += 1
                    if protected_calls == 1:
                        return False
                return original(records, res_model, res_id)

            with patch.object(
                    AttachmentClass,
                    '_billing_owned_target',
                    bypass_prospective_once):
                Attachment.browse(attachment_id).write({
                    'datas': b'Zm9yYmlkZGVuIHdyaXRl',
                    'res_model': 'woow.billing.incident',
                    'res_id': incident_id,
                })

        def create_activity(env):
            Activity = env['mail.activity'].sudo()
            ActivityClass = type(Activity)
            with patch.object(
                    ActivityClass,
                    '_targets_billing_activity_values',
                    return_value=False):
                Activity.create({
                    'res_model_id': env['ir.model']._get(_MODEL).id,
                    'res_id': incident_id,
                    'activity_type_id': env.ref(
                        'mail.mail_activity_data_todo').id,
                    'user_id': self.manager_user_id,
                    'summary': create_activity_summary,
                    'note': 'postcheck-only forbidden create',
                    'date_deadline': fields.Date.today(),
                })

        def write_activity(env):
            Activity = env['mail.activity'].sudo()
            ActivityClass = type(Activity)
            with patch.object(
                    ActivityClass,
                    '_targets_billing_activity_values',
                    return_value=False):
                Activity.browse(activity_id).write({
                    'res_model_id': env['ir.model']._get(_MODEL).id,
                    'res_id': incident_id,
                    'summary': '%s-activity-forbidden-write' % marker,
                })

        operations = (
            ('mail.message.create', create_message),
            ('mail.message.write', write_message),
            ('ir.attachment.create', create_attachment),
            ('ir.attachment.write', write_attachment),
            ('mail.activity.create', create_activity),
            ('mail.activity.write', write_activity),
        )
        for name, callback in operations:
            catch_and_commit(name, callback)

        # A genuinely independent third cursor is the only observer used for
        # the post-catch durable state assertions.
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            self.assertFalse(env['mail.message'].sudo().search([
                ('subject', '=', create_message_subject),
            ]))
            message = env['mail.message'].sudo().browse(message_id).exists()
            self.assertTrue(message)
            self.assertEqual(message.model, 'res.partner')
            self.assertEqual(message.res_id, self.control_partner_id)
            self.assertEqual(
                str(message.body), '<p>original message body</p>')

            self.assertFalse(env['ir.attachment'].sudo().search([
                ('name', '=', create_attachment_name),
            ]))
            attachment = env['ir.attachment'].sudo().browse(
                attachment_id).exists()
            self.assertTrue(attachment)
            self.assertEqual(attachment.res_model, 'res.partner')
            self.assertEqual(attachment.res_id, self.control_partner_id)
            self.assertEqual(
                attachment.datas, b'b3JpZ2luYWwgYXR0YWNobWVudA==')

            self.assertFalse(env['mail.activity'].sudo().search([
                ('summary', '=', create_activity_summary),
            ]))
            activity = env['mail.activity'].sudo().browse(activity_id).exists()
            self.assertTrue(activity)
            self.assertEqual(activity.res_model, 'res.partner')
            self.assertEqual(activity.res_id, self.control_partner_id)
            self.assertEqual(activity.summary, '%s-activity-control' % marker)
        self.assertEqual(
            denials,
            {name: True for name, _callback in operations},
            'each fault-injected path must reach and raise from its postcheck',
        )

    def test_attachment_unlink_commit_oracles_use_three_real_connections(self):
        """Caught denial and private cleanup must have durable DB oracles."""
        from odoo.addons.woow_paas_platform.models import billing_incident

        incident_id = self._call_log(self._snapshot(
            'attachment-unlink-commit-oracles',
            state='needs_human',
        ))
        with self.registry.cursor() as setup_cr:
            setup_env = api.Environment(
                setup_cr, odoo.SUPERUSER_ID, {})
            ordinary = setup_env['ir.attachment'].sudo().create({
                'name': '%s-mixed-ordinary.bin' % self.marker,
                'datas': b'b3JkaW5hcnk=',
                'res_model': 'res.partner',
                'res_id': self.control_partner_id,
            })
            protected_mixed, protected_cleanup = (
                setup_env['ir.attachment'].sudo().create([{
                    'name': '%s-%s.bin' % (self.marker, purpose),
                    'datas': b'cHJvdGVjdGVk',
                } for purpose in ('mixed-protected', 'private-cleanup')])
            )
            protected_m2m = setup_env['ir.attachment'].sudo().create({
                'name': '%s-mixed-m2m-protected.bin' % self.marker,
                'datas': b'bTJtIHByb3RlY3RlZA==',
                'res_model': 'res.partner',
                'res_id': self.control_partner_id,
            })
            review_type = setup_env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            dedicated = setup_env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.manager_user_id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(dedicated)
            setup_cr.execute(
                'UPDATE ir_attachment SET res_model = %s, res_id = %s '
                'WHERE id = ANY(%s)',
                (
                    _MODEL,
                    incident_id,
                    [protected_mixed.id, protected_cleanup.id],
                ),
            )
            setup_cr.execute(
                'INSERT INTO activity_attachment_rel '
                '(activity_id, attachment_id) VALUES (%s, %s)',
                (dedicated.id, protected_m2m.id),
            )
            ordinary_id = ordinary.id
            protected_mixed_id = protected_mixed.id
            protected_m2m_id = protected_m2m.id
            protected_m2m_activity_id = dedicated.id
            protected_cleanup_id = protected_cleanup.id
            # This is the setup visibility boundary required by the oracle.
            setup_cr.commit()

        direct_mixed_ids = sorted([ordinary_id, protected_mixed_id])
        m2m_mixed_ids = sorted([ordinary_id, protected_m2m_id])
        mixed_ids = sorted(set(direct_mixed_ids + m2m_mixed_ids))
        for provenance, payload_ids in (
                ('direct', direct_mixed_ids),
                ('m2m', m2m_mixed_ids)):
            caller_cr = self.registry.cursor()
            try:
                caller_env = api.Environment(
                    caller_cr, self.manager_user_id, {})
                try:
                    caller_env['ir.attachment'].browse(payload_ids).unlink()
                except AccessError:
                    # Deliberately make any pre-denial partial unlink durable.
                    caller_cr.commit()
                else:
                    self.fail(
                        '%s protected＋ordinary unlink must raise AccessError'
                        % provenance)
            finally:
                caller_cr.close()

        with self.registry.cursor() as verify_cr:
            verify_cr.execute(
                'SELECT id FROM ir_attachment WHERE id = ANY(%s) ORDER BY id',
                (mixed_ids,),
            )
            self.assertEqual(
                [row_id for row_id, in verify_cr.fetchall()],
                mixed_ids,
                'third connection must see every mixed row after catch＋commit',
            )
            verify_cr.execute(
                'SELECT 1 FROM activity_attachment_rel '
                'WHERE activity_id = %s AND attachment_id = %s',
                (protected_m2m_activity_id, protected_m2m_id),
            )
            self.assertTrue(
                verify_cr.fetchone(),
                'third connection must still see the protected M2M link',
            )

        with self.registry.cursor() as cleanup_cr:
            cleanup_env = api.Environment(
                cleanup_cr, odoo.SUPERUSER_ID, {})
            payload = cleanup_env['ir.attachment'].sudo().browse(
                protected_cleanup_id)
            self.assertTrue(
                billing_incident._attachment_cleanup(payload).unlink())
            cleanup_cr.commit()

        with self.registry.cursor() as verify_cr:
            verify_cr.execute(
                'SELECT id FROM ir_attachment WHERE id = ANY(%s) ORDER BY id',
                ([
                    ordinary_id,
                    protected_mixed_id,
                    protected_m2m_id,
                    protected_cleanup_id,
                ],),
            )
            self.assertEqual(
                [row_id for row_id, in verify_cr.fetchall()],
                mixed_ids,
                'third connection must see only private-cleanup payload gone',
            )

    def test_parent_activity_cleanup_matches_core_and_real_uninstall(self):
        """Normal cleanup stays core; true uninstall skips evidence re-anchor."""
        from odoo.addons.woow_paas_platform.models import billing_incident

        incident_id = self._call_log(self._snapshot(
            'parent-activity-cleanup',
            state='needs_human',
        ))
        with self.registry.cursor() as setup_cr:
            setup_env = api.Environment(
                setup_cr, odoo.SUPERUSER_ID, {})
            review_type = setup_env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            dedicated = setup_env['mail.activity'].sudo().search([
                ('res_model', '=', _MODEL),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.manager_user_id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(dedicated)
            ordinary = setup_env['mail.activity'].sudo().create({
                'res_model_id': setup_env['ir.model']._get(
                    'res.partner').id,
                'res_id': self.control_partner_id,
                'activity_type_id': setup_env.ref(
                    'mail.mail_activity_data_todo').id,
                'user_id': self.manager_user_id,
                'summary': '%s ordinary parent cleanup' % self.marker,
                'note': 'generic Odoo cleanup control',
                'date_deadline': fields.Date.today(),
            })
            ordinary_attachment = setup_env['ir.attachment'].sudo().create({
                'name': '%s-ordinary-parent-attachment.bin' % self.marker,
                'datas': b'b3JkaW5hcnkgcGF5bG9hZA==',
                'res_model': 'mail.activity',
                'res_id': ordinary.id,
            })
            dedicated_attachment = setup_env['ir.attachment'].sudo().create({
                'name': '%s-uninstall-parent-attachment.bin' % self.marker,
                'datas': b'dW5pbnN0YWxsIHBheWxvYWQ=',
            })
            setup_cr.execute(
                'UPDATE ir_attachment SET res_model = %s, res_id = %s '
                'WHERE id = %s',
                ('mail.activity', dedicated.id, dedicated_attachment.id),
            )
            ordinary_id = ordinary.id
            ordinary_attachment_id = ordinary_attachment.id
            dedicated_id = dedicated.id
            dedicated_attachment_id = dedicated_attachment.id
            setup_cr.commit()

        with self.registry.cursor() as ordinary_cr:
            ordinary_env = api.Environment(
                ordinary_cr, self.manager_user_id, {})
            self.assertTrue(
                ordinary_env['mail.activity'].browse(ordinary_id).unlink())
            ordinary_cr.commit()
        with self.registry.cursor() as verify_cr:
            verify_cr.execute(
                'SELECT '
                'EXISTS(SELECT 1 FROM mail_activity WHERE id = %s), '
                'EXISTS(SELECT 1 FROM ir_attachment WHERE id = %s)',
                (ordinary_id, ordinary_attachment_id),
            )
            self.assertEqual(
                verify_cr.fetchone(),
                (False, False),
                'Odoo BaseModel.unlink(), not a DB FK cascade, must delete '
                'the ordinary parent and its direct attachment',
            )

        uninstall_cr = self.registry.cursor()
        try:
            uninstall_env = api.Environment(
                uninstall_cr, odoo.SUPERUSER_ID, {})
            module = uninstall_env['ir.module.module'].sudo().search([
                ('name', '=', 'woow_paas_platform'),
            ], limit=1)
            original_state = module.state
            module.write({'state': 'to remove'})
            uninstall_activity = uninstall_env['mail.activity'].sudo() \
                .with_context(_force_unlink=True).browse(dedicated_id)
            self.assertTrue(
                billing_incident._is_odoo_module_uninstall(
                    uninstall_activity.env),
                'test must enter both real state=to remove and core context',
            )
            self.assertTrue(uninstall_activity.unlink())
            module.write({'state': original_state})
            uninstall_cr.commit()
        finally:
            uninstall_cr.close()

        with self.registry.cursor() as verify_cr:
            verify_cr.execute(
                'SELECT '
                'EXISTS(SELECT 1 FROM mail_activity WHERE id = %s), '
                'EXISTS(SELECT 1 FROM ir_attachment WHERE id = %s), '
                '(SELECT state FROM ir_module_module WHERE id = %s)',
                (dedicated_id, dedicated_attachment_id, module.id),
            )
            self.assertEqual(
                verify_cr.fetchone(),
                (False, False, original_state),
                'true uninstall must skip re-anchor and let core parent '
                'attachment cleanup run, then restore module fixture state',
            )

    def test_resolution_and_roster_mutation_share_one_lock_order(self):
        """Manual/independent cleanup cannot deadlock roster convergence."""
        from odoo.addons.woow_paas_platform.models import billing_incident

        cases = (
            ('manual', 'res.users'),
            ('independent', 'res.groups'),
        )
        for resolution_path, mutation_path in cases:
            with self.subTest(
                    resolution_path=resolution_path,
                    mutation_path=mutation_path):
                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {
                        'mail_notify_force_send': False,
                    })
                    manager_group = env.ref(
                        'account.group_account_manager')
                    base_group = env.ref('base.group_user')
                    former = env['res.users'].sudo().with_context(
                        no_reset_password=True).create({
                            'name': '%s-roster-lock-%s' % (
                                self.marker, resolution_path),
                            'login': '%s-roster-lock-%s' % (
                                self.marker, resolution_path),
                            'email': '%s-roster-lock-%s@example.test' % (
                                self.marker, resolution_path),
                            'groups_id': [(6, 0, [
                                base_group.id, manager_group.id,
                            ])],
                        })
                    former_id = former.id
                    former_partner_id = former.partner_id.id
                    cr.commit()

                snapshot = self._snapshot(
                    'resolve-roster-lock-%s' % resolution_path,
                    state='needs_human',
                )
                incident_id = self._call_log(snapshot)
                if resolution_path == 'manual':
                    with self.registry.cursor() as cr:
                        manager_env = api.Environment(
                            cr, self.manager_user_id, {})
                        manager_env[_MODEL].browse(incident_id).write({
                            'manual_check_result': 'ecpay_not_found',
                            'resolution_note': (
                                'manual roster lock ordering verification'),
                        })
                        cr.commit()

                resolve_cr = self.registry._db.cursor()
                mutation_cr = self.registry._db.cursor()
                worker_context = {'mail_notify_force_send': False}
                resolve_env = api.Environment(
                    resolve_cr, self.manager_user_id, worker_context)
                mutation_env = api.Environment(
                    mutation_cr, odoo.SUPERUSER_ID, worker_context)
                start_barrier = threading.Barrier(2)
                mutation_has_roster_lock = threading.Event()
                gate_guard = threading.Lock()
                gated = {'mutation': False, 'resolution': False}
                errors = []
                results = []
                original_lock = billing_incident._acquire_billing_roster_lock

                def ordered_roster_lock(cr):
                    role = 'mutation' if cr is mutation_cr else 'resolution'
                    with gate_guard:
                        first = not gated[role]
                        if first:
                            gated[role] = True
                    if not first:
                        return original_lock(cr)
                    start_barrier.wait(timeout=5)
                    if role == 'mutation':
                        result = original_lock(cr)
                        mutation_has_roster_lock.set()
                        return result
                    if not mutation_has_roster_lock.wait(timeout=5):
                        raise AssertionError(
                            'mutation worker did not acquire roster lock first')
                    return original_lock(cr)

                def resolve():
                    def perform_resolution():
                        resolve_cr.execute(
                            'SET LOCAL lock_timeout = %s', ('5s',))
                        if resolution_path == 'manual':
                            return resolve_env[_MODEL].browse(
                                incident_id).action_mark_manually_resolved()
                        return resolve_env[_MODEL]._resolve(
                            snapshot['external_ref'],
                            resolution_note=(
                                'independent roster lock ordering '
                                'verification'),
                            resolved_by_ref=self.manager_user_id,
                        )

                    try:
                        try:
                            result = perform_resolution()
                        except SerializationFailure as exc:
                            # A cursor whose RR snapshot predates the roster
                            # winner may see 40001 while deleting an activity
                            # that winner already removed.  Odoo retries the
                            # whole RPC on 40001; mirror that production seam.
                            self.assertEqual(
                                exc.pgcode,
                                errorcodes.SERIALIZATION_FAILURE,
                            )
                            resolve_cr.rollback()
                            result = perform_resolution()
                        results.append(('resolution', result))
                        resolve_cr.commit()
                    except Exception as exc:
                        errors.append(('resolution', exc))
                        resolve_cr.rollback()

                def mutate_roster():
                    try:
                        mutation_cr.execute(
                            'SET LOCAL lock_timeout = %s', ('5s',))
                        manager_group = mutation_env.ref(
                            'account.group_account_manager')
                        if mutation_path == 'res.users':
                            mutation_env['res.users'].sudo().browse(
                                former_id).write({
                                    'groups_id': [(3, manager_group.id)],
                                })
                        else:
                            manager_group.sudo().write({
                                'users': [(3, former_id)],
                            })
                        mutation_cr.commit()
                    except Exception as exc:
                        errors.append(('mutation', exc))
                        mutation_cr.rollback()

                try:
                    with patch.object(
                            billing_incident,
                            '_acquire_billing_roster_lock',
                            ordered_roster_lock):
                        threads = [
                            threading.Thread(
                                target=resolve,
                                name='b864-resolve-roster-%s'
                                % resolution_path,
                            ),
                            threading.Thread(
                                target=mutate_roster,
                                name='b864-mutate-roster-%s'
                                % mutation_path,
                            ),
                        ]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=10)
                    alive = [
                        thread for thread in threads if thread.is_alive()]
                    stacks = {
                        thread.name: ''.join(traceback.format_stack(
                            sys._current_frames()[thread.ident]))
                        for thread in alive
                    }
                    self.assertFalse(
                        alive,
                        'resolution/roster workers must not deadlock: %r'
                        % stacks,
                    )
                finally:
                    if not resolve_cr.closed:
                        resolve_cr.close()
                    if not mutation_cr.closed:
                        mutation_cr.close()
                self.assertFalse(
                    errors,
                    'resolution/roster workers failed: %r' % errors,
                )
                self.assertTrue(gated['mutation'] and gated['resolution'])
                self.assertEqual(results, [('resolution', incident_id if (
                    resolution_path == 'independent') else True)])

                with self.registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    review_type = env.ref(
                        'woow_paas_platform.'
                        'mail_activity_type_billing_incident_review')
                    incident = env[_MODEL].sudo().browse(incident_id)
                    self.assertEqual(incident.state, 'resolved')
                    self.assertFalse(env['mail.activity'].sudo().with_context(
                        active_test=False).search([
                            ('res_model', '=', _MODEL),
                            ('res_id', '=', incident_id),
                            ('activity_type_id', '=', review_type.id),
                        ]), 'resolved incident must retain no dedicated activity')
                    self.assertFalse(env['mail.followers'].sudo().search([
                        ('res_model', '=', _MODEL),
                        ('res_id', '=', incident_id),
                        ('partner_id', '=', former_partner_id),
                    ]), 'former manager must retain no incident following')
                    former = env['res.users'].sudo().browse(former_id)
                    self.assertNotIn(
                        env.ref('account.group_account_manager'),
                        former.groups_id,
                    )
                    with self.assertRaises(AccessError):
                        incident.with_user(former).read(['external_ref'])

    def test_schedule_resolve_orderings_converge_without_stale_todo(self):
        snapshot = self._snapshot('activity-ordering')
        incident_id = self._call_log(snapshot)
        self._set_incident_state(incident_id, 'needs_human')

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            self.assertTrue(
                env[_MODEL]._schedule_review_activities(incident_id))
        self.assertGreater(self._review_activity_count(incident_id), 0)

        self.assertEqual(
            self._call_resolve(
                snapshot['external_ref'],
                'schedule-before-resolve ordering',
            ),
            incident_id,
        )
        self.assertEqual(self._review_activity_count(incident_id), 0)

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            self.assertFalse(
                env[_MODEL]._schedule_review_activities(incident_id),
                'resolve-before-late-schedule must reread resolved state',
            )
        self.assertEqual(
            self._review_activity_count(incident_id), 0,
            'both transaction orderings must converge without stale review',
        )
