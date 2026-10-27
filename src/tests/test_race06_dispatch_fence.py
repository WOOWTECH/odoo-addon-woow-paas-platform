from odoo.exceptions import AccessError, ValidationError
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

    def test_verified_provider_app_is_durable_review_not_retry(self):
        self.fence.action_record_verified_app(
            '0123456789abcdef0123456789abcdef',
            'verified exact app name and hostname at provider')
        self.assertEqual(self.fence.state, 'provider_adopted')
        self.assertEqual(
            self.fence.provider_app_id,
            '0123456789abcdef0123456789abcdef')

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
