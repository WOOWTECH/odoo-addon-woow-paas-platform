"""Run real worker methods without Odoo; DB/locking coverage lives in src/tests.

Only the model's worker methods/constants are compiled from its AST. The memory
adapter replaces persistence, never result classification or send sequencing.
No network transport is constructed. Run: python3 tests/test_llm_credit_worker.py
"""
import ast
import copy
import importlib
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PKG = '_credit_worker_local'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'src/services')]
sys.modules[PKG] = pkg
nac = importlib.import_module(PKG + '.newapi_client')
redact = importlib.import_module(PKG + '.llm_redact')


class ValidationError(Exception):
    pass


def load_worker():
    tree = ast.parse((ROOT / 'src/models/llm_credit_order.py').read_text())
    model = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LlmCreditOrder')
    methods = []
    include = False
    for node in model.body:
        if isinstance(node, ast.FunctionDef):
            if node.name == '_cron_llm_credit':
                include = True
            if node.name == '_require_capability':
                break
            if include:
                node.decorator_list = [d for d in node.decorator_list
                                       if isinstance(d, ast.Name) and d.id == 'staticmethod']
                methods.append(node)
    constants = [n for n in tree.body if isinstance(n, ast.Assign) and
                 any(isinstance(t, ast.Name) and (t.id.startswith('CREDIT_') or
                     t.id in {'ORDER_MODEL', '_INT8_MAX', '_QUOTA_REJECTIONS'}) for t in n.targets)]
    request_id = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_request_id')
    ns = {'timedelta': timedelta, 'time': time, 'nac': nac, 'ValidationError': ValidationError,
          'llm_redact': redact, '_': lambda s: s, 'safe_code': lambda s: s,
          '_request_id': None, '_REQUEST_ID_RE': __import__('re').compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z'),
          'fields': types.SimpleNamespace(Datetime=types.SimpleNamespace(now=datetime.utcnow)),
          'llm_config': lambda env: env.config,
          'llm_unconfigured': lambda env, config: config.get('unconfigured', False),
          'run_llm_cron': lambda env, *a, **kw: env.runner(*a, **kw),
          'LlmAlert': types.SimpleNamespace(CREDIT_REVIEW='credit_review', CREDIT_BUDGET='credit_budget'),
          'BALANCE_CRON_XMLID': 'balance', '_logger': MagicMock()}
    ns.update(SUPERUSER_ID=1, EVENT_TABLE='woow_paas_platform_llm_event',
              ORDER_TABLE='woow_paas_platform_llm_credit_order', math=math,
              _utcnow=datetime.utcnow, LlmAlertInputError=ValidationError)
    common = ast.parse((ROOT / 'src/models/llm_common.py').read_text())
    shared = [n for n in common.body if (
        isinstance(n, ast.ClassDef) and n.name == 'LlmError') or (
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {
            'THROTTLE_LIMITS', 'THROTTLE_ATTEMPT_LIMITS', 'THROTTLE_WINDOW_SECONDS',
            'ERR_LLM_RATE_LIMITED', 'RESULT_NOT_EXECUTED'} for t in n.targets))]
    exec(compile(ast.fix_missing_locations(ast.Module(body=shared, type_ignores=[])),
                 str(ROOT / 'src/models/llm_common.py'), 'exec'), ns)
    event_tree = ast.parse((ROOT / 'src/models/llm_event.py').read_text())
    helper_nodes = [n for n in event_tree.body if isinstance(n, ast.FunctionDef) and
                    n.name in {'_throttle_reserve_cr', '_check_id', '_check_target'}]
    helper_nodes += [n for n in event_tree.body if isinstance(n, ast.Assign) and
                     any(isinstance(t, ast.Name) and t.id == 'TARGET_MODELS' for t in n.targets)]
    exec(compile(ast.fix_missing_locations(ast.Module(body=helper_nodes, type_ignores=[])),
                 str(ROOT / 'src/models/llm_event.py'), 'exec'), ns)
    ast_model = ast.ClassDef(name='Worker', bases=[], keywords=[], body=methods, decorator_list=[])
    exceptions = [n for n in tree.body if isinstance(n, ast.ClassDef) and
                  n.name in {'_CreditReservationInvalid', '_CreditClaimDeadline', '_CreditClaimCursor'}]
    module = ast.fix_missing_locations(ast.Module(body=constants + exceptions + [request_id, ast_model], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/models/llm_credit_order.py'), 'exec'), ns)
    return ns['Worker']


Worker = load_worker()


class MemoryWorker(Worker):
    """Simulate durable boundaries, keeping the actual worker control flow."""

    def __init__(self):
        self.env = MagicMock()
        self.env.config = {}
        self.runtime = {'guard_tripped': False}
        self.env.__getitem__.return_value._llm_read.side_effect = lambda: dict(self.runtime)
        self.client = MagicMock()
        self.env.__getitem__.return_value._llm_client.return_value = self.client
        self.claim = {'id': 1, 'account_id': 7, 'attempt': 1, 'event_id': 9}
        self.row = {'state': 'sending', 'attempt': 1, 'source': 'user',
                    'newapi_user_id': 101, 'account_user_id': 101,
                    'account_state': 'active', 'quota': 1562500,
                    'before_snapshot_present': False}
        self.owned = True
        self.events = []
        self.client.ap_get_user.return_value = nac.Ok(data={
            'id': 101, 'quota': 2 ** 40, 'used_quota': 2 ** 40, 'request_count': 2 ** 40})
        self.client.ap_add_quota.return_value = nac.Ok(request_id='rid-worker')

    def _llm_claimed_row(self, claim):
        if self.owned and self.row['state'] == 'sending' and self.row['attempt'] == claim['attempt']:
            return dict(self.row)
        return None

    def _llm_worker_update(self, claim, values, *, terminal_event=None):
        if not self._llm_claimed_row(claim):
            return False
        self.row.update(values)
        if terminal_event:
            self.events.append(terminal_event)
        return True

    def send(self):
        self._llm_send_claimed(self.claim, 999999999999)


class TestCreditWorkerLocal(unittest.TestCase):
    def setUp(self):
        self.worker = MemoryWorker()

    def test_guard_precedes_half_open_probe(self):
        self.worker.runtime['guard_tripped'] = True
        self.assertFalse(self.worker._cron_llm_credit())
        self.worker.env.runner.assert_not_called()

    def test_n27_precedes_runtime_and_probe(self):
        self.worker.env.config = {'unconfigured': True}
        self.assertFalse(self.worker._cron_llm_credit())
        self.worker.env.__getitem__.assert_not_called()
        self.worker.env.runner.assert_not_called()

    def test_snapshot_durable_before_one_post_and_int8(self):
        def post(*args, **kwargs):
            self.assertTrue(self.worker.row['before_snapshot_present'])
            self.assertEqual([self.worker.row[k] for k in (
                'quota_before', 'used_quota_before', 'request_count_before')], [2 ** 40] * 3)
            self.assertEqual(args, (101, 1562500))
            self.assertEqual(kwargs['priority'], nac.AP_PRIORITY_CONVERSION)
            return nac.Ok(request_id='rid-ok')
        self.worker.client.ap_add_quota.side_effect = post
        self.worker.send()
        self.worker.send()
        self.assertEqual(self.worker.row['state'], 'credited')
        self.assertEqual(self.worker.row['credit_evidence'], 'response')
        self.assertEqual(self.worker.events, ['ok'])
        self.worker.client.ap_add_quota.assert_called_once()

    def test_zero_snapshot_is_present(self):
        self.worker.client.ap_get_user.return_value = nac.Ok(data={
            'id': 101, 'quota': 0, 'used_quota': 0, 'request_count': 0})
        self.worker.send()
        self.assertTrue(self.worker.row['before_snapshot_present'])
        self.assertEqual(self.worker.row['quota_before'], 0)

    def test_closed_sources_and_id_mismatch_never_call(self):
        for source, state in [('user', 'needs_review'), ('manual', 'cancelled'), ('dr', 'cancelled')]:
            with self.subTest(source=source):
                w = MemoryWorker()
                w.row.update(source=source, account_state='closed')
                w.send()
                self.assertEqual(w.row['state'], state)
                w.client.ap_get_user.assert_not_called()
                w.client.ap_add_quota.assert_not_called()
        self.worker.row['account_user_id'] = 102
        self.worker.send()
        self.assertEqual(self.worker.row['state'], 'needs_review')
        self.worker.client.ap_get_user.assert_not_called()

    def test_suspended_settles_existing_paid_order(self):
        self.worker.row['account_state'] = 'suspended'
        self.worker.send()
        self.assertEqual(self.worker.row['state'], 'credited')

    def test_bad_snapshot_does_not_post(self):
        for key, value in [('quota', True), ('quota', 2 ** 63), ('used_quota', -1),
                           ('request_count', None), ('id', 102)]:
            with self.subTest(key=key, value=value):
                w = MemoryWorker()
                w.client.ap_get_user.return_value.data[key] = value
                w.send()
                self.assertEqual(w.row['state'], 'needs_review')
                w.client.ap_add_quota.assert_not_called()

    def test_read_failures_retry_without_post(self):
        for result in [nac.NotExecuted(), nac.Unknown(), nac.Definite(reason='no_message_key')]:
            w = MemoryWorker()
            w.client.ap_get_user.return_value = result
            w.send()
            self.assertEqual(w.row['state'], 'paid')
            self.assertEqual(w.events, ['not_executed'])
            w.client.ap_add_quota.assert_not_called()

    def test_unknown_is_never_retried_even_with_ok_without_request_id(self):
        for result in [nac.Unknown('timeout'), nac.Unknown('reset'), nac.Ok(),
                       nac.Ok(request_id='bad\nrequest')]:
            w = MemoryWorker()
            w.client.ap_add_quota.return_value = result
            w.send()
            w.send()
            self.assertEqual(w.row['state'], 'unknown')
            self.assertEqual(w.events, ['unknown'])
            w.client.ap_add_quota.assert_called_once()

    def test_not_executed_bounded_backoff_and_sixth_review(self):
        for attempt, minutes in enumerate([1, 2, 5, 15, 60, None], 1):
            w = MemoryWorker()
            w.claim['attempt'] = w.row['attempt'] = attempt
            w.client.ap_add_quota.return_value = nac.NotExecuted('connect')
            before = datetime.utcnow()
            w.send()
            if minutes:
                self.assertEqual(w.row['state'], 'paid')
                self.assertLess(abs((w.row['next_attempt_at'] - before).total_seconds() - minutes * 60), 2)
            else:
                self.assertEqual(w.row['state'], 'needs_review')
                self.assertIsNone(w.row['next_attempt_at'])
        w = MemoryWorker()
        w._llm_worker_retry(w.claim, 'llm_credit_not_executed', retry_after=120)
        self.assertGreater((w.row['next_attempt_at'] - datetime.utcnow()).total_seconds(), 118)

    def test_proxy_budget_waits_and_alerts_without_circuit_failure(self):
        result = nac.NotExecuted('proxy_budget', retry_after=180)
        self.worker.client.ap_add_quota.return_value = result
        self.worker.send()
        self.assertEqual(self.worker.row['state'], 'paid')
        self.assertEqual(self.worker.row['last_error_code'], 'llm_credit_budget')
        self.assertGreater((self.worker.row['next_attempt_at'] - datetime.utcnow()).total_seconds(), 178)
        self.assertEqual(self.worker.env.__getitem__.return_value._llm_alert.call_args.args[0], 'credit_budget')
        self.assertEqual(nac.circuit_effect('ap', result), (None, None))

    def test_five_definite_keys_use_source_safe_seam(self):
        keys = Worker._llm_classify_credit.__globals__['_QUOTA_REJECTIONS']
        self.assertEqual(len(keys), 5)
        for key in keys:
            for source in ['user', 'manual', 'dr']:
                with self.subTest(key=key, source=source):
                    w = MemoryWorker()
                    w.row['source'] = source
                    w.client.ap_add_quota.return_value = nac.Definite(message_key=key)
                    w.send()
                    self.assertEqual(w.row['state'], 'needs_review' if source == 'user' else 'cancelled')
                    if source == 'user':
                        self.assertEqual(w.row['last_error_code'], 'llm_credit_void_pending')
                    self.assertEqual(w.events, ['failed'])
                    self.assertNotIn('void_wallet_transaction_id', w.row)

    def test_policy_auth_and_denied_review_on_get_and_post(self):
        for result in [nac.Definite(proxy_error='policy', status=403),
                       nac.Definite(proxy_error='auth', status=401),
                       nac.Definite(code='AUTH_UNAUTHORIZED', status=401),
                       nac.Definite(proxy_error='denied', status=403)]:
            for method in ['ap_get_user', 'ap_add_quota']:
                w = MemoryWorker()
                getattr(w.client, method).return_value = result
                w.send()
                self.assertEqual(w.row['state'], 'needs_review')
        self.assertEqual(nac.circuit_effect('ap', nac.Definite(proxy_error='denied')), (None, None))

    def test_verifier_takes_over_during_get(self):
        def get(*a, **kw):
            self.worker.owned = False
            return nac.Ok(data={'id': 101, 'quota': 0, 'used_quota': 0, 'request_count': 0})
        self.worker.client.ap_get_user.side_effect = get
        self.worker.send()
        self.worker.client.ap_add_quota.assert_not_called()
        self.assertEqual(self.worker.events, [])

    def test_verifier_takes_over_during_post(self):
        def post(*a, **kw):
            self.worker.owned = False
            return nac.Ok(request_id='rid-late')
        self.worker.client.ap_add_quota.side_effect = post
        self.worker.send()
        self.assertEqual(self.worker.row['state'], 'sending')
        self.assertEqual(self.worker.events, [])
        self.worker.env.ref.assert_not_called()

    def test_guard_trips_during_get(self):
        def get(*a, **kw):
            self.worker.runtime['guard_tripped'] = True
            return nac.Ok(data={'id': 101, 'quota': 0, 'used_quota': 0, 'request_count': 0})
        self.worker.client.ap_get_user.side_effect = get
        self.worker.send()
        self.worker.client.ap_add_quota.assert_not_called()
        self.assertEqual(self.worker.row['state'], 'paid')


class TestCreditClaimDeadlineLocal(unittest.TestCase):
    def _claim(self, db):
        worker = MemoryWorker()
        worker.env.registry.cursor.side_effect = db.cursor
        ns = Worker._llm_claim_credit.__globals__
        with patch.dict(ns, time=types.SimpleNamespace(monotonic=lambda: db.elapsed),
                        _begin=lambda *a, **kw: None,
                        psycopg2=types.SimpleNamespace(Error=_ClaimLockTimeout,
                            errors=types.SimpleNamespace(UniqueViolation=type('UniqueViolation', (Exception,), {})))):
            return worker._llm_claim_credit(deadline=30)

    def _assert_live_timeouts(self, db):
        finish = Worker._llm_claim_credit.__globals__['CREDIT_CLAIM_FINISH_BUDGET']
        for started, lock_ms, statement_ms in db.business_budgets:
            self.assertLessEqual(statement_ms, int((30 - started - finish) * 1000))
            self.assertLessEqual(lock_ms, statement_ms)

    def test_slow_settings_and_long_lock_rollback_before_deadline(self):
        db = _ClaimDeadlineMemory(waits=(0.1, 10), set_cost=0.12,
                                  savepoint_cost=0.05, finish_cost=0.15)
        self.assertIsNone(self._claim(db))
        self.assertLess(db.elapsed, 30)
        self._assert_live_timeouts(db)
        self.assertEqual(db.order, db.original_order)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.commits, 0)

    def test_slow_settings_and_short_locks_still_commit_before_deadline(self):
        db = _ClaimDeadlineMemory(waits=(0.1, 0.1), set_cost=0.12,
                                  savepoint_cost=0.05, finish_cost=0.15)
        self.assertIsNotNone(self._claim(db))
        self.assertLess(db.elapsed, 30)
        self._assert_live_timeouts(db)
        self.assertEqual(db.order['state'], 'sending')
        self.assertEqual(db.order['attempt'], 2)
        self.assertEqual(len(db.events), 2)
        self.assertEqual(db.commits, 1)

    def test_slower_settings_refresh_timeouts_before_business_sql(self):
        db = _ClaimDeadlineMemory(waits=(0.1, 0.1), set_cost=0.2)
        db.elapsed = 0
        self.assertIsNotNone(self._claim(db))
        self._assert_live_timeouts(db)
        self.assertGreater(db.settings_count, 2 * len(db.business_budgets))
        self.assertEqual(db.commits, 1)

    def test_two_lock_waits_share_remaining_deadline_and_rollback(self):
        db = _ClaimDeadlineMemory(waits=(3.8, 3.8))
        self.assertIsNone(self._claim(db))
        self.assertLess(db.elapsed, 30)
        self.assertEqual([kind for kind, _cap in db.waits_seen], ['event', 'advisory'])
        self.assertLess(db.waits_seen[1][1], 600)
        self.assertEqual(db.order, db.original_order)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.events[0]['result'], 'not_executed')
        self.assertEqual(db.commits, 0)

    def test_statement_budget_bounds_multiple_locks_inside_one_sql(self):
        db = _ClaimDeadlineMemory(waits=(0.1, (3.0, 3.0)))
        self.assertIsNone(self._claim(db))
        self.assertLess(db.elapsed, 30)
        self.assertEqual(len(db.waits_seen), 3)
        self.assertLess(db.waits_seen[-1][1], 2000)
        self.assertEqual(db.order, db.original_order)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.commits, 0)

    def test_expired_between_locks_does_not_start_second_wait(self):
        db = _ClaimDeadlineMemory(waits=(3.8, 3.8), after_event=0.7)
        self.assertIsNone(self._claim(db))
        self.assertEqual([kind for kind, _cap in db.waits_seen], ['event'])
        self.assertEqual(db.order, db.original_order)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.commits, 0)

    def test_budget_expiry_after_insert_rolls_back_event_and_claim(self):
        db = _ClaimDeadlineMemory(waits=(0.1, 0.1), after_insert=5)
        self.assertIsNone(self._claim(db))
        self.assertTrue(db.inserted)
        self.assertEqual(len(db.events), 1)
        self.assertEqual(db.order, db.original_order)
        self.assertEqual(db.commits, 0)

    def test_two_short_waits_commit_owned_retry_event(self):
        db = _ClaimDeadlineMemory(waits=(0.1, 0.1))
        claim = self._claim(db)
        self.assertEqual(claim, {'id': 500, 'account_id': 7, 'attempt': 2, 'event_id': 2})
        self.assertEqual(db.order['state'], 'sending')
        self.assertEqual(db.order['attempt'], 2)
        self.assertEqual(db.events[-1]['target_id'], 500)
        self.assertFalse(db.events[-1]['credit_t1_pending'])
        self.assertEqual(db.commits, 1)

    def test_tick_passes_its_deadline_to_claim(self):
        worker = MemoryWorker()
        tick = types.SimpleNamespace(deadline=30, can_take=lambda _n: True)
        worker._llm_claim_credit = MagicMock(return_value=None)
        worker._llm_credit_tick({}, tick)
        worker._llm_claim_credit.assert_called_once_with(deadline=30)

    def test_insufficient_lock_budget_opens_no_cursor(self):
        worker = MemoryWorker()
        ns = Worker._llm_claim_credit.__globals__
        with patch.dict(ns, time=types.SimpleNamespace(monotonic=lambda: 27.0)):
            self.assertIsNone(worker._llm_claim_credit(deadline=30))
        worker.env.registry.cursor.assert_not_called()

    def test_repeated_blocked_denials_stop_within_tick_budget(self):
        worker = MemoryWorker()
        ns = Worker._llm_claim_credit.__globals__
        elapsed = [0.0]
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        worker.env.registry.cursor.return_value = cursor
        cursor.dictfetchone.side_effect = lambda: {
            'id': cursor.dictfetchone.call_count, 'account_id': cursor.dictfetchone.call_count,
            'partner_id': 70, 'attempt': 2, 'convert_event_id': 1, 'source': 'user'}
        error_type = ns['LlmError']
        db_error = type('FakeDbError', (Exception,), {})

        def slow_denial(cr, row):
            # Model each account's advisory wait ending just before lock_timeout,
            # followed by a full N6 bucket. SQL semantics have committed tests.
            elapsed[0] += 4.9
            raise error_type('llm_rate_limited', retry_after=60)
        with patch.dict(ns, time=types.SimpleNamespace(monotonic=lambda: elapsed[0]),
                        _begin=lambda *a, **kw: None,
                        psycopg2=types.SimpleNamespace(Error=db_error,
                            errors=types.SimpleNamespace(UniqueViolation=db_error))), \
                patch.object(Worker, '_llm_claim_event_cr', staticmethod(slow_denial)):
            self.assertIsNone(worker._llm_claim_credit(deadline=30))
        self.assertEqual(cursor.dictfetchone.call_count, 6)
        self.assertLess(elapsed[0], 30)
        self.assertEqual(worker.env.registry.cursor.call_count, 6)


