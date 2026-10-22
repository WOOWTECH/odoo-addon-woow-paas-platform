"""Dormant I2 adapter tests without Odoo; fake storage is explicitly not PostgreSQL.

The real ORM/SQL tests live in src/tests/test_llm_reconcile_candidate.py.
Run: python tests/test_llm_reconcile_candidate.py -v
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PKG = '_i2_candidate_test'


def load_module():
    replacements = {}
    for name, path in ((PKG, ROOT / 'src'), (PKG + '.models', ROOT / 'src/models'),
                       (PKG + '.services', ROOT / 'src/services')):
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        replacements[name] = module
    odoo = types.ModuleType('odoo')
    odoo.api = types.SimpleNamespace(model=lambda f: f, model_create_multi=lambda f: f)
    odoo.fields = types.SimpleNamespace(**{
        name: lambda *a, **kw: None for name in ('Many2one', 'Selection', 'Char', 'Integer')})
    odoo.fields.Datetime = lambda *args, **kwargs: None
    odoo.fields.Datetime.now = lambda: None
    odoo.models = types.SimpleNamespace(Model=object)
    odoo._ = lambda text: text
    exceptions = types.ModuleType('odoo.exceptions')
    exceptions.AccessError = type('AccessError', (Exception,), {})
    exceptions.ValidationError = type('ValidationError', (Exception,), {})
    common = types.ModuleType(PKG + '.models.llm_common')
    common.LlmQuotaInteger = lambda **kw: None
    common.llm_unconfigured = lambda env: env.unconfigured
    billing = types.ModuleType(PKG + '.models.billing_incident')
    billing._is_odoo_module_uninstall = lambda env: False
    replacements.update({'odoo': odoo, 'odoo.exceptions': exceptions,
                         common.__name__: common, billing.__name__: billing})
    with patch.dict(sys.modules, replacements):
        spec = importlib.util.spec_from_file_location(
            PKG + '.models.llm_reconcile_candidate', ROOT / 'src/models/llm_reconcile_candidate.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


adapter = load_module()
Sample = adapter.BalanceSample


class Row:
    def __init__(self, store, values):
        self.store = store
        self.id = len(store) + 1
        self.__dict__.update(values)

    def __getitem__(self, name):
        return getattr(self, name)

    def invalidate_recordset(self):
        pass

    def with_context(self, *args, **kwargs):
        return self

    def write(self, values):
        self.__dict__.update(values)

    def unlink(self):
        self.store.clear()


class FakeEnv:
    su = True
    unconfigured = False

    def __init__(self):
        self.cr = Mock()
        self.cr.fetchone.return_value = ('active', 121222)
        self.model = Mock()

    def __getitem__(self, name):
        return self.model


class Observer(adapter.LlmReconcileCandidate):
    def __init__(self, store, env, bounds=(0, 0, 0)):
        self.store, self.env, self.bounds = store, env, bounds
        self.archives = []

    def search(self, *args, **kwargs):
        return self.store[0] if self.store else Empty()

    def browse(self):
        return Empty()

    def _archive(self, state, reason):
        self.archives.append({**{key: state[key] for key in adapter._SNAPSHOT_FIELDS}, 'reason': reason})
        self.store.remove(state)

    def _ledger_bounds(self, account_id):
        return self.bounds

    def with_context(self, *args, **kwargs):
        return self

    def create(self, values):
        self.store.append(Row(self.store, values))


class Empty:
    def __bool__(self):
        return False

    def invalidate_recordset(self):
        pass


class TestCandidateFoundation(unittest.TestCase):
    def setUp(self):
        self.store = []
        self.env = FakeEnv()
        self.observer = Observer(self.store, self.env)

    def observe(self, total, at, count=10):
        with patch.object(adapter.time, 'time', return_value=at):
            return self.observer._observe(1, Sample(total, 0, count, at))

    def test_restart_rehydrates_original_sample_and_does_not_infer_zero_i3(self):
        big = 2 ** 53 + 1
        self.assertEqual(self.observe(big, 100).outcome, 'candidate_up')
        self.observer = Observer(self.store, self.env)  # fresh worker over persisted fields
        self.assertEqual(self.observe(big, 399).outcome, 'candidate_up')
        decision = self.observe(big, 400)
        self.assertEqual(decision.outcome, 'evidence_pending')
        self.assertEqual(decision.severity, 'none')
        self.assertEqual(self.store[0].first_quota, big)
        self.assertEqual(self.store[0].first_sampled_at, 100)

    def test_changed_bounds_and_signed_discrepancy_recalculate_baseline(self):
        self.observe(100_000, 100)
        self.observer.bounds = (100_000, 100_000, 0)
        self.assertEqual(self.observe(100_000, 400).outcome, 'clear')
        self.assertFalse(self.store[0].direction)
        self.observer.bounds = (0, 0, 100_000)
        self.assertEqual(self.observe(100_000, 500).outcome, 'clear')
        self.observer.bounds = (0, 0, -100_000)
        self.assertEqual(self.observe(0, 600).outcome, 'candidate_up')

    def test_down_recheck_requires_quiet_requests_and_keeps_baseline(self):
        self.observer.bounds = (100_000, 100_000, 0)
        self.assertEqual(self.observe(0, 100).outcome, 'candidate_down')
        self.assertEqual(self.observe(0, 400, count=11).outcome, 'candidate_down')
        self.assertEqual(self.store[0].first_sampled_at, 400)
        self.assertEqual(self.observe(0, 700, count=11).outcome, 'mismatch')
        self.assertEqual(self.store[0].first_sampled_at, 400)

    def test_stale_duplicate_and_future_samples_do_not_mutate_state(self):
        self.observe(100_000, 100)
        before = dict(self.store[0].__dict__)
        for at, now in ((100, 100), (99, 100), (101, 162), (200, 199)):
            with self.subTest(at=at, now=now):
                with patch.object(adapter.time, 'time', return_value=now):
                    with self.assertRaises(adapter.ValidationError):
                        self.observer._observe(1, Sample(100_000, 0, 10, at))
                self.assertEqual(self.store[0].__dict__, before)

    def test_closed_or_missing_accounts_preserve_pending_first_sample(self):
        big = 2 ** 53 + 1
        for account, reason in ((('closed', 121222), 'closed'), (None, 'missing_account')):
            self.env.cr.fetchone.return_value = ('active', 121222)
            self.observe(big, 100)
            self.observe(big, 400)
            before = {key: self.store[0][key] for key in adapter._SNAPSHOT_FIELDS}
            self.env.cr.fetchone.return_value = account
            self.observer._ledger_bounds = Mock(side_effect=AssertionError('closed sampling'))
            self.assertIsNone(self.observer._observe(1, None))
            self.assertEqual(self.store, [])
            archived = self.observer.archives[-1]
            self.assertEqual(archived, {**before, 'reason': reason})
            self.assertEqual(archived['first_quota'], big)
            self.assertEqual(archived['outcome'], 'evidence_pending')
            self.observer._ledger_bounds = lambda account_id: (0, 0, 0)

    def test_new_identity_archives_old_baseline_and_restarts_five_minutes(self):
        self.observe(100_000, 100)
        self.observe(100_000, 400)
        self.env.cr.fetchone.return_value = ('active', 999)
        self.assertEqual(self.observe(100_000, 500).outcome, 'candidate_up')
        self.assertEqual(self.store[0].newapi_user_id, 999)
        self.assertEqual(self.store[0].first_sampled_at, 500)
        self.assertEqual(self.observer.archives[0]['newapi_user_id'], 121222)
        self.assertEqual(self.observer.archives[0]['reason'], 'identity_changed')

    def test_closed_then_reopened_same_identity_starts_fresh(self):
        self.observe(100_000, 100)
        self.env.cr.fetchone.return_value = ('closed', 121222)
        self.assertIsNone(self.observer._observe(1, None))
        self.env.cr.fetchone.return_value = ('active', 121222)
        self.assertEqual(self.observe(100_000, 500).outcome, 'candidate_up')
        self.assertEqual(self.store[0].first_sampled_at, 500)
        self.assertEqual(self.observer.archives[0]['first_sampled_at'], 100)

    def test_legacy_unbound_identity_is_never_attributed_to_current_account(self):
        self.observe(100_000, 100)
        self.store[0].newapi_user_id = 0
        self.store[0].lower_text = False
        self.assertEqual(self.observe(100_000, 500).outcome, 'candidate_up')
        self.assertEqual(self.observer.archives[0]['reason'], 'identity_unbound')
        self.assertIs(self.observer.archives[0]['lower_text'], False)
        self.assertEqual(self.store[0].first_sampled_at, 500)

    def test_archive_keeps_signed_aggregates_beyond_int64_as_text(self):
        big = 2 ** 64 + 1
        self.observer.bounds = (big, big + 1, -big)
        self.observe(0, 100)
        self.env.cr.fetchone.return_value = ('closed', 121222)
        self.observer._observe(1, None)
        archived = self.observer.archives[0]
        self.assertEqual((archived['lower_text'], archived['upper_text'], archived['known_text']),
                         (str(big), str(big + 1), str(-big)))

    def test_unconfigured_db_has_no_reads_or_candidate_effects(self):
        self.env.unconfigured = True
        self.observer.search = Mock(side_effect=AssertionError('candidate read'))
        self.assertIsNone(self.observer._observe(1, None))
        self.env.cr.execute.assert_not_called()
        self.assertEqual(self.store, [])

    def test_sql_text_casts_preserve_aggregates_above_int64(self):
        observer = adapter.LlmReconcileCandidate()
        observer.env = self.env
        big = 2 ** 64 + 1
        self.env.cr.fetchone.return_value = (str(big), str(big + 1), str(-big))
        self.assertEqual(observer._ledger_bounds(1), (big, big + 1, -big))
        query, params = self.env.cr.execute.call_args.args
        self.assertEqual(query.count('::text'), 3)
        self.assertEqual(params, (1, 1))
        self.assertIn("('user', 'manual', 'dr', 'recharge')", query)
        self.assertIn("('credited', 'sending', 'unknown', 'needs_review')", query)

    def test_non_sudo_cannot_observe_or_aggregate(self):
        self.env.su = False
        with self.assertRaises(adapter.AccessError):
            self.observe(100_000, 100)
        observer = adapter.LlmReconcileCandidate()
        observer.env = self.env
        with self.assertRaises(adapter.AccessError):
            observer._ledger_bounds(1)
        self.env.cr.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
