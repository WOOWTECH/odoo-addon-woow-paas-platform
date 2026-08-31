"""#864 PR-D：帳務 incident 地基的 public model／後台行為守線。"""

import ast
import base64
import json
import re
import threading
import time
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

    def test_global_ledger_policy_names_odoo_role_and_has_no_company_rule(self):
        """Comment #11112 is a durable role contract, not an accident of data."""
        module_root = Path(__file__).resolve().parents[1]
        policy_surfaces = {
            'model': (module_root / 'models' / 'billing_incident.py').read_text(),
            'view': (module_root / 'views' / 'billing_incident_views.xml').read_text(),
        }
        exact_contract = (
            'Odoo role `account.group_account_manager` (not a Workspace role)',
            'all incidents across all companies',
            'all current managers in that Odoo group',
            'no company M2O/rule; base.group_system behavior is not narrowed',
        )
        for surface, source in policy_surfaces.items():
            normalized_source = ' '.join(source.split())
            with self.subTest(surface=surface):
                for wording in exact_contract:
                    self.assertIn(wording, normalized_source)

        model = self.env['ir.model']._get('woow.billing.incident')
        manager_group = self.env.ref('account.group_account_manager')
        accesses = self.env['ir.model.access'].sudo().search([
            ('model_id', '=', model.id),
        ])
        self.assertEqual(accesses.mapped('group_id'), manager_group)
        self.assertEqual(
            (accesses.perm_read, accesses.perm_write,
             accesses.perm_create, accesses.perm_unlink),
            (True, True, False, False),
        )
        self.assertFalse(
            self.env['ir.rule'].sudo().search([('model_id', '=', model.id)]),
            'global finance ledger must not acquire any company record rule',
        )
        self.assertFalse(
            {'company_id', 'company_ref'} & set(self.env[model.model]._fields),
            'global finance ledger must not acquire a company M2O',
        )

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
        self.assertIsInstance(self.Incident._log(self._snapshot(
            external_ref=self.ref + '-legal-chinese-detail',
            detail='已於 ECPay 後台核對交易狀態，帳務資料一致。',
        )), int, '合法中文查證理由不得被 marker gate 誤殺')
        self.assertIsInstance(self.Incident._log(self._snapshot(
            external_ref=self.ref + '-legal-http-detail',
            detail='HTTP 交易查詢回應 200，交易狀態一致。',
        )), int, '無 card 語境的合法中文 HTTP／交易理由不得被誤殺')

        sensitive_details = (
            'ResultData=secret-payload-864',
            'card4no: 1111',
            'CARD6NO=411111',
            'CardNo 4111111111111111',
            'cvv: 123',
            'CVC=123',
            'C.V.C:123',
            '查證時看到CVC碼：123',
            'CID: 123',
            'C-I-D=123',
            'CSC/123',
            'card security code 123',
            'card verification value 123',
            'card verification code: 123',
            'CVN: 123',
            'CVN2: 123',
            'C\u200bV\u200bN\u200b2: 123',
            'security_code=123',
            'security.code:123',
            'S.A.D=forbidden-authentication-data',
            'SAD=forbidden-authentication-data',
            'sensitive authentication data payload',
            'PAN=4111111111111111',
            '卡號4111111111111111',
            '卡号4111111111111111',
            '卡\u200b號：4111',
            '卡\u200b号：4111',
            '敏感驗證資料：forbidden',
            '敏感认证数据：forbidden',
            '安全碼：123',
            '安\u200b全\u200b碼：123',
            '卡片安全碼：123',
            '信用卡安全碼：123',
            '信用卡安全码：123',
            '卡片驗證值：123',
            '卡片验证值：123',
            '卡片背面三碼：123',
            '卡片背面三码：123',
            '卡片背面末三碼：123',
            '卡片背面末三码：123',
            '信用卡背面三碼：123',
            '信用卡背面三碼',
            '信用卡背面三码：123',
            '信用卡背面末三碼：123',
            '信用卡背面末三码：123',
            '背後三碼：123',
            '背后三码：123',
            '末三碼：123',
            '末三码：123',
            '卡的參考資料是 123',
            '卡片記錄 1234',
            '信用卡查詢碼 123',
            'card reference 1234',
            '個人識別碼：1234',
            '个人识别码：1234',
            '驗證碼：123',
            '验证码：123',
            '驗證值：123',
            '验证值：123',
            'PIN碼：1234',
            'PIN區塊：forbidden',
            '磁條：forbidden',
            '磁軌資料：forbidden',
            '磁轨数据：forbidden',
            '第二軌：forbidden',
            '第二轨：forbidden',
            '軌道資料：forbidden',
            '授權碼：forbidden',
            'unlabelled 4111111111111111',
            '交易4111111111111111',
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

    def test_pci_validator_canonicalizes_html_unicode_and_sad_markers(self):
        entity_digits = ''.join(
            '&#%s;' % ord(character) for character in '4111111111111111')
        sensitive_details = (
            '<b>plain verification reason</b>',
            'plain <!-- hidden comment --> verification reason',
            'title="plain verification reason"',
            'plain &amp; verification reason',
            'plain &lt;b&gt;verification&lt;/b&gt; reason',
            '<span title="4111111111111111">verification</span>',
            'unlabelled 4111<span>1111</span><span>1111</span><span>1111</span>',
            'unlabelled 4111.1111.1111.1111',
            'unlabelled 4111&#46;1111&#46;1111&#46;1111',
            'unlabelled %s' % entity_digits,
            'unlabelled 4111\ufe0f1111\ufe0f1111\ufe0f1111',
            'unlabelled 4111\u200b1111\u200b1111\u200b1111',
            'unlabelled 4111\u03071111\u03071111\u03071111',
            'unlabelled 4111\u20dd1111\u20dd1111\u20dd1111',
            'unlabelled 4111\u09031111\u09031111\u09031111',
            'unlabelled 4111😀1111😀1111😀1111',
            'unlabelled 4111^1111^1111^1111',
            'unlabelled ４１１１．１１１１．１１１１．１１１１',
            '<b>PIN</b> block=forbidden-authentication-data',
            'track1 data=forbidden-authentication-data',
            'track 2 data=forbidden-authentication-data',
            'magnetic stripe data=forbidden-authentication-data',
        )
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        for index, detail in enumerate(sensitive_details):
            with self.subTest(detail=detail), self.assertLogs(
                    logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._log(self._snapshot(
                    external_ref='%s-canonical-pci-%s' % (self.ref, index),
                    detail=detail,
                )))
            self.assertNotIn(detail, '\n'.join(logs.output))
        self.assertFalse(self.Incident.sudo().search([
            ('external_ref', 'like', self.ref + '-canonical-pci-%'),
        ]))

    def test_structured_log_fields_do_not_run_free_text_pci_scans(self):
        structured_cases = (
            {
                'partner_email': 'pin@example.com',
                'failure_code': 'PAN_REVIEW',
                'rtn_code': 'PIN',
                'skip_reason': 'PAN_REQUIRED',
                'assessment_reason': 'PIN_REVIEW',
                'currency': 'PAN',
                'source_model': 'pan.payment.pin',
                'trade_no': '20260830123456789012',
            },
            {
                'partner_email': 'pan@example.com',
                'failure_code': 'PIN_REQUIRED',
                'rtn_code': 'PAN',
                'skip_reason': 'PIN_REVIEW',
                'assessment_reason': 'PAN_REVIEW',
                'currency': 'PIN',
                'source_model': 'pin.payment.pan',
                'trade_no': 'invoice:123',
            },
        )
        for index, structured in enumerate(structured_cases):
            with self.subTest(structured=structured):
                incident_id = self.Incident._log(self._snapshot(
                    external_ref='%s-structured-%s' % (self.ref, index),
                    detail='safe reconciliation summary',
                    **structured,
                ))
                self.assertIsInstance(
                    incident_id, int,
                    'email/provider/code fields must use their own shape, not '
                    'free-text PIN/PAN marker scanning',
                )

        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        for index, detail in enumerate((
                'ResultData=secret-free-text-864',
                'PAN 4111111111111111')):
            with self.subTest(detail=detail), self.assertLogs(
                    logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._log(self._snapshot(
                    external_ref='%s-free-text-only-%s' % (self.ref, index),
                    partner_email='pan@example.com',
                    failure_code='PIN_REQUIRED',
                    detail=detail,
                )))
            self.assertNotIn(detail, '\n'.join(logs.output))

    def test_structured_log_fields_enforce_email_code_and_model_shape(self):
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        cases = (
            ('partner_email', 'not-an-email'),
            ('partner_email', 'pin@example.com\r\nforged'),
            ('partner_email', '%s@example.com' % ('a' * 245)),
            ('currency', 'PIN VALUE'),
            ('failure_code', 'PAN\nFORGED'),
            ('skip_reason', '<b>PIN</b>'),
            ('assessment_reason', 'PAN reason'),
            ('source_model', 'PAN Model'),
        )
        for index, (field_name, invalid_value) in enumerate(cases):
            with self.subTest(field_name=field_name), self.assertLogs(
                    logger, level='ERROR') as logs:
                self.assertFalse(self.Incident._log(self._snapshot(
                    external_ref='%s-invalid-structured-%s' % (
                        self.ref, index),
                    **{field_name: invalid_value},
                )))
            self.assertNotIn(invalid_value, '\n'.join(logs.output))

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
            output = '\n'.join(logs.output)
            self.assertNotIn(
                secret_value,
                output,
                '非法輸入 log 只能記 field/category/type，不得回印原值',
            )
            if field_name in ('kind', 'state'):
                self.assertIn('category=unsupported_selection', output)
                self.assertNotIn('category=sensitive_marker', output)
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

        for namespaced_ref in (
                'invoice:123', 'invoice:2026_08-30/abc.DEF'):
            with self.subTest(namespaced_ref=namespaced_ref):
                self._extra_cleanup_refs.add(namespaced_ref)
                namespaced_id = self.Incident._log(self._snapshot(
                    external_ref=namespaced_ref,
                    trade_no='invoice:123',
                ))
                self.assertIsInstance(namespaced_id, int)

    def test_provider_references_reject_crlf_controls_and_bad_shape(self):
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        cases = (
            ('external_ref', 'MTN: 4111111111111111'),
            ('external_ref', 'PAN-4111111111111111'),
            ('external_ref', 'PIN-1234'),
            ('external_ref', 'invoice:PAN-4111111111111111'),
            ('external_ref', 'invoice:bad\r\nforged-log-line'),
            ('external_ref', 'invoice:bad\x01control'),
            ('external_ref', '123456789012345678901'),
            ('external_ref', 'invoice:<script>'),
            ('trade_no', 'PAN-4111111111111111'),
            ('trade_no', 'PIN-1234'),
            ('trade_no', 'TRADE\r\nFORGED'),
            ('trade_no', 'TRADE WITH SPACE'),
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
            'card verification value 123',
            'card verification code: 123',
            'CVN: 123',
            'CVN2: 123',
            'C\u200bV\u200bN\u200b2: 123',
            'SAD=forbidden-authentication-data',
            '卡號4111111111111111',
            '卡号4111111111111111',
            '卡\u200b號：4111',
            '卡\u200b号：4111',
            '敏感驗證資料：forbidden',
            '敏感认证数据：forbidden',
            '安全碼：123',
            '安\u200b全\u200b碼：123',
            '卡片安全碼：123',
            '信用卡安全碼：123',
            '信用卡安全码：123',
            '卡片驗證值：123',
            '卡片验证值：123',
            '卡片背面三碼：123',
            '卡片背面三码：123',
            '卡片背面末三碼：123',
            '卡片背面末三码：123',
            '信用卡背面三碼：123',
            '信用卡背面三碼',
            '信用卡背面三码：123',
            '信用卡背面末三碼：123',
            '信用卡背面末三码：123',
            '背後三碼：123',
            '背后三码：123',
            '末三碼：123',
            '末三码：123',
            '卡的參考資料是 123',
            '卡片記錄 1234',
            '信用卡查詢碼 123',
            'card reference 1234',
            '個人識別碼：1234',
            '个人识别码：1234',
            '驗證碼：123',
            '验证码：123',
            '驗證值：123',
            '验证值：123',
            'PIN碼：1234',
            'PIN區塊：forbidden',
            '磁條資料：forbidden',
            '磁軌資料：forbidden',
            '磁轨数据：forbidden',
            '第二軌：forbidden',
            '第二轨：forbidden',
            '軌道資料：forbidden',
            '授權碼：forbidden',
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

        legal_ref = self.ref + '-legal-chinese-resolution'
        legal_id = self.Incident._log(self._snapshot(
            external_ref=legal_ref,
            state='needs_human',
        ))
        legal_note = 'HTTP 交易查詢回應 200，交易狀態一致。'
        self.assertEqual(
            self.Incident._resolve(
                legal_ref,
                resolution_note=legal_note,
                resolved_by_ref=99,
            ),
            legal_id,
            '合法中文 resolution reason 必須通過共用 validator',
        )
        self.assertEqual(self._read(legal_id)['resolution_note'], legal_note)

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
                    'legacy generic activity', activity_type.id, manager.id,
                    summary, 'Must survive billing review cleanup',
                    env.uid, env.uid,
                ),
            )
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
                expected_manager_ids = sorted(
                    self._account_manager_users_for_test(env).ids)
                active_user_ids = [
                    row.user_id.id
                    for row in env['mail.activity'].sudo().search([
                        ('res_model', '=', 'woow.billing.incident'),
                        ('res_id', '=', incident_id),
                        ('activity_type_id', '=', review_type.id),
                        ('active', '=', True),
                    ], order='user_id')
                ]
                self.assertEqual(
                    active_user_ids,
                    expected_manager_ids,
                    'done 同一 transaction 結束前必須每位 manager 恰一筆 active alert',
                )
                cr.commit()
            self.assertTrue(any(
                channel == 'mail.activity/updated'
                and message.get('activity_deleted')
                for channel, message in calls['bus']
            ), 'dedicated terminal must emit the standard deletion bus update')

            self.assertEqual(
                self.Incident._log(self._snapshot(
                    external_ref=self.ref + '-orm-action-done',
                    state='needs_human',
                )),
                incident_id,
            )
        self.assertEqual(
            self._activity_user_ids(incident_id, active=True),
            sorted(self._account_manager_users_for_test(self.env).ids),
            'done 必須立即重建每位 manager 恰一筆；duplicate observation 只能 dedupe',
        )
        self.assertEqual(self._read(incident_id)['state'], 'needs_human')

    def test_scheduler_uses_clean_context_and_explicit_security_values(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-clean-scheduler-context',
            state='needs_human',
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
            detached = env['ir.attachment'].sudo().create({
                'name': 'hostile-default-orm.bin',
                'datas': base64.b64encode(b'must not become activity data'),
            })
            ActivityClass = type(env['mail.activity'])
            original_create = ActivityClass.create
            scheduler_calls = []

            def create_spy(records, values):
                if any(
                        value.get('activity_type_id') == review_type.id
                        for value in values):
                    scheduler_calls.append((
                        dict(records.env.context),
                        [dict(value) for value in values],
                    ))
                return original_create(records, values)

            with patch.object(ActivityClass, 'create', create_spy):
                activity.with_context(
                    default_active=False,
                    default_attachment_ids=[detached.id],
                    default_user_id=env.user.id,
                ).unlink()

            self.assertTrue(scheduler_calls)
            security_fields = {
                'res_model_id', 'res_id', 'activity_type_id', 'user_id',
                'summary', 'note', 'date_deadline', 'active',
                'attachment_ids', 'automated', 'request_partner_id',
                'recommended_activity_type_id', 'previous_activity_type_id',
            }
            for context, values_list in scheduler_calls:
                self.assertFalse(
                    [key for key in context if key.startswith('default_')],
                    'scheduler capability 前必須以 allowlist 重建 context',
                )
                for values in values_list:
                    self.assertTrue(security_fields.issubset(values))
                    self.assertIs(values['active'], True)
                    self.assertEqual(values['attachment_ids'], [(5, 0, 0)])
            cr.rollback()

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
            for feedback in (
                    'verification CVC=123 is forbidden',
                    '查證時看到CVC碼：123',
                    '查證時看到 CVN：123',
                    '查證時看到 CVN2：123',
                    '查證時看到 card verification value：123',
                    '查證時看到 card verification code：123',
                    '查證時看到 C\u200bV\u200bN\u200b2：123',
                    '查證時看到安\u200b全\u200b碼：123',
                    '查證時看到卡片安全碼：123',
                    '查證時看到信用卡安全码：123',
                    '查證時看到卡片驗證值：123',
                    '查證時看到卡片验证值：123',
                    '查證時看到卡片背面三碼：123',
                    '查證時看到卡片背面三码：123',
                    '查證時看到卡片背面末三碼：123',
                    '查證時看到卡片背面末三码：123',
                    '查證時看到信用卡背面三碼：123',
                    '查證時看到信用卡背面三碼',
                    '查證時看到信用卡背面三码：123',
                    '查證時看到信用卡背面末三碼：123',
                    '查證時看到信用卡背面末三码：123',
                    '查證時看到背後三碼：123',
                    '查證時看到背后三码：123',
                    '查證時看到末三碼：123',
                    '查證時看到末三码：123',
                    '查證卡的參考資料是 123',
                    '查證卡片記錄 1234',
                    '查證信用卡查詢碼 123',
                    'card reference 1234',
                    '查證時看到個人識別碼：1234',
                    '查證時看到个人识别码：1234',
                    '查證時看到驗證碼：123',
                    '查證時看到验证码：123',
                    '查證時看到驗證值：123',
                    '查證時看到验证值：123',
                    '查證時看到 PIN區塊：forbidden',
                    '查證時看到磁條資料：forbidden',
                    '查證時看到磁軌資料：forbidden',
                    '查證時看到磁轨数据：forbidden',
                    '查證時看到第二軌：forbidden',
                    '查證時看到第二轨：forbidden',
                    '查證時看到軌道資料：forbidden',
                    '查證時看到授權碼：forbidden',
                    '卡號4111111111111111',
                    '卡号4111111111111111',
                    '卡\u200b號：4111',
                    '卡\u200b号：4111',
                    '敏感驗證資料：forbidden',
                    '敏感认证数据：forbidden',
                    '分隔卡號 4111\u09031111\u09031111\u09031111'):
                with self.subTest(feedback=feedback), \
                        self.assertRaises(UserError):
                    activity.action_feedback(feedback=feedback)
                self.assertTrue(activity.exists())
                self.assertTrue(activity.active)

            completed_user_id = activity.user_id.id
            safe_feedback = (
                'HTTP 交易查詢回應 200，已核對交易狀態一致。')
            message_floor = env['mail.message'].sudo().search(
                [], order='id desc', limit=1).id
            notification_floor = env['mail.notification'].sudo().search(
                [], order='id desc', limit=1).id
            mail_floor = env['mail.mail'].sudo().search(
                [], order='id desc', limit=1).id

            messages, next_activities = activity._action_done(
                feedback=safe_feedback)

            self.assertEqual(messages._name, 'mail.message')
            self.assertEqual(next_activities._name, 'mail.activity')
            self.assertFalse(
                messages,
                'dedicated done core contract must return empty mail.message',
            )
            self.assertFalse(
                next_activities,
                'dedicated done core contract must return empty next activities',
            )
            self.assertFalse(activity.exists(), 'original activity is dismissed')
            self.assertFalse(env['mail.message'].sudo().search([
                ('id', '>', message_floor),
            ]), 'dedicated done must not create any mail.message')
            self.assertFalse(env['mail.notification'].sudo().search([
                ('id', '>', notification_floor),
            ]), 'dedicated done must not create any mail.notification')
            self.assertFalse(env['mail.mail'].sudo().search([
                ('id', '>', mail_floor),
            ]), 'dedicated done must not enqueue any mail.mail')
            self.assertFalse(env['mail.message'].sudo().search([
                ('body', 'ilike', safe_feedback),
            ]), 'validated activity feedback must not be persisted')
            active_for_completed_user = env['mail.activity'].sudo().search_count([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', completed_user_id),
                ('active', '=', True),
            ])
            self.assertEqual(
                active_for_completed_user,
                1,
                'legal dedicated done must re-alert completed user exactly once',
            )
            self.assertEqual(
                incident.state,
                'needs_human',
                'activity completion must never resolve the incident',
            )
            cr.commit()

    def test_activity_done_rejects_attachments_html_pan_and_cvc(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-activity-done-ingress',
            state='needs_human',
        ))
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            incident = env['woow.billing.incident'].sudo().browse(incident_id)
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
            attachment = env['ir.attachment'].sudo().create({
                'name': 'unscanned-proof.txt',
                'datas': base64.b64encode(b'unscanned attachment content'),
                'mimetype': 'text/plain',
            })

            feedback_cases = (
                {
                    'feedback': 'verification completed',
                    'attachment_ids': [attachment.id],
                },
                {'feedback': '<b>plain verification completed</b>'},
                {'feedback': 'plain <!-- comment --> verification completed'},
                {'feedback': 'title="plain verification completed"'},
                {'feedback': 'plain &amp; verification completed'},
                {
                    'feedback': (
                        'PAN 4111<span>1111</span><span>1111</span>'
                        '<span>1111</span>'
                    ),
                },
                {'feedback': 'verification C&#86;C=123 is forbidden'},
            )
            for values in feedback_cases:
                with self.subTest(values=values), self.assertRaises(UserError):
                    activity.action_feedback(**values)
                self.assertTrue(activity.exists())
                self.assertTrue(activity.active)

            with self.assertRaises(AccessError):
                attachment.write({
                    'res_model': 'mail.activity',
                    'res_id': activity.id,
                })
            attachment.invalidate_recordset(['res_model', 'res_id'])
            self.assertFalse(attachment.res_model)
            self.assertFalse(attachment.res_id)

            # Legacy/corrupt pre-guard rows still fail closed at completion.
            cr.execute(
                'UPDATE ir_attachment SET res_model = %s, res_id = %s '
                'WHERE id = %s',
                ('mail.activity', activity.id, attachment.id),
            )
            attachment.invalidate_recordset(['res_model', 'res_id'])
            with self.assertRaises(UserError):
                activity.action_done()
            self.assertTrue(activity.exists())
            self.assertTrue(activity.active)

            with self.assertRaises(UserError):
                incident.message_post_with_source(
                    'mail.message_activity_done',
                    attachments=[('unscanned-inline.txt', b'content')],
                    author_id=env.user.partner_id.id,
                    render_values={
                        'activity': activity,
                        'feedback': 'verification completed',
                        'display_assignee': False,
                    },
                    mail_activity_type_id=activity.activity_type_id.id,
                    subtype_xmlid='mail.mt_activities',
                )
            self.assertTrue(activity.exists())
            self.assertTrue(activity.active)
            cr.rollback()

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
            self.assertTrue(activity.write({
                'summary': 'Updated generic activity',
                'note': 'Generic activity writes remain unaffected',
            }))
            self.assertTrue(activity.action_done())
            self.assertFalse(activity.exists())
            cr.rollback()

    def test_related_message_and_attachment_guards_are_targeted(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-related-guards',
            state='needs_human',
        ))
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            incident = env['woow.billing.incident'].sudo().browse(incident_id)
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

            Message = env['mail.message'].sudo()
            for body in (
                    'generic safe body',
                    'PAN 4111111111111111',
                    'ResultData=forbidden-related-message'):
                with self.subTest(body=body), self.assertRaises(AccessError):
                    Message.create({
                        'model': 'woow.billing.incident',
                        'res_id': incident_id,
                        'message_type': 'comment',
                        'body': body,
                    })

            protected_message = incident.message_post_with_source(
                'mail.message_activity_done',
                author_id=env.user.partner_id.id,
                render_values={
                    'activity': activity,
                    'feedback': 'private message guard verification',
                    'display_assignee': False,
                },
                mail_activity_type_id=activity.activity_type_id.id,
                subtype_xmlid='mail.mt_activities',
            )
            self.assertTrue(
                protected_message,
                'private guarded source must still exercise message defenses',
            )
            original_body = protected_message.body
            for body in (
                    'generic safe replacement',
                    'PAN 4111111111111111',
                    'ResultData=forbidden-related-write'):
                with self.subTest(write_body=body), \
                        self.assertRaises(AccessError):
                    protected_message.write({'body': body})
                protected_message.invalidate_recordset(['body'])
                self.assertEqual(protected_message.body, original_body)
            with self.assertRaises(AccessError):
                protected_message.unlink()

            replacement_activity = env['mail.activity'].sudo().search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(replacement_activity)
            Attachment = env['ir.attachment'].sudo()
            protected_targets = (
                ('woow.billing.incident', incident.id),
                ('mail.activity', replacement_activity.id),
                ('mail.message', protected_message.id),
            )
            for index, (res_model, res_id) in enumerate(protected_targets):
                name = 'forbidden-related-%s-%s.bin' % (index, uuid.uuid4().hex)
                with self.subTest(create_target=(res_model, res_id)), \
                        self.assertRaises(AccessError):
                    Attachment.create({
                        'name': name,
                        'datas': base64.b64encode(b'caller-controlled payload'),
                        'res_model': res_model,
                        'res_id': res_id,
                    })
                self.assertFalse(Attachment.search([('name', '=', name)]))

                movable = Attachment.create({
                    'name': 'movable-%s' % name,
                    'datas': base64.b64encode(b'legal unrelated payload'),
                    'res_model': 'res.partner',
                    'res_id': env.user.partner_id.id,
                })
                before = movable.read(['res_model', 'res_id', 'datas'])[0]
                with self.assertRaises(AccessError):
                    movable.write({
                        'datas': base64.b64encode(b'forbidden new payload'),
                        'res_model': res_model,
                        'res_id': res_id,
                    })
                self.assertEqual(
                    movable.read(['res_model', 'res_id', 'datas'])[0], before,
                    'old→protected target move must reject atomically',
                )

            other_partner = env['res.partner'].create({
                'name': 'Unrelated attachment control %s' % uuid.uuid4().hex,
            })
            control_message = Message.create({
                'model': 'res.partner',
                'res_id': other_partner.id,
                'message_type': 'comment',
                'body': 'unrelated model message remains legal',
            })
            self.assertTrue(control_message.write({
                'body': 'unrelated message write remains legal',
            }))
            control_attachment = Attachment.create({
                'name': 'unrelated-control.bin',
                'datas': base64.b64encode(b'unrelated payload'),
                'res_model': 'res.partner',
                'res_id': other_partner.id,
            })
            self.assertTrue(control_attachment.write({
                'res_id': env.user.partner_id.id,
            }))
            cr.rollback()

    def test_future_id_attachment_squatting_cannot_bind_billing_targets(self):
        incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-future-id-squatting',
            'kind': 'charged_not_credited',
            'state': 'needs_human',
        })
        Attachment = self.env['ir.attachment'].sudo()
        for malformed_res_id in (False, 0.0, 'not-an-id', '０１'):
            with self.subTest(direct_incident_res_id=malformed_res_id), \
                    self.assertRaises(AccessError):
                Attachment.create({
                    'name': 'direct-incident-%s.bin' % uuid.uuid4().hex,
                    'datas': base64.b64encode(b'direct incident payload'),
                    'res_model': 'woow.billing.incident',
                    'res_id': malformed_res_id,
                })

        def exact_next_id(table):
            self.env.cr.execute(
                "SELECT nextval(pg_get_serial_sequence(%s, 'id'))",
                (table,),
            )
            # The consumed value cannot be reused.  With the standard +1
            # sequence increment, the next ORM insert receives this exact id.
            return self.env.cr.fetchone()[0] + 1

        future_activity_id = exact_next_id('mail_activity')
        activity_payload = Attachment.create({
            'name': 'future-billing-activity-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'future activity payload'),
            'res_model': 'mail.activity',
            'res_id': future_activity_id,
        })
        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(incident.id))
        self.env.invalidate_all()
        squatted_activity = self.env['mail.activity'].sudo().browse(
            future_activity_id).exists()
        self.assertTrue(
            squatted_activity,
            'controlled billing alert must survive future-id squatting',
        )
        self.assertFalse(
            activity_payload.exists(),
            'pre-planted activity payload must be deleted, not roll back alert',
        )
        self.assertFalse(self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'mail.activity'),
            ('res_id', '=', future_activity_id),
        ]))

        partner = self.env['res.partner'].create({
            'name': 'Future attachment control %s' % uuid.uuid4().hex,
        })
        todo_type = self.env.ref('mail.mail_activity_data_todo')
        partner_model = self.env['ir.model']._get('res.partner')
        future_control_activity_id = exact_next_id('mail_activity')
        control_activity_payload = Attachment.create({
            'name': 'future-control-activity-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'legal future activity payload'),
            'res_model': 'mail.activity',
            'res_id': future_control_activity_id,
        })
        control_activity = self.env['mail.activity'].sudo().create({
            'res_model_id': partner_model.id,
            'res_id': partner.id,
            'activity_type_id': todo_type.id,
            'user_id': self.env.user.id,
            'summary': 'Legal future attachment control',
            'note': 'Other target models remain legal',
            'date_deadline': fields.Date.today(),
        })
        self.assertEqual(control_activity.id, future_control_activity_id)
        self.assertTrue(control_activity_payload.exists())

        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(incident.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        review_activity = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', incident.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ], limit=1)
        self.assertTrue(review_activity)

        future_message_id = exact_next_id('mail_message')
        message_payload = Attachment.create({
            'name': 'future-billing-message-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'future message payload'),
            'res_model': 'mail.message',
            'res_id': future_message_id,
        })
        message = incident.message_post_with_source(
            'mail.message_activity_done',
            author_id=self.env.user.partner_id.id,
            render_values={
                'activity': review_activity,
                'feedback': 'future message attachment squatting probe',
                'display_assignee': False,
            },
            mail_activity_type_id=review_activity.activity_type_id.id,
            subtype_xmlid='mail.mt_activities',
        )
        self.assertEqual(message.id, future_message_id)
        self.env.invalidate_all()
        squatted_message = self.env['mail.message'].sudo().browse(
            future_message_id).exists()
        self.assertTrue(
            squatted_message,
            'controlled incident message must survive future-id squatting',
        )
        self.assertFalse(
            message_payload.exists(),
            'pre-planted message payload must be deleted, not roll back message',
        )
        self.assertFalse(self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'mail.message'),
            ('res_id', '=', future_message_id),
        ]))

        future_control_message_id = exact_next_id('mail_message')
        control_message_payload = Attachment.create({
            'name': 'future-control-message-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'legal future message payload'),
            'res_model': 'mail.message',
            'res_id': future_control_message_id,
        })
        control_message = self.env['mail.message'].sudo().create({
            'model': 'res.partner',
            'res_id': partner.id,
            'message_type': 'comment',
            'body': 'Legal other-model future attachment control',
        })
        self.assertEqual(control_message.id, future_control_message_id)
        self.assertTrue(control_message_payload.exists())

        (control_activity_payload | control_message_payload).unlink()

    def test_activity_canonicalization_is_scoped_to_possible_billing_targets(self):
        incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-strict-activity-target',
            'kind': 'charged_not_credited',
            'state': 'open',
        })
        Activity = self.env['mail.activity'].sudo()
        billing_model = self.env['ir.model']._get('woow.billing.incident')
        partner_model = self.env['ir.model']._get('res.partner')
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        todo_type = self.env.ref('mail.mail_activity_data_todo')
        partner = self.env['res.partner'].create({
            'name': 'Strict activity target control %s' % uuid.uuid4().hex,
        })
        billing_values = {
            'res_model_id': billing_model.id,
            'res_id': incident.id,
            'activity_type_id': todo_type.id,
            'user_id': self.env.user.id,
            'summary': 'Generic type still targets billing incident',
            'note': 'Only the scheduler may create any incident activity',
            'date_deadline': fields.Date.today(),
        }

        for values in (
                billing_values,
                {
                    **billing_values,
                    'res_model_id': str(billing_model.id),
                    'res_id': str(incident.id),
                    'activity_type_id': str(todo_type.id),
                }):
            with self.subTest(values=values), self.assertRaises(AccessError):
                Activity.create(values)
        with self.assertRaises(AccessError):
            Activity.with_context(
                default_res_model_id=str(billing_model.id),
                default_res_id=str(incident.id),
                default_activity_type_id=str(todo_type.id),
            ).create({
                'user_id': self.env.user.id,
                'summary': 'Default billing model probe',
                'note': 'Billing defaults remain strict',
                'date_deadline': fields.Date.today(),
            })
        with self.assertRaises(AccessError):
            Activity.create({
                **billing_values,
                'res_model_id': partner_model.id,
                'res_id': partner.id,
                'activity_type_id': str(review_type.id),
            })

        generic_values = {
            **billing_values,
            'res_model_id': partner_model.id,
            'res_id': partner.id,
            'activity_type_id': todo_type.id,
            'summary': 'Explicit other-model core control',
        }
        control = Activity.with_context(
            default_res_id=False,
            default_activity_type_id=False,
        ).create(generic_values)
        self.assertEqual(control.res_model_id, partner_model)
        self.assertEqual(control.res_id, partner.id)
        self.assertEqual(control.activity_type_id, todo_type)
        self.assertTrue(control.with_context(
            default_res_id=False,
            default_activity_type_id=False,
        ).write({'summary': 'Other-model write keeps core behavior'}))

        strict_values = {
            'res_model_id': billing_model.id,
            'res_id': incident.id,
            'activity_type_id': todo_type.id,
        }
        for field_name, valid_id in strict_values.items():
            unicode_id = ''.join(
                chr(0xFF10 + int(digit)) for digit in str(valid_id))
            for malformed in (
                    '0%s' % valid_id, float(valid_id), True, unicode_id):
                values = {**billing_values, field_name: malformed}
                if field_name == 'res_model_id':
                    values['activity_type_id'] = review_type.id
                with self.subTest(
                        field_name=field_name, malformed=malformed), \
                        self.assertRaises(AccessError):
                    Activity.create(values)

        # Even if a future converter/default path evades preflight, the final
        # stored incident target remains reserved.  Patch only the classifier
        # to force execution through that postcheck.
        ActivityClass = type(Activity)
        with patch.object(
                ActivityClass,
                '_targets_billing_activity_values',
                return_value=False):
            with self.assertRaises(AccessError):
                Activity.create(billing_values)
            before = control.read([
                'res_model_id', 'res_id', 'activity_type_id', 'summary',
            ])[0]
            with self.assertRaises(AccessError):
                control.write({
                    'res_model_id': billing_model.id,
                    'res_id': incident.id,
                })
            self.assertEqual(control.read(list(before))[0], before)

    def test_dedicated_activity_create_and_write_are_not_generic_ingress(self):
        incident_id = self.Incident._log(self._snapshot(
            external_ref=self.ref + '-activity-orm-guard',
            state='needs_human',
        ))
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            Activity = env['mail.activity'].sudo()
            model = env['ir.model']._get('woow.billing.incident')
            review_type = env.ref(
                'woow_paas_platform.'
                'mail_activity_type_billing_incident_review')
            todo_type = env.ref('mail.mail_activity_data_todo')
            activity = Activity.search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident_id),
                ('activity_type_id', '=', review_type.id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(activity)

            create_values = {
                'res_model_id': model.id,
                'res_id': incident_id,
                'activity_type_id': review_type.id,
                'user_id': activity.user_id.id,
                'summary': 'Forged dedicated review',
                'note': 'Created outside the scheduler',
                'date_deadline': fields.Date.today(),
            }
            partner_model = env['ir.model']._get('res.partner')
            for values in (
                    create_values,
                    {**create_values, 'activity_type_id': todo_type.id},
                    {
                        **create_values,
                        'res_model_id': partner_model.id,
                        'res_id': env.user.partner_id.id,
                    }):
                with self.subTest(create_values=values), \
                        self.assertRaises(AccessError):
                    Activity.create(values)

            replacement_user = new_test_user(
                env,
                login='billing-activity-reassign-%s' % uuid.uuid4().hex,
                groups='base.group_user',
            )
            before = activity.read([
                'active', 'note', 'summary', 'res_id',
                'activity_type_id', 'user_id',
            ])[0]
            forbidden_writes = (
                {'active': False},
                {'note': 'PAN 4111111111111111'},
                {'summary': 'Forged summary'},
                {'res_id': incident_id + 1},
                {'activity_type_id': todo_type.id},
                {'user_id': replacement_user.id},
            )
            for values in forbidden_writes:
                with self.subTest(write_values=values), \
                        self.assertRaises(AccessError):
                    activity.write(values)
                self.assertEqual(
                    activity.read(list(before))[0], before,
                    'dedicated activity generic write denial must be atomic',
                )
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
            cr.execute(
                'UPDATE mail_activity SET active = false '
                'WHERE res_model = %s AND res_id = %s '
                'AND activity_type_id = %s',
                ('woow.billing.incident', incident_id, review_type.id),
            )
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

    def test_incident_followers_are_limited_to_current_active_managers(self):
        manager_group = self.env.ref('account.group_account_manager')
        manager = new_test_user(
            self.env,
            login='billing-follower-manager-%s' % uuid.uuid4().hex,
            groups='account.group_account_manager',
        )
        nonmanager = new_test_user(
            self.env,
            login='billing-follower-regular-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-follower-authority',
            'kind': 'charged_not_credited',
            'state': 'needs_human',
        })
        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(incident.id))
        Followers = self.env['mail.followers'].sudo()
        follower_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', incident.id),
        ]
        self.assertEqual(Followers.search_count(
            follower_domain + [('partner_id', '=', manager.partner_id.id)]), 1)
        with self.assertRaises(AccessError):
            incident.with_user(manager).message_subscribe(
                partner_ids=[manager.partner_id.id])
        with self.assertRaises(AccessError):
            incident.with_user(manager).message_subscribe(
                partner_ids=[nonmanager.partner_id.id])
        with self.assertRaises(AccessError):
            incident.with_user(nonmanager).message_subscribe(
                partner_ids=[manager.partner_id.id])

        unrelated = self.env['res.partner'].create({
            'name': 'Follower cleanup scope %s' % uuid.uuid4().hex,
        })
        unrelated.message_subscribe(partner_ids=[manager.partner_id.id])
        manager_group.sudo().write({'users': [(3, manager.id)]})
        self.assertEqual(Followers.search_count(
            follower_domain + [('partner_id', '=', manager.partner_id.id)]), 0)
        self.assertEqual(Followers.search_count([
            ('res_model', '=', 'res.partner'),
            ('res_id', '=', unrelated.id),
            ('partner_id', '=', manager.partner_id.id),
        ]), 1, 'role revocation cleanup must affect only incident followers')
        with self.assertRaises(AccessError):
            incident.with_user(manager).message_subscribe(
                partner_ids=[manager.partner_id.id])

    def test_schedule_converges_removed_added_and_duplicate_manager_roster(self):
        manager_group = self.env.ref('account.group_account_manager')
        removed = new_test_user(
            self.env,
            login='billing-roster-removed-%s' % uuid.uuid4().hex,
            groups='account.group_account_manager',
        )
        added = new_test_user(
            self.env,
            login='billing-roster-added-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-roster-convergence',
            'kind': 'charged_not_credited',
            'state': 'needs_human',
        })
        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(incident.id))
        second_incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-roster-convergence-second',
            'kind': 'charged_not_credited',
            'state': 'orphan',
        })
        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(
                second_incident.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activity_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', incident.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ]
        Activity = self.env['mail.activity'].sudo()
        Followers = self.env['mail.followers'].sudo()
        follower_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', incident.id),
        ]
        self.assertIn(removed.id, Activity.search(activity_domain).user_id.ids)
        self.assertEqual(Followers.search_count(
            follower_domain + [('partner_id', '=', removed.partner_id.id)]), 1)

        self.env.cr.execute(
            'INSERT INTO mail_activity '
            '(res_model_id, res_model, res_id, res_name, activity_type_id, '
            'user_id, summary, note, date_deadline, automated, active, '
            'create_uid, write_uid, create_date, write_date) '
            'SELECT res_model_id, res_model, res_id, res_name, '
            'activity_type_id, user_id, %s, note, date_deadline, automated, '
            'active, create_uid, write_uid, NOW(), NOW() '
            'FROM mail_activity WHERE res_model = %s AND res_id = %s '
            'AND activity_type_id = %s AND active = true '
            'ORDER BY id LIMIT 1',
            (
                'Deliberate duplicate roster fixture',
                'woow.billing.incident', incident.id, review_type.id,
            ),
        )
        Activity.invalidate_model()
        before_activities = Activity.search(activity_domain)
        before_activity_ids = before_activities.ids
        before_user_ids = before_activities.user_id.ids
        before_counts = {
            user_id: Activity.search_count(
                activity_domain + [('user_id', '=', user_id)])
            for user_id in set(before_user_ids)
        }
        self.assertGreater(
            max(before_counts.values()), 1,
            '前置：至少一位現任 manager 必須有 duplicate active activity',
        )

        unrelated_group = self.env['res.groups'].sudo().create({
            'name': 'Unrelated roster group %s' % uuid.uuid4().hex,
        })
        unrelated_group.write({'users': [(4, added.id)]})
        Activity.invalidate_model()
        self.assertEqual(
            Activity.search(activity_domain).ids,
            before_activity_ids,
            '其他 group users 變更不得改動 dedicated activity rows',
        )
        self.assertGreater(
            max(
                Activity.search_count(
                    activity_domain + [('user_id', '=', user_id)])
                for user_id in set(before_user_ids)
            ),
            1,
            '其他 group users 變更不得誤觸 manager roster convergence',
        )

        logger_target = (
            'odoo.addons.woow_paas_platform.models.billing_incident._logger')
        with patch(logger_target) as logger:
            self.assertTrue(manager_group.sudo().write({
                'users': [(3, removed.id), (4, added.id)],
            }))
        manager_group.invalidate_recordset(['users'])
        Activity.invalidate_model()
        current_manager_ids = sorted(manager_group.users.filtered('active').ids)
        final_activities = Activity.search(activity_domain)
        final_user_ids = sorted(final_activities.user_id.ids)
        final_counts = {
            user_id: Activity.search_count(
                activity_domain + [('user_id', '=', user_id)])
            for user_id in current_manager_ids
        }
        self.assertEqual(final_user_ids, current_manager_ids)
        self.assertEqual(
            final_counts, {user_id: 1 for user_id in current_manager_ids},
            'every current manager must have exactly one active dedicated row',
        )
        self.assertEqual(len(final_activities), len(current_manager_ids))
        self.assertNotIn(removed.id, final_user_ids)
        self.assertIn(added.id, final_user_ids)
        self.assertEqual(Followers.search_count(
            follower_domain + [('partner_id', '=', removed.partner_id.id)]), 0)
        self.assertEqual(Followers.search_count(
            follower_domain + [('partner_id', '=', added.partner_id.id)]), 1)
        second_user_ids = sorted(Activity.search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', second_incident.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ]).user_id.ids)
        self.assertEqual(
            second_user_ids,
            current_manager_ids,
            'manager group hook 必須同 transaction 收斂所有 unresolved incidents',
        )

        implied_candidate = new_test_user(
            self.env,
            login='billing-roster-implied-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        implying_group = self.env['res.groups'].sudo().create({
            'name': 'Billing roster implied create path %s' % uuid.uuid4().hex,
            'users': [(4, implied_candidate.id)],
            'implied_ids': [(4, manager_group.id)],
        })
        implied_candidate.invalidate_recordset(['groups_id'])
        Activity.invalidate_model()
        self.assertIn(manager_group, implied_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', implied_candidate.id)]), 1,
            'res.groups create users+implied_ids 必須即時補恰一筆 activity',
        )
        self.assertEqual(Followers.search_count(
            follower_domain + [
                ('partner_id', '=', implied_candidate.partner_id.id),
            ]), 1)

        copy_candidate = new_test_user(
            self.env,
            login='billing-roster-copy-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        copied_group = implying_group.copy(default={
            'name': 'Billing roster implied copy path %s' % uuid.uuid4().hex,
            'users': [(4, copy_candidate.id)],
        })
        copy_candidate.invalidate_recordset(['groups_id'])
        Activity.invalidate_model()
        self.assertIn(manager_group, copy_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', copy_candidate.id)]), 1,
            'res.groups copy users+implied chain 必須即時收斂 activity',
        )
        copied_group._remove_group(manager_group)
        copy_candidate.invalidate_recordset(['groups_id'])
        Activity.invalidate_model()
        self.assertNotIn(manager_group, copy_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', copy_candidate.id)]), 0)

        implying_group._remove_group(manager_group)
        implied_candidate.invalidate_recordset(['groups_id'])
        Activity.invalidate_model()
        self.assertNotIn(manager_group, implied_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', implied_candidate.id)]), 0,
            'res.groups implied_ids remove path 必須即時移除 stale activity',
        )
        self.assertEqual(Followers.search_count(
            follower_domain + [
                ('partner_id', '=', implied_candidate.partner_id.id),
            ]), 0)

        unlink_candidate = new_test_user(
            self.env,
            login='billing-roster-unlink-%s' % uuid.uuid4().hex,
            groups='account.group_account_manager',
        )
        Activity.invalidate_model()
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', unlink_candidate.id)]), 1,
            'res.users.create manager path 必須即時補恰一筆 activity',
        )
        unlink_candidate_id = unlink_candidate.id
        self.assertTrue(unlink_candidate.sudo().unlink())
        Activity.invalidate_model()
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', unlink_candidate_id)]), 0,
            'res.users.unlink manager path 必須移除 stale activity',
        )

        logged = repr(logger.mock_calls)
        self.assertNotIn(
            incident.external_ref, logged,
            'roster convergence logs must not disclose raw MTN/external_ref',
        )

    def test_groups_create_context_ir_defaults_and_copy_converge_roster(self):
        manager_group = self.env.ref('account.group_account_manager')
        incident = self.Incident._create_test_fixture({
            'external_ref': self.ref + '-group-default-carriers',
            'kind': 'charged_not_credited',
            'state': 'needs_human',
        })
        self.assertTrue(
            self.Incident._ensure_review_activities_current_cursor(incident.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activity_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', incident.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ]
        Activity = self.env['mail.activity'].sudo()
        Groups = self.env['res.groups'].sudo()

        context_candidate = new_test_user(
            self.env,
            login='billing-default-context-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        context_group = Groups.with_context(
            default_users=[context_candidate.id],
            default_implied_ids=[manager_group.id],
        ).create({
            'name': 'Billing context-default group %s' % uuid.uuid4().hex,
        })
        context_candidate.invalidate_recordset(['groups_id'])
        self.assertIn(context_group, context_candidate.groups_id)
        self.assertIn(manager_group, context_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', context_candidate.id)]), 1)

        ir_default_candidate = new_test_user(
            self.env,
            login='billing-ir-default-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        Defaults = self.env['ir.default'].sudo()
        Defaults.set('res.groups', 'users', [ir_default_candidate.id])
        Defaults.set('res.groups', 'implied_ids', [manager_group.id])
        self.env.invalidate_all()
        ir_default_group = Groups.create({
            'name': 'Billing ir.default group %s' % uuid.uuid4().hex,
        })
        ir_default_candidate.invalidate_recordset(['groups_id'])
        self.assertIn(ir_default_group, ir_default_candidate.groups_id)
        self.assertIn(manager_group, ir_default_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', ir_default_candidate.id)]), 1)

        copy_candidate = new_test_user(
            self.env,
            login='billing-default-copy-%s' % uuid.uuid4().hex,
            groups='base.group_user',
        )
        copied = context_group.copy(default={
            'name': 'Billing default-carrier copy %s' % uuid.uuid4().hex,
            'users': [(6, 0, [copy_candidate.id])],
        })
        copy_candidate.invalidate_recordset(['groups_id'])
        self.assertIn(copied, copy_candidate.groups_id)
        self.assertIn(manager_group, copy_candidate.groups_id)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', copy_candidate.id)]), 1)

    def test_needs_human_activity_is_per_manager_deduplicated_and_resolved(self):
        manager_ids = self.env.ref(
            'account.group_account_manager').users.filtered('active').ids
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

    def test_activity_identity_is_safe_and_ui_link_still_reveals_mtn(self):
        raw_mtn = '20260830987654321012'
        self._extra_cleanup_refs.add(raw_mtn)
        incident_id = self.Incident._log(self._snapshot(
            external_ref=raw_mtn,
            state='needs_human',
        ))
        self.assertIsInstance(incident_id, int)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT summary, note, res_name, res_id FROM mail_activity '
                'WHERE res_model = %s AND res_id = %s',
                ('woow.billing.incident', incident_id),
            )
            activities = cr.fetchall()
        self.assertTrue(activities)
        for summary, note, res_name, res_id in activities:
            self.assertNotIn(raw_mtn, summary or '')
            self.assertNotIn(raw_mtn, note or '')
            self.assertNotIn(raw_mtn, res_name or '')
            self.assertIn(str(incident_id), summary)
            self.assertIn(str(incident_id), note)
            self.assertIn(str(incident_id), res_name)
            self.assertIn('Charged but not credited', summary)
            self.assertIn('Charged but not credited', note)
            self.assertIn('Charged but not credited', res_name)
            self.assertEqual(res_id, incident_id)

        manager = self._account_manager_users_for_test(self.env)[:1]
        with self.env.registry.cursor() as cr:
            manager_env = self.env(cr=cr, user=manager.id)
            linked_incident = manager_env['woow.billing.incident'].browse(
                activities[0][3])
            self.assertEqual(
                linked_incident.read(['external_ref'])[0]['external_ref'],
                raw_mtn,
                'activity res_model/res_id link must still open the incident '
                'whose explicit evidence column shows the raw MTN',
            )

        malicious_ref = self.ref + '-<img-src=x-onerror=alert864>'
        logger = 'odoo.addons.woow_paas_platform.models.billing_incident'
        with self.assertLogs(logger, level='ERROR') as logs:
            self.assertFalse(self.Incident._log(self._snapshot(
                external_ref=malicious_ref,
                state='needs_human',
            )))
        self.assertNotIn(malicious_ref, '\n'.join(logs.output))

    def test_activity_orm_insert_sql_failure_never_logs_raw_mtn(self):
        raw_mtn = '20260831987654321012'
        self._extra_cleanup_refs.add(raw_mtn)
        ActivityClass = type(self.env['mail.activity'])
        original_create = ActivityClass.create
        fault_params = []
        seen_activity_sql = []

        def create_with_activity_insert_failure(records, values_list):
            original_execute = records.env.cr.execute

            def execute(query, params=None, log_exceptions=True):
                query_code = getattr(query, 'code', query)
                query_params = getattr(query, 'params', params)
                query_text = (
                    query_code.decode(errors='replace')
                    if isinstance(query_code, bytes)
                    else str(query_code)
                )
                seen_activity_sql.append(query_text)
                if (not fault_params
                        and re.search(
                            r'INSERT\s+INTO\s+"?mail_activity"?',
                            query_text,
                            re.IGNORECASE)):
                    fault_params.append(repr(query_params))
                    return original_execute(
                        'BILLING_ACTIVITY_FAULT_INJECT ' + query_text,
                        query_params,
                        log_exceptions=log_exceptions,
                    )
                return original_execute(
                    query, params, log_exceptions=log_exceptions)

            records.env.cr.execute = execute
            try:
                return original_create(records, values_list)
            finally:
                records.env.cr.execute = original_execute

        with patch.object(
                ActivityClass, 'create',
                create_with_activity_insert_failure), self.assertLogs(
                    'odoo.sql_db', level='ERROR') as sql_logs:
            incident_id = self.Incident._log(self._snapshot(
                external_ref=raw_mtn,
                state='orphan',
            ))
        self.assertIsInstance(incident_id, int)
        self.assertTrue(
            fault_params,
            'fault injection must intercept the real mail.activity ORM INSERT; '
            'seen=%r' % seen_activity_sql,
        )
        self.assertEqual(
            sum(raw_mtn in params for params in fault_params), 0,
            'mail.activity ORM SQL params must never contain raw external_ref',
        )
        self.assertEqual(
            '\n'.join(sql_logs.output).count(raw_mtn), 0,
            'odoo.sql_db must not render raw MTN on activity INSERT failure',
        )
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
        self.assertEqual(
            en_summary,
            'Billing Incident #%s: Charged but not credited requires '
            'manual review' % self.incident.id,
        )
        self.assertEqual(
            zh_summary,
            '帳務事件 #%s：已扣款但未入點，需要人工檢查' % self.incident.id,
        )
        self.assertIn('Billing Incident #%s' % self.incident.id, en_note)
        self.assertIn('帳務事件 #%s' % self.incident.id, zh_note)
        self.assertIn('Charged but not credited', en_note)
        self.assertIn('已扣款但未入點', zh_note)
        self.assertIn(
            'does not resolve the incident or send a resolution notification',
            en_note,
        )
        self.assertIn('不會關閉 incident，也不會寄送結案通知', zh_note)
        self.assertNotIn(self.incident.external_ref, en_summary + en_note)
        self.assertNotIn(self.incident.external_ref, zh_summary + zh_note)
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
        attachment = self.env['ir.attachment'].sudo().create({
            'name': 'module-uninstall-incident-evidence.bin',
            'datas': base64.b64encode(b'legacy direct evidence'),
        })
        self.env.cr.execute(
            'UPDATE ir_attachment SET res_model = %s, res_id = %s '
            'WHERE id = %s',
            ('woow.billing.incident', self.incident.id, attachment.id),
        )
        attachment.invalidate_recordset(['res_model', 'res_id'])
        module.write({'state': 'to remove'})
        self.assertTrue(
            attachment.sudo().with_context(_force_unlink=True).unlink())
        self.assertFalse(attachment.exists())
        self.assertTrue(
            self.incident.sudo().with_context(_force_unlink=True).unlink())
        self.assertFalse(self.incident.exists())
        # TransactionCase rollback restores module state and deleted rows.

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
                'Billing incident access uses the Odoo role '
                '`account.group_account_manager` (not a Workspace role). It '
                'reads all incidents across all companies; review activities '
                'go to all current managers in that Odoo group; no company '
                'M2O/rule; base.group_system behavior is not narrowed.'
            ): (
                '帳務 incident 存取使用 Odoo role '
                '`account.group_account_manager`（不是 Workspace role）；可跨'
                '全部公司讀取全部 incident，review activity 派給該 Odoo group '
                '所有現任 manager；不加 company M2O/rule，也不縮減 '
                'base.group_system 行為。'
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
        self.system_user, self.system_login = self._new_user(
            'billing-rpc-system-%s' % marker,
            self.env.ref('base.group_system'),
            self.company_a,
        )
        Incident = self.env['woow.billing.incident']
        self.incident_a = Incident._create_test_fixture({
            'external_ref': 'rpc:a-%s' % marker,
            'kind': 'charged_not_credited',
            'state': 'needs_human',
            'source_model': 'res.company',
            'source_ref': self.company_a.id,
            'trade_no': 'RPC-TRADE-A',
            'detail': 'company A safe summary',
        })
        self.incident_b = Incident._create_test_fixture({
            'external_ref': 'rpc:b-%s' % marker,
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

    def _assert_access_error(self, result, message, contains=None):
        self.assertIn('error', result, '%s（回 %r）' % (message, result))
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('AccessError', name, '%s（回 %r）' % (message, result))
        if contains:
            error_text = json.dumps(result['error'], ensure_ascii=False)
            self.assertIn(contains, error_text, '%s（回 %r）' % (message, result))

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

    def _rpc_error_signature(self, response):
        error_data = response.get('error', {}).get('data', {})
        return (error_data.get('name'), error_data.get('message'))

    def test_generic_write_preflight_has_no_existence_or_lock_oracle(self):
        IncidentClass = type(self.env['woow.billing.incident'])
        original_lock = IncidentClass._lock_and_refresh_states
        lock_calls = []

        def lock_spy(records, *args, **kwargs):
            lock_calls.append((records.env.uid, tuple(records.ids)))
            return original_lock(records, *args, **kwargs)

        with patch.object(
                IncidentClass, '_lock_and_refresh_states', lock_spy):
            self.authenticate(self.regular_login, self.regular_login)
            missing_id = self.incident_b.id + 10 ** 7
            started = time.monotonic()
            missing = self._call_kw(
                'woow.billing.incident', 'write',
                [[missing_id], {
                    'resolution_note': 'nonmanager missing probe',
                }],
            )
            missing_elapsed = time.monotonic() - started
            started = time.monotonic()
            existing = self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {
                    'resolution_note': 'nonmanager existing probe',
                }],
            )
            existing_elapsed = time.monotonic() - started

            self.authenticate(self.manager_a_login, self.manager_a_login)
            forbidden = self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {'state': 'open'}],
            )

        self._assert_access_error(
            missing,
            'nonmanager missing-id write must fail at role preflight',
        )
        self._assert_access_error(
            existing,
            'nonmanager existing-id write must fail at role preflight',
        )
        self.assertEqual(
            self._rpc_error_signature(existing),
            self._rpc_error_signature(missing),
            'existing/nonexistent ids must expose the exact same AccessError',
        )
        self.assertLess(
            abs(existing_elapsed - missing_elapsed),
            0.5,
            'existing/nonexistent true-RPC denial timing must not expose '
            'row existence',
        )
        self._assert_access_error(
            forbidden,
            'manager forbidden values must fail at the values allowlist',
        )
        self.assertEqual(
            lock_calls,
            [],
            'role/ACL/value denials must never reach SELECT FOR UPDATE',
        )

    def test_nonmanager_denial_cannot_make_durable_log_lock_timeout(self):
        IncidentClass = type(self.env['woow.billing.incident'])
        original_write = IncidentClass.write
        denial_reached = threading.Event()
        release_denial = threading.Event()
        rpc_result = {}

        def hold_rpc_transaction_after_denial(records, values):
            try:
                return original_write(records, values)
            except AccessError:
                if records.env.uid == self.regular_user.id:
                    denial_reached.set()
                    # HttpCase serializes test cursors across request threads;
                    # a short overlap is enough here.  The companion RPC spy
                    # above proves this denial never invokes the row-lock seam.
                    release_denial.wait(timeout=0.5)
                raise

        self.authenticate(self.regular_login, self.regular_login)

        def denied_rpc_write():
            rpc_result['response'] = self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {
                    'resolution_note': 'held nonmanager denial',
                }],
            )

        with patch.object(
                IncidentClass, 'write', hold_rpc_transaction_after_denial):
            worker = threading.Thread(
                target=denied_rpc_write,
                name='billing-preauth-held-denial-rpc',
            )
            worker.start()
            self.assertTrue(
                denial_reached.wait(timeout=2),
                'true RPC must reach and hold its AccessError transaction',
            )
            try:
                logger_target = (
                    'odoo.addons.woow_paas_platform.models.'
                    'billing_incident._logger')
                with patch(logger_target) as logger:
                    logged_id = self.env['woow.billing.incident']._log({
                        'external_ref': self.incident_a.external_ref,
                        'kind': self.incident_a.kind,
                        'detail': 'pre-auth lock concurrency control',
                    })
                self.assertEqual(
                    logged_id,
                    self.incident_a.id,
                    'nonmanager denial must hold no row lock that can trip '
                    '_log lock_timeout',
                )
                self.assertNotIn(
                    'category=persistence_failed', repr(logger.mock_calls))
            finally:
                release_denial.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self._assert_access_error(
            rpc_result['response'],
            'held true RPC must finish as the expected AccessError',
        )

    def test_role_only_manager_allowed_and_system_only_denied(self):
        manager_group = self.env.ref('account.group_account_manager')
        system_group = self.env.ref('base.group_system')
        user_group = self.env.ref('base.group_user')
        self.assertIn(manager_group, self.manager_a.groups_id)
        self.assertIn(user_group, self.manager_a.groups_id)
        self.assertNotIn(system_group, self.manager_a.groups_id)
        self.assertIn(system_group, self.system_user.groups_id)
        self.assertIn(user_group, self.system_user.groups_id)
        self.assertNotIn(manager_group, self.system_user.groups_id)
        menu_id = self.env.ref(
            'woow_paas_platform.menu_billing_incidents').id

        self.authenticate(self.manager_a_login, self.manager_a_login)
        rows = self._assert_ok(
            self._call_kw(
                'woow.billing.incident', 'read',
                [[self.incident_a.id], ['external_ref']],
            ),
            'account-manager-only 應可讀 global incident ledger',
        )
        self.assertEqual(rows[0]['external_ref'], self.incident_a.external_ref)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {
                    'manual_check_result': 'ecpay_found',
                    'resolution_note': 'role-only manager manual audit',
                }],
            ),
            'account-manager-only 應可保存人工欄位',
        ))
        manager_menus = self._assert_ok(
            self._call_kw(
                'ir.ui.menu', 'search_read',
                [[('id', '=', menu_id)], ['id']],
            ),
            'account-manager-only menu 查詢應成功',
        )
        self.assertEqual([row['id'] for row in manager_menus], [menu_id])

        before = self._audit_snapshot(self.incident_a)
        self.authenticate(self.system_login, self.system_login)
        self._assert_access_error(
            self._call_kw(
                'woow.billing.incident', 'read',
                [[self.incident_a.id], ['external_ref']],
            ),
            '純 base.group_system 不得讀 incident',
        )
        self._assert_access_error(
            self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {
                    'resolution_note': 'system-only forbidden write',
                }],
            ),
            '純 base.group_system 不得寫 incident 人工欄',
        )
        system_menus = self._assert_ok(
            self._call_kw(
                'ir.ui.menu', 'search_read',
                [[('id', '=', menu_id)], ['id']],
            ),
            'system-only menu 查詢本身應成功',
        )
        self.assertEqual(system_menus, [])
        self.assertEqual(self._audit_snapshot(self.incident_a), before)

    def test_manager_revocation_is_immediate_across_incident_mail_graph(self):
        manager_group = self.env.ref('account.group_account_manager')
        user_group = self.env.ref('base.group_user')
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        Incident = self.env['woow.billing.incident']
        self.assertTrue(
            Incident._ensure_review_activities_current_cursor(
                self.incident_a.id))
        activity = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('user_id', '=', self.manager_a.id),
            ('active', '=', True),
        ], limit=1)
        manager_activity = activity.with_user(self.manager_a)
        protected_message = self.incident_a.with_user(
            self.manager_a).message_post_with_source(
                'mail.message_activity_done',
                author_id=self.manager_a.partner_id.id,
                render_values={
                    'activity': manager_activity,
                    'feedback': 'role revocation message ownership probe',
                    'display_assignee': False,
                },
                mail_activity_type_id=manager_activity.activity_type_id.id,
                subtype_xmlid='mail.mt_activities',
            )
        self.assertTrue(protected_message)
        self.assertEqual(protected_message.author_id, self.manager_a.partner_id)
        follower_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
        ]
        Followers = self.env['mail.followers'].sudo()
        self.assertEqual(Followers.search_count(
            follower_domain + [
                ('partner_id', '=', self.manager_a.partner_id.id),
            ]), 1, 'activity create must subscribe its current manager assignee')

        # Exercise the standard res.users API path used by the user form.  A
        # res.groups.write(users=...) hook alone does not see these commands.
        self.authenticate(self.system_login, self.system_login)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'res.users', 'write',
                [[self.manager_a.id], {
                    'groups_id': [(3, manager_group.id)],
                }],
            ),
            'standard user.write must remove the account-manager role',
        ))
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'res.users', 'write',
                [[self.regular_user.id], {
                    'groups_id': [(4, manager_group.id)],
                }],
            ),
            'standard user.write must add the account-manager role',
        ))
        self.manager_a.invalidate_recordset(['groups_id', 'active'])
        self.regular_user.invalidate_recordset(['groups_id', 'active'])
        self.assertIn(user_group, self.manager_a.groups_id)
        self.assertNotIn(manager_group, self.manager_a.groups_id)
        self.assertIn(manager_group, self.regular_user.groups_id)
        activity_domain = [
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ]
        Activity = self.env['mail.activity'].sudo()
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', self.manager_a.id)]), 0)
        self.assertEqual(Followers.search_count(
            follower_domain + [
                ('partner_id', '=', self.manager_a.partner_id.id),
            ]), 0, 'role removal must synchronously revoke incident following')
        replacement = Activity.search(
            activity_domain + [('user_id', '=', self.regular_user.id)],
            limit=1,
        )
        self.assertEqual(
            len(replacement), 1,
            '新 manager 必須在 user.write 同 transaction 補恰一筆 activity',
        )
        for incident in (self.incident_a, self.incident_b):
            self.assertEqual(Activity.search_count([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', incident.id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.regular_user.id),
                ('active', '=', True),
            ]), 1, 'user.write must converge every unresolved incident')

        self.assertTrue(self._assert_ok(
            self._call_kw(
                'res.users', 'write',
                [[self.regular_user.id], {'active': False}],
            ),
            'deactivate current manager through standard user.write',
        ))
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', self.regular_user.id)]), 0)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'res.users', 'write',
                [[self.regular_user.id], {'active': True}],
            ),
            'reactivate current manager through standard user.write',
        ))
        self.regular_user.invalidate_recordset(['active', 'groups_id'])
        self.assertTrue(self.regular_user.active)
        self.assertIn(manager_group, self.regular_user.groups_id)
        replacement = Activity.search(
            activity_domain + [('user_id', '=', self.regular_user.id)],
        )
        self.assertEqual(
            len(replacement), 1,
            'reactivation must restore exactly one active dedicated activity',
        )
        self.assertEqual(Followers.search_count(
            follower_domain + [
                ('partner_id', '=', self.regular_user.partner_id.id),
            ]), 1, 'reactivated current manager must regain necessary following')

        message_floor = self.env['mail.message'].sudo().search(
            [], order='id desc', limit=1).id
        notification_floor = self.env['mail.notification'].sudo().search(
            [], order='id desc', limit=1).id
        mail_floor = self.env['mail.mail'].sudo().search(
            [], order='id desc', limit=1).id
        feedback = 'post-revocation terminal feedback stays ephemeral'
        self.assertFalse(
            replacement.with_user(self.regular_user).action_feedback(
                feedback=feedback),
            'dedicated action_feedback returns False for empty message contract',
        )
        self.assertFalse(self.env['mail.message'].sudo().search([
            ('id', '>', message_floor),
        ]), 'dedicated terminal must create no mail.message')
        partner_ids = (
            self.manager_a.partner_id | self.regular_user.partner_id).ids
        self.assertFalse(self.env['mail.notification'].sudo().search([
            ('id', '>', notification_floor),
            ('res_partner_id', 'in', partner_ids),
        ]), 'former/current notification rows must both stay zero')
        self.assertFalse(self.env['mail.mail'].sudo().search([
            ('id', '>', mail_floor),
            ('recipient_ids', 'in', partner_ids),
        ]), 'former/current outgoing incident mail must both stay zero')
        self.assertFalse(self.env['mail.message'].sudo().search([
            ('body', 'ilike', feedback),
        ]), 'dedicated activity feedback must not persist')
        replacement = Activity.search(
            activity_domain + [('user_id', '=', self.regular_user.id)],
            limit=1,
        )
        self.assertEqual(len(replacement), 1)

        self.authenticate(self.regular_login, self.regular_login)
        current_message = self._assert_ok(
            self._call_kw(
                'mail.message', 'read',
                [[protected_message.id], ['body']],
            ),
            'current manager must retain normal incident-message read access',
        )
        self.assertEqual(current_message[0]['id'], protected_message.id)
        self._assert_access_error(
            self._call_kw(
                'woow.billing.incident', 'message_subscribe',
                [[self.incident_a.id], [self.regular_user.partner_id.id]],
            ),
            '一般 current manager 也不得直接訂閱；只有 scheduler capability '
            '可維護 follower roster',
        )

        # A deliberately corrupt generic-type incident activity proves that
        # current-group search/read/action gates cover every incident target,
        # not only the dedicated review type.  Roster convergence above must
        # still remove the real stale dedicated row assigned to the old user.
        todo_type = self.env.ref('mail.mail_activity_data_todo')
        self.env.cr.execute(
            'INSERT INTO mail_activity '
            '(res_model_id, res_model, res_id, res_name, activity_type_id, '
            'user_id, summary, note, date_deadline, automated, active, '
            'create_uid, write_uid, create_date, write_date) '
            'SELECT res_model_id, res_model, res_id, res_name, %s, '
            '%s, summary, note, date_deadline, automated, active, '
            'create_uid, write_uid, NOW(), NOW() '
            'FROM mail_activity WHERE id = %s RETURNING id',
            (todo_type.id, self.manager_a.id, replacement.id),
        )
        stale_activity_id = self.env.cr.fetchone()[0]
        Activity.invalidate_model()

        self.authenticate(self.manager_a_login, self.manager_a_login)
        self._assert_access_error(
            self._call_kw(
                'woow.billing.incident', 'read',
                [[self.incident_a.id], ['external_ref']],
            ),
            '離組後 incident read 必須立即拒絕',
        )
        self._assert_access_error(
            self._call_kw(
                'woow.billing.incident', 'message_subscribe',
                [[self.incident_a.id], [self.manager_a.partner_id.id]],
            ),
            '離組後不得重新訂閱 incident',
        )
        self._assert_access_error(
            self._call_kw(
                'mail.activity', 'read',
                [[stale_activity_id], ['summary']],
            ),
            '離組後即使 assigned 也不得 read generic incident activity',
        )
        self._assert_access_error(
            self._call_kw(
                'mail.activity', 'action_done', [[stale_activity_id]],
            ),
            '離組後即使 assigned 也不得 action generic incident activity',
        )
        self._assert_access_error(
            self._call_kw(
                'mail.message', 'read',
                [[protected_message.id], ['body']],
            ),
            '離組後即使是 author/create_uid 也不得 read incident message',
        )
        visible_activities = self._assert_ok(
            self._call_kw(
                'mail.activity', 'search_read',
                [[('id', '=', stale_activity_id)], ['id']],
            ),
            '離組後 activity search 本身應成功但必須 filter target',
        )
        visible_messages = self._assert_ok(
            self._call_kw(
                'mail.message', 'search_read',
                [[('id', '=', protected_message.id)], ['id']],
            ),
            '離組後 message search 本身應成功但必須 filter target',
        )
        self.assertEqual(visible_activities, [])
        self.assertEqual(visible_messages, [])

        self.env.cr.execute(
            'DELETE FROM mail_activity WHERE id = %s', (stale_activity_id,))
        Activity.invalidate_model()
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', self.manager_a.id)]), 0)
        self.assertEqual(Activity.search_count(
            activity_domain + [('user_id', '=', self.regular_user.id)]), 1)

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

    def test_manager_rpc_cannot_create_dedicated_billing_activity(self):
        self.authenticate(self.manager_a_login, self.manager_a_login)
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        model = self.env['ir.model']._get('woow.billing.incident')
        values = {
            'res_model_id': model.id,
            'res_id': self.incident_a.id,
            'activity_type_id': review_type.id,
            'user_id': self.manager_a.id,
            'summary': 'Forged RPC billing review',
            'note': 'RPC must not mint dedicated activity rows',
            'date_deadline': fields.Date.to_string(fields.Date.today()),
        }
        for context in ({}, {'woow_billing_activity_scheduler': True}):
            with self.subTest(context=context):
                denied = self._call_kw(
                    'mail.activity',
                    'create',
                    [values],
                    {'context': context},
                )
                self._assert_access_error(
                    denied,
                    'manager RPC 不得建立 billing incident dedicated activity',
                )
        self.assertFalse(self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
        ]))

    def test_rpc_terminal_context_cannot_poison_rearmed_activity(self):
        Incident = self.env['woow.billing.incident']
        self.assertTrue(
            Incident._ensure_review_activities_current_cursor(
                self.incident_a.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        detached = self.env['ir.attachment'].sudo().create({
            'name': 'hostile-default-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'must never reach rearmed activity'),
        })
        hostile_context = {
            'default_active': False,
            'default_attachment_ids': [detached.id],
        }

        self.authenticate(self.manager_a_login, self.manager_a_login)
        for method in ('action_done', 'unlink'):
            activity = self.env['mail.activity'].sudo().search([
                ('res_model', '=', 'woow.billing.incident'),
                ('res_id', '=', self.incident_a.id),
                ('activity_type_id', '=', review_type.id),
                ('user_id', '=', self.manager_a.id),
                ('active', '=', True),
            ], limit=1)
            self.assertTrue(activity, 'terminal 前置必須有 active review activity')
            terminal = self._call_kw(
                'mail.activity', method, [[activity.id]],
                {'context': hostile_context},
            )
            self._assert_ok(
                terminal,
                '真 RPC %s 不得讓 hostile default_* 污染 scheduler' % method,
            )
            replacements = self.env['mail.activity'].sudo().with_context(
                active_test=False).search([
                    ('res_model', '=', 'woow.billing.incident'),
                    ('res_id', '=', self.incident_a.id),
                    ('activity_type_id', '=', review_type.id),
                    ('user_id', '=', self.manager_a.id),
                ])
            self.assertEqual(
                len(replacements), 1,
                'terminal 後完成 user 必須恰一筆 dedicated activity',
            )
            self.assertTrue(replacements.active)
            self.assertFalse(
                replacements.attachment_ids,
                'caller default_attachment_ids 不得進入 internal create',
            )

    def test_manager_rpc_related_create_write_unlink_and_move_guards(self):
        Incident = self.env['woow.billing.incident']
        self.assertTrue(
            Incident._ensure_review_activities_current_cursor(
                self.incident_a.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activity = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('user_id', '=', self.manager_a.id),
            ('active', '=', True),
        ], limit=1)
        self.assertTrue(activity)
        manager_activity = activity.with_user(self.manager_a)
        protected_message = self.incident_a.with_user(
            self.manager_a).message_post_with_source(
                'mail.message_activity_done',
                author_id=self.manager_a.partner_id.id,
                render_values={
                    'activity': manager_activity,
                    'feedback': 'plain RPC control verification',
                    'display_assignee': False,
                },
                mail_activity_type_id=manager_activity.activity_type_id.id,
                subtype_xmlid='mail.mt_activities',
            )
        replacement_activity = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('active', '=', True),
        ], limit=1)
        self.assertTrue(protected_message and replacement_activity)
        control_project = self.env['project.project'].with_user(
            self.manager_a).create({
                'name': 'Unrelated RPC control %s' % uuid.uuid4().hex,
            })
        generic_activity = self.env['mail.activity'].sudo().create({
            'res_model_id': self.env['ir.model']._get('project.project').id,
            'res_id': control_project.id,
            'activity_type_id': self.env.ref(
                'mail.mail_activity_data_todo').id,
            'user_id': self.manager_a.id,
            'summary': 'Unrelated RPC attachment control',
            'note': 'Billing guards must not affect this activity',
            'date_deadline': fields.Date.today(),
        })
        self.env.cr.flush()

        self.authenticate(self.manager_a_login, self.manager_a_login)
        for body in (
                'generic safe RPC body',
                'PAN 4111111111111111',
                'ResultData=forbidden-rpc-related'):
            denied = self._call_kw(
                'mail.message',
                'create',
                [{
                    'model': 'woow.billing.incident',
                    'res_id': self.incident_a.id,
                    'message_type': 'comment',
                    'body': body,
                }],
                {'context': {'woow_billing_incident_chatter': True}},
            )
            self._assert_access_error(
                denied,
                'manager RPC cannot forge incident chatter create capability',
                contains='internal activity completion flow',
            )
            denied_write = self._call_kw(
                'mail.message',
                'write',
                [[protected_message.id], {'body': body}],
            )
            self._assert_access_error(
                denied_write,
                'manager RPC cannot rewrite incident-owned chatter',
                contains='managed automatically',
            )

        protected_targets = (
            ('woow.billing.incident', self.incident_a.id),
            ('mail.activity', replacement_activity.id),
            ('mail.message', protected_message.id),
        )
        for index, (res_model, res_id) in enumerate(protected_targets):
            for context in (
                    {},
                    {'woow_billing_attachment_cleanup': True}):
                name = 'rpc-forbidden-related-%s-%s.bin' % (
                    index, uuid.uuid4().hex)
                denied_create = self._call_kw(
                    'ir.attachment',
                    'create',
                    [{
                        'name': name,
                        'datas': base64.b64encode(
                            b'forbidden direct payload').decode(),
                        'res_model': res_model,
                        'res_id': res_id,
                    }],
                    {'context': context},
                )
                self._assert_access_error(
                    denied_create,
                    'RPC attachment create cannot forge cleanup capability',
                    contains='Attachments are not allowed',
                )
                self.assertFalse(self.env['ir.attachment'].sudo().search([
                    ('name', '=', name),
                ]))

        control_name = 'rpc-related-control-%s.bin' % uuid.uuid4().hex
        control_id = self._assert_ok(
            self._call_kw(
                'ir.attachment',
                'create',
                [{
                    'name': control_name,
                    'datas': base64.b64encode(b'legal control').decode(),
                    'res_model': 'mail.activity',
                    'res_id': generic_activity.id,
                }],
            ),
            'unrelated attachment create must remain legal',
        )
        self.assertIsInstance(control_id, int)
        denied_move = self._call_kw(
            'ir.attachment',
            'write',
            [[control_id], {
                'datas': base64.b64encode(b'forbidden move payload').decode(),
                'res_model': 'woow.billing.incident',
                'res_id': self.incident_a.id,
            }],
            {'context': {'woow_billing_attachment_cleanup': True}},
        )
        self._assert_access_error(
            denied_move,
            'RPC old→incident attachment move must reject atomically',
            contains='Attachments are not allowed',
        )
        control = self.env['ir.attachment'].sudo().browse(control_id)
        control.invalidate_recordset(['res_model', 'res_id', 'datas'])
        self.assertEqual(control.res_model, 'mail.activity')
        self.assertEqual(control.res_id, generic_activity.id)
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'ir.attachment',
                'write',
                [[control_id], {'name': control_name + '.renamed'}],
            ),
            'unrelated attachment write must remain legal',
        ))

        # Direct evidence rows can exist from legacy data or controlled data
        # repair even though the create guard rejects that shape today.
        # Corrupt only their generic relation stamps so the unlink operation
        # under test still crosses the real account-manager RPC seam.
        purposes = (
            'single', 'activity', 'message', 'forged', 'mixed', 'cleanup',
        )
        legacy_protected = self.env['ir.attachment'].sudo().create([{
            'name': 'rpc-direct-unlink-%s-%s.bin' % (
                purpose, uuid.uuid4().hex),
            'datas': base64.b64encode(b'legacy direct evidence'),
        } for purpose in purposes])
        protected_stamps = (
            ('woow.billing.incident', self.incident_a.id),
            ('mail.activity', replacement_activity.id),
            ('mail.message', protected_message.id),
            ('woow.billing.incident', self.incident_a.id),
            ('woow.billing.incident', self.incident_a.id),
            ('woow.billing.incident', self.incident_a.id),
        )
        for attachment, (res_model, res_id) in zip(
                legacy_protected, protected_stamps):
            self.env.cr.execute(
                'UPDATE ir_attachment SET res_model = %s, res_id = %s '
                'WHERE id = %s',
                (res_model, res_id, attachment.id),
            )
        legacy_protected.invalidate_recordset(['res_model', 'res_id'])
        (
            protected_single,
            protected_activity_attachment,
            protected_message_attachment,
            protected_forged,
            protected_mixed,
            protected_cleanup,
        ) = legacy_protected

        def attachment_ids_still_in_db(attachment_ids):
            # A fresh third cursor + raw DB read avoids trusting stale ORM
            # recordsets after the real HTTP request transaction returns.
            with self.env.registry.cursor() as cr:
                cr.execute(
                    'SELECT id FROM ir_attachment WHERE id = ANY(%s) '
                    'ORDER BY id',
                    (attachment_ids,),
                )
                return [row_id for row_id, in cr.fetchall()]

        denied_unlink = self._call_kw(
            'ir.attachment', 'unlink', [[protected_single.id]],
        )
        self._assert_access_error(
            denied_unlink,
            'account manager RPC 不得刪 direct incident evidence attachment',
            contains='Attachments are not allowed',
        )
        self.assertEqual(
            attachment_ids_still_in_db([protected_single.id]),
            [protected_single.id],
            '真 RPC 拒絕後第三 cursor／DB 仍必須保有 direct evidence',
        )

        for carrier_attachment in (
                protected_activity_attachment,
                protected_message_attachment):
            carrier_denial = self._call_kw(
                'ir.attachment', 'unlink', [[carrier_attachment.id]],
            )
            self._assert_access_error(
                carrier_denial,
                'account manager RPC 不得刪 incident-owned carrier attachment',
                contains='Attachments are not allowed',
            )
        carrier_attachment_ids = sorted([
            protected_activity_attachment.id,
            protected_message_attachment.id,
        ])
        self.assertEqual(
            attachment_ids_still_in_db(carrier_attachment_ids),
            carrier_attachment_ids,
        )

        forged_unlink = self._call_kw(
            'ir.attachment', 'unlink', [[protected_forged.id]],
            {'context': {'woow_billing_attachment_cleanup': True}},
        )
        self._assert_access_error(
            forged_unlink,
            'JSON truthy context 不得偽造 private attachment cleanup capability',
            contains='Attachments are not allowed',
        )
        self.assertEqual(
            attachment_ids_still_in_db([protected_forged.id]),
            [protected_forged.id],
        )

        mixed_ids = [control_id, protected_mixed.id]
        try:
            self.env['ir.attachment'].with_user(
                self.manager_a).browse(mixed_ids).unlink()
        except AccessError:
            pass
        else:
            self.fail(
                'mixed protected＋ordinary recordset 必須 preflight 後整批拒絕')
        self.assertEqual(
            attachment_ids_still_in_db(mixed_ids),
            sorted(mixed_ids),
            'caller catch AccessError 後也不得 partial delete 任一 attachment',
        )

        self.assertTrue(self._assert_ok(
            self._call_kw('ir.attachment', 'unlink', [[control_id]]),
            'account manager 必須仍可刪 unrelated attachment 正對照',
        ))
        self.assertEqual(
            attachment_ids_still_in_db([control_id]),
            [],
            'unrelated positive control 必須真的從 DB 刪除',
        )

        from odoo.addons.woow_paas_platform.models import billing_incident
        self.assertTrue(
            billing_incident._attachment_cleanup(protected_cleanup).unlink(),
            'server-only private cleanup capability 必須保留正常清理路徑',
        )
        self.assertEqual(
            attachment_ids_still_in_db([protected_cleanup.id]),
            [],
        )

        control_message_id = self._assert_ok(
            self._call_kw(
                'mail.message',
                'create',
                [{
                    'model': 'project.project',
                    'res_id': control_project.id,
                    'message_type': 'comment',
                    'body': 'unrelated RPC message remains legal',
                }],
            ),
            'unrelated mail.message create must remain legal',
        )
        self.assertIsInstance(control_message_id, int)

    def test_rpc_attachment_target_ids_are_canonicalized_fail_closed(self):
        self.assertTrue(
            self.env['woow.billing.incident']
            ._ensure_review_activities_current_cursor(self.incident_a.id))
        review_type = self.env.ref(
            'woow_paas_platform.mail_activity_type_billing_incident_review')
        activity = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('user_id', '=', self.manager_a.id),
            ('active', '=', True),
        ], limit=1)
        manager_activity = activity.with_user(self.manager_a)
        protected_message = self.incident_a.with_user(
            self.manager_a).message_post_with_source(
                'mail.message_activity_done',
                author_id=self.manager_a.partner_id.id,
                render_values={
                    'activity': manager_activity,
                    'feedback': 'canonical attachment guard setup',
                    'display_assignee': False,
                },
                mail_activity_type_id=manager_activity.activity_type_id.id,
                subtype_xmlid='mail.mt_activities',
            )
        replacement = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'woow.billing.incident'),
            ('res_id', '=', self.incident_a.id),
            ('activity_type_id', '=', review_type.id),
            ('user_id', '=', self.manager_a.id),
            ('active', '=', True),
        ], limit=1)
        control_project = self.env['project.project'].with_user(
            self.manager_a).create({
                'name': 'Canonical attachment control %s' % uuid.uuid4().hex,
            })
        legacy_protected = self.env['ir.attachment'].sudo().create({
            'name': 'legacy-protected-%s.bin' % uuid.uuid4().hex,
            'datas': base64.b64encode(b'legacy protected row'),
        })
        self.env.cr.execute(
            'UPDATE ir_attachment SET res_model = %s, res_id = %s '
            'WHERE id = %s',
            ('mail.message', protected_message.id, legacy_protected.id),
        )
        legacy_protected.invalidate_recordset(['res_model', 'res_id'])
        self.env.cr.flush()

        self.authenticate(self.manager_a_login, self.manager_a_login)
        protected_targets = (
            ('woow.billing.incident', self.incident_a.id),
            ('mail.activity', replacement.id),
            ('mail.message', protected_message.id),
        )
        for res_model, protected_id in protected_targets:
            for supplied_id in (
                    str(protected_id), '0%s' % protected_id,
                    float(protected_id), True):
                name = 'canonical-denied-%s-%s.bin' % (
                    res_model.replace('.', '-'), uuid.uuid4().hex)
                denied = self._call_kw(
                    'ir.attachment',
                    'create',
                    [{
                        'name': name,
                        'datas': base64.b64encode(b'denied payload').decode(),
                        'res_model': res_model,
                        'res_id': supplied_id,
                    }],
                )
                self._assert_access_error(
                    denied,
                    'protected carrier 的 string/float/bool id 必須 fail closed',
                    contains='Attachments are not allowed',
                )
                self.assertFalse(self.env['ir.attachment'].sudo().search([
                    ('name', '=', name),
                ]))

        denied_old_target = self._call_kw(
            'ir.attachment',
            'write',
            [[legacy_protected.id], {'name': 'forbidden-old-target-rename'}],
            {'context': {'woow_billing_attachment_cleanup': True}},
        )
        self._assert_access_error(
            denied_old_target,
            '既有 protected old target 的 metadata write 也必須拒絕',
            contains='Attachments are not allowed',
        )
        legacy_protected.invalidate_recordset(['name', 'res_model', 'res_id'])
        self.assertNotEqual(
            legacy_protected.name, 'forbidden-old-target-rename')
        self.assertEqual(legacy_protected.res_model, 'mail.message')
        self.assertEqual(legacy_protected.res_id, protected_message.id)

        control_name = 'canonical-other-model-%s.bin' % uuid.uuid4().hex
        control_id = self._assert_ok(
            self._call_kw(
                'ir.attachment',
                'create',
                [{
                    'name': control_name,
                    'datas': base64.b64encode(b'legal other model').decode(),
                    'res_model': 'project.project',
                    'res_id': control_project.id,
                }],
            ),
            '合法其他 model attachment control 不受 targeted guard 影響',
        )
        attachment = self.env['ir.attachment'].sudo().browse(control_id)
        original = attachment.read(['res_model', 'res_id', 'datas'])[0]
        for res_model, protected_id in protected_targets:
            for supplied_id in (
                    str(protected_id), '0%s' % protected_id,
                    float(protected_id), True):
                denied = self._call_kw(
                    'ir.attachment',
                    'write',
                    [[control_id], {
                        'res_model': res_model,
                        'res_id': supplied_id,
                        'datas': base64.b64encode(b'denied move').decode(),
                    }],
                )
                self._assert_access_error(
                    denied,
                    'old→protected move 的 final target 必須 fail closed',
                    contains='Attachments are not allowed',
                )
                attachment.invalidate_recordset()
                self.assertEqual(
                    attachment.read(['res_model', 'res_id', 'datas'])[0],
                    original,
                    'target guard 拒絕後不得留下部分 write',
                )

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
        legal_note = '已於 ECPay 後台核對交易狀態，帳務資料一致。'
        self.assertTrue(self._assert_ok(
            self._call_kw(
                'woow.billing.incident', 'write',
                [[self.incident_a.id], {'resolution_note': legal_note}],
            ),
            '合法中文 resolution_note 不得被 SAD marker 誤殺',
        ))
        before = self._audit_snapshot(self.incident_a)
        self.assertEqual(before['resolution_note'], legal_note)
        for note in (
                '<b>plain verification reason</b>',
                'plain <!-- comment --> verification reason',
                'title="plain verification reason"',
                'plain &amp; verification reason',
                'MTN: 4111111111111111',
                'ResultData=forbidden-rpc-payload-864',
                'CVC=123',
                'CID: 123',
                'card security code 123',
                'SAD=forbidden-authentication-data',
                '卡片安全碼：123',
                '信用卡安全码：123',
                '卡片驗證值是 123',
                '卡片验证值为 123',
                '信用卡背面末三碼 123',
                '信用卡背面末三码 123',
                '驗證碼：123',
                '验证码：123',
                '驗證值 123',
                '验证值 123',
                '個人識別碼 1234',
                '个人识别码 1234',
                'PIN區塊：forbidden',
                '磁條資料：forbidden',
                '磁軌資料：forbidden',
                '磁轨数据：forbidden',
                '第二軌：forbidden',
                '第二轨：forbidden',
                '軌道資料：forbidden',
                '授權碼：forbidden',
                '分隔卡號 4111\u09031111\u09031111\u09031111'):
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