class _EventMemory:
    """Cursor boundary for real reservation code, no Odoo or SQL server.

    SQL execution/READ COMMITTED races are separately covered by committed Odoo
    tests. This adapter makes the actual retry path's cap bypass reproducible.
    """
    def __init__(self, count=29, result='not_executed'):
        stamp = datetime.utcnow() - timedelta(minutes=5)
        self.events = [dict(id=i + 1, account_id=7, partner_id=70, action='convert',
                            result=result, create_date=stamp, uid=None, real_ip=None,
                            target_model=None, target_id=None) for i in range(count)]
        self.lock = threading.RLock()
        self.lock_keys = []

    def cursor(self):
        database = self

        class Cursor:
            def __enter__(self):
                self.locked = False
                self.rowcount = 1
                return self

            def __exit__(self, *exc):
                if self.locked:
                    database.lock.release()

            def execute(self, sql, params=(), **kw):
                if 'pg_advisory_xact_lock(' in sql:
                    database.lock.acquire()
                    self.locked = True
                    database.lock_keys.append(params[0])
                elif sql.startswith('SELECT result, action, account_id'):
                    self.output = next((dict(e) for e in database.events if e['id'] == params[0]), None)
                elif sql.startswith('SELECT create_date, result'):
                    account_id, action, since = params
                    self.output = sorted((e['create_date'], e['result']) for e in database.events
                                         if e['account_id'] == account_id and e['action'] == action
                                         and e['create_date'] > since)
                elif sql.startswith('INSERT INTO'):
                    columns = sql[sql.index('(') + 1:sql.index(')')].split(', ')
                    expr = sql.split('VALUES (', 1)[1].split(')', 1)[0].split(', ')
                    values = iter(params)
                    row = {key: next(values) if value == '%s' else
                           False if value == 'false' else int(value)
                           for key, value in zip(columns, expr)}
                    row['id'] = len(database.events) + 1
                    database.events.append(row)
                    self.output = (row['id'],)
                elif sql.startswith('UPDATE'):
                    self.rowcount = 1
                else:
                    raise AssertionError('unexpected SQL: ' + sql)

            def dictfetchone(self):
                return self.output

            def fetchone(self):
                return self.output

            def fetchall(self):
                return self.output
        return Cursor()


