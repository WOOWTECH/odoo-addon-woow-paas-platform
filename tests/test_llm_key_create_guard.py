"""Execute real key creation control flow without Odoo or network.

Persistence is a memory adapter, not PG evidence. Final PG slot/lock/commit
checks must be scheduled in an owned scratch DB after review.
"""
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PKG = '_llm_key_gate_tests'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'src/services')]
sys.modules[PKG] = pkg
nac = importlib.import_module(PKG + '.newapi_client')


class LlmError(Exception):
    not_executed = False

    def __init__(self, code, **params):
        self.code, self.params = code, params


def load_worker():
    path = ROOT / 'src/models/llm_api_key.py'
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LlmApiKey')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {
        '_llm_assert_key_create_allowed', '_llm_create_with_reveal', '_llm_create_remote',
        '_llm_create_free_trial_key', '_llm_create_paid_key', '_llm_finish'}]
    for node in methods:
        node.decorator_list = []
    ns = dict(nac=nac, LlmError=LlmError, PARAM_ORDERS_ENABLED='woow_paas_platform.llm_orders_enabled',
              ERR_LLM_GUARD='llm_guard', ERR_LLM_UNAVAILABLE='llm_unavailable',
              ERR_LLM_INTERNAL='llm_internal', RESULT_NOT_EXECUTED='not_executed',
              EVENT_MODEL='event', KEY_MODEL='key', time=time,
              _utcnow=lambda: datetime(2026, 10, 21, 12), _iso=lambda value: str(value),
              _REVEALED_KEY_RE=__import__('re').compile(r'[A-Za-z0-9]{32,}'),
              TOKEN_LIST_FAILED=object(), safe_code=lambda value: value, _logger=MagicMock())
    node = ast.ClassDef(name='Worker', bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), 'exec'), ns)
    return ns['Worker'], ns


Worker, NS = load_worker()


class MemoryWorker(Worker):
    def __init__(self, kind='free_trial'):
        self.env = MagicMock()
        self.account = MagicMock()
        self.event = MagicMock()
        self.env.__getitem__.side_effect = lambda name: self.event if name == 'event' else self.account
        self.client = MagicMock()
        self.account._llm_web_client.return_value = self.client
        self.event._llm_throttle_reserve.return_value = 17
        self.spec = dict(kind=kind, expires_at=datetime(2026, 10, 22), model_allowlist='qwen',
                         key_label='', limit_points=None, models=('qwen',))
        self.mode = 'ok'
        self.checked = 0
        self.locked = False
        self.operations = []
        self.state = None
        self.slot = 0
        self.first_outcome = nac.Ok(data={})
        self.token = {'id': 33}
        self.client.user_create_token.side_effect = self.post
        self.client.user_reveal_token.return_value = nac.Ok(data={'key': 'R' * 40})

    def _llm_assert_key_create_allowed(self):
        self.checked += 1
        if self.mode != 'ok':
            exc = LlmError('llm_guard')
            exc.not_executed = True
            raise exc

    def _llm_insert_creating(self, *args, **kwargs):
        self.state = 'creating'
        self.slot = int(self.spec['kind'] == 'free_trial')
        if self.mode == 'flip_after_reserve':
            self.mode = 'blocked'
        return 9, 'wp_9_test'

    @contextmanager
    def _llm_creation_guard(self, key_id, **kwargs):
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def _llm_finish(self, key_id, values):
        self.assert_unlocked()
        self.operations.append(values['state'])
        if values['state'] == 'deleted' and self.state == 'creating':
            self.slot = 0
        self.state = values['state']
        return {'state': self.state, 'disabled_by': None}

    def assert_unlocked(self):
        if self.locked:
            raise AssertionError('independent finish while key lock held')

    def _llm_find_token(self, *args):
        return self.token

    def _llm_raise_for(self, account, response):
        raise LlmError('llm_unavailable')

    def post(self, *args, **kwargs):
        if self.mode == 'flip_after_post':
            self.mode = 'blocked'
        return self.first_outcome

    def create(self):
        return self._llm_create_with_reveal(self.account, 'request', None, '', self.spec,
                                          lambda name: {'name': name})


