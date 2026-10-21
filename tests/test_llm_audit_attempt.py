"""I3 logical-attempt evidence, with no Odoo or live proxy dependency."""
import ast
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/services/llm_audit_attempt.py'
spec = importlib.util.spec_from_file_location('llm_audit_attempt', SOURCE)
attempts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = attempts
spec.loader.exec_module(attempts)

ATTEMPT = '8a2e668b-2756-479e-afcc-2aa19b9b25f5'
OTHER = '26d1fab7-6974-4283-93bc-0846f79db777'
STAMP = '2026-10-21T04:34:56.123456Z'


def intent(seq=1):
    return {'seq': seq, 'attempt_id': ATTEMPT, 'kind': 'intent', 'at': STAMP,
            'target_user_id': 77, 'quota': 15625, 'result': False,
            'status': 0, 'request_id': False}


def outcome(seq=2, *, result='forwarded', status=200, request_id='safe-id-1'):
    return {'seq': seq, 'attempt_id': ATTEMPT, 'kind': 'outcome', 'at': STAMP,
            'target_user_id': 0, 'quota': 0, 'result': result,
            'status': status, 'request_id': request_id}


class TestLogicalAttempt(unittest.TestCase):
    def test_forwarded_receipt_is_only_a_correlation_candidate(self):
        before, after = intent(), outcome()
        original = copy.deepcopy((before, after))
        candidate = attempts.collapse_attempt(before, after)
        self.assertEqual(candidate.classification, 'correlation_candidate')
        self.assertEqual(candidate.request_id, 'safe-id-1')
        self.assertEqual((candidate.intent_seq, candidate.outcome_seq), (1, 2))
        self.assertEqual((candidate.target_user_id, candidate.quota), (77, 15625))
        self.assertEqual((before, after), original)
        self.assertNotIn('credited', repr(candidate))
        self.assertFalse(hasattr(candidate, 'order_id'))
        with self.assertRaises(AttributeError):
            candidate.classification = 'credited'

    def test_intent_only_and_ambiguous_outcomes_remain_uncertain(self):
        self.assertEqual(attempts.collapse_attempt(intent()).classification, 'uncertain')
        for result in ('connect', 'reset', 'timeout', 'cancelled'):
            with self.subTest(result=result):
                row = outcome(result=result, status=0, request_id=False)
                self.assertEqual(attempts.collapse_attempt(intent(), row).classification,
                                 'uncertain')
        self.assertEqual(attempts.collapse_attempt(
            intent(), outcome(request_id=False)).classification, 'uncertain')

    def test_only_explicit_not_sent_is_definitely_unsent(self):
        row = outcome(result='not_sent', status=0, request_id=False)
        result = attempts.collapse_attempt(intent(), row)
        self.assertEqual(result.classification, 'not_sent')
        self.assertIsNone(result.request_id)
        self.assertEqual(result.outcome_seq, 2)

    def test_pair_identity_and_sequence_must_agree_without_float_rounding(self):
        wrong = outcome()
        wrong['attempt_id'] = OTHER
        for row in (wrong, outcome(seq=1), outcome(seq=0), outcome(seq=True),
                    outcome(seq=1.0), intent(seq=2)):
            with self.subTest(row=row), self.assertRaises(attempts.AuditAttemptError):
                attempts.collapse_attempt(intent(), row)
        large = 2 ** 53 + 1
        pair = attempts.collapse_attempt(intent(seq=large), outcome(seq=large + 1))
        self.assertEqual((pair.intent_seq, pair.outcome_seq), (large, large + 1))

    def test_malformed_or_secret_shaped_data_never_becomes_evidence(self):
        bad = []
        for field, value in (('seq', True), ('target_user_id', 0), ('quota', 1.0),
                             ('quota', 156250001), ('attempt_id', 'caller-id'),
                             ('at', 'yesterday'), ('status', True)):
            row = intent()
            row[field] = value
            bad.append((row, None))
        for request_id in ('nap_secret', 'sk-secret', 'Bearer-token', 'space id',
                           'a' * 129, 123):
            row = outcome(request_id=request_id)
            bad.append((intent(), row))
        for before, after in bad:
            with self.subTest(before=before, after=after), self.assertRaises(
                    attempts.AuditAttemptError):
                attempts.collapse_attempt(before, after)

    def test_inconsistent_outcome_shape_is_rejected(self):
        rows = [outcome(result='not_sent', status=200, request_id=False),
                outcome(result='timeout', status=0, request_id='safe-id'),
                outcome(result='forwarded', status=0, request_id=False),
                outcome(result='forwarded', status=200, request_id=True),
                outcome(result='unknown', status=0, request_id=False)]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(attempts.AuditAttemptError):
                attempts.collapse_attempt(intent(), row)

    def test_module_is_pure_and_inactive(self):
        tree = ast.parse(SOURCE.read_text())
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)}
        imported.update(alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse(imported & {'odoo', 'httpx', 'requests', 'socket', 'subprocess'})
        self.assertFalse(any(isinstance(node, (ast.AsyncFunctionDef, ast.Await))
                             for node in ast.walk(tree)))


if __name__ == '__main__':
    unittest.main()
