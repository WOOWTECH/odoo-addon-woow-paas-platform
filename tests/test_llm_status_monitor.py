"""Pure monitor safety decisions, without Odoo or outbound calls."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'src/services/llm_status_monitor.py'
spec = importlib.util.spec_from_file_location('status_monitor', path)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
NOW = datetime(2026, 10, 21, 12)


def healthy():
    return dict(status_armed=True, status_result='ok', status_fingerprint='a',
                status_started_at=NOW - timedelta(seconds=20),
                status_completed_at=NOW - timedelta(seconds=1))


class TestStatusMonitorSafety(unittest.TestCase):
    def test_inactive_install_has_no_guard_effect(self):
        self.assertIsNone(monitor.guard_reason({}, NOW, 'a'))

    def test_exact_freshness_boundary_and_future_samples(self):
        row = healthy()
        row['status_started_at'] = NOW - timedelta(seconds=360)
        row['status_completed_at'] = row['status_started_at'] + timedelta(seconds=1)
        self.assertIsNone(monitor.guard_reason(row, NOW, 'a'))
        self.assertEqual(monitor.guard_reason(row, NOW + timedelta(microseconds=1), 'a'),
                         'llm_status_stale')
        row = healthy()
        row['status_completed_at'] = NOW + timedelta(seconds=1)
        self.assertEqual(monitor.guard_reason(row, NOW, 'a'), 'llm_status_stale')

    def test_failure_pending_missing_and_changed_config_fail_closed(self):
        for value in ('pending', 'invalid', 'unavailable', 'unknown', False):
            row = healthy()
            row['status_result'] = value
            self.assertEqual(monitor.guard_reason(row, NOW, 'a'), 'llm_status_invalid')
        row = healthy()
        self.assertEqual(monitor.guard_reason(row, NOW, 'b'), 'llm_status_config_changed')
        for key in ('status_started_at', 'status_completed_at'):
            row = healthy()
            row[key] = None
            self.assertEqual(monitor.guard_reason(row, NOW, 'a'), 'llm_status_stale')

    def test_slow_or_reversed_sample_is_not_trusted(self):
        row = healthy()
        row['status_started_at'] = NOW - timedelta(seconds=32)
        self.assertEqual(monitor.guard_reason(row, NOW, 'a'), 'llm_status_stale')
        row['status_started_at'] = NOW
        self.assertEqual(monitor.guard_reason(row, NOW, 'a'), 'llm_status_stale')

    def test_other_guard_is_never_cleared_even_without_reason(self):
        for reasons in ('i7,opaque_reason', '', None):
            row = dict(healthy(), guard_tripped=True, guard_reasons=reasons, guard_since=NOW)
            result = monitor.effective_guard(row, NOW, 'a')
            self.assertTrue(result['guard_tripped'])
            self.assertEqual(result['guard_reasons'], reasons)
            self.assertEqual(result['guard_since'], NOW)

    def test_status_failure_does_not_mutate_raw_guard(self):
        row = dict(healthy(), guard_tripped=False, guard_reasons='i7', guard_since=None)
        original = dict(row)
        result = monitor.effective_guard(row, NOW + timedelta(minutes=7), 'a')
        self.assertTrue(result['guard_tripped'])
        self.assertIn('i7', result['guard_reasons'])
        self.assertEqual(row, original)
        self.assertFalse(monitor.effective_guard(row, NOW, 'a')['guard_tripped'])

    def test_pending_lease_and_regular_five_minute_schedule(self):
        self.assertTrue(monitor.due({}, NOW))
        row = dict(healthy(), status_result='pending', status_started_at=NOW)
        self.assertFalse(monitor.due(row, NOW + timedelta(seconds=29)))
        self.assertTrue(monitor.due(row, NOW + timedelta(seconds=30)))
        row['status_result'] = 'ok'
        self.assertFalse(monitor.due(row, NOW + timedelta(seconds=299)))
        self.assertTrue(monitor.due(row, NOW + timedelta(seconds=300)))
        self.assertTrue(monitor.due(row, NOW - timedelta(seconds=1)))

    def test_finish_requires_current_generation_pending_and_lease(self):
        row = dict(healthy(), status_generation=4, status_result='pending', status_started_at=NOW)
        for generation, elapsed, expected in ((4, 0, True), (3, 1, False),
                                               (4, 30, False), (4, -1, False)):
            self.assertEqual(monitor.owns(row, generation, NOW + timedelta(seconds=elapsed)), expected)
        row['status_result'] = 'invalid'
        self.assertFalse(monitor.owns(row, 4, NOW))

    def test_restart_retains_guard_from_persisted_receipt(self):
        row = healthy()
        restarted = dict(row)
        self.assertIsNone(monitor.guard_reason(restarted, NOW, 'a'))
        self.assertEqual(monitor.guard_reason(restarted, NOW + timedelta(hours=1), 'a'),
                         'llm_status_stale')


if __name__ == '__main__':
    unittest.main()