class _ClaimLockTimeout(Exception):
    pass


class _ClaimDeadlineMemory(_EventMemory):
    """Execute real claim/reserve SQL flow with deterministic waits and rollback.

    This models PostgreSQL timeout settings, not its locking implementation;
    real locking and isolation remain the committed Odoo suite's responsibility.
    """
    def __init__(self, waits, *, after_event=0, after_insert=0, set_cost=0,
                 savepoint_cost=0, finish_cost=0):
        super().__init__(count=1)
        self.events[0].update(target_model=Worker._llm_claim_event_cr.__globals__['ORDER_MODEL'], target_id=500)
        self.order = dict(id=500, account_id=7, partner_id=70, attempt=1,
                          convert_event_id=1, source='user', state='paid')
        self.original_order = dict(self.order)
        self.elapsed = 25.0
        self.delays = dict(zip(('event', 'advisory'), waits))
        self.waits_seen = []
        self.after_event, self.after_insert = after_event, after_insert
        self.commits, self.inserted = 0, False
        self.set_cost, self.savepoint_cost, self.finish_cost = set_cost, savepoint_cost, finish_cost
        self.business_budgets = []
        self.settings_count = 0

    def cursor(self):
        database = self
        base = super().cursor()

        class Cursor(type(base)):
            def __enter__(self):
                super().__enter__()
                self.initial = copy.deepcopy((database.order, database.events))
                self.timeouts = {'lock_timeout': 5000, 'statement_timeout': 0}
                self.event_fetched = False
                return self

            def restore(self, snapshot):
                database.order, database.events = copy.deepcopy(snapshot)

            def __exit__(self, *exc):
                database.elapsed += database.finish_cost
                if exc[0]:
                    self.restore(self.initial)
                else:
                    database.commits += 1
                return super().__exit__(*exc)

            @contextmanager
            def savepoint(self, **kwargs):
                snapshot = copy.deepcopy((database.order, database.events))
                try:
                    yield
                except Exception:
                    self.restore(snapshot)
                    raise
                finally:
                    database.elapsed += database.savepoint_cost

            def wait(self, kind):
                delays = database.delays[kind]
                if not isinstance(delays, tuple):
                    delays = (delays,)
                statement_left = self.timeouts['statement_timeout'] or float('inf')
                for delay in delays:
                    cap = min(self.timeouts['lock_timeout'], statement_left)
                    database.waits_seen.append((kind, cap))
                    database.elapsed += min(delay, cap / 1000)
                    if delay * 1000 >= cap:
                        raise _ClaimLockTimeout()
                    statement_left -= delay * 1000

            def execute(self, sql, params=(), **kwargs):
                if sql.startswith('SET LOCAL '):
                    database.elapsed += database.set_cost
                    database.settings_count += 1
                    key = sql.split()[2]
                    self.timeouts[key] = int(str(params[0]).removesuffix('ms'))
                    return
                database.business_budgets.append((database.elapsed,
                    self.timeouts['lock_timeout'], self.timeouts['statement_timeout']))
                if 'AS target SET state' in sql:
                    database.order.update(state='sending', attempt=database.order['attempt'] + 1)
                    self.output = dict(database.order)
                    return
                if sql.startswith('SELECT result, action, account_id'):
                    self.wait('event')
                    super().execute(sql, params, **kwargs)
                    self.event_fetched = True
                    return
                if 'pg_advisory_xact_lock(' in sql:
                    self.wait('advisory')
                if sql.startswith('UPDATE') and 'convert_event_id =' in sql:
                    database.order['convert_event_id'] = params[0]
                super().execute(sql, params, **kwargs)
                if sql.startswith('INSERT INTO'):
                    database.inserted = True
                    database.elapsed += database.after_insert

            def dictfetchone(self):
                result = super().dictfetchone()
                if self.event_fetched:
                    self.event_fetched = False
                    database.elapsed += database.after_event
                return result
        return Cursor()