class TestKeyCreateAdmission(unittest.TestCase):
    def test_both_kinds_block_before_credentials_throttle_or_mirror(self):
        for kind in ('free_trial', 'paid'):
            worker = MemoryWorker(kind)
            worker.mode = 'blocked'
            with self.subTest(kind=kind), self.assertRaises(LlmError) as caught:
                worker.create()
            self.assertEqual(caught.exception.code, 'llm_guard')
            self.assertTrue(caught.exception.not_executed)
            worker.account._llm_up_token.assert_not_called()
            worker.event._llm_throttle_reserve.assert_not_called()
            worker.client.user_create_token.assert_not_called()
            self.assertIsNone(worker.state)

    def test_public_paid_and_free_entrypoints_gate_before_catalogue_or_reserve(self):
        for kind in ('free_trial', 'paid'):
            worker = MemoryWorker(kind)
            worker.mode = 'blocked'
            with self.subTest(kind=kind), self.assertRaises(LlmError) as caught:
                if kind == 'free_trial':
                    worker._llm_create_free_trial_key(None, 'request', None, '')
                else:
                    worker._llm_create_paid_key(None, 'request', None, '', None,
                                                name='test', limit_points=1,
                                                expires='90d', models=None)
            self.assertEqual(caught.exception.code, 'llm_guard')
            worker.event._llm_throttle_reserve.assert_not_called()
            worker.client.user_create_token.assert_not_called()
            self.assertIsNone(worker.state)

    def test_flip_after_reserve_unlocks_deletes_refunds_and_finishes_not_executed(self):
        for kind in ('free_trial', 'paid'):
            worker = MemoryWorker(kind)
            worker.mode = 'flip_after_reserve'
            # First gate succeeds, only final admission sees the flip.
            worker._llm_assert_key_create_allowed = types.MethodType(
                lambda self: MemoryWorker._llm_assert_key_create_allowed(self)
                if self.mode != 'flip_after_reserve' else None, worker)
            with self.subTest(kind=kind), self.assertRaises(LlmError) as caught:
                worker.create()
            self.assertEqual(caught.exception.code, 'llm_guard')
            self.assertEqual(worker.state, 'deleted')
            self.assertEqual(worker.slot, 0)
            worker.event._llm_event_finish.assert_called_once_with(
                17, 'not_executed', error_code='llm_guard', target_model='key', target_id=9)
            worker.client.user_create_token.assert_not_called()
            worker.client.user_reveal_token.assert_not_called()
            self.assertEqual(worker.operations, ['deleted'])

    def test_successful_first_post_then_guard_flip_keeps_reveal(self):
        worker = MemoryWorker()
        worker.mode = 'flip_after_post'
        worker._llm_assert_key_create_allowed = types.MethodType(
            lambda self: MemoryWorker._llm_assert_key_create_allowed(self)
            if self.mode != 'flip_after_post' else None, worker)
        result = worker.create()
        self.assertEqual(result['status'], 'active')
        self.assertEqual(worker.slot, 1)
        worker.client.user_create_token.assert_called_once()
        worker.client.user_reveal_token.assert_called_once()

    def test_unknown_first_post_then_guard_flip_preserves_safe_retry_and_reveal(self):
        worker = MemoryWorker()
        worker.mode = 'flip_after_post'
        worker._llm_assert_key_create_allowed = types.MethodType(
            lambda self: MemoryWorker._llm_assert_key_create_allowed(self)
            if self.mode != 'flip_after_post' else None, worker)
        worker.first_outcome = nac.Unknown('read_timeout')
        tokens = iter((None, {'id': 33}))
        worker._llm_find_token = lambda *args: next(tokens)
        self.assertEqual(worker.create()['status'], 'active')
        self.assertEqual(worker.client.user_create_token.call_count, 2)
        self.assertEqual(worker.slot, 1)

    def test_unknown_and_failed_list_keeps_reservation_without_retry(self):
        worker = MemoryWorker()
        worker.first_outcome = nac.Unknown('read_timeout')
        worker.token = NS['TOKEN_LIST_FAILED']
        with self.assertRaises(LlmError):
            worker.create()
        self.assertEqual(worker.state, 'failed_reveal')
        self.assertEqual(worker.slot, 1)
        worker.client.user_create_token.assert_called_once()
        worker.client.user_reveal_token.assert_not_called()
        self.assertEqual(worker.event._llm_event_finish.call_args.args[1], 'failed')

    def test_existing_transport_not_executed_still_returns_slot(self):
        worker = MemoryWorker()
        worker.first_outcome = nac.NotExecuted('connect_timeout')
        with self.assertRaises(LlmError) as caught:
            worker.create()
        self.assertTrue(caught.exception.not_executed)
        self.assertEqual(worker.slot, 0)
        self.assertEqual(worker.state, 'deleted')
        worker.client.user_create_token.assert_called_once()
        self.assertEqual(worker.event._llm_event_finish.call_args.args[1], 'not_executed')


