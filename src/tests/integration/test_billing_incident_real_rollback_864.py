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
import time
import uuid

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
                run_uuid, uuid.uuid4().hex)
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
                cr.execute(
                    "DELETE FROM mail_activity WHERE res_model = %s AND res_id IN "
                    "(SELECT id FROM woow_billing_incident "
                    " WHERE external_ref LIKE %s)",
                    (_MODEL, self.marker + '%'),
                )
                cr.execute(
                    'DELETE FROM woow_billing_incident '
                    'WHERE external_ref LIKE %s',
                    (self.marker + '%',),
                )
                env['account.move'].sudo().search([
                    ('ref', '=', self.marker),
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
            'external_ref': '%s-%s' % (self.marker, suffix),
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
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            cr.execute(
                'SELECT COUNT(*) FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s',
                (_MODEL, incident_id, review_type.id),
            )
            return cr.fetchone()[0]

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

            # Cursor B resolves and commits after cursor A has been opened but
            # before A attempts its manager write.  The write guard must lock
            # and reread instead of trusting a stale/bare recordset.
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
            except AccessError:
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
            result = env[_MODEL]._log(snapshot)
            elapsed = time.monotonic() - started
            self.assertFalse(result)
            self.assertLess(
                elapsed, 3.0,
                'lock_timeout must bound failure instead of hanging',
            )
            cr.rollback()
        finally:
            cr.close()

        incident = self._read_incident(snapshot['external_ref'])
        self.assertEqual(incident[2], 1)

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
