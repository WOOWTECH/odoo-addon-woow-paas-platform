"""LLM-12b Task1: append-only local discrepancy acknowledgement contracts."""
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models import llm_known_discrepancy as discrepancy_module
from ..models.llm_common import _llm_internal

MODEL = 'woow_paas_platform.llm_known_discrepancy'
ACCOUNT = 'woow_paas_platform.llm_account'
EVENT = 'woow_paas_platform.llm_event'
WIZARD = 'woow_paas_platform.llm_known_discrepancy_wizard'


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmKnownDiscrepancy(TransactionCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:12]
        self.partner = self.env['res.partner'].create({'name': 'LLM discrepancy ' + suffix})
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': self.partner.id,
            'state': 'active',
        })
        self.operator = new_test_user(
            self.env, login='llm_discrepancy_' + suffix,
            groups='base.group_user,base.group_system',
            email='llm-discrepancy-%s@example.test' % suffix)
        self.operator._llm_set_named_human_eligible(True)
        self.service = self.env[MODEL].with_user(self.operator)
        self.configured = patch.object(discrepancy_module, 'llm_unconfigured', return_value=False)
        self.configured.start()
        self.addCleanup(self.configured.stop)

    def _ack(self, delta, reason='Verified local reconciliation evidence', request_id=None):
        return self.service._acknowledge(
            self.account.id, delta, reason,
            str(uuid.uuid4()) if request_id is None else request_id)

    def test_named_identity_and_crud_guards_reject_sudo_and_context_forgery(self):
        ordinary = new_test_user(
            self.env, login='llm_discrepancy_ordinary_' + uuid.uuid4().hex[:8],
            groups='base.group_user', email='ordinary@example.test')
        shared = new_test_user(
            self.env, login='llm_discrepancy_shared_' + uuid.uuid4().hex[:8],
            groups='base.group_portal', email='shared@example.test')
        unprovisioned = new_test_user(
            self.env, login='llm_discrepancy_service_' + uuid.uuid4().hex[:8],
            groups='base.group_user,base.group_system', email='service@example.test')
        inactive = new_test_user(
            self.env, login='llm_discrepancy_inactive_' + uuid.uuid4().hex[:8],
            groups='base.group_user,base.group_system', email='inactive@example.test')
        inactive._llm_set_named_human_eligible(True)
        inactive.write({'active': False})
        for user in (ordinary, shared, unprovisioned, inactive, self.env.ref('base.user_root'),
                     self.env.ref('base.user_admin')):
            with self.subTest(user=user.id), self.assertRaises(AccessError):
                self.env[MODEL].with_user(user)._acknowledge(
                    self.account.id, 1, 'Verified local reconciliation evidence', str(uuid.uuid4()))
        forged = self.env[MODEL].sudo().with_context(
            woow_llm_known_discrepancy_capability=True,
            default_account_id=self.account.id,
            default_quota_delta=1,
            default_reason='Verified local reconciliation evidence',
        )
        with self.assertRaises(AccessError):
            forged.create({'account_id': self.account.id, 'quota_delta': 1,
                           'reason': 'Verified local reconciliation evidence',
                           'created_uid': self.operator.id, 'request_id': str(uuid.uuid4())})
        record = self._ack(1)
        with self.assertRaises(AccessError):
            record.sudo().write({'reason': 'Changed evidence after the fact'})
        with self.assertRaises(AccessError):
            record.sudo().unlink()

    def test_signed_int64_exact_input_reason_and_uuid_validation(self):
        maximum = self._ack(2 ** 63 - 1)
        minimum = self._ack(-(2 ** 63))
        self.assertEqual(maximum.quota_delta, 2 ** 63 - 1)
        self.assertEqual(maximum.quota_delta_text, str(2 ** 63 - 1))
        self.assertEqual(minimum.quota_delta, -(2 ** 63))
        self.assertEqual(minimum.quota_delta_text, str(-(2 ** 63)))

        for invalid in (True, 1.0, '1', 0, 2 ** 63, -(2 ** 63) - 1):
            with self.subTest(delta=repr(invalid)), self.assertRaises(ValidationError):
                self._ack(invalid)
        for reason in ('short', ' ' * 12, 'x' * 2001, None):
            with self.subTest(reason=repr(reason)), self.assertRaises(ValidationError):
                self._ack(1, reason=reason)
        for request_id in ('not-a-uuid', '', '550e8400e29b41d4a716446655440000'):
            with self.subTest(request_id=request_id), self.assertRaises(ValidationError):
                self._ack(1, request_id=request_id)

    def test_idempotency_returns_same_row_and_rejects_payload_reuse(self):
        request_id = str(uuid.uuid4())
        first = self._ack(9007199254740993, reason='  Same evidence after trim  ',
                          request_id=request_id)
        replay = self._ack(9007199254740993, reason='Same evidence after trim',
                           request_id=request_id)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(self.env[EVENT].sudo().search_count([
            ('action', '=', 'known_discrepancy'), ('target_id', '=', first.id),
        ]), 1)
        with self.assertRaises(ValidationError):
            self._ack(9007199254740994, reason='Same evidence after trim',
                      request_id=request_id)
        with self.assertRaises(ValidationError):
            self._ack(9007199254740993, reason='Different evidence for same request',
                      request_id=request_id)
        another_operator = new_test_user(
            self.env, login='llm_discrepancy_other_' + uuid.uuid4().hex[:8],
            groups='base.group_user,base.group_system', email='other@example.test')
        another_operator._llm_set_named_human_eligible(True)
        with self.assertRaises(ValidationError):
            self.env[MODEL].with_user(another_operator)._acknowledge(
                self.account.id, 9007199254740993, 'Same evidence after trim', request_id)

    def test_acl_read_visibility_and_reason_field_restriction(self):
        record = self._ack(9007199254740993)
        manager = new_test_user(
            self.env, login='llm_discrepancy_manager_' + uuid.uuid4().hex[:8],
            groups='base.group_user,account.group_account_manager', email='manager@example.test')
        ordinary = new_test_user(
            self.env, login='llm_discrepancy_acl_user_' + uuid.uuid4().hex[:8],
            groups='base.group_user', email='acl-user@example.test')
        portal = new_test_user(
            self.env, login='llm_discrepancy_acl_portal_' + uuid.uuid4().hex[:8],
            groups='base.group_portal', email='acl-portal@example.test')

        operator_record = self.env[MODEL].with_user(self.operator).sudo(False).browse(record.id)
        manager_record = self.env[MODEL].with_user(manager).sudo(False).browse(record.id)
        self.assertFalse(operator_record.env.su)
        self.assertFalse(manager_record.env.su)
        # In Odoo 18 check_access_rights(..., raise_exception=True) returns
        # None on success. has_access is the boolean API; also read the row.
        self.assertTrue(operator_record.has_access('read'))
        self.assertTrue(manager_record.has_access('read'))
        operator_values = operator_record.read(['quota_delta_text', 'reason'])[0]
        self.assertEqual(operator_values['quota_delta_text'], '9007199254740993')
        self.assertEqual(operator_values['reason'], 'Verified local reconciliation evidence')
        for user in (ordinary, portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.env[MODEL].with_user(user).sudo(False).browse(record.id).read(
                    ['quota_delta_text'])
        self.assertEqual(manager_record.read(['quota_delta_text'])[0]['quota_delta_text'],
                         '9007199254740993')
        with self.assertRaises(AccessError):
            manager_record.read(['reason'])

    def test_public_wizard_parses_exact_large_int_and_guards_default_get(self):
        Wizard = self.env[WIZARD].with_user(self.operator)
        supplied_uuid = str(uuid.uuid4())
        defaults = Wizard.with_context(default_request_id=supplied_uuid).default_get(
            ['account_id', 'request_id'])
        self.assertNotEqual(defaults['request_id'], supplied_uuid)

        wizard = Wizard.create({
            'account_id': self.account.id,
            'quota_delta': '9007199254740993',
            'reason': 'Operator verified exact upstream difference',
        })
        action = wizard.action_acknowledge()
        record = self.env[MODEL].sudo().browse(action['res_id'])
        self.assertEqual(record.quota_delta, 9007199254740993)
        self.assertEqual(record.quota_delta_text, '9007199254740993')

        with self.assertRaises(AccessError):
            self.env[WIZARD].with_user(self.env.ref('base.user_admin')).default_get(
                ['account_id', 'request_id'])

    def test_service_create_ignores_forged_context_defaults(self):
        request_id = str(uuid.uuid4())
        forged_service = self.service.with_context(
            default_account_id=999999,
            default_quota_delta=7,
            default_reason='Context supplied replacement reason',
            default_created_uid=self.env.ref('base.user_admin').id,
            default_request_id=str(uuid.uuid4()),
            default_llm_event_id=999999,
        )
        record = forged_service._acknowledge(
            self.account.id, 41, 'Verified values come from service arguments', request_id)
        self.assertEqual(record.account_id, self.account)
        self.assertEqual(record.quota_delta, 41)
        self.assertEqual(record.reason, 'Verified values come from service arguments')
        self.assertEqual(record.created_uid, self.operator)
        self.assertEqual(record.request_id, request_id)
        self.assertTrue(record.llm_event_id)

    def test_audit_is_same_transaction_and_reason_never_enters_event(self):
        record = self._ack(-17, reason='Sensitive but bounded operator explanation')
        event = record.llm_event_id
        self.assertTrue(event)
        self.assertEqual(event.action, 'known_discrepancy')
        self.assertEqual(event.account_id, self.account)
        self.assertEqual(event.uid, self.operator)
        self.assertEqual(event.target_model, MODEL)
        self.assertEqual(event.target_id, record.id)
        self.assertNotIn('Sensitive but bounded', event.details_json or '')

        failure_delta = 23
        try:
            with patch.object(type(self.env[EVENT]), '_llm_insert_event_cr',
                              side_effect=RuntimeError('injected audit failure')):
                self._ack(failure_delta)
        except RuntimeError:
            pass
        else:
            self.fail('Injected audit failure was swallowed')
        self.assertFalse(self.env[MODEL].sudo().search([
            ('account_id', '=', self.account.id), ('quota_delta', '=', failure_delta),
        ]))

    def test_exact_quota_aggregation_and_no_network_calls(self):
        with patch('requests.sessions.Session.request') as request:
            self._ack(2 ** 63 - 1)
            self._ack(-(2 ** 63))
            self.assertEqual(self.env[MODEL]._quota_total(self.account.id), -1)
            self._ack(2 ** 63 - 1)
            self._ack(2 ** 63 - 1)
            self.assertEqual(self.env[MODEL]._quota_total(self.account.id), 2 * (2 ** 63 - 1) - 1)
            self._ack(-(2 ** 63))
            self._ack(-(2 ** 63))
            self.assertEqual(self.env[MODEL]._quota_total(self.account.id), -3)
            self._ack(-(2 ** 63))
            self.assertEqual(self.env[MODEL]._quota_total(self.account.id), -(2 ** 63) - 3)
            request.assert_not_called()

    def test_unconfigured_service_fails_closed_without_writing(self):
        with patch.object(discrepancy_module, 'llm_unconfigured', return_value=True):
            with self.assertRaises(UserError):
                self._ack(1)
        self.assertFalse(self.env[MODEL].sudo().search([
            ('account_id', '=', self.account.id),
        ]))
