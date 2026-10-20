"""Pure rc.44 credit evidence tests; no Odoo installation or API is needed."""
import copy
import importlib
import json
from pathlib import Path
import sys
import types
import unittest

PACKAGE = 'woow_llm_credit_evidence_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / 'src/services')]
sys.modules[PACKAGE] = package
evidence = importlib.import_module(PACKAGE + '.llm_credit_evidence')


class TestCreditLogEvidence(unittest.TestCase):

    def setUp(self):
        self.log = {
            'type': 1, 'user_id': 42, 'created_at': 1001, 'request_id': 'request-123',
            'other': {
                'admin_info': {'admin_id': 7},
                'op': {'action': 'user.quota_add', 'params': {
                    'target_user_id': 42, 'requested_quota': 1562500, 'mode': 'add',
                    'from': 300, 'to': 1562800,
                }},
            },
        }
        self.args = {'target_user_id': 42, 'quota': 1562500, 'quota_before': 300,
                     'sent_at': 1000, 'observed_at': 1100, 'service_admin_id': 7}

    def proof(self, **overrides):
        return evidence.credit_log_proof(self.log, **dict(self.args, **overrides))

    def test_exact_request_receipt_and_complete_operation_match(self):
        for request_id in (None, 'request-123'):
            with self.subTest(request_id=request_id):
                proof = self.proof(expected_request_id=request_id)
                self.assertEqual((proof.request_id, proof.quota, proof.admin_id),
                                 ('request-123', 1562500, 7))

    def test_json_other_matches_without_using_display_log_quota(self):
        self.log['other'] = json.dumps(self.log['other'])
        self.log['quota'] = 0  # rc.44 does not populate Log.quota for manual adjustments.
        self.assertIsNotNone(self.proof())

    def test_legacy_display_quota_cannot_substitute_for_requested_quota(self):
        params = self.log['other']['op']['params']
        params['quota'] = params.pop('requested_quota')
        with self.assertRaisesRegex(evidence.EvidenceError, '^invalid_evidence_number$'):
            self.proof()

    def test_receipt_proves_identity_even_if_pre_read_quota_was_stale(self):
        self.assertIsNotNone(self.proof(expected_request_id='request-123', quota_before=999))
        self.assertIsNone(self.proof(quota_before=999))

    def test_receipt_can_resolve_a_crash_before_the_pre_read_snapshot(self):
        self.assertIsNotNone(self.proof(expected_request_id='request-123', quota_before=None))
        with self.assertRaisesRegex(evidence.EvidenceError, '^invalid_evidence_number$'):
            self.proof(quota_before=None)

    def test_operation_match_requires_independent_admin_trust(self):
        self.assertIsNone(self.proof(service_admin_id=None))
        self.assertIsNone(self.proof(service_admin_id=8))
        self.assertIsNotNone(self.proof(service_admin_id=None, expected_request_id='request-123'))
        self.assertIsNotNone(self.proof(service_admin_id=8, expected_request_id='request-123'))

    def test_wrong_user_amount_time_or_request_is_not_proof(self):
        for args in ({'target_user_id': 43}, {'quota': 1562501}, {'sent_at': 1002},
                     {'observed_at': 1000}, {'expected_request_id': 'other-request'}):
            with self.subTest(args=args):
                self.assertIsNone(self.proof(**args))

    def test_wrong_operation_mode_target_or_delta_is_not_proof(self):
        original = copy.deepcopy(self.log)
        for key, value in (('mode', 'subtract'), ('target_user_id', 43),
                           ('requested_quota', 1562501), ('to', 1562801)):
            self.log = copy.deepcopy(original)
            self.log['other']['op']['params'][key] = value
            with self.subTest(key=key):
                self.assertIsNone(self.proof())
        self.log = copy.deepcopy(original)
        self.log['other']['op']['action'] = 'user.quota_override'
        self.assertIsNone(self.proof())

    def test_reused_request_requires_review_not_no_evidence(self):
        with self.assertRaisesRegex(evidence.EvidenceError, '^credit_log_already_claimed$'):
            self.proof(claimed_request_ids={'request-123'})

    def test_boolean_string_and_int8_overflow_are_rejected(self):
        for key in ('requested_quota', 'from', 'to', 'target_user_id'):
            for value in (True, '1', 2 ** 63, -(2 ** 63) - 1):
                log = copy.deepcopy(self.log)
                log['other']['op']['params'][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(evidence.EvidenceError):
                    evidence.credit_log_proof(log, **self.args)
        self.log['other']['admin_info']['admin_id'] = True
        with self.assertRaises(evidence.EvidenceError):
            self.proof()

    def test_malformed_other_never_means_completed_empty_query(self):
        for other in ('{', 'null', '[]', {}, None, 'x' * 65537):
            self.log['other'] = other
            with self.subTest(kind=type(other).__name__), self.assertRaises(evidence.EvidenceError):
                self.proof()

    def test_request_identifier_cannot_carry_secret_or_unbounded_text(self):
        for value in ('sk-' + 'x' * 20, 'nap_' + 'x' * 20, 'Bearer abc', 'x' * 129, ''):
            self.log['request_id'] = value
            with self.assertRaisesRegex(evidence.EvidenceError, '^invalid_evidence_request_id$'):
                self.proof()


class TestQuiescentEvidence(unittest.TestCase):

    def setUp(self):
        self.before = evidence.QuotaSnapshot(300, 500, 10)

    def result(self, first, second=None, elapsed=300):
        return evidence.quiescent_result(
            self.before, first, second or first, quota=1562500, elapsed_seconds=elapsed)

    def test_exact_delta_and_exact_zero(self):
        self.assertEqual(self.result(evidence.QuotaSnapshot(1562800, 500, 10)), 'credited')
        self.assertEqual(self.result(self.before), 'not_executed')

    def test_five_minutes_are_required_without_sleep(self):
        sample = evidence.QuotaSnapshot(1562800, 500, 10)
        for elapsed in (0, 1, 299):
            self.assertEqual(self.result(sample, elapsed=elapsed), 'wait')
        self.assertEqual(self.result(sample, elapsed=300), 'credited')

    def test_traffic_at_any_point_requires_review(self):
        for first, second in (
                (evidence.QuotaSnapshot(1562800, 501, 10), None),
                (evidence.QuotaSnapshot(1562800, 500, 11), None),
                (self.before, evidence.QuotaSnapshot(301, 500, 10)),
                (self.before, evidence.QuotaSnapshot(300, 501, 11))):
            with self.subTest(first=first, second=second):
                self.assertEqual(self.result(first, second), 'needs_review')

    def test_unrelated_and_negative_deltas_require_review(self):
        for quota in (299, 301, 1562799, 1562801, -1):
            self.assertEqual(self.result(evidence.QuotaSnapshot(quota, 500, 10)), 'needs_review')

    def test_invalid_or_missing_samples_are_rejected(self):
        for values in ((True, 0, 0), (0, -1, 0), (0, 0, -1), (2 ** 63, 0, 0), ('1', 0, 0)):
            with self.assertRaises(evidence.EvidenceError):
                evidence.QuotaSnapshot(*values)
        with self.assertRaises(evidence.EvidenceError):
            self.result(None)
        with self.assertRaises(evidence.EvidenceError):
            self.result(self.before, elapsed=-1)


if __name__ == '__main__':
    unittest.main()
