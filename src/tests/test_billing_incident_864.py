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

from odoo import fields
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
        automatic_fields = {
            'id', 'create_uid', 'write_uid', 'create_date', 'write_date',
            'display_name', '__last_update',
        }
        mail_infrastructure_fields = set(self.env['mail.thread']._fields)
        business_fields = (
            set(Incident._fields)
            - automatic_fields
            - mail_infrastructure_fields
        )
        # PR-D is an evolvable foundation: lock the required scalar contract,
        # not a future-exact business schema.  The all-field relational scan
        # below remains exhaustive, so a later source FK still fails loudly.
        self.assertTrue(
            set(expected_types).issubset(business_fields),
            'PR-D required source/business scalar subset must remain present',
        )
        self.assertEqual(
            {
                name: Incident._fields[name].type
                for name in expected_types
            },
            expected_types,
        )
        relational = {'many2one', 'one2many', 'many2many'}
        allowed_relational_infrastructure = {
            'create_uid',
            'write_uid',
            'message_follower_ids',
            'message_ids',
            'message_partner_ids',
            'rating_ids',
            'website_message_ids',
        }
        unexpected_relations = {
            name: field.type
            for name, field in Incident._fields.items()
            if (field.type in relational
                and name not in allowed_relational_infrastructure)
        }
        self.assertFalse(
            unexpected_relations,
            '只允許 Odoo audit/mail infrastructure relations；任何新增 '
            'source/business M2O/O2M/M2M 都必須紅',
        )
        self.assertTrue(
            self.env['ir.model']._get('woow.billing.incident').is_mail_thread,
            'mail.thread metadata 是 standard mail.activity contract，並非 source FK',
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

    def test_enum_selections_are_single_source_for_validators(self):
        Incident = self.env['woow.billing.incident']
        expected = {
            'kind': (
                ('intent', 'Payment intent'),
                ('charged_not_credited', 'Charged but not credited'),
                ('callback_tx_missing', 'Callback transaction missing'),
                ('invoice_half_settled', 'Invoice half settled'),
                ('bind_failed', 'Card binding failed'),
                ('stuck_pending', 'Stuck pending payment'),
                ('manual_invoice_stale', 'Manual invoice stale'),
            ),
            'state': (
                ('open', 'Open'),
                ('resolved', 'Resolved'),
                ('orphan', 'Orphan'),
                ('needs_human', 'Needs human review'),
            ),
            'manual_check_result': (
                ('ecpay_found', 'Found in ECPay'),
                ('ecpay_not_found', 'Not found in ECPay'),
            ),
        }
        constants = {
            'kind': Incident._KIND_SELECTION,
            'state': Incident._STATE_SELECTION,
            'manual_check_result': Incident._MANUAL_RESULT_SELECTION,
        }
        for field_name, expected_selection in expected.items():
            with self.subTest(field_name=field_name):
                self.assertEqual(constants[field_name], expected_selection)
                self.assertEqual(
                    tuple(Incident._fields[field_name].selection),
                    constants[field_name],
                )
        self.assertEqual(
            Incident._KINDS,
            frozenset(key for key, _label in Incident._KIND_SELECTION),
        )
        self.assertEqual(
            Incident._LOG_STATES,
            frozenset(key for key, _label in Incident._STATE_SELECTION)
            - {'resolved'},
        )
        self.assertEqual(
            Incident._MANUAL_RESULTS,
            frozenset(
                key for key, _label in Incident._MANUAL_RESULT_SELECTION),
        )

    def test_review_activity_type_is_model_restricted_when_core_supports_it(self):
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        supported_models = dict(
            review_type._fields['res_model']._description_selection(self.env))
        self.assertIn('woow.billing.incident', supported_models)
        self.assertEqual(review_type.name, 'Billing incident review')
        self.assertEqual(review_type.res_model, 'woow.billing.incident')

    def test_external_ref_is_database_unique(self):
        Incident = self.env['woow.billing.incident']
        ref = 'schema-%s' % uuid.uuid4().hex
        Incident._create_test_fixture({
            'external_ref': ref,
            'kind': 'intent',
        })
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Incident._create_test_fixture({
                    'external_ref': ref,
                    'kind': 'intent',
                })
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_database_constraints_reject_invalid_counters_and_time_order(self):
        Incident = self.env['woow.billing.incident']
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
                        Incident._create_test_fixture(values)
                self.env.cr.execute('SELECT 1')
                self.assertEqual(self.env.cr.fetchone()[0], 1)
                executed.append(index)
        self.assertEqual(executed, list(range(len(cases))))
        valid = Incident._create_test_fixture({
            'external_ref': 'constraint-valid-%s' % uuid.uuid4().hex,
            'kind': 'intent',
        })
        self.assertTrue(valid)

    def test_database_requires_resolution_timestamp_for_resolved_state(self):
        Incident = self.env['woow.billing.incident']
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                Incident._create_test_fixture({
                    'external_ref': 'constraint-resolved-%s' % uuid.uuid4().hex,
                    'kind': 'intent',
                    'state': 'resolved',
                })
        valid = Incident._create_test_fixture({
            'external_ref': 'constraint-resolved-valid-%s' % uuid.uuid4().hex,
            'kind': 'intent',
            'state': 'resolved',
            'resolved_at': fields.Datetime.now(),
        })
        self.assertEqual(valid.state, 'resolved')
        self.assertTrue(valid.resolved_at)

    def test_generic_sudo_create_cannot_forge_any_ledger_row(self):
        Incident = self.env['woow.billing.incident'].sudo()
        for values in (
                {
                    'external_ref': 'forged:%s' % uuid.uuid4().hex,
                    'kind': 'intent',
                },
                {
                    'external_ref': 'forged-resolved:%s' % uuid.uuid4().hex,
                    'kind': 'intent',
                    'state': 'resolved',
                }):
            with self.subTest(values=values), self.assertRaises(AccessError):
                Incident.create(values)


@tagged('post_install', '-at_install')
class TestBillingIncidentPublicApi864(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Incident = self.env['woow.billing.incident']
        self.ref = 'test:864%s' % uuid.uuid4().hex
        self._extra_cleanup_refs = set()
        self.addCleanup(self._purge)

    def _purge(self):
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM mail_activity WHERE res_model = %s AND res_id IN "
                "(SELECT id FROM woow_billing_incident "
                " WHERE external_ref LIKE %s OR external_ref = ANY(%s))",
                (
                    'woow.billing.incident',
                    self.ref + '%',
                    list(self._extra_cleanup_refs),
                ),
            )
            for table, model_column in (
                    ('mail_followers', 'res_model'),
                    ('mail_message', 'model')):
                cr.execute(
                    'DELETE FROM %s WHERE %s = %%s AND res_id IN '
                    '(SELECT id FROM woow_billing_incident '
                    ' WHERE external_ref LIKE %%s OR external_ref = ANY(%%s))'
                    % (table, model_column),
                    (
                        'woow.billing.incident',
                        self.ref + '%',
                        list(self._extra_cleanup_refs),
                    ),
                )
            cr.execute(
                'DELETE FROM woow_billing_incident '
                'WHERE external_ref LIKE %s OR external_ref = ANY(%s)',
                (self.ref + '%', list(self._extra_cleanup_refs)),
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
            'CVC=123',
            'C.V.C:123',
            'CID: 123',
            'C-I-D=123',
            'CSC/123',
            'card security code 123',
            'security_code=123',
            'security.code:123',
            'S.A.D=forbidden-authentication-data',
            'SAD=forbidden-authentication-data',
            'sensitive authentication data payload',
            'PAN=4111111111111111',
            'unlabelled 4111111111111111',
            'unlabelled 4111.1111-1111 1111',
            'MTN: 4111111111111111',
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

    def test_sensitive_validator_covers_every_log_text_ingress(self):
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        marker = 'ResultData=secret-all-ingress-864'
        for index, field_name in enumerate(sorted(
                self.Incident._LOG_CHAR_FIELDS)):
            with self.subTest(field_name=field_name):
                values = self._snapshot(
                    external_ref='%s-all-text-%s' % (self.ref, index),
                )
                values[field_name] = marker
                with self.assertLogs(logger, level='ERROR') as logs:
                    self.assertFalse(self.Incident._log(values))
                self.assertNotIn(marker, '\n'.join(logs.output))
        self.assertFalse(self.Incident.sudo().search([
            ('external_ref', 'like', self.ref + '-all-text-%'),
        ]))

    def test_invalid_selection_and_datetime_never_leak_or_persist(self):
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        cases = (
            ('kind', 'invalid-kind-4111111111111111'),
            ('state', 'invalid-state-4111111111111111'),
            ('next_query_at', 'invalid-date-ResultData-4111111111111111'),
        )
        for index, (field_name, secret_value) in enumerate(cases):
            external_ref = '%s-invalid-%s' % (self.ref, index)
            with self.subTest(field_name=field_name), self.assertLogs(
                    logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._log(self._snapshot(
                    external_ref=external_ref,
                    **{field_name: secret_value},
                )))
            self.assertNotIn(
                secret_value,
                '\n'.join(logs.output),
                '非法輸入 log 只能記 field/category/type，不得回印原值',
            )
            self.assertFalse(self.Incident.sudo().search([
                ('external_ref', '=', external_ref),
            ]))

    def test_provider_reference_shape_allows_raw_mtn_and_namespaced_ref(self):
        raw_mtn = '20260830123456789012'
        self._extra_cleanup_refs.add(raw_mtn)
        raw_id = self.Incident._log(self._snapshot(
            external_ref=raw_mtn,
            trade_no='20260830123456789012',
            detail='safe provider reconciliation summary',
        ))
        self.assertIsInstance(raw_id, int)
        self.assertEqual(
            self._read(raw_id)['trade_no'], '20260830123456789012')

        luhn_coincidence_mtn = '4111111111111111'
        self._extra_cleanup_refs.add(luhn_coincidence_mtn)
        self.assertIsInstance(self.Incident._log(self._snapshot(
            external_ref=luhn_coincidence_mtn,
            trade_no=luhn_coincidence_mtn,
            detail='dedicated provider references may be numeric',
        )), int)

        namespaced_ref = 'invoice:2026_08-30/abc.DEF'
        self._extra_cleanup_refs.add(namespaced_ref)
        namespaced_id = self.Incident._log(self._snapshot(
            external_ref=namespaced_ref,
            trade_no='TRADE-20260830_ABC.1',
        ))
        self.assertIsInstance(namespaced_id, int)

    def test_provider_references_reject_crlf_controls_bad_shape_and_markers(self):
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        cases = (
            ('external_ref', 'MTN: 4111111111111111'),
            ('external_ref', 'invoice:bad\r\nforged-log-line'),
            ('external_ref', 'invoice:bad\x01control'),
            ('external_ref', '123456789012345678901'),
            ('external_ref', 'invoice:<script>'),
            ('trade_no', 'TRADE\r\nFORGED'),
            ('trade_no', 'TRADE WITH SPACE'),
            ('trade_no', 'security-code-123'),
        )
        for index, (field_name, invalid_value) in enumerate(cases):
            external_ref = '%s-invalid-provider-%s' % (self.ref, index)
            values = self._snapshot(external_ref=external_ref)
            values[field_name] = invalid_value
            with self.subTest(field_name=field_name, value=invalid_value), \
                    self.assertLogs(logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._log(values))
            output = '\n'.join(logs.output)
            self.assertNotIn(invalid_value, output)
            self.assertIn('field=%s' % field_name, output)
            self.assertIn('category=', output)

        invalid_resolve_ref = 'invoice:lookup\r\nforged'
        with self.assertLogs(logger, level='ERROR') as logs:
            self.assertFalse(self.Incident._resolve(
                invalid_resolve_ref,
                resolution_note='safe note',
            ))
        self.assertNotIn(invalid_resolve_ref, '\n'.join(logs.output))

    def test_resolve_rejects_sensitive_note_without_leak_or_db_change(self):
        incident_id = self.Incident._log(self._snapshot(state='needs_human'))
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        secret_notes = (
            'MTN: 4111111111111111',
            'CVC=123',
            'CID: 123',
            'card security code 123',
            'SAD=forbidden-authentication-data',
        )
        for secret_note in secret_notes:
            with self.subTest(note=secret_note), self.assertLogs(
                    logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._resolve(
                    self.ref,
                    resolution_note=secret_note,
                    resolved_by_ref=99,
                ))
            self.assertNotIn(secret_note, '\n'.join(logs.output))
        incident = self._read(incident_id)
        self.assertEqual(incident['state'], 'needs_human')
        self.assertFalse(incident['resolution_note'])
        self.assertFalse(incident['resolved_at'])

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
        output = '\n'.join(logs.output)
        self.assertIn('field=kind', output)
        self.assertIn('category=collision', output)
        self.assertIn('incident id=%s' % incident_id, output)
        self.assertNotIn(self.ref, output)
        self.assertNotIn('fingerprint=', output)

    def test_resolve_lookup_sql_failure_never_logs_raw_reference_or_fingerprint(self):
        from odoo.modules.registry import Registry

        raw_ref = 'lookup:%s' % uuid.uuid4().hex
        real_cursor = Registry.cursor
        lookup_calls = []

        def _cursor_with_failure(registry, *args, **kwargs):
            cr = real_cursor(registry, *args, **kwargs)
            original = cr.execute

            def execute(query, params=None, log_exceptions=True):
                query_params = getattr(query, 'params', params) or ()
                if raw_ref in repr(query_params):
                    lookup_calls.append((str(query), log_exceptions))
                    return original(
                        'SELECT %s::integer',
                        (raw_ref,),
                        log_exceptions=log_exceptions,
                    )
                return original(
                    query, params, log_exceptions=log_exceptions)

            cr.execute = execute
            return cr

        with patch('odoo.modules.registry.Registry.cursor',
                   _cursor_with_failure), self.assertLogs(
                level='ERROR') as logs:
            self.assertFalse(self.Incident._resolve(
                raw_ref,
                resolution_note='safe lookup failure note',
            ))
        output = '\n'.join(logs.output)
        self.assertTrue(lookup_calls, 'test must intercept the ref lookup SQL')
        self.assertTrue(
            all(log_exceptions is False
                for _query, log_exceptions in lookup_calls),
            'raw parameterized external_ref lookup must disable SQL mogrify logs',
        )
        self.assertEqual(output.count(raw_ref), 0)
        self.assertNotIn('fingerprint=', output)
        self.assertIn('field=external_ref', output)
        self.assertIn('category=lookup_failed', output)

    def test_resolve_is_idempotent_missing_safe_and_resolved_log_never_reopens(self):
        self.assertFalse(self.Incident._resolve(
            '%s-missing' % self.ref,
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

    def test_idempotent_resolve_retries_failed_activity_cleanup(self):
        incident_id = self.Incident._log(
            self._snapshot(state='needs_human'))
        self.assertTrue(self._activity_user_ids(incident_id, active=True))
        target = (
            'odoo.addons.woow_paas_platform.models.billing_incident.'
            'BillingIncident._clear_review_activities'
        )
        with patch(target, side_effect=RuntimeError('cleanup unavailable')), \
                self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.billing_incident',
                    level='ERROR'):
            self.assertEqual(
                self.Incident._resolve(
                    self.ref,
                    resolution_note='first audit survives cleanup failure',
                    resolved_by_ref=73,
                ),
                incident_id,
            )
        first_audit = self._read(incident_id)
        self.assertEqual(first_audit['state'], 'resolved')
        self.assertTrue(
            self._activity_user_ids(incident_id, active=None),
            'first best-effort cleanup failure must leave a repairable stale row',
        )

        self.assertEqual(
            self.Incident._resolve(
                self.ref,
                resolution_note='must not overwrite first audit',
                resolved_by_ref=74,
            ),
            incident_id,
        )
        repaired = self._read(incident_id)
        self.assertEqual(repaired['resolution_note'],
                         first_audit['resolution_note'])
        self.assertEqual(repaired['resolved_by_ref'],
                         first_audit['resolved_by_ref'])
        self.assertEqual(repaired['resolved_at'], first_audit['resolved_at'])
        self.assertEqual(self._activity_user_ids(incident_id, active=None), [])

    def test_log_failure_never_raises_or_masks_the_caller_error(self):
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('connection pool exhausted')):
            with self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.billing_incident',
                    level='CRITICAL'):
                self.assertFalse(self.Incident._log(self._snapshot()))

    def _activity_user_ids(self, incident_id, active=None):
        with self.env.registry.cursor() as cr:
            query = (
                'SELECT user_id FROM mail_activity WHERE res_model = %s '
                'AND res_id = %s'
            )
            params = ['woow.billing.incident', incident_id]
            if active is not None:
                query += ' AND active = %s'
                params.append(active)
            cr.execute(query + ' ORDER BY user_id', params)
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

    def _set_incident_state(self, incident_id, state):
        with self.env.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_billing_incident SET state = %s WHERE id = %s',
                (state, incident_id),
            )
            cr.commit()

    def _insert_activity(self, incident_id, activity_type_xmlid,
                         summary='Unrelated activity'):
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            activity_type = env.ref(activity_type_xmlid)
            manager = self._account_manager_users_for_test(env)[:1]
            model = env['ir.model']._get('woow.billing.incident')
            self.assertTrue(activity_type and manager and model)
            env['mail.activity'].sudo().create({
                'res_model_id': model.id,
                'res_id': incident_id,
                'activity_type_id': activity_type.id,
                'user_id': manager.id,
                'summary': summary,
                'note': 'Must survive billing review cleanup',
                'date_deadline': fields.Date.today(),
            })
            cr.commit()
            return activity_type.id

    def _insert_other_activity(self, incident_id):
        return self._insert_activity(
            incident_id, 'mail.mail_activity_data_call')

    def _account_manager_users_for_test(self, env):
        return env.ref('account.group_account_manager').users

    def test_review_activity_uses_orm_and_bus_then_standard_done_realerts(self):
        ActivityClass = type(self.env['mail.activity'])
        UsersClass = type(self.env['res.users'])
        original_create = ActivityClass.create
        original_unlink = ActivityClass.unlink
        original_bus_send = UsersClass._bus_send
        calls = {'create': 0, 'unlink': 0, 'bus': []}

        def create_spy(records, values):
            calls['create'] += 1
            return original_create(records, values)

        def unlink_spy(records):
            calls['unlink'] += 1
            return original_unlink(records)

        def bus_spy(users, channel, message):
            calls['bus'].append((channel, message))
            return original_bus_send(users, channel, message)

        with patch.object(ActivityClass, 'create', create_spy), \
                patch.object(ActivityClass, 'unlink', unlink_spy), \
                patch.object(UsersClass, '_bus_send', bus_spy):
            incident_id = self.Incident._log(self._snapshot(
                external_ref=self.ref + '-orm-action-done',
                state='needs_human',
            ))
            self.assertGreater(calls['create'], 0)
            self.assertTrue(any(
                channel == 'mail.activity/updated'
                and message.get('activity_created')
                for channel, message in calls['bus']
            ))

            with self.env.registry.cursor() as cr:
                env = self.env(cr=cr)
                review_type = env.ref(
                    'woow_paas_platform.'
                    'mail_activity_type_billing_incident_review')
                activity = env['mail.activity'].sudo().search([
                    ('res_model', '=', 'woow.billing.incident'),
                    ('res_id', '=', incident_id),
                    ('activity_type_id', '=', review_type.id),
                    ('active', '=', True),
                ], limit=1)
                self.assertTrue(activity)
                activity.action_done()
                incident = env['woow.billing.incident'].browse(incident_id)
                self.assertEqual(
                    incident.state,
                    'needs_human',
                    'standard activity done 只完成待辦，不得關閉 incident',
                )
                self.assertTrue(env['mail.activity'].sudo().search([
                    ('res_model', '=', 'woow.billing.incident'),
                    ('res_id', '=', incident_id),
                    ('activity_type_id', '=', review_type.id),
                    ('active', '=', True),
                ]), 'done 同一 transaction 結束前必須重建 active alert')
                cr.commit()
            self.assertGreater(calls['unlink'], 0)

            self.assertEqual(
                self.Incident._log(self._snapshot(
                    external_ref=self.ref + '-orm-action-done',
                    state='needs_human',
                )),
                incident_id,
            )
        self.assertTrue(
            self._activity_user_ids(incident_id, active=True),
            'done 必須立即重建；duplicate observation 只能 dedupe',
        )
        self.assertEqual(self._read(incident_id)['state'], 'needs_human')

    def test_incident_chatter_rejects_free_posts_and_pci_activity_feedback(self):
        external_ref = self.ref + '-chatter'
        incident_id = self.Incident._log(self._snapshot(
            external_ref=external_ref,
            state='needs_human',
        ))
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            incident = env['woow.billing.incident'].sudo().browse(incident_id)
            for body in (
                    'free account-manager chatter',
                    'PAN 4111111111111111',
                    'C.V.C: 123'):
                with self.subTest(body=body), self.assertRaises(AccessError):
                    incident.message_post(body=body)

            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            activity = env['mail.activity'].sudo().search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(activity)
            with self.assertRaises(UserError):
                activity.action_feedback(
                    feedback='verification CVC=123 is forbidden')
            self.assertTrue(activity.exists())
            self.assertTrue(activity.active)

            self.assertTrue(activity.action_done())
            self.assertTrue(env['mail.activity'].sudo().search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ]), 'legal core activity-done chatter must succeed and re-alert')
            cr.commit()

    def test_message_post_with_non_activity_source_is_rejected(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-source-reject',
        ))
        with self.env.registry.cursor() as cr:
            incident = self.env(cr=cr)[
                'woow.billing.incident'].sudo().browse(incident_id)
            with self.assertRaises(AccessError):
                incident.message_post_with_source(
                    'mail.message_user_assigned',
                    render_values={'body': 'safe but unsupported source'},
                )
            cr.rollback()

    def test_generic_activity_done_on_other_model_is_unaffected(self):
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            partner = env['res.partner'].create({
                'name': 'Generic activity unaffected %s' % uuid.uuid4().hex,
            })
            activity = env['mail.activity'].sudo().create({
                'res_model_id': env['ir.model']._get('res.partner').id,
                'res_id': partner.id,
                'activity_type_id': env.ref(
                    'mail.mail_activity_data_todo').id,
                'user_id': env.user.id,
                'summary': 'Generic core activity',
                'note': 'No billing incident behavior applies',
                'date_deadline': fields.Date.today(),
            })
            self.assertTrue(activity.action_done())
            self.assertFalse(activity.exists())
            cr.rollback()

    def test_inactive_dedicated_activity_does_not_suppress_new_active_alert(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-inactive-review',
            state='needs_human',
        ))
        manager_ids = sorted(
            self._account_manager_users_for_test(self.env).ids)
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            activities = env['mail.activity'].sudo().search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
            ])
            self.assertTrue(activities)
            activities.write({'active': False})
            cr.commit()
        self.assertEqual(self._activity_user_ids(incident_id, active=True), [])

        self.assertEqual(
            self.Incident._log(self._snapshot(
                external_ref=self.ref + '-inactive-review',
                state='needs_human',
            )),
            incident_id,
        )
        self.assertEqual(
            self._activity_user_ids(incident_id, active=True), manager_ids,
            'inactive dedicated rows 不得參與 active alert dedupe',
        )
        self.assertEqual(
            self.Incident._resolve(
                self.ref + '-inactive-review',
                resolution_note='remove active and inactive dedicated rows',
            ),
            incident_id,
        )
        self.assertEqual(
            self._activity_user_ids(incident_id, active=None), [],
            'resolve cleanup 必須 active_test=False 清掉 custom type 全部 rows',
        )

    def test_generic_todo_does_not_suppress_dedicated_review_activity(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-generic-does-not-dedupe',
        ))
        self._set_incident_state(incident_id, 'needs_human')
        todo_id = self._insert_activity(
            incident_id,
            'mail.mail_activity_data_todo',
            summary='Generic Todo must not suppress billing review',
        )
        self.assertTrue(self.Incident._schedule_review_activities(incident_id))

        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activity_type_ids = self._activity_type_ids(incident_id)
        self.assertEqual(activity_type_ids.count(todo_id), 1)
        self.assertEqual(
            activity_type_ids.count(review_type.id),
            len(self._account_manager_users_for_test(self.env)),
            'dedupe provenance 必須靠 dedicated type，不可被 generic Todo 抑制',
        )

    def test_resolve_deletes_dedicated_review_but_keeps_generic_todo(self):
        external_ref = self.ref + '-generic-survives-resolve'
        incident_id = self.Incident._log(self._snapshot(
            external_ref=external_ref,
        ))
        self._set_incident_state(incident_id, 'needs_human')
        todo_id = self._insert_activity(
            incident_id,
            'mail.mail_activity_data_todo',
            summary='Generic Todo must survive billing review cleanup',
        )
        self.assertTrue(self.Incident._schedule_review_activities(incident_id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        self.assertIn(review_type.id, self._activity_type_ids(incident_id))

        self.assertEqual(
            self.Incident._resolve(
                external_ref,
                resolution_note='dedicated activity cleanup probe',
            ),
            incident_id,
        )
        self.assertEqual(
            self._activity_type_ids(incident_id),
            [todo_id],
            'resolve 不得刪 generic Todo，只能刪 dedicated review activity',
        )

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
            '同 incident/user 的 review activity 不得因 duplicate _log 重建',
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
        review_type_id = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review').id
        other_type_id = self._insert_other_activity(incident_id)
        self.assertIn(review_type_id, self._activity_type_ids(incident_id))
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
            'resolve 只能刪 dedicated review，不得刪其他 activity type',
        )

    def test_activity_displays_shape_validated_raw_mtn_via_html_safe_note(self):
        raw_mtn = '20260830987654321012'
        self._extra_cleanup_refs.add(raw_mtn)
        incident_id = self.Incident._log(self._snapshot(
            external_ref=raw_mtn,
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
        self.assertTrue(activities)
        for note, res_name in activities:
            self.assertIn(raw_mtn, note)
            self.assertEqual(res_name, raw_mtn)
            self.assertNotIn('<img', note.lower())
            self.assertNotIn('<script', note.lower())

        malicious_ref = self.ref + '-<img-src=x-onerror=alert864>'
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        with self.assertLogs(logger, level='ERROR') as logs:
            self.assertFalse(self.Incident._log(self._snapshot(
                external_ref=malicious_ref,
                state='needs_human',
            )))
        self.assertNotIn(malicious_ref, '\n'.join(logs.output))

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
        self.assertIn('category=no_review_assignee', '\n'.join(logs.output))
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
        self.incident = self.env[
            'woow.billing.incident']._create_test_fixture({
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

    def test_account_manager_cannot_post_free_chatter_pan_or_cvc(self):
        incident = self.incident.with_user(self.manager)
        for body in (
                'free manager chatter',
                'PAN 4111111111111111',
                'CVC: 123'):
            with self.subTest(body=body), self.assertRaises(AccessError):
                incident.message_post(body=body)

    def test_review_activity_content_uses_each_manager_language_and_kind_label(self):
        self.manager.lang = 'en_US'
        zh_manager = new_test_user(
            self.env,
            login='billing-zh-manager-%s' % uuid.uuid4().hex,
            groups='account.group_account_manager',
            lang='zh_TW',
        )
        manager_ids = (self.manager | zh_manager).ids
        target = (
            'odoo.addons.woow_paas_platform.models.billing_incident.'
            'BillingIncident._account_manager_users'
        )

        def _two_managers(_records, env):
            return env['res.users'].browse(manager_ids)

        with patch(target, autospec=True, side_effect=_two_managers):
            self.env['woow.billing.incident'] \
                ._ensure_review_activities_current_cursor(self.incident.id)

        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident.id),
            ('activity_type_id', '=', review_type.id),
            ('user_id', 'in', manager_ids),
            ('active', '=', True),
        ])
        by_user = {
            activity.user_id.id: (activity.summary, activity.note)
            for activity in activities
        }
        self.assertEqual(set(by_user), set(manager_ids))
        en_summary, en_note = by_user[self.manager.id]
        zh_summary, zh_note = by_user[zh_manager.id]
        self.assertEqual(en_summary,
                         'Billing incident requires manual review')
        self.assertEqual(zh_summary, '帳務事件需要人工檢查')
        self.assertIn('Charged but not credited', en_note)
        self.assertIn('已扣款但未入點', zh_note)
        self.assertNotIn('(charged_not_credited)', en_note)
        self.assertNotIn('（charged_not_credited）', zh_note)

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

    def test_sudo_cannot_mutate_evidence_reopen_resolved_or_unlink(self):
        sudo_record = self.incident.sudo()
        unresolved_before = self.incident.read([
            'state', 'detail', 'manual_check_result', 'resolution_note',
        ])[0]
        for values in (
                {'detail': 'sudo-forged evidence'},
                {'state': 'open'},
                {'manual_check_result': 'ecpay_found'}):
            with self.subTest(unresolved_values=values), self.assertRaises(
                    AccessError):
                sudo_record.write(values)
        self.assertEqual(
            self.incident.read(list(unresolved_before))[0], unresolved_before)

        manager_record = self.incident.with_user(self.manager)
        manager_record.write({
            'manual_check_result': 'ecpay_found',
            'resolution_note': 'first immutable resolution audit',
        })
        self.assertTrue(manager_record.action_mark_manually_resolved())
        resolved_before = self.incident.read([
            'state', 'detail', 'manual_check_result', 'resolution_note',
            'resolved_at', 'resolved_by_ref',
        ])[0]
        for values in (
                {'detail': 'late sudo-forged evidence'},
                {'state': 'needs_human'},
                {'manual_check_result': 'ecpay_not_found'},
                {'resolution_note': 'late sudo audit overwrite'}):
            with self.subTest(resolved_values=values), self.assertRaises(
                    AccessError):
                sudo_record.write(values)
        for record in (
                sudo_record,
                sudo_record.with_context(_force_unlink=True)):
            with self.subTest(context=dict(record.env.context)), \
                    self.assertRaises(AccessError):
                record.unlink()
        self.assertEqual(
            self.incident.read(list(resolved_before))[0], resolved_before)

    def test_real_odoo_module_uninstall_context_can_remove_ledger_row(self):
        module = self.env['ir.module.module'].sudo().search([
            ('name', '=', 'woow_paas_platform'),
        ], limit=1)
        self.assertTrue(module)
        module.write({'state': 'to remove'})
        self.assertTrue(
            self.incident.sudo().with_context(_force_unlink=True).unlink())
        self.assertFalse(self.incident.exists())
        # TransactionCase rollback restores both module state and ledger row.

    def test_manual_resolution_button_rejects_sensitive_raw_seeded_note(self):
        record = self.incident.with_user(self.manager)
        # The public ORM write seam must reject this value, including sudo.
        # Raw SQL is intentional test corruption for the button's second-line
        # validation and is rolled back with this TransactionCase.
        self.env.cr.execute(
            'UPDATE woow_billing_incident '
            'SET manual_check_result = %s, resolution_note = %s '
            'WHERE id = %s',
            (
                'ecpay_found',
                'ResultData=forbidden-button-payload-864',
                self.incident.id,
            ),
        )
        self.incident.invalidate_recordset()
        with self.assertRaises(UserError):
            record.action_mark_manually_resolved()
        self.incident.invalidate_recordset()
        self.assertEqual(self.incident.state, 'needs_human')
        self.assertFalse(self.incident.resolved_at)

    def test_manual_resolution_keeps_other_activity_type(self):
        record = self.incident.with_user(self.manager)
        record.write({
            'manual_check_result': 'ecpay_not_found',
            'resolution_note': 'current-cursor Todo-only cleanup',
        })
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        other_type = self.env.ref('mail.mail_activity_data_call')
        model = self.env['ir.model']._get('woow.billing.incident')
        for activity_type in (review_type, other_type):
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
            'current-cursor transition 也只能刪 dedicated review activity',
        )

    def test_all_billing_incident_user_visible_sources_have_zh_tw(self):
        module_root = Path(__file__).resolve().parents[1]
        model_path = module_root / 'models' / 'billing_incident.py'
        view_path = module_root / 'views' / 'billing_incident_views.xml'
        po_path = module_root / 'i18n' / 'zh_TW.po'
        Incident = self.env['woow.billing.incident']

        required_msgids = {Incident._description}
        automatic_fields = {
            'id', 'create_uid', 'write_uid', 'create_date', 'write_date',
            'display_name', '__last_update',
        }
        business_fields = (
            set(Incident._fields)
            - automatic_fields
            - set(self.env['mail.thread']._fields)
        )
        for field_name in business_fields:
            field = Incident._fields[field_name]
            required_msgids.add(field.string)
            if field.help:
                required_msgids.add(field.help)
            if field.type == 'selection':
                selection = field._description_selection(self.env)
                required_msgids.update(label for _key, label in selection)
        required_msgids.update(
            constraint[2] for constraint in Incident._sql_constraints)

        model_tree = ast.parse(model_path.read_text())
        for node in ast.walk(model_tree):
            if not isinstance(node, ast.Call):
                continue
            if (isinstance(node.func, ast.Name)
                    and node.func.id == '_'
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                required_msgids.add(node.args[0].value)
            if (isinstance(node.func, ast.Name)
                    and node.func.id == 'get_translation'
                    and len(node.args) >= 3
                    and isinstance(node.args[2], ast.Constant)
                    and isinstance(node.args[2].value, str)):
                required_msgids.add(node.args[2].value)

        view_tree = etree.parse(str(view_path))
        for view_record in view_tree.xpath(
                "//record[@model='ir.ui.view']"):
            for arch in view_record.xpath("./field[@name='arch']/*"):
                for element in arch.iter():
                    if element.get('string'):
                        required_msgids.add(element.get('string'))
                    if element.text and element.text.strip():
                        required_msgids.add(' '.join(element.text.split()))
        for record_model in ('mail.activity.type', 'ir.actions.act_window'):
            required_msgids.update(
                ' '.join(node.text.split())
                for node in view_tree.xpath(
                    "//record[@model='%s']/field[@name='name']" % record_model)
                if node.text and node.text.strip()
            )
        required_msgids.update(
            node.get('name') for node in view_tree.xpath('//menuitem')
            if node.get('name')
        )
        required_msgids.discard(None)
        required_msgids.discard('')
        self.assertFalse(
            sorted(msgid for msgid in required_msgids
                   if re.search(r'[\u3400-\u9fff]', msgid)),
            'billing incident translatable source 必須保持 English msgid',
        )
        self.assertGreaterEqual(
            len(required_msgids), 70,
            'billing incident i18n 枚舉異常縮水，可能漏掃 source 類別',
        )

        po_entries = {
            entry.msgid: entry
            for entry in polib.pofile(str(po_path))
            if not entry.obsolete
        }
        missing = sorted(
            msgid for msgid in required_msgids
            if (msgid not in po_entries
                or not po_entries[msgid].msgstr.strip()
                or 'fuzzy' in po_entries[msgid].flags)
        )
        self.assertFalse(
            missing,
            '所有 billing incident user-visible source 都需非空 zh_TW：%r'
            % missing,
        )

        exact_translations = {
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
        for msgid, msgstr in exact_translations.items():
            self.assertEqual(po_entries[msgid].msgstr, msgstr)

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
        Incident = self.env['woow.billing.incident']
        self.incident_a = Incident._create_test_fixture({
            'external_ref': 'rpc-a-%s' % marker,
            'kind': 'charged_not_credited',
            'state': 'needs_human',
            'source_model': 'res.company',
            'source_ref': self.company_a.id,
            'trade_no': 'RPC-TRADE-A',
            'detail': 'company A safe summary',
        })
        self.incident_b = Incident._create_test_fixture({
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

    def _assert_user_error(self, result, message):
        self.assertIn('error', result, '%s（回 %r）' % (message, result))
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('UserError', name, '%s（回 %r）' % (message, result))

    def _audit_snapshot(self, incident):
        incident.invalidate_recordset()
        return incident.read([
            'external_ref', 'kind', 'state', 'trade_no', 'detail',
            'manual_check_result', 'resolution_note',
            'resolved_at', 'resolved_by_ref',
        ])[0]

    def test_rpc_cannot_export_fixture_capability_or_forge_create_context(self):
        self.authenticate(self.manager_a_login, self.manager_a_login)
        external_ref = 'rpc-forged-create-%s' % uuid.uuid4().hex
        private_call = self._call_kw(
            'woow.billing.incident',
            '_create_test_fixture',
            [{'external_ref': external_ref, 'kind': 'intent'}],
        )
        self._assert_access_error(
            private_call,
            'underscore test fixture seam must not be exported through RPC',
        )
        forged_create = self._call_kw(
            'woow.billing.incident',
            'create',
            [{'external_ref': external_ref, 'kind': 'intent'}],
            {'context': {'woow_billing_incident_create': True}},
        )
        self._assert_access_error(
            forged_create,
            'truthy RPC context cannot forge the object-identity create cap',
        )
        self.assertFalse(self.env['woow.billing.incident'].sudo().search([
            ('external_ref', '=', external_ref),
        ]))

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

    def test_manager_rpc_rejects_sensitive_resolution_note_atomically(self):
        self.authenticate(self.manager_a_login, self.manager_a_login)
        before = self._audit_snapshot(self.incident_a)
        for note in (
                'MTN: 4111111111111111',
                'ResultData=forbidden-rpc-payload-864',
                'CVC=123',
                'CID: 123',
                'card security code 123',
                'SAD=forbidden-authentication-data'):
            with self.subTest(note=note):
                denied = self._call_kw(
                    'woow.billing.incident',
                    'write',
                    [[self.incident_a.id], {'resolution_note': note}],
                )
                self._assert_user_error(
                    denied,
                    'RPC resolution_note 必須套用共用 sensitive validator',
                )
                self.assertEqual(self._audit_snapshot(self.incident_a), before)

    def test_resolved_incident_rejects_every_rpc_write_and_keeps_first_audit(self):
        self.authenticate(self.manager_a_login, self.manager_a_login)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'woow.billing.incident',
                'write',
                [[self.incident_a.id], {
                    'manual_check_result': 'ecpay_found',
                    'resolution_note': 'first RPC resolution audit',
                }],
            ),
            'account manager 應可先保存 unresolved 人工查證',
        ))
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'woow.billing.incident',
                'action_mark_manually_resolved',
                [[self.incident_a.id]],
            ),
            'account manager 應可透過真 RPC 受控關單',
        ))
        first_audit = self._audit_snapshot(self.incident_a)
        self.assertEqual(first_audit['state'], 'resolved')

        forbidden_writes = (
            {'manual_check_result': 'ecpay_not_found'},
            {'resolution_note': 'late RPC audit overwrite'},
            {'state': 'open'},
            {'detail': 'late forged evidence'},
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
                    'resolved incident 的人工欄、state、evidence 一律 immutable',
                )
                forged = self._call_kw(
                    'woow.billing.incident',
                    'write',
                    [[self.incident_a.id], values],
                    {'context': {
                        'woow_billing_incident_resolution_transition': True,
                    }},
                )
                self._assert_access_error(
                    forged,
                    'RPC truthy context 不得偽造 module-private transition capability',
                )
                self.assertEqual(
                    self._audit_snapshot(self.incident_a), first_audit,
                    '拒絕後必須完整保留 first resolution audit',
                )

        denied_unlink = self._call_kw(
            'woow.billing.incident',
            'unlink',
            [[self.incident_a.id]],
            {'context': {'_force_unlink': True}},
        )
        self._assert_access_error(
            denied_unlink,
            '一般 RPC 不得偽造 Odoo module uninstall context 刪除 ledger',
        )
        self.assertEqual(self._audit_snapshot(self.incident_a), first_audit)

        unresolved_before = self._audit_snapshot(self.incident_b)
        denied = self._call_kw(
            'woow.billing.incident',
            'write',
            [[self.incident_a.id, self.incident_b.id], {
                'resolution_note': 'batch write must be atomic',
            }],
        )
        self._assert_access_error(
            denied,
            'recordset 任一 resolved 時必須拒絕整批 manager write',
        )
        self.assertEqual(self._audit_snapshot(self.incident_a), first_audit)
        self.assertEqual(
            self._audit_snapshot(self.incident_b), unresolved_before,
            '整批拒絕不得局部修改 unresolved row',
        )

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
