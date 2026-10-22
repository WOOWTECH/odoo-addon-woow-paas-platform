#!/usr/bin/env python3
"""Local LLM-13 regressions using production functions without importing Odoo.

AST loading removes only Odoo decorators/import-time model registration. Tests
execute the actual function bodies; SQLite executes the actual candidate SQL
with only PostgreSQL casts/placeholders adapted. These checks do not replace
Odoo ORM/transaction acceptance, which must run after sandbox release.
"""
import ast
import copy
import sqlite3
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
NOTICE = 'woow_paas_platform.llm_notification'
EVENT = 'woow_paas_platform.llm_event'
NOW = datetime(2031, 1, 1, 12)  # noqa: DTZ001 - Odoo uses naive UTC


def load_function(path, name, namespace=None, *, prefix=False):
    tree = ast.parse((ROOT / path).read_text())
    node = copy.deepcopy(next(n for n in ast.walk(tree)
                              if isinstance(n, ast.FunctionDef) and n.name == name))
    node.decorator_list = []
    if prefix:
        stop = next(i for i, n in enumerate(node.body)
                    if isinstance(n, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == 'candidates' for t in n.targets))
        node.body = node.body[:stop + 1] + [ast.Return(value=ast.Name(id='candidates', ctx=ast.Load()))]
    scope = dict(namespace or {})
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(code, str(ROOT / path), 'exec'), scope)  # noqa: S102 - trusted production AST
    return scope[name]


class KeyRecord:
    def __init__(self, reason=False, plan=None):
        self.id = 1
        self.state = 'disabled'
        self.disabled_by = reason
        self.not_me_disable_plan = plan
        self.not_me_disabled_notice_id = 7
        self.not_me_attempt_notice_id = 7
        self.not_me_disable_next_at = NOW

    def write(self, values):
        self.__dict__.update(values)

    def _llm_status(self):
        return 'disabled'

    def _llm_dto(self):
        return {'state': self.state}


class TestDisableProvenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provenance = staticmethod(load_function('src/models/llm_notifications.py', '_disable_provenance'))

    def test_cross_generation_mirror_cannot_erase_prior_user_disable(self):
        key = KeyRecord(False, {'complete': True, 'disabled_by_before_suspend': 'user'})
        self.assertEqual(self.provenance(key), {'disabled_by_before_suspend': 'user'})

    def test_all_prior_restrictions_survive_mirror_updates(self):
        for reason in ('user', 'ops', 'overdraft', 'free_daily_cap', 'dr_reconcile'):
            with self.subTest(reason=reason):
                key = KeyRecord(False, {'disabled_by_before_suspend': reason})
                self.assertEqual(self.provenance(key), {'disabled_by_before_suspend': reason})

    def test_legacy_missing_provenance_stays_unknown(self):
        self.assertEqual(self.provenance(KeyRecord(False, {'complete': True})), {})
        self.assertEqual(self.provenance(KeyRecord('suspend')), {})

    def test_new_enabled_key_has_explicit_provenance(self):
        self.assertEqual(self.provenance(KeyRecord()), {'disabled_by_before_suspend': False})

    def test_successful_human_status_change_retires_prior_receipt(self):
        body = load_function('src/models/llm_api_key.py', '_llm_set_key_status', {
            '_llm_internal': lambda record: record,
        })
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                key = KeyRecord('user', {'disabled_by_before_suspend': 'user'})
                account = SimpleNamespace(_llm_require_current_terms=Mock())
                client = SimpleNamespace(user_set_token_status=Mock(return_value=SimpleNamespace(ok=True)))
                service = SimpleNamespace(
                    _llm_management_key=lambda *_, account=account, key=key: (account, key),
                    _llm_management_token_id=lambda *_: 1,
                    _llm_management_remote=lambda *_, client=client: ('ip', 'fake-up', client),
                    _llm_management_audit=Mock(),
                )
                body(service, None, 1, enabled)
                self.assertNotIn('complete', key.not_me_disable_plan)
                self.assertEqual(key.not_me_disabled_notice_id, 0)
                self.assertEqual(key.not_me_attempt_notice_id, 0)
                # The just-confirmed decision also survives another late mirror.
                key.disabled_by = False
                self.assertEqual(self.provenance(key), {'disabled_by_before_suspend': False if enabled else 'user'})

    def test_failed_human_status_change_keeps_prior_provenance(self):
        body = load_function('src/models/llm_api_key.py', '_llm_set_key_status', {
            '_llm_internal': lambda record: record,
        })
        key = KeyRecord('user', {'disabled_by_before_suspend': 'user'})
        client = SimpleNamespace(user_set_token_status=lambda *_: SimpleNamespace(ok=False))
        def fail(*_):
            raise RuntimeError('definite rejection')
        service = SimpleNamespace(
            _llm_management_key=lambda *_: (SimpleNamespace(), key),
            _llm_management_token_id=lambda *_: 1,
            _llm_management_remote=lambda *_, client=client: ('ip', 'fake-up', client),
            _llm_management_raise=fail,
        )
        with self.assertRaises(RuntimeError):
            body(service, None, 1, False)
        self.assertEqual(key.not_me_disable_plan['disabled_by_before_suspend'], 'user')


