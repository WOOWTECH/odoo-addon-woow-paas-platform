from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase


MODEL = 'woow_paas_platform.access_dispatch_fence'
CONFIRM = 'I VERIFIED THE PROVIDER HAS NO APP FOR THIS IDENTITY'


class TestRace06DispatchFenceReview(TransactionCase):

    def setUp(self):
        super().setUp()
        self.fence = self.env[MODEL].sudo().create({
            'anchor_model': 'res.partner',
            'anchor_id': self.env.company.partner_id.id,
            'app_name': 'race06-focused',
            'primary_hostname': 'race06-focused.example.invalid',
            'state': 'dispatching',
            'effect_boundary_version': 1,
        })

    def test_single_retry_requires_exact_confirmation_and_reason(self):
        with self.assertRaises(ValidationError):
            self.fence.action_authorize_single_retry(
                'provider looked empty', 'reviewed provider inventory')
        with self.assertRaises(ValidationError):
            self.fence.action_authorize_single_retry(CONFIRM, 'short')
        self.assertTrue(self.fence.action_authorize_single_retry(
            CONFIRM, 'reviewed exact provider identity and audit history'))
        self.assertEqual(self.fence.state, 'retry_authorized')
        self.assertEqual(self.fence.reviewed_by, self.env.user)

    def test_post_send_absence_cannot_authorize_retry(self):
        self.fence.write({'effect_started_at': fields.Datetime.now()})
        with self.assertRaises(UserError):
            self.fence.action_authorize_single_retry(
                CONFIRM, 'provider inventory currently shows no matching app')
        self.assertEqual(self.fence.state, 'dispatching')
        self.assertFalse(self.fence.provider_app_id)

    def test_unverified_manual_provider_id_is_rejected(self):
        with self.assertRaises(UserError):
            self.fence.action_record_verified_app(
                '0123456789abcdef0123456789abcdef',
                'verified exact app name and hostname at provider')
        self.assertEqual(self.fence.state, 'dispatching')
        self.assertFalse(self.fence.provider_app_id)

    def test_non_system_user_cannot_review(self):
        user = self.env['res.users'].create({
            'name': 'RACE06 reviewer denied',
            'login': 'race06-reviewer-denied@example.invalid',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.fence.with_user(user).action_record_verified_app(
                '0123456789abcdef0123456789abcdef',
                'attempted provider review without system role')

    def test_review_adopts_only_unique_exact_provider_readback(self):
        class Operator:
            def review_access_app(self, app_name, primary_hostname):
                self.args = (app_name, primary_hostname)
                return {'status': 'unique', 'app_id': 'app-reviewed',
                        'match_count': 1}

        operator = Operator()
        with patch.object(type(self.fence), '_provider_review_client',
                          return_value=operator):
            self.assertTrue(self.fence.action_review_and_adopt_provider_app(
                'admin reviewed the exact provider inventory'))
        self.assertEqual(operator.args, (
            'race06-focused', 'race06-focused.example.invalid'))
        self.assertEqual(self.fence.state, 'provider_adopted')
        self.assertEqual(self.fence.provider_app_id, 'app-reviewed')

    def test_review_absence_keeps_fence_closed(self):
        class Operator:
            def review_access_app(self, app_name, primary_hostname):
                return {'status': 'absent', 'app_id': None, 'match_count': 0}
        with patch.object(type(self.fence), '_provider_review_client',
                          return_value=Operator()):
            with self.assertRaises(UserError):
                self.fence.action_review_and_adopt_provider_app(
                    'admin reviewed the exact provider inventory')
        self.assertEqual(self.fence.state, 'dispatching')
        self.assertFalse(self.fence.provider_app_id)