class TestCreditRetryThrottleLocal(unittest.TestCase):
    def _row(self, order_id=500, attempt=2, event_id=None):
        return {'id': order_id, 'account_id': 7, 'partner_id': 70,
                'attempt': attempt, 'convert_event_id': event_id, 'source': 'user'}

    def test_attempt_30_allowed_but_31_rejected_on_actual_retry_path(self):
        db = _EventMemory()
        db.events[0].update(target_model=Worker._llm_claim_event_cr.__globals__['ORDER_MODEL'], target_id=500)
        with db.cursor() as cr:
            Worker._llm_claim_event_cr(cr, self._row(event_id=1))
        db.events[-1]['result'] = 'not_executed'
        self.assertEqual(len(db.events), 30)
        error_type = Worker._llm_claim_event_cr.__globals__['LlmError']
        with self.assertRaises(error_type) as error:
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row(event_id=30))
        self.assertEqual(error.exception.code, 'llm_rate_limited')
        self.assertGreater(error.exception.params['retry_after'], 0)
        self.assertEqual(len(db.events), 30)

    def test_tenth_counted_attempt_blocks_eleventh(self):
        db = _EventMemory(count=11, result='ok')
        db.events[0].update(result='not_executed', target_model=Worker._llm_claim_event_cr.__globals__['ORDER_MODEL'], target_id=500)
        error_type = Worker._llm_claim_event_cr.__globals__['LlmError']
        with self.assertRaises(error_type):
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row(event_id=1))
        self.assertEqual(len(db.events), 11)

    def test_two_parallel_retries_cannot_both_take_last_slot(self):
        db = _EventMemory()
        for index, order_id in enumerate([500, 501]):
            db.events[index].update(target_model=Worker._llm_claim_event_cr.__globals__['ORDER_MODEL'], target_id=order_id)
        barrier = threading.Barrier(2)
        error_type = Worker._llm_claim_event_cr.__globals__['LlmError']

        def reserve(order_id):
            barrier.wait(timeout=5)
            try:
                with db.cursor() as cr:
                    return Worker._llm_claim_event_cr(cr, self._row(order_id=order_id, event_id=order_id - 499))
            except error_type:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [500, 501]))
        self.assertEqual(len([value for value in results if value]), 1)
        self.assertEqual(len(db.events), 30)
        self.assertEqual(db.lock_keys, ['woow_llm_throttle:7:convert'] * 2)

    def test_first_t1_reservation_reused_at_cap_without_recount(self):
        db = _EventMemory(count=30)
        db.events[-1].update(result='started', target_model=None, target_id=None)
        with db.cursor() as cr:
            event_id = Worker._llm_claim_event_cr(cr, self._row(attempt=1, event_id=30))
        self.assertEqual(event_id, 30)
        self.assertEqual(len(db.events), 30)
        self.assertFalse(db.lock_keys)


