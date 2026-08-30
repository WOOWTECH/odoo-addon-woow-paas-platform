"""#864 PR-D：帳務 incident 地基的 public model／後台行為守線。"""

import ast
import json
import re
import uuid
from pathlib import Path
from unittest.mock import patch

from lxml import etree
import polib
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestBillingIncidentSchema864(TransactionCase):

    def test_business_schema_uses_only_scalar_snapshot_fields(self):
        Incident = self.env['woow.billing.incident']
        expected_types = {
            'external_ref': 'char',
            'kind': 'selection',
            'partner_ref': 'integer',
            'partner_email': 'char',
            'payment_token_ref': 'integer',
            'amount': 'float',
            'credits': 'float',
            'bonus': 'float',
            'currency': 'char',
            'trade_no': 'char',
            'rtn_code': 'char',
            'failure_code': 'char',
            'state': 'selection',
            'skip_reason': 'char',
            'last_skipped_at': 'datetime',
            'skip_count': 'integer',
            'attempts': 'integer',
            'next_query_at': 'datetime',
            'query_attempts': 'integer',
            'first_seen_at': 'datetime',
            'last_seen_at': 'datetime',
            'occurrences': 'integer',
            'assessment_reason': 'char',
            'detail': 'text',
            'source_model': 'char',
            'source_ref': 'integer',
            'manual_check_result': 'selection',
            'resolution_note': 'text',
            'resolved_at': 'datetime',
            'resolved_by_ref': 'integer',
        }
        self.assertEqual(
            {name: Incident._fields[name].type for name in expected_types},
            expected_types,
        )
        relational = {'many2one', 'one2many', 'many2many'}
        self.assertFalse(
            [(name, Incident._fields[name].type) for name in expected_types
             if Incident._fields[name].type in relational],
            '自訂 business snapshot 欄位不可帶 relation/FK',
        )

        defaults = Incident.default_get([
            'state', 'skip_count', 'attempts', 'query_attempts',
            'occurrences', 'first_seen_at', 'last_seen_at',
        ])
        self.assertEqual(defaults['state'], 'open')
        self.assertEqual(defaults['skip_count'], 0)
        self.assertEqual(defaults['attempts'], 0)
        self.assertEqual(defaults['query_attempts'], 0)
        self.assertEqual(defaults['occurrences'], 1)
        self.assertTrue(defaults['first_seen_at'])
        self.assertTrue(defaults['last_seen_at'])

    def test_external_ref_is_database_unique(self):
        Incident = self.env['woow.billing.incident'].sudo()
        ref = 'schema-%s' % uuid.uuid4().hex
        Incident.create({'external_ref': ref, 'kind': 'intent'})
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Incident.create({'external_ref': ref, 'kind': 'intent'})
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_database_constraints_reject_invalid_counters_and_time_order(self):
        Incident = self.env['woow.billing.incident'].sudo()
        cases = (
            {'skip_count': -1},
            {'attempts': -1},
            {'query_attempts': -1},
            {'occurrences': 0},
            {
                'first_seen_at': '2026-08-30 02:00:00',
                'last_seen_at': '2026-08-30 01:00:00',
            },
        )
        executed = []
        for index, bad_values in enumerate(cases):
            with self.subTest(values=bad_values):
                values = {
                    'external_ref': 'constraint-%s-%s' % (
                        index, uuid.uuid4().hex),
                    'kind': 'intent',
                    **bad_values,
                }
                with self.assertRaises(IntegrityError):
                    with self.env.cr.savepoint():
                        Incident.create(values)
                self.env.cr.execute('SELECT 1')
                self.assertEqual(self.env.cr.fetchone()[0], 1)
                executed.append(index)
        self.assertEqual(executed, list(range(len(cases))))
        valid = Incident.create({
            'external_ref': 'constraint-valid-%s' % uuid.uuid4().hex,
            'kind': 'intent',
        })
        self.assertTrue(valid)