class TestAuditObligations(unittest.TestCase):
    def setUp(self):
        datetime_field = SimpleNamespace(to_string=lambda stamp: stamp.isoformat(),
                                         from_string=datetime.fromisoformat)
        self.ns = {
            'fields': SimpleNamespace(Datetime=datetime_field), '_utcnow': lambda: NOW,
            '_llm_internal': lambda record: record, 'SUPERUSER_ID': 1,
            '_REASON_CODES': {'ops': 'llm_suspend_ops'}, 'KEY': KEY, 'ACCOUNT': ACCOUNT,
            'NOTICE': NOTICE, 'ACCOUNT_TABLE': 'woow_paas_platform_llm_account',
            'nac': SimpleNamespace(ROLE_CRON='cron'), 'timedelta': timedelta,
        }
        self.Event = SimpleNamespace(
            _llm_record_values=lambda action, **values: {'action': action, **values},
            _llm_insert_event_cr=Mock(side_effect=RuntimeError('audit unavailable')),
        )
        self.env = Env({EVENT: self.Event, NOTICE: SimpleNamespace(_llm_queue_account_disable=Mock())})
        self.account = AccountRecord(self.env)
        for method in ('_llm_lifecycle_event', '_llm_abort_resume', '_llm_suspend', '_llm_resume_key_status'):
            function = load_function('src/models/llm_account_lifecycle.py', method, self.ns)
            setattr(self.account, method, function.__get__(self.account))

    def test_public_transition_audit_still_fails_closed(self):
        with self.assertRaises(RuntimeError):
            self.account._llm_lifecycle_event('suspend')
        self.assertFalse(self.account.lifecycle_audit_pending)

    def test_worker_audit_failure_retains_validated_obligation(self):
        self.assertFalse(self.account._llm_lifecycle_event('key_status', defer=True))
        pending = self.account.lifecycle_audit_pending
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['values']['uid'], 7)
        self.assertEqual(pending[0]['values']['details'], {'reason': 'llm_suspend_ops', 'count': 7})

    def test_revocation_aborts_resume_even_when_all_audit_inserts_fail(self):
        self.account._llm_abort_resume()
        self.assertFalse(self.account.resume_pending)
        self.assertEqual(self.account.suspend_generation, 8)
        self.assertEqual(len(self.account.lifecycle_audit_pending), 2)
        self.env[NOTICE]._llm_queue_account_disable.assert_called_once_with(1)

    def test_remote_enable_then_audit_failure_queues_requarantine(self):
        remote = {'enabled': False}
        def status(_ip, _up, _token, enabled, **_kw):
            remote['enabled'] = enabled
            return SimpleNamespace(ok=True)
        self.env[KEY] = SimpleNamespace(_llm_management_token_id=lambda _key: 11)
        self.account._llm_client = lambda _role: SimpleNamespace(user_set_token_status=status)
        self.account._llm_tenant_ip = lambda: 'ip'
        self.account._llm_up_token = lambda **_kw: 'fake-up'
        self.assertFalse(self.account._llm_resume_key_status(KeyRecord(), True, SimpleNamespace(deadline=10)))
        self.assertTrue(remote['enabled'])
        self.assertFalse(self.account.resume_pending)
        self.assertEqual(self.account.suspend_generation, 8)
        self.assertEqual(len(self.account.lifecycle_audit_pending), 3)

    def test_one_obligation_replays_and_clears_atomically(self):
        self.account._llm_lifecycle_event('key_status', defer=True)
        self.Event._llm_insert_event_cr.side_effect = None
        service = AccountRecord(self.env)
        service.search = lambda *_args, **_kw: [self.account]
        self.env.cr.fetchone = lambda: (1,)
        flush = load_function('src/models/llm_account_lifecycle.py', '_llm_flush_lifecycle_audit', self.ns)
        tick = SimpleNamespace(can_take=lambda: True, took=Mock())
        self.assertTrue(flush(service, tick))
        self.assertFalse(self.account.lifecycle_audit_pending)
        tick.took.assert_called_once()


