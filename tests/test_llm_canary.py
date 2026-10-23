"""Canary claim authority, lease fencing and health-window accounting."""
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / 'src/services/llm_canary.py'
spec = importlib.util.spec_from_file_location('canary_policy', PATH)
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)


class TestCanaryPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, 16)
        self.work = dict(request_id='request', account_id=42, partner_id=51,
                         fingerprint='configuration', phase='creating')
        self.claim = policy.issue_claim(3, self.work)
        self.row = dict(canary_work=self.work, canary_generation=3,
                        canary_lease_until=self.now + timedelta(seconds=45),
                        canary_started_at=self.now, canary_failures=0, last_canary_at=None)

    def test_rpc_shaped_claims_and_unsealed_instances_cannot_authorize(self):
        for value in (True, {'generation': 3}, policy.CanaryClaim(
                3, 'request', 42, 51, 'configuration')):
            self.assertFalse(policy.authorized(value))
            self.assertFalse(policy.owns(self.row, value, self.now))
        self.assertTrue(policy.owns(self.row, self.claim, self.now))

    def test_generation_request_account_partner_and_configuration_are_fenced(self):
        for key, value in (('request_id', 'other'), ('account_id', 43),
                           ('partner_id', 52), ('fingerprint', 'changed')):
            changed = dict(self.row, canary_work=dict(self.work, **{key: value}))
            self.assertFalse(policy.owns(changed, self.claim, self.now))
        self.assertFalse(policy.owns(dict(self.row, canary_generation=4), self.claim, self.now))
        self.assertFalse(policy.owns(self.row, self.claim, self.now + timedelta(seconds=45)))

    def test_round_interval_and_backward_clock(self):
        self.assertTrue(policy.due(None, self.now))
        self.assertFalse(policy.due(self.now, self.now + timedelta(minutes=29)))
        self.assertTrue(policy.due(self.now, self.now + timedelta(minutes=30)))
        self.assertFalse(policy.due(self.now, self.now - timedelta(hours=1)))

    def test_pending_cleanup_retries_do_not_double_count_failure(self):
        first = policy.outcome(self.row, 'cleanup_pending', self.now, completed=False)
        self.assertEqual(first['failures'], 1)
        self.assertFalse(first['alert'])
        row = dict(self.row, canary_failures=1, last_canary_at=self.now)
        self.assertIsNone(policy.outcome(row, 'cleanup_pending',
                                        self.now + timedelta(minutes=1), completed=False))
        second = policy.outcome(row, 'cleanup_pending',
                                self.now + timedelta(minutes=30), completed=False)
        self.assertEqual(second['failures'], 2)
        self.assertTrue(second['alert'])

    def test_cleanup_completion_does_not_recount_same_failed_sample(self):
        row = dict(self.row, canary_failures=1, last_canary_at=self.now)
        sample = policy.outcome(row, 'probe_failed', self.now + timedelta(seconds=60), completed=True)
        self.assertEqual(sample['failures'], 1)
        self.assertEqual(sample['sample_at'], self.now)

    def test_success_resets_failure_streak(self):
        row = dict(self.row, canary_failures=2, last_canary_at=self.now)
        sample = policy.outcome(row, 'ok', self.now + timedelta(seconds=60), completed=True)
        self.assertEqual(sample['failures'], 0)
        self.assertFalse(sample['alert'])


if __name__ == '__main__':
    unittest.main()
