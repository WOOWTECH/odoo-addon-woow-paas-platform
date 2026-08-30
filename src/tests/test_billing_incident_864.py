"""#864 PR-D：帳務 incident 地基的 public model／後台行為守線。"""

import ast
import uuid
from unittest.mock import patch

from lxml import etree
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged


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
            Incident.create({'external_ref': ref, 'kind': 'intent'})

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
        for index, bad_values in enumerate(cases):
            with self.subTest(values=bad_values):
                values = {
                    'external_ref': 'constraint-%s-%s' % (
                        index, uuid.uuid4().hex),
                    'kind': 'intent',
                    **bad_values,
                }
                with self.assertRaises(IntegrityError):
                    Incident.create(values)


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
        record.write({'resolution_note': 'draft manager note'})
        self.assertEqual(self.incident.resolution_note, 'draft manager note')

        with self.assertRaises(AccessError):
            self.env['woow.billing.incident'].with_user(self.manager).create({
                'external_ref': 'forbidden-create-%s' % uuid.uuid4().hex,
                'kind': 'intent',
            })
        with self.assertRaises(AccessError):
            record.unlink()
        with self.assertRaises(AccessError):
            self.incident.with_user(self.regular_user).read(['external_ref'])

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
            'topup 記錄可能已被回捲、在 Odoo 裡查不到是預期現象，'
            '不是關單理由'
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
