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
            env['res.users'].sudo().browse(removed_id).write({
                'groups_id': [(3, manager_group.id)],
            })
            env['res.users'].sudo().browse(added_id).write({
                'groups_id': [(4, manager_group.id)],
            })
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