class TestUnknownEnableRestrictions(unittest.TestCase):
    def setUp(self):
        TestAuditObligations.setUp(self)
        self.Event._llm_insert_event_cr.side_effect = None
        self.remote = {'enabled': False, 'unknown': True}
        self.env[KEY] = SimpleNamespace(_llm_management_token_id=lambda _key: 11)
        def status(_ip, _up, _token, enabled, **_kw):
            self.remote['enabled'] = enabled
            return SimpleNamespace(ok=not self.remote['unknown'])
        self.account._llm_client = lambda _role: SimpleNamespace(user_set_token_status=status)
        self.account._llm_tenant_ip = lambda: 'ip'
        self.account._llm_up_token = lambda **_kw: 'fake-up'
        retry = load_function('src/models/llm_account_lifecycle.py', '_llm_resume_retry', self.ns)
        self.account._llm_resume_retry = retry.__get__(self.account)
        self.key = KeyRecord('suspend', {'disabled_by_before_suspend': False})
        self.key.not_me_disable_attempts = 0
        self.tick = SimpleNamespace(deadline=10)
        self.assertFalse(self.account._llm_resume_key_status(self.key, True, self.tick))
        self.account._llm_resume_retry(self.key, NOW)
        self.assertTrue(self.remote['enabled'])
        # Execute the actual restriction branch without importing an Odoo registry.
        tree = ast.parse((ROOT / 'src/models/llm_account_lifecycle.py').read_text())
        node = copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                                  and ast.unparse(n.test) == 'expired or exhausted or capped'))
        wrapper = ast.parse('def restriction(account, key, tick, now, expired, exhausted, capped, free_usage):\n    pass').body[0]
        wrapper.body = [node, ast.Return(value=ast.Constant(False))]
        scope = dict(self.ns)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), '<production restriction>', 'exec'), scope)  # noqa: S102 - trusted production AST
        self.restriction = scope['restriction']

    def test_new_cap_requires_confirmed_remote_disable(self):
        self.remote['unknown'] = False
        self.assertTrue(self.restriction(self.account, self.key, self.tick, NOW,
                                         False, False, True, {'day_tpe': '2031-01-01'}))
        self.assertFalse(self.remote['enabled'])
        self.assertEqual(self.key.disabled_by, 'free_daily_cap')

    def test_unknown_disable_does_not_publish_a_completed_restriction(self):
        self.restriction(self.account, self.key, self.tick, NOW,
                         False, False, True, {'day_tpe': '2031-01-01'})
        self.assertEqual(self.key.disabled_by, 'suspend')
        self.assertTrue(self.account.resume_pending)
        self.assertEqual(self.key.not_me_disable_attempts, 2)

    def test_audit_failure_during_safety_disable_aborts_resume(self):
        self.remote['unknown'] = False
        self.Event._llm_insert_event_cr.side_effect = RuntimeError('audit unavailable')
        self.restriction(self.account, self.key, self.tick, NOW,
                         False, False, True, {'day_tpe': '2031-01-01'})
        self.assertFalse(self.remote['enabled'])
        self.assertFalse(self.account.resume_pending)
        self.assertTrue(self.account.lifecycle_audit_pending)