class TestFreshCreateGate(unittest.TestCase):
    def gate(self, row=None, switch=('True',)):
        worker = Worker()
        worker.env = MagicMock()
        runtime = worker.env.__getitem__.return_value.sudo.return_value
        runtime._llm_read.return_value = {'guard_tripped': False} if row is None else row
        cursor = worker.env.registry.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = switch
        return worker, runtime, cursor

    def test_orders_disabled_invalid_and_missing_default_are_read_from_sql(self):
        for switch in (('False',), ('0',), ('garbage',), ('',), (None,), (True,)):
            worker, runtime, cursor = self.gate(switch=switch)
            with self.subTest(switch=switch), self.assertRaises(LlmError) as caught:
                worker._llm_assert_key_create_allowed()
            self.assertEqual(caught.exception.code, 'llm_guard')
            self.assertTrue(caught.exception.not_executed)
            cursor.execute.assert_called_once_with(
                'SELECT value FROM ir_config_parameter WHERE key = %s',
                (NS['PARAM_ORDERS_ENABLED'],), log_exceptions=False)
            worker.env.__getitem__.return_value.get_param.assert_not_called()
        for switch in (None, ('True',), ('1',), (' true ',)):
            worker, _, _ = self.gate(switch=switch)
            self.assertIsNone(worker._llm_assert_key_create_allowed())

    def test_true_unknown_or_malformed_guard_rows_fail_closed(self):
        for row in ({'guard_tripped': True}, {}, [], 'unknown', {'guard_tripped': 0},
                    {'guard_tripped': None}, {'guard_tripped': 'false'}):
            worker, _, _ = self.gate(row=row)
            with self.subTest(row=row), self.assertRaises(LlmError):
                worker._llm_assert_key_create_allowed()

    def test_actual_status_freshness_verdict_is_consumed(self):
        policy = importlib.import_module(PKG + '.llm_status_monitor')
        from datetime import timedelta
        now = datetime(2026, 10, 21, 12)
        for age, result, fingerprint in ((361, 'ok', 'a'), (0, 'invalid', 'a'),
                                          (0, 'ok', 'b')):
            row = dict(status_armed=True, status_started_at=now-timedelta(seconds=age),
                       status_completed_at=now-timedelta(seconds=age), status_result=result,
                       status_fingerprint=fingerprint, guard_tripped=False)
            verdict = policy.effective_guard(row, now, 'a')
            worker, _, _ = self.gate(row=verdict)
            with self.subTest(age=age, result=result), self.assertRaises(LlmError):
                worker._llm_assert_key_create_allowed()

    def test_db_failures_are_sanitized_and_never_fall_back_to_allow(self):
        for target in ('runtime', 'sql'):
            worker, runtime, cursor = self.gate()
            if target == 'runtime':
                runtime._llm_read.side_effect = RuntimeError('SECRET value')
            else:
                cursor.execute.side_effect = RuntimeError('SECRET value')
            with self.subTest(target=target), self.assertRaises(LlmError) as caught:
                worker._llm_assert_key_create_allowed()
            self.assertEqual(caught.exception.code, 'llm_guard')
            self.assertTrue(caught.exception.not_executed)
            self.assertEqual(caught.exception.params, {})
            self.assertNotIn('SECRET', str(caught.exception))


class TestExistingRejectionCleanup(unittest.TestCase):
    def test_real_finish_code_releases_free_slot_once_and_preserves_rejection_evidence(self):
        # Run existing _llm_finish bytecode with a strict in-memory SQL adapter.
        # This proves control flow/idempotence, NOT PostgreSQL locking or commit.
        from unittest.mock import patch
        free = types.ModuleType(PKG + '.llm_free_usage')
        free.USAGE_TABLE = 'usage'
        free.taipei_day = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).date()
        for kind in ('free_trial', 'paid'):
            class Cursor:
                def __init__(self):
                    self.state = 'creating'
                    self.values = {}
                    self.slot = int(kind == 'free_trial')
                    self.refunds = 0
                    self.result = None

                def execute(self, query, params=(), **kwargs):
                    if query.startswith('SELECT state, disabled_by, newapi_token_id'):
                        self.result = (self.state, None, None)
                    elif query.startswith('UPDATE key SET'):
                        columns = query.split(' SET ', 1)[1].split(' WHERE ', 1)[0]
                        columns = [part.strip().split(' = ')[0] for part in columns.split(',')]
                        if self.state != 'creating':
                            self.result = None
                            return
                        self.values.update(zip(columns, params))
                        self.state = self.values['state']
                        self.result = (7, kind, datetime(2026, 10, 20), self.state, None)
                    elif query.startswith('UPDATE usage SET trial_keys_created=GREATEST'):
                        self.slot = max(0, self.slot - 1)
                        self.refunds += 1
                        self.result = None
                    else:
                        raise AssertionError('unexpected SQL shape')

                def fetchone(self):
                    return self.result

            cr = Cursor()
            worker = Worker()
            worker.env = MagicMock()
            worker.env.registry.cursor.return_value.__enter__.return_value = cr
            obligations = MagicMock()
            obligations._lock_key_obligations.return_value = []
            adapter_env = {'woow_paas_platform.llm_backoffice_operation': obligations}
            names = dict(__package__=PKG, SUPERUSER_ID=1, KEY_TABLE='key', timezone=timezone,
                         _COLUMN_RE=__import__('re').compile(r'[a-z_]+'),
                         _begin=lambda *a, **k: None, _env_on=lambda *a: adapter_env)
            with self.subTest(kind=kind), patch.dict(NS, names), \
                    patch.dict(sys.modules, {free.__name__: free}):
                first = worker._llm_finish(9, {'state': 'deleted'})
                second = worker._llm_finish(9, {'state': 'deleted'})
                self.assertEqual(first['state'], 'deleted')
                self.assertIsNone(second)
                self.assertTrue(cr.values['creation_rejected'])
                self.assertEqual(cr.slot, 0)
                self.assertEqual(cr.refunds, int(kind == 'free_trial'))
                obligations._settle_not_created_obligations.assert_called_once_with([])


if __name__ == '__main__':
    unittest.main()