class TestCreditReservationIntegrityLocal(unittest.TestCase):
    def _row(self, *, attempt=1, event_id=None, source='user'):
        return {'id': 500, 'account_id': 7, 'partner_id': 70, 'source': source,
                'attempt': attempt, 'convert_event_id': event_id}

    def test_missing_nonzero_reference_never_reserves_again(self):
        db = _EventMemory(count=1)
        with self.assertRaises(ValueError):
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row(event_id=999999))
        self.assertEqual(len(db.events), 1)

    def test_user_without_t1_reference_never_reserves(self):
        db = _EventMemory(count=0)
        with self.assertRaises(ValueError):
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row())
        self.assertFalse(db.events)

    def test_first_user_attempt_requires_started_reservation(self):
        for result in ('not_executed', 'failed', 'ok', 'unknown'):
            db = _EventMemory(count=1, result=result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                with db.cursor() as cr:
                    Worker._llm_claim_event_cr(cr, self._row(event_id=1))
            self.assertEqual(len(db.events), 1)

    def test_retry_requires_safe_prior_result_and_permanent_owner(self):
        model = Worker._llm_claim_event_cr.__globals__['ORDER_MODEL']
        for result in ('started', 'failed', 'ok', 'unknown'):
            db = _EventMemory(count=1, result=result)
            db.events[0].update(target_model=model, target_id=500)
            with self.subTest(result=result), self.assertRaises(ValueError):
                with db.cursor() as cr:
                    Worker._llm_claim_event_cr(cr, self._row(attempt=2, event_id=1))
            self.assertEqual(len(db.events), 1)
        db = _EventMemory(count=1, result='not_executed')
        with self.assertRaises(ValueError):
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row(attempt=2, event_id=1))

    def test_manual_dr_first_attempt_may_reserve_without_t1(self):
        for source in ('manual', 'dr'):
            db = _EventMemory(count=0)
            with db.cursor() as cr:
                event_id = Worker._llm_claim_event_cr(cr, self._row(source=source))
            self.assertEqual(event_id, 1)
            self.assertEqual(db.events[0]['target_id'], 500)

    def test_mismatched_permanent_target_is_not_rebound(self):
        db = _EventMemory(count=1, result='started')
        db.events[0].update(target_model=Worker._llm_claim_event_cr.__globals__['ORDER_MODEL'], target_id=501)
        with self.assertRaises(ValueError):
            with db.cursor() as cr:
                Worker._llm_claim_event_cr(cr, self._row(event_id=1))
        self.assertEqual(db.events[0]['target_id'], 501)
        self.assertEqual(len(db.events), 1)


if __name__ == '__main__':
    unittest.main()