@tagged('post_install', '-at_install')
class TestBillingIncidentPublicApi864(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Incident = self.env['woow.billing.incident']
        self.ref = '864%s' % uuid.uuid4().hex
        self.addCleanup(self._purge)

    def _purge(self):
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM mail_activity WHERE res_model = %s AND res_id IN "
                "(SELECT id FROM woow_billing_incident WHERE external_ref LIKE %s)",
                ('woow.billing.incident', self.ref + '%'),
            )
            cr.execute(
                'DELETE FROM woow_billing_incident WHERE external_ref LIKE %s',
                (self.ref + '%',),
            )
            cr.commit()

    def _read(self, incident_id):
        """Read through a fresh snapshot; the outer TransactionCase cursor is RR."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT kind, state, occurrences, trade_no, detail, '
                'assessment_reason, first_seen_at, last_seen_at, '
                'resolution_note, resolved_by_ref, resolved_at '
                'FROM woow_billing_incident WHERE id = %s',
                (incident_id,),
            )
            row = cr.fetchone()
        self.assertTrue(row, 'incident id %s must exist' % incident_id)
        names = (
            'kind', 'state', 'occurrences', 'trade_no', 'detail',
            'assessment_reason', 'first_seen_at', 'last_seen_at',
            'resolution_note', 'resolved_by_ref', 'resolved_at',
        )
        return dict(zip(names, row))

    def _snapshot(self, **overrides):
        values = {
            'external_ref': self.ref,
            'kind': 'charged_not_credited',
            'partner_ref': 42,
            'partner_email': 'incident@example.test',
            'payment_token_ref': 71,
            'amount': 990.0,
            'credits': 990.0,
            'bonus': 90.0,
            'currency': 'TWD',
            'trade_no': 'TRADE-ORIGINAL',
            'rtn_code': '1',
            'failure_code': 'CREDIT_FAILED',
            'source_model': 'woow.wallet.topup',
            'source_ref': 314,
            'detail': 'first evidence',
        }
        values.update(overrides)
        return values

    def test_log_atomically_upserts_same_kind_evidence(self):
        incident_id = self.Incident._log(self._snapshot())
        duplicate_id = self.Incident._log(self._snapshot(
            trade_no='TRADE-LATEST',
            detail='latest evidence',
            assessment_reason='captured',
        ))

        self.assertIsInstance(incident_id, int)
        self.assertEqual(duplicate_id, incident_id)
        incident = self._read(incident_id)
        self.assertEqual(incident['occurrences'], 2)
        self.assertEqual(incident['trade_no'], 'TRADE-LATEST')
        self.assertEqual(incident['detail'], 'latest evidence')
        self.assertEqual(incident['assessment_reason'], 'captured')
        self.assertEqual(incident['state'], 'open')
        self.assertLessEqual(incident['first_seen_at'],
                             incident['last_seen_at'])

    def test_log_rejects_missing_unknown_nested_and_recordset_inputs(self):
        bad_inputs = (
            {},
            {'external_ref': self.ref},
            self._snapshot(card_number='4111111111111111'),
            self._snapshot(ResultData='encrypted-card-payload'),
            self._snapshot(detail={'not': 'flat'}),
            self.env['res.partner'],
        )
        for snapshot in bad_inputs:
            with self.subTest(snapshot=repr(snapshot)):
                self.assertFalse(self.Incident._log(snapshot))
        self.assertFalse(
            self.Incident.sudo().search([('external_ref', '=', self.ref)]),
            'invalid／密文 unknown key 不得留下半筆 incident',
        )

    def test_duplicate_log_never_downgrades_existing_unresolved_state(self):
        for initial_state in ('needs_human', 'orphan'):
            with self.subTest(initial_state=initial_state):
                external_ref = '%s-%s' % (self.ref, initial_state)
                incident_id = self.Incident._log(self._snapshot(
                    external_ref=external_ref,
                    state=initial_state,
                ))
                duplicate_id = self.Incident._log(self._snapshot(
                    external_ref=external_ref,
                    state='open',
                    detail='late duplicate must not downgrade state',
                ))
                self.assertEqual(duplicate_id, incident_id)
                incident = self._read(incident_id)
                self.assertEqual(
                    incident['state'], initial_state,
                    '%s → open 是禁止的 state downgrade' % initial_state,
                )
                self.assertEqual(incident['occurrences'], 2)

    def test_log_detail_rejects_sensitive_markers_and_luhn_pan_without_leaking(self):
        legal_ref = self.ref + '-legal-mtn'
        legal_id = self.Incident._log(self._snapshot(
            external_ref=legal_ref,
            trade_no='20260830123456789',
            detail='MTN 20260830123456789 reconciliation attempt 2',
        ))
        self.assertIsInstance(
            legal_id, int,
            '合法 MTN／TradeNo 摘要不得被 PAN heuristic 誤殺',
        )

        sensitive_details = (
            'ResultData=secret-payload-864',
            'card4no: 1111',
            'CARD6NO=411111',
            'CardNo 4111111111111111',
            'cvv: 123',
            'PAN=4111111111111111',
            'unlabelled 4111111111111111',
        )
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        for index, detail in enumerate(sensitive_details):
            with self.subTest(detail=detail), self.assertLogs(
                    logger, level='ERROR') as logs:
                result = self.Incident._log(self._snapshot(
                    external_ref='%s-sensitive-%s' % (self.ref, index),
                    detail=detail,
                ))
                self.assertFalse(result)
                self.assertNotIn(
                    detail, '\n'.join(logs.output),
                    '拒絕 log 只能記 field/category，不得回印敏感值',
                )
        self.assertFalse(self.Incident.sudo().search([
            ('external_ref', 'like', self.ref + '-sensitive-%'),
        ]))

    def test_log_kind_mismatch_is_fail_soft_and_keeps_original(self):
        incident_id = self.Incident._log(self._snapshot())
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.billing_incident',
                level='CRITICAL') as logs:
            result = self.Incident._log(self._snapshot(kind='bind_failed'))
        self.assertFalse(result)
        incident = self._read(incident_id)
        self.assertEqual(incident['kind'], 'charged_not_credited')
        self.assertEqual(incident['occurrences'], 1)
        self.assertIn('kind mismatch', '\n'.join(logs.output))

    def test_resolve_is_idempotent_missing_safe_and_resolved_log_never_reopens(self):
        self.assertFalse(self.Incident._resolve(
            'missing-%s' % self.ref,
            resolution_note='nothing to resolve',
        ))
        incident_id = self.Incident._log(self._snapshot(state='needs_human'))
        resolved_id = self.Incident._resolve(
            self.ref,
            resolution_note='system reconciliation completed',
            resolved_by_ref=99,
        )
        again_id = self.Incident._resolve(
            self.ref,
            resolution_note='must not overwrite the first audit',
            resolved_by_ref=100,
        )
        duplicate_id = self.Incident._log(self._snapshot(detail='late duplicate'))

        self.assertEqual((resolved_id, again_id, duplicate_id),
                         (incident_id, incident_id, incident_id))
        incident = self._read(incident_id)
        self.assertEqual(incident['state'], 'resolved')
        self.assertEqual(incident['resolution_note'],
                         'system reconciliation completed')
        self.assertEqual(incident['resolved_by_ref'], 99)
        self.assertTrue(incident['resolved_at'])
        self.assertEqual(incident['occurrences'], 1)

    def test_log_failure_never_raises_or_masks_the_caller_error(self):
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('connection pool exhausted')):
            with self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.billing_incident',
                    level='CRITICAL'):
                self.assertFalse(self.Incident._log(self._snapshot()))

    def _activity_user_ids(self, incident_id):
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT user_id FROM mail_activity WHERE res_model = %s "
                "AND res_id = %s ORDER BY user_id",
                ('woow.billing.incident', incident_id),
            )
            return [row[0] for row in cr.fetchall()]

    def _activity_type_ids(self, incident_id):
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT activity_type_id FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s '
                'ORDER BY activity_type_id',
                ('woow.billing.incident', incident_id),
            )
            return [row[0] for row in cr.fetchall()]

    def _insert_other_activity(self, incident_id):
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            todo = env.ref('mail.mail_activity_data_todo')
            other_type = env['mail.activity.type'].search([
                ('id', '!=', todo.id),
            ], limit=1)
            manager = self._account_manager_users_for_test(env)[:1]
            model = env['ir.model']._get('woow.billing.incident')
            self.assertTrue(other_type and manager and model)
            cr.execute(
                'INSERT INTO mail_activity '
                '(res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, summary, note, date_deadline, '
                'automated, active, create_uid, write_uid, create_date, '
                'write_date) VALUES '
                '(%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, '
                'false, true, %s, %s, NOW(), NOW())',
                (
                    model.id, 'woow.billing.incident', incident_id,
                    'other activity', other_type.id, manager.id,
                    'Unrelated activity', 'Must survive Todo cleanup',
                    1, 1,
                ),
            )
            cr.commit()
            return other_type.id

    def _account_manager_users_for_test(self, env):
        return env.ref('account.group_account_manager').users

    def test_needs_human_activity_is_per_manager_deduplicated_and_resolved(self):
        manager_ids = self.env.ref(
            'account.group_account_manager').users.ids
        self.assertTrue(
            manager_ids,
            '前置：sandbox 的 account manager 群組至少要有一位使用者',
        )
        incident_id = self.Incident._log(
            self._snapshot(state='needs_human'))
        self.assertEqual(
            self._activity_user_ids(incident_id), sorted(manager_ids))

        self.Incident._log(self._snapshot(state='needs_human'))
        self.assertEqual(
            self._activity_user_ids(incident_id), sorted(manager_ids),
            '同 incident/user 的 Todo 不得因 duplicate _log 重建',
        )
        other_ref = self.ref + '-other'
        other_id = self.Incident._log(self._snapshot(
            external_ref=other_ref,
            state='needs_human',
        ))
        self.assertEqual(
            self._activity_user_ids(other_id), sorted(manager_ids),
            '前置：另一筆 incident 也要有自己的 Todo',
        )

        self.assertEqual(
            self.Incident._resolve(
                self.ref, resolution_note='activity cleanup probe'),
            incident_id,
        )
        self.assertEqual(
            self._activity_user_ids(incident_id), [],
            'resolved 只應清掉該 incident 的 activities',
        )
        self.assertEqual(
            self._activity_user_ids(other_id), sorted(manager_ids),
            'resolve 不得清掉其他 incident 的 activities',
        )

    def test_resolve_cleanup_keeps_non_todo_activity_on_same_incident(self):
        incident_id = self.Incident._log(self._snapshot(state='needs_human'))
        todo_id = self.env.ref('mail.mail_activity_data_todo').id
        other_type_id = self._insert_other_activity(incident_id)
        self.assertIn(todo_id, self._activity_type_ids(incident_id))
        self.assertIn(other_type_id, self._activity_type_ids(incident_id))

        self.assertEqual(
            self.Incident._resolve(
                self.ref,
                resolution_note='Todo-only cleanup regression',
            ),
            incident_id,
        )
        self.assertEqual(
            self._activity_type_ids(incident_id), [other_type_id],
            'resolve 只能刪本機制建立的 Todo，不得刪其他 activity type',
        )

    def test_activity_html_escapes_untrusted_reference_and_plain_text_outputs(self):
        malicious_ref = (
            self.ref + '-<img src=x onerror=alert(864)>')
        incident_id = self.Incident._log(self._snapshot(
            external_ref=malicious_ref,
            state='needs_human',
        ))
        self.assertIsInstance(incident_id, int)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT note, res_name FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s',
                ('woow.billing.incident', incident_id),
            )
            activities = cr.fetchall()
        self.assertTrue(activities, '惡意 ref 的人工 incident 仍應建立 Todo')
        for note, res_name in activities:
            self.assertNotIn('<img', note.lower())
            self.assertNotIn('<img', res_name.lower())
            self.assertIn('&lt;img', note.lower())
            self.assertIn('&lt;img', res_name.lower())

        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.billing_incident',
                level='CRITICAL') as logs:
            self.assertFalse(self.Incident._log(self._snapshot(
                external_ref=malicious_ref,
                kind='bind_failed',
            )))
        self.assertNotIn(
            '<img', '\n'.join(logs.output).lower(),
            'plain-text log 不得輸出可被 HTML renderer 執行的 raw markup',
        )

    def test_activity_failure_is_fail_soft_after_incident_commit(self):
        target = (
            'odoo.addons.woow_paas_platform.models.billing_incident.'
            'BillingIncident._schedule_review_activities'
        )
        with patch(target, side_effect=RuntimeError('mail activity unavailable')):
            with self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.billing_incident',
                    level='ERROR'):
                incident_id = self.Incident._log(
                    self._snapshot(state='orphan'))
        self.assertIsInstance(incident_id, int)
        self.assertEqual(self._read(incident_id)['state'], 'orphan')

    def test_zero_account_managers_logs_error_without_losing_incident(self):
        target = (
            'odoo.addons.woow_paas_platform.models.billing_incident.'
            'BillingIncident._account_manager_users'
        )
        with patch(target, return_value=self.env['res.users']):
            with self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.billing_incident',
                    level='ERROR') as logs:
                incident_id = self.Incident._log(
                    self._snapshot(state='needs_human'))
        self.assertIsInstance(incident_id, int)
        self.assertIn('zero users', '\n'.join(logs.output))
        self.assertEqual(self._read(incident_id)['state'], 'needs_human')

    def test_log_arms_lock_timeout_before_touching_the_incident_table(self):
        from odoo.modules.registry import Registry

        real_cursor = Registry.cursor
        seen = []

        def _spy(registry, *args, **kwargs):
            cr = real_cursor(registry, *args, **kwargs)
            original = cr.execute

            def execute(query, params=None, log_exceptions=True):
                seen.append((str(query), params))
                return original(query, params, log_exceptions=log_exceptions)

            cr.execute = execute
            return cr

        with patch('odoo.modules.registry.Registry.cursor', _spy):
            self.assertTrue(self.Incident._log(self._snapshot()))
        self.assertTrue(seen)
        self.assertIn('lock_timeout', seen[0][0])
        self.assertEqual(seen[0][1], (self.Incident._LOCK_TIMEOUT,))


@tagged('post_install', '-at_install')
class TestBillingIncidentAdminBackend864(TransactionCase):

    def setUp(self):
        super().setUp()
        marker = uuid.uuid4().hex
        self.manager = new_test_user(
            self.env,
            login='billing-manager-%s' % marker,
            groups='account.group_account_manager',
        )
        self.regular_user = new_test_user(
            self.env,
            login='billing-regular-%s' % marker,
            groups='base.group_user',
        )
        self.wallet_partner = self.env['res.partner'].create({
            'name': 'Billing incident wallet probe %s' % marker,
        })
        self.wallet_card = self.wallet_partner.get_wallet_loyalty_card()
        self.wallet_card.points = 123.0
        self.incident = self.env['woow.billing.incident'].sudo().create({
            'external_ref': 'manual-%s' % marker,
            'kind': 'charged_not_credited',
            'state': 'needs_human',
            'partner_ref': self.wallet_partner.id,
            'partner_email': 'manual@example.test',
            'amount': 800.0,
            'credits': 800.0,
            'bonus': 80.0,
            'currency': 'TWD',
            'trade_no': 'TRADE-MANUAL',
            'rtn_code': '1',
            'assessment_reason': 'manual_review_required',
        })

    def test_acl_is_account_manager_read_write_only(self):
        record = self.incident.with_user(self.manager)
        self.assertEqual(record.read(['external_ref'])[0]['external_ref'],
                         self.incident.external_ref)
        record.write({
            'manual_check_result': 'ecpay_found',
            'resolution_note': 'draft manager note',
        })
        self.assertEqual(self.incident.manual_check_result, 'ecpay_found')
        self.assertEqual(self.incident.resolution_note, 'draft manager note')

        immutable_before = record.read([
            'external_ref', 'kind', 'state', 'trade_no', 'detail',
            'resolved_at', 'resolved_by_ref',
        ])[0]
        forbidden_writes = (
            {'external_ref': 'forbidden-ref'},
            {'kind': 'bind_failed'},
            {'state': 'open'},
            {'trade_no': 'FORGED-TRADE'},
            {'detail': 'forged evidence'},
            {'resolved_at': '2026-08-31 00:00:00'},
            {'resolved_by_ref': self.manager.id},
        )
        for values in forbidden_writes:
            with self.subTest(values=values), self.assertRaises(AccessError):
                record.write(values)
        self.assertEqual(
            record.read(list(immutable_before))[0], immutable_before,
            '被拒絕的 audit snapshot 寫入不得留下任何 DB 變更',
        )

        with self.assertRaises(AccessError):
            self.env['woow.billing.incident'].with_user(self.manager).create({
                'external_ref': 'forbidden-create-%s' % uuid.uuid4().hex,
                'kind': 'intent',
            })
        with self.assertRaises(AccessError):
            record.unlink()
        with self.assertRaises(AccessError):
            self.incident.with_user(self.regular_user).read(['external_ref'])
        with self.assertRaises(AccessError):
            self.incident.with_user(
                self.regular_user).action_mark_manually_resolved()

    def test_manual_resolution_requires_ecpay_result_and_reason(self):
        record = self.incident.with_user(self.manager)
        with self.assertRaises(UserError):
            record.action_mark_manually_resolved()
        record.manual_check_result = 'ecpay_found'
        with self.assertRaises(UserError):
            record.action_mark_manually_resolved()
        record.resolution_note = 'ECPay 後台確認 MTN 存在；僅記錄查證並關單。'

        # UI action must stay on the current cursor; calling the public
        # independent-cursor `_resolve` here would self-lock this edited row.
        wallet_tx_count = self.env['wallet.transaction'].search_count([
            ('partner_id', '=', self.wallet_partner.id),
        ])
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('must not open a new cursor')):
            result = record.action_mark_manually_resolved()
        self.assertTrue(result)
        self.wallet_card.invalidate_recordset(['points'])
        self.assertEqual(
            self.wallet_card.points, 123.0,
            '人工已處理只記查證／關單，不得修改 wallet points',
        )
        self.assertEqual(
            self.env['wallet.transaction'].search_count([
                ('partner_id', '=', self.wallet_partner.id),
            ]),
            wallet_tx_count,
            '人工已處理不得建立任何補點／退款 wallet ledger',
        )
        self.assertEqual(record.state, 'resolved')
        self.assertTrue(record.resolved_at)
        self.assertEqual(record.resolved_by_ref, self.manager.id)
        first_note = record.resolution_note
        first_resolved_at = record.resolved_at

        with self.assertRaises(UserError):
            record.action_mark_manually_resolved()
        self.assertEqual(record.resolution_note, first_note)
        self.assertEqual(record.resolved_at, first_resolved_at)

    def test_manual_resolution_keeps_other_activity_type(self):
        record = self.incident.with_user(self.manager)
        record.write({
            'manual_check_result': 'ecpay_not_found',
            'resolution_note': 'current-cursor Todo-only cleanup',
        })
        todo = self.env.ref('mail.mail_activity_data_todo')
        other_type = self.env['mail.activity.type'].search([
            ('id', '!=', todo.id),
        ], limit=1)
        model = self.env['ir.model']._get('woow.billing.incident')
        for activity_type in (todo, other_type):
            self.env.cr.execute(
                'INSERT INTO mail_activity '
                '(res_model_id, res_model, res_id, res_name, '
                'activity_type_id, user_id, summary, note, date_deadline, '
                'automated, active, create_uid, write_uid, create_date, '
                'write_date) VALUES '
                '(%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, '
                'false, true, %s, %s, NOW(), NOW())',
                (
                    model.id, 'woow.billing.incident', record.id,
                    'manual cleanup', activity_type.id, self.manager.id,
                    'Current cursor cleanup', 'Activity type probe',
                    1, 1,
                ),
            )

        self.assertTrue(record.action_mark_manually_resolved())
        self.env.cr.execute(
            'SELECT activity_type_id FROM mail_activity '
            'WHERE res_model = %s AND res_id = %s ORDER BY activity_type_id',
            ('woow.billing.incident', record.id),
        )
        self.assertEqual(
            [row[0] for row in self.env.cr.fetchall()], [other_type.id],
            'current-cursor transition 也只能刪本機制 Todo',
        )

    def test_billing_incident_view_sources_and_zh_tw_translations(self):
        form_arch = etree.fromstring(self.env.ref(
            'woow_paas_platform.view_billing_incident_form').arch.encode())
        source = etree.tostring(form_arch, encoding='unicode')
        self.assertIsNone(
            re.search(r'[\u3400-\u9fff]', source),
            'view source 必須是英文；繁中只能放在 zh_TW.po',
        )

        expected = {
            'Mark as manually resolved': '人工已處理',
            (
                'The top-up record may have been rolled back; not finding it '
                'in Odoo is expected and is not a reason to close the incident.'
            ): (
                'topup 記錄可能已被回捲、在 Odoo 裡查不到是預期現象，'
                '不是關單理由'
            ),
            (
                'Billing incidents are a global finance ledger. Every account '
                'manager can read every incident across companies by design.'
            ): (
                '帳務 incident 是全域財務帳本；所有 account manager '
                '都可刻意跨公司讀取全部 incident。'
            ),
        }
        button = form_arch.xpath(
            "//button[@name='action_mark_manually_resolved']")[0]
        self.assertEqual(button.get('string'), 'Mark as manually resolved')
        form_text = ' '.join(''.join(form_arch.itertext()).split())
        for msgid in expected:
            if msgid != 'Mark as manually resolved':
                self.assertIn(msgid, form_text)

        po_path = Path(__file__).resolve().parents[1] / 'i18n' / 'zh_TW.po'
        translations = {
            entry.msgid: entry.msgstr for entry in polib.pofile(str(po_path))
        }
        for msgid, msgstr in expected.items():
            self.assertEqual(
                translations.get(msgid), msgstr,
                'zh_TW.po 缺精確翻譯：%s' % msgid,
            )

    def test_backend_views_action_and_menu_are_manager_only_readonly_snapshot(self):
        manager_group = self.env.ref('account.group_account_manager')
        action = self.env.ref('woow_paas_platform.action_billing_incident')
        menu = self.env.ref('woow_paas_platform.menu_billing_incidents')
        root = self.env.ref('woow_paas_platform.menu_woow_paas_platform_root')
        search = self.env.ref(
            'woow_paas_platform.view_billing_incident_search')

        self.assertEqual(action.res_model, 'woow.billing.incident')
        self.assertEqual(action.view_mode, 'list,form')
        self.assertEqual(action.search_view_id, search)
        self.assertEqual(action.groups_id, manager_group)
        self.assertEqual(menu.groups_id, manager_group)
        self.assertEqual(menu.parent_id, root)
        context = ast.literal_eval(action.context or '{}')
        self.assertFalse(context.get('create', True))
        self.assertFalse(context.get('delete', True))

        list_arch = etree.fromstring(self.env.ref(
            'woow_paas_platform.view_billing_incident_list').arch.encode())
        form_arch = etree.fromstring(self.env.ref(
            'woow_paas_platform.view_billing_incident_form').arch.encode())
        for arch in (list_arch, form_arch):
            self.assertEqual(arch.get('create'), 'false')
            self.assertEqual(arch.get('delete'), 'false')

        warning = (
            'The top-up record may have been rolled back; not finding it in '
            'Odoo is expected and is not a reason to close the incident.'
        )
        self.assertIn(warning, ''.join(form_arch.itertext()))
        buttons = form_arch.xpath(
            "//button[@name='action_mark_manually_resolved']")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].get('type'), 'object')

        snapshot_fields = {
            'external_ref', 'kind', 'partner_ref', 'partner_email',
            'payment_token_ref', 'amount', 'credits', 'bonus', 'currency',
            'trade_no', 'rtn_code', 'failure_code', 'state', 'skip_reason',
            'last_skipped_at', 'skip_count', 'attempts', 'next_query_at',
            'query_attempts', 'first_seen_at', 'last_seen_at', 'occurrences',
            'assessment_reason', 'detail', 'source_model', 'source_ref',
            'resolved_at', 'resolved_by_ref',
        }
        for name in snapshot_fields:
            nodes = form_arch.xpath("//field[@name='%s']" % name)
            self.assertTrue(nodes, 'form 缺 snapshot 欄位 %s' % name)
            self.assertTrue(
                all(node.get('readonly') == '1' for node in nodes),
                'snapshot 欄位 %s 必須 readonly' % name,
            )


@tagged('post_install', '-at_install')
class TestBillingIncidentRpcWriteGuard864(HttpCase):
    """Audit writes and global-ledger reads through the real web RPC seam."""

    def setUp(self):
        super().setUp()
        marker = uuid.uuid4().hex
        manager_group = self.env.ref('account.group_account_manager')
        user_group = self.env.ref('base.group_user')
        self.company_a = self.env.company
        self.company_b = self.env['res.company'].sudo().create({
            'name': 'Billing global ledger company B %s' % marker,
            # sh_subscription makes this field SQL NOT NULL but does not
            # provide a create default for additional companies.
            'sh_journal_id': self.company_a.sh_journal_id.id,
        })
        self.manager_a, self.manager_a_login = self._new_user(
            'billing-rpc-manager-a-%s' % marker,
            manager_group,
            self.company_a,
        )
        self.manager_b, self.manager_b_login = self._new_user(
            'billing-rpc-manager-b-%s' % marker,
            manager_group,
            self.company_b,
        )
        self.regular_user, self.regular_login = self._new_user(
            'billing-rpc-regular-%s' % marker,
            user_group,
            self.company_a,
        )
        Incident = self.env['woow.billing.incident'].sudo()
        self.incident_a = Incident.create({
            'external_ref': 'rpc-a-%s' % marker,
            'kind': 'charged_not_credited',
            'state': 'needs_human',
            'source_model': 'res.company',
            'source_ref': self.company_a.id,
            'trade_no': 'RPC-TRADE-A',
            'detail': 'company A safe summary',
        })
        self.incident_b = Incident.create({
            'external_ref': 'rpc-b-%s' % marker,
            'kind': 'charged_not_credited',
            'state': 'orphan',
            'source_model': 'res.company',
            'source_ref': self.company_b.id,
            'trade_no': 'RPC-TRADE-B',
            'detail': 'company B safe summary',
        })

    def _new_user(self, login, group, company):
        user = self.env['res.users'].sudo().with_context(
            no_reset_password=True).create({
                'name': login,
                'login': login,
                'password': login,
                'email': '%s@example.test' % login,
                'company_id': company.id,
                'company_ids': [(6, 0, [company.id])],
                'groups_id': [(6, 0, [group.id])],
            })
        return user, login

    def _call_kw(self, model, method, args, kwargs=None):
        body = json.dumps({
            'jsonrpc': '2.0',
            'method': 'call',
            'params': {
                'model': model,
                'method': method,
                'args': args,
                'kwargs': kwargs or {},
            },
        })
        response = self.url_open(
            '/web/dataset/call_kw',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        self.env.invalidate_all()
        return response.json()

    def _assert_ok(self, result, message):
        self.assertNotIn('error', result, '%s（回 %r）' % (message, result))
        return result.get('result')

    def _assert_access_error(self, result, message):
        self.assertIn('error', result, '%s（回 %r）' % (message, result))
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('AccessError', name, '%s（回 %r）' % (message, result))

    def _audit_snapshot(self, incident):
        incident.invalidate_recordset()
        return incident.read([
            'external_ref', 'kind', 'state', 'trade_no', 'detail',
            'resolved_at', 'resolved_by_ref',
        ])[0]

    def test_manager_rpc_can_write_manual_fields_only_and_denials_are_atomic(self):
        self.authenticate(self.manager_a_login, self.manager_a_login)
        result = self._call_kw(
            'woow.billing.incident',
            'write',
            [[self.incident_a.id], {
                'manual_check_result': 'ecpay_found',
                'resolution_note': 'manager RPC verification draft',
            }],
        )
        self.assertTrue(self._assert_ok(
            result, 'account manager 應可經 RPC 保存兩個人工欄位'))
        self.incident_a.invalidate_recordset()
        self.assertEqual(self.incident_a.manual_check_result, 'ecpay_found')
        self.assertEqual(
            self.incident_a.resolution_note,
            'manager RPC verification draft',
        )

        before = self._audit_snapshot(self.incident_a)
        forbidden_writes = (
            {'state': 'open'},
            {'external_ref': 'rpc-forged-ref'},
            {'kind': 'bind_failed'},
            {'trade_no': 'RPC-FORGED-TRADE'},
            {'detail': 'forged audit detail'},
            {'resolved_at': '2026-08-31 00:00:00'},
            {'resolved_by_ref': self.manager_a.id},
        )
        for values in forbidden_writes:
            with self.subTest(values=values):
                denied = self._call_kw(
                    'woow.billing.incident',
                    'write',
                    [[self.incident_a.id], values],
                )
                self._assert_access_error(
                    denied,
                    'account manager 不得經 RPC 改 audit snapshot／state',
                )
                self.assertEqual(
                    self._audit_snapshot(self.incident_a), before,
                    '被拒絕的 RPC write 不得留下局部 DB 變更',
                )

        self.authenticate(self.regular_login, self.regular_login)
        denied = self._call_kw(
            'woow.billing.incident',
            'read',
            [[self.incident_a.id], ['external_ref']],
        )
        self._assert_access_error(denied, '非 account manager 不得讀 incident')

    def test_two_company_limited_managers_read_global_ledger_by_design(self):
        incident_ids = [self.incident_a.id, self.incident_b.id]
        expected_refs = {
            self.incident_a.external_ref,
            self.incident_b.external_ref,
        }
        for login in (self.manager_a_login, self.manager_b_login):
            with self.subTest(login=login):
                self.authenticate(login, login)
                rows = self._assert_ok(
                    self._call_kw(
                        'woow.billing.incident',
                        'read',
                        [incident_ids, ['external_ref']],
                    ),
                    'company-limited account manager 應讀到全域財務帳本',
                )
                self.assertEqual(
                    {row['external_ref'] for row in rows}, expected_refs,
                    '跨 company 全讀是刻意的 global-ledger 契約',
                )

        self.authenticate(self.manager_a_login, self.manager_a_login)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'woow.billing.incident',
                'write',
                [[self.incident_b.id], {
                    'resolution_note': 'cross-company manual audit note',
                }],
            ),
            'global ledger 上的人工欄位也可由任一 account manager 保存',
        ))
        denied = self._call_kw(
            'woow.billing.incident',
            'write',
            [[self.incident_b.id], {'state': 'open'}],
        )
        self._assert_access_error(
            denied,
            'global read/write reach 不得繞過 audit write allowlist',
        )
        self.incident_b.invalidate_recordset()
        self.assertEqual(self.incident_b.state, 'orphan')
