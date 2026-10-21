"""Standalone tests for I2 balance decisions; no Odoo installation needed."""
import importlib
from pathlib import Path
import sys
import types
import unittest

PACKAGE = 'woow_llm_reconcile_balance_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / 'src/services')]
sys.modules[PACKAGE] = package
balance = importlib.import_module(PACKAGE + '.llm_reconcile_balance')


class TestBalanceReconciliation(unittest.TestCase):

    def sample(self, total, at, count=10, used=0):
        return balance.BalanceSample(total - used, used, count, at)

    def test_inclusive_bounds_and_epsilon_edges_are_clear(self):
        for total in (100_000, 100_000 + balance.EPSILON,
                      200_000, 200_000 + balance.EPSILON):
            with self.subTest(total=total):
                self.assertEqual(balance.reconcile(
                    self.sample(total, 100), 100_000, 200_000).outcome, 'clear')

    def test_upward_and_downward_exact_epsilon_crossings(self):
        cases = ((200_000 + balance.EPSILON, 'clear'),
                 (200_001 + balance.EPSILON, 'candidate_up'),
                 (100_000 - balance.EPSILON, 'clear'),
                 (99_999 - balance.EPSILON, 'candidate_down'))
        for total, expected in cases:
            with self.subTest(total=total):
                self.assertEqual(balance.reconcile(
                    self.sample(total, 10), 100_000, 200_000).outcome, expected)

    def test_up_recheck_requires_300_seconds_and_complete_i3_evidence(self):
        first = self.sample(200_000 + balance.EPSILON + 1, 100)
        start = balance.reconcile(first, 100_000, 200_000)
        self.assertEqual(start.outcome, 'candidate_up')
        middle = self.sample(first.total, 200)
        self.assertEqual(balance.reconcile(
            middle, 100_000, 200_000, candidate=start.candidate
        ).candidate, start.candidate)
        for at in (399,):
            result = balance.reconcile(
                self.sample(first.total, at), 100_000, 200_000,
                candidate=start.candidate,
                evidence=balance.I3Evidence(True, 0))
            self.assertEqual(result.outcome, 'candidate_up')
        current = self.sample(first.total, 400)
        pending = balance.reconcile(
            current, 100_000, 200_000, candidate=start.candidate,
            evidence=None)
        self.assertEqual(pending.outcome, 'evidence_pending')
        self.assertEqual(pending.candidate, start.candidate)
        self.assertEqual(balance.reconcile(
            current, 100_000, 200_000, candidate=pending.candidate,
            evidence=balance.I3Evidence(False, 0)).candidate, start.candidate)
        self.assertEqual(balance.reconcile(
            current, 100_000, 200_000, candidate=start.candidate,
            evidence=balance.I3Evidence(True, 2)).outcome, 'external_credit')
        result = balance.reconcile(
            current, 100_000, 200_000, candidate=start.candidate,
            evidence=balance.I3Evidence(True, 0))
        self.assertEqual((result.outcome, result.severity),
                         ('settlement_anomaly', 'high'))

    def test_upward_transient_or_changed_current_bounds_restarts_candidate(self):
        first = self.sample(200_000 + balance.EPSILON + 1, 100)
        start = balance.reconcile(first, 100_000, 200_000)
        evidence = balance.I3Evidence(True, 0)
        # A transient dip clears the condition; a later rise starts anew.
        dipped = self.sample(200_000, 200)
        self.assertEqual(balance.reconcile(
            dipped, 100_000, 200_000, candidate=start.candidate
        ).outcome, 'clear')
        risen = self.sample(first.total, 400)
        self.assertEqual(balance.reconcile(
            risen, 100_000, 200_000,
            evidence=evidence).outcome, 'candidate_up')
        # The latest sample crosses today's bound, while the original baseline
        # no longer does; the timer must restart from the latest sample.
        raised = self.sample(220_000, 500)
        restarted = balance.reconcile(
            raised, 100_000, 204_000, candidate=start.candidate,
            evidence=evidence)
        self.assertEqual(restarted.outcome, 'candidate_up')
        self.assertEqual(restarted.candidate.sample, raised)
        self.assertEqual(balance.reconcile(
            self.sample(first.total, 400), 100_000, 200_000,
            known_discrepancy=balance.EPSILON + 1,
            candidate=start.candidate,
            evidence=evidence).outcome, 'clear')

    def test_down_requires_two_low_samples_300_seconds_and_stable_count(self):
        low = 100_000 - balance.EPSILON - 1
        first = self.sample(low, 100, count=7)
        start = balance.reconcile(first, 100_000, 200_000)
        self.assertEqual(start.outcome, 'candidate_down')
        early = balance.reconcile(
            self.sample(low, 399, count=7), 100_000, 200_000,
            candidate=start.candidate)
        self.assertEqual(early.outcome, 'candidate_down')
        changed_count = self.sample(low, 400, count=8)
        result = balance.reconcile(
            changed_count, 100_000, 200_000, candidate=start.candidate)
        self.assertEqual(result.outcome, 'candidate_down')
        self.assertEqual(result.candidate.sample, changed_count)
        stable = self.sample(low, 699, count=8)
        result = balance.reconcile(
            stable, 100_000, 200_000, candidate=result.candidate)
        self.assertEqual(result.outcome, 'candidate_down')
        stable = self.sample(low, 700, count=8)
        result = balance.reconcile(stable, 100_000, 200_000,
                                   candidate=result.candidate)
        self.assertEqual((result.outcome, result.severity), ('mismatch', 'medium'))

    def test_changed_ledger_or_discrepancy_cannot_confirm_stale_candidate(self):
        previous = self.sample(200_000 + balance.EPSILON + 1, 100)
        started = balance.reconcile(previous, 100_000, 200_000)
        self.assertEqual(started.outcome, 'candidate_up')
        # The old sample is no longer above today's upper bound.
        current = self.sample(220_000, 400)
        result = balance.reconcile(
            current, 100_000, 204_000, candidate=started.candidate,
            evidence=balance.I3Evidence(True, 0))
        self.assertEqual(result.outcome, 'candidate_up')
        self.assertEqual(result.candidate.sample, current)
        # A newly recognized signed discrepancy also neutralizes the samples.
        result = balance.reconcile(
            current, 100_000, 200_000, known_discrepancy=balance.EPSILON + 1,
            candidate=started.candidate, evidence=balance.I3Evidence(True, 0))
        self.assertEqual(result.outcome, 'clear')

    def test_changed_lower_bound_restarts_down_candidate_when_only_latest_is_low(self):
        baseline = self.sample(84_000, 100, count=3)
        started = balance.reconcile(baseline, 100_000, 200_000)
        self.assertEqual(started.outcome, 'candidate_down')
        latest = self.sample(74_000, 400, count=3)
        # Under today's lower bound, the original sample is neutral but the
        # latest sample is low. It starts a fresh timer instead of mismatching.
        result = balance.reconcile(
            latest, 90_000, 200_000, candidate=started.candidate)
        self.assertEqual(result.outcome, 'candidate_down')
        self.assertEqual(result.candidate.sample, latest)

    def test_integer_aggregates_remain_exact_above_float_precision(self):
        huge = 2 ** 60 + 13
        sample = self.sample(huge, 1, used=huge - 7)
        self.assertEqual(sample.total, huge)
        self.assertEqual(balance.reconcile(sample, huge, huge).outcome, 'clear')
        self.assertEqual(balance.reconcile(
            self.sample(huge + balance.EPSILON + 1, 10), huge, huge
        ).outcome, 'candidate_up')

    def test_signed_discrepancy_adjustment(self):
        sample = self.sample(100_000, 1)
        self.assertEqual(balance.reconcile(
            sample, 100_000, 100_000, known_discrepancy=-balance.EPSILON - 1
        ).outcome, 'candidate_up')
        self.assertEqual(balance.reconcile(
            sample, 100_000, 100_000, known_discrepancy=balance.EPSILON + 1
        ).outcome, 'candidate_down')

    def test_duplicate_calls_are_deterministic_and_do_not_mutate_candidate(self):
        first = self.sample(200_000 + balance.EPSILON + 1, 100)
        started = balance.reconcile(first, 100_000, 200_000)
        current = self.sample(first.total, 400)
        args = dict(candidate=started.candidate,
                    evidence=balance.I3Evidence(True, 0))
        one = balance.reconcile(current, 100_000, 200_000, **args)
        two = balance.reconcile(current, 100_000, 200_000, **args)
        self.assertEqual(one, two)

    def test_invalid_bounds_samples_candidate_and_evidence_rejected(self):
        for lower, upper in ((-1, 0), (2, 1), (False, 1), (1.0, 1), (1, 2.0)):
            with self.subTest(bounds=(lower, upper)), self.assertRaises(
                    balance.ReconcileInputError):
                balance.reconcile(self.sample(1, 1), lower, upper)
        for values in ((True, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0),
                       (0, 0, 0, -1), (2 ** 63, 0, 0, 0)):
            with self.subTest(sample=values), self.assertRaises(
                    balance.ReconcileInputError):
                balance.BalanceSample(*values)
        current = self.sample(100_000, 20)
        with self.assertRaises(balance.ReconcileInputError):
            balance.reconcile(current, 0, 200_000,
                              candidate=balance.Candidate('up', self.sample(100_000, 21)))
        with self.assertRaises(balance.ReconcileInputError):
            balance.reconcile(current, 0, 200_000,
                              evidence=balance.I3Evidence(True, True))
        for bad in (True, 1.0, '1'):
            with self.subTest(discrepancy=bad), self.assertRaises(
                    balance.ReconcileInputError):
                balance.reconcile(current, 0, 200_000, known_discrepancy=bad)


if __name__ == '__main__':
    unittest.main()
