from odoo.tests.common import TransactionCase


class TestClassifyFailure(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Topup = self.env['woow.wallet.topup']

    def test_classify_card_declined(self):
        self.assertEqual(
            self.Topup._classify_failure('Card declined by issuing bank'),
            'card_declined',
        )

    def test_classify_limit_exceeded(self):
        self.assertEqual(
            self.Topup._classify_failure('amount exceeds limit'),
            'limit_exceeded',
        )

    def test_classify_duplicate(self):
        self.assertEqual(
            self.Topup._classify_failure('RtnCode=10100073 duplicate'),
            'duplicate_transaction',
        )

    def test_classify_unknown_falls_to_generic(self):
        self.assertEqual(
            self.Topup._classify_failure('totally random error'),
            'generic',
        )

    def test_classify_empty_or_none(self):
        self.assertEqual(self.Topup._classify_failure(''), 'generic')
        self.assertEqual(self.Topup._classify_failure(None), 'generic')