class Env(dict):
    def __init__(self, records):
        super().__init__(records)
        self.cr = SimpleNamespace(savepoint=nullcontext, execute=Mock())


class AccountRecord:
    def __init__(self, env):
        self.env = env
        self.id = 1
        self.partner_id = SimpleNamespace(id=2)
        self.lifecycle_operator_id = SimpleNamespace(id=7)
        self.lifecycle_audit_pending = False
        self.suspend_reason = 'ops'
        self.suspend_generation = 7
        self.not_me_notice_id = 0
        self.resume_pending = True
        self.state = 'suspended'

    def ensure_one(self):
        pass

    def sudo(self):
        return self

    def invalidate_recordset(self):
        pass

    def _llm_lifecycle_lock(self):
        pass

    def write(self, values):
        self.__dict__.update(values)


class TestResumeCandidateFairness(unittest.TestCase):
    def candidates(self, *, revoke_first=False):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE woow_paas_platform_llm_account (
                id INTEGER, state TEXT, resume_pending BOOLEAN,
                lifecycle_audit_pending TEXT, write_date TEXT,
                lifecycle_operator_id INTEGER, partner_id INTEGER);
            CREATE TABLE woow_paas_platform_llm_api_key (
                account_id INTEGER, state TEXT, disabled_by TEXT, not_me_disable_next_at TEXT);
            CREATE TABLE res_users (id INTEGER, partner_id INTEGER, active BOOLEAN);
        """)
        for ident in range(1, 22):
            operator = 9 if revoke_first and ident == 1 else 7
            connection.execute('INSERT INTO woow_paas_platform_llm_account VALUES (?,?,?,?,?,?,?)',
                               (ident, 'suspended', True, None, str(ident).zfill(3), operator, ident + 100))
            next_at = NOW + timedelta(hours=1) if ident <= 20 else NOW - timedelta(seconds=1)
            connection.execute('INSERT INTO woow_paas_platform_llm_api_key VALUES (?,?,?,?)',
                               (ident, 'disabled', 'suspend', next_at.isoformat()))
        class Cursor:
            def execute(self, sql, params):
                sql = sql.replace('::jsonb', '').replace('= ANY(%s)', 'IN (%s)')
                flattened = []
                for param in params:
                    if isinstance(param, list):
                        sql = sql.replace('%s', ','.join('?' for _ in param) or 'NULL', 1)
                        flattened.extend(param)
                    else:
                        sql = sql.replace('%s', '?', 1)
                        flattened.append(param.isoformat() if isinstance(param, datetime) else param)
                self.cursor = connection.execute(sql, flattened)
            def fetchall(self):
                return self.cursor.fetchall()
        class Users:
            def sudo(self):
                return self
            def with_context(self, **_kwargs):
                return self
            def browse(self, ident):
                return SimpleNamespace(id=ident)
        def authorize(_env, user):
            if user.id == 9:
                raise PermissionError('revoked')
        env = Env({KEY: SimpleNamespace(sudo=lambda: SimpleNamespace(flush_model=lambda: None)),
                   'res.users': Users()})
        env.cr = Cursor()
        account = SimpleNamespace(env=env, flush_model=lambda: None)
        function = load_function('src/models/llm_account_lifecycle.py', '_llm_resume_tick', {
            '_utcnow': lambda: NOW, 'KEY': KEY, 'ACCOUNT_TABLE': 'woow_paas_platform_llm_account',
            'nac': SimpleNamespace(MIN_CALL_SECONDS=1), '_named_system_operator': authorize,
            'AccessError': PermissionError,
        }, prefix=True)
        return function(account, SimpleNamespace(can_take=lambda *_: True))

    def test_backoff_accounts_do_not_hide_twenty_first_due_account(self):
        self.assertEqual(self.candidates(), [21])

    def test_revocation_is_not_delayed_by_remote_backoff(self):
        self.assertEqual(self.candidates(revoke_first=True), [1, 21])


if __name__ == '__main__':
    unittest.main()
