"""Offline Odoo record/PG-cursor contract fakes; NOT real Odoo/PG integration."""
import ast
from contextlib import contextmanager
from dataclasses import replace
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
import hashlib

ROOT = Path(__file__).resolve().parents[1]
# Load only dormant service files; do not import addon __init__ (requires Odoo).
package = ModuleType('notice_adapter_services')
package.__path__ = [str(ROOT / 'src/services')]
sys.modules[package.__name__] = package
a = importlib.import_module(package.__name__ + '.llm_price_notice_odoo_adapter')
t = importlib.import_module(package.__name__ + '.llm_price_notice_transport')
PAYLOAD = b'To: recipient@example.test\r\n\r\nSynthetic\r\n'


class SqlCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        return self._cursor.execute(sql.replace('%s', '?'), params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class SqlConnection:
    def __init__(self, path):
        self._connection = sqlite3.connect(path)
        self._connection.execute('PRAGMA foreign_keys = ON')

    def cursor(self):
        return SqlCursor(self._connection.cursor())

    def __getattr__(self, name):
        return getattr(self._connection, name)


class Record:
    def __init__(self, **values):
        self.__dict__.update(values)

    def __len__(self):
        return 1

    def exists(self):
        return self

    @property
    def state(self):
        raise AssertionError('mail_state_is_not_evidence')

    @property
    def message_id(self):
        raise AssertionError('message_id_is_not_evidence')


class TestOdooMailBoundary(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / 'synthetic.sqlite')
        with sqlite3.connect(self.path) as connection:
            for sql in (*t.SCHEMA, a.BINDING_SCHEMA):
                connection.execute(sql)
        self.journal = a.OdooJournal(lambda: SqlConnection(self.path))
        self.attempt = t.Attempt(str(uuid4()), 'a' * 64, 'stg', 'paas:synthetic',
                                 'recipient@example.test', 1, hashlib.sha256(PAYLOAD).hexdigest())
        self.journal.prepare(self.attempt)
        self.binding = a.MailBinding(self.attempt, 'synthetic_odoo', 41, 17)
        self.campaign = Record(fingerprint='a' * 64, environment='stg')
        self.obligation = Record(campaign_id=self.campaign, recipient_key='paas:synthetic',
                                 destination_snapshot='recipient@example.test', destination_revision=1)
        self.mail = Record(email_to='recipient@example.test', email_cc=False, recipient_ids=[])
        self.mail_model = Mock()
        self.mail_model.browse.return_value = self.mail
        self.obligation_model = Mock()
        self.obligation_model.browse.return_value = self.obligation
        self.models = {'mail.mail': self.mail_model,
                       'woow_paas_platform.llm_price_notice_recipient': self.obligation_model}
        self.database = 'synthetic_odoo'
        self.reader_calls = 0
        self.test_mode = False
        owner = self

        class FakeEnv:
            @property
            def cr(self):
                return SimpleNamespace(dbname=owner.database)

            def __getitem__(self, key):
                return owner.models[key]

        @contextmanager
        def open_environment():
            self.reader_calls += 1
            yield FakeEnv()

        self.boundary = a.OdooMailBoundary(
            self.journal, open_environment, runtime_environment='stg',
            test_mode=lambda: self.test_mode)
        guard = patch('socket.create_connection', side_effect=AssertionError('no_network'))
        guard.start()
        self.addCleanup(guard.stop)

    def bind(self, binding=None):
        self.boundary.bind(binding or self.binding, payload=PAYLOAD,
                           envelope=(self.attempt.destination,))

    def preflight(self, binding=None, **changes):
        values = dict(payload=PAYLOAD, envelope=(self.attempt.destination,))
        values.update(changes)
        return self.boundary.preflight(binding or self.binding, **values)

    def test_matching_committed_source_binding_and_bytes_without_acceptance(self):
        self.bind()
        self.assertEqual(self.preflight(), self.binding)
        self.assertEqual(self.reader_calls, 2)  # no cached outer ORM recordset
        self.mail_model.browse.assert_called_with(41)
        self.obligation_model.browse.assert_called_with(17)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                'SELECT * FROM llm_notice_transport_acceptance').fetchall(), [])
            self.assertEqual(connection.execute(
                'SELECT status FROM llm_notice_transport_attempt').fetchone(), ('prepared',))

    def test_no_committed_binding_or_attempt_denies(self):
        with self.assertRaisesRegex(ValueError, 'binding_not_committed'):
            self.preflight()
        missing = replace(self.binding, attempt=replace(self.attempt, attempt_uuid=str(uuid4())))
        with self.assertRaisesRegex(ValueError, 'attempt_not_committed'):
            self.bind(missing)

    def test_uncommitted_attempt_not_visible_to_other_connection(self):
        outer = sqlite3.connect(self.path)
        self.addCleanup(outer.close)
        missing = replace(self.attempt, attempt_uuid=str(uuid4()))
        outer.execute("""INSERT INTO llm_notice_transport_attempt VALUES
                      (?, ?, ?, ?, ?, ?, ?, 'prepared')""",
                      tuple(getattr(missing, name) for name in t.Attempt.__dataclass_fields__))
        with self.assertRaisesRegex(ValueError, 'not_committed'):
            self.journal.attempt(missing.attempt_uuid)
        outer.rollback()

    def test_missing_committed_mail_or_obligation_denies(self):
        for model in (self.mail_model, self.obligation_model):
            original = model.browse.return_value
            model.browse.return_value = Mock(exists=Mock(return_value=[]))
            with self.assertRaisesRegex(ValueError, 'source_missing'):
                self.bind()
            model.browse.return_value = original

    def test_test_bypass_fails_before_any_database_read(self):
        for value in (True, None, 0, 'false'):
            self.test_mode = value
            with self.assertRaisesRegex(ValueError, 'test_bypass'):
                self.bind()
            with self.assertRaisesRegex(ValueError, 'test_bypass'):
                self.preflight()
        self.assertEqual(self.reader_calls, 0)

    def test_final_envelope_and_mime_must_match_exactly(self):
        self.bind()
        for envelope in ([], (), ('other@example.test',),
                         (self.attempt.destination, 'extra@example.test')):
            with self.assertRaisesRegex(ValueError, 'single_final_envelope'):
                self.preflight(envelope=envelope)
        for payload in (PAYLOAD + b'changed', PAYLOAD.decode()):
            with self.assertRaisesRegex(ValueError, 'mime_snapshot'):
                self.preflight(payload=payload)

    def test_noncanonical_matching_snapshot_denies_bind_and_preflight(self):
        for payload in (PAYLOAD[:-2], PAYLOAD.replace(b'\r\n', b'\n'),
                        PAYLOAD + b'bare\rreturn\r\n', PAYLOAD + b'null\x00byte\r\n'):
            with self.subTest(payload=repr(payload)):
                attempt = replace(self.attempt, attempt_uuid=str(uuid4()),
                                  payload_sha256=hashlib.sha256(payload).hexdigest())
                binding = replace(self.binding, attempt=attempt, mail_id=self.binding.mail_id + 1)
                self.binding = binding
                self.journal.prepare(attempt)
                # Simulate an already persisted untrusted snapshot/binding.
                self.journal.bind(binding)
                for operation in (self.boundary.bind, self.boundary.preflight):
                    with self.assertRaisesRegex(ValueError, 'final_mime_snapshot'):
                        operation(binding, payload=payload, envelope=(attempt.destination,))
                self.assertEqual(self.reader_calls, 0)

    def test_direct_address_cc_or_partner_expansion_denies(self):
        for field, value in (('email_to', 'Name <recipient@example.test>'),
                             ('email_cc', 'extra@example.test'), ('recipient_ids', [1])):
            original = getattr(self.mail, field)
            setattr(self.mail, field, value)
            with self.assertRaisesRegex(ValueError, 'mail_recipient'):
                self.bind()
            setattr(self.mail, field, original)

    def test_campaign_destination_revision_and_audience_mismatch_deny(self):
        for record, field, value in (
            (self.campaign, 'fingerprint', 'b' * 64), (self.campaign, 'environment', 'prod'),
            (self.obligation, 'recipient_key', 'legacy:other'),
            (self.obligation, 'destination_snapshot', 'other@example.test'),
            (self.obligation, 'destination_revision', 2),
        ):
            original = getattr(record, field)
            setattr(record, field, value)
            with self.assertRaisesRegex(ValueError, 'obligation_snapshot'):
                self.bind()
            setattr(record, field, original)

    def test_wrong_runtime_or_odoo_database_denies(self):
        self.boundary._environment = 'prod'
        with self.assertRaisesRegex(ValueError, 'runtime_environment'):
            self.bind()
        self.boundary._environment = 'stg'
        self.database = 'another_odoo'
        with self.assertRaisesRegex(ValueError, 'database_mismatch'):
            self.bind()

    def test_persisted_binding_identity_cannot_be_replaced(self):
        self.bind()
        self.bind()  # idempotent same binding
        for field, value in (('mail_id', 42), ('obligation_id', 18),
                             ('database_name', 'other')):
            changed = replace(self.binding, **{field: value})
            with self.assertRaisesRegex(ValueError, 'binding_conflict'):
                self.journal.bind(changed)
            with self.assertRaisesRegex(ValueError, 'binding_snapshot'):
                self.preflight(changed)

    def test_same_odoo_mail_cannot_bind_another_attempt(self):
        self.bind()
        another = replace(self.attempt, attempt_uuid=str(uuid4()))
        self.journal.prepare(another)
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.bind(replace(self.binding, attempt=another))

    def test_claimed_attempt_and_changed_snapshot_deny(self):
        self.bind()
        with self.assertRaisesRegex(ValueError, 'mismatched'):
            self.journal.bind(replace(self.binding, attempt=replace(self.attempt, destination_revision=2)))
        self.journal._claim(self.attempt.attempt_uuid)
        with self.assertRaisesRegex(ValueError, 'not_ready'):
            self.preflight()
        with self.assertRaisesRegex(ValueError, 'not_ready'):
            self.bind()

    def test_committed_source_is_rechecked_after_binding(self):
        self.bind()
        self.obligation.destination_revision = 2
        with self.assertRaisesRegex(ValueError, 'obligation_snapshot'):
            self.preflight()


class FakePgCursor:
    def __init__(self, database='synthetic_odoo', pid=22):
        self.dbname = database
        self.identity = database, pid
        self.events = []

    def execute(self, sql, params=()):
        self.events.append(('execute', sql, params))

    def fetchone(self):
        return self.identity

    def commit(self):
        self.events.append('commit')

    def rollback(self):
        self.events.append('rollback')

    def close(self):
        self.events.append('close')


class TestRegistryConnectionFactory(unittest.TestCase):
    def setUp(self):
        self.outer = FakePgCursor(pid=11)
        self.fresh = FakePgCursor(pid=22)
        self.registry = Mock(cursor=Mock(return_value=self.fresh))
        self.factory = a.RegistryConnectionFactory(self.registry, self.outer)

    def test_constructor_inert_and_cursor_view_close_precedes_safe_commit(self):
        self.registry.cursor.assert_not_called()
        self.assertEqual(self.outer.events, [])
        connection = self.factory()
        connection.cursor().close()
        self.assertNotIn('close', self.fresh.events)
        connection.commit()
        connection.close()
        self.assertEqual(self.fresh.events[-2:], ['commit', 'close'])
        self.assertFalse(any(e in self.outer.events for e in ('commit', 'close', 'rollback')))

    def test_journal_exception_rolls_back_only_independent_cursor(self):
        journal = t.Journal(self.factory)
        with self.assertRaisesRegex(RuntimeError, 'synthetic'):
            with journal._transaction():
                raise RuntimeError('synthetic')
        self.assertEqual(self.fresh.events[-2:], ['rollback', 'close'])
        self.assertNotIn('rollback', self.outer.events)

    def test_outer_cursor_or_shared_backend_rejected_without_closing_caller(self):
        for cursor in (self.outer, FakePgCursor(pid=11)):
            self.registry.cursor.return_value = cursor
            with self.assertRaisesRegex(ValueError, 'independent'):
                self.factory()
            self.assertNotIn('close', cursor.events)
            self.assertNotIn('rollback', cursor.events)

    def test_identity_probe_failure_never_closes_potential_shared_owner(self):
        for method in ('execute', 'fetchone'):
            for error in (TimeoutError('probe cancelled'), KeyboardInterrupt()):
                with self.subTest(method=method, error=type(error).__name__):
                    shared = FakePgCursor(pid=11)
                    shared.close = self.outer.close  # Closing wrapper closes caller.
                    self.registry.cursor.return_value = shared
                    with patch.object(shared, method, side_effect=error):
                        with self.assertRaises(type(error)):
                            self.factory()
                    self.assertNotIn('close', self.outer.events)
                    self.assertNotIn('rollback', self.outer.events)

    def test_invalid_backend_identity_never_establishes_close_ownership(self):
        for pid in (None, True, '22', 0, -1):
            with self.subTest(pid=pid):
                shared = FakePgCursor(pid=pid)
                shared.close = self.outer.close
                self.registry.cursor.return_value = shared
                with self.assertRaisesRegex(ValueError, 'independent'):
                    self.factory()
                self.assertNotIn('close', self.outer.events)
                self.assertNotIn('rollback', self.outer.events)

    def test_wrong_database_is_closed_and_rejected(self):
        self.fresh.identity = ('other_database', 22)
        with self.assertRaisesRegex(ValueError, 'same_database'):
            self.factory()
        self.assertEqual(self.fresh.events[-1], 'close')
        self.assertNotIn('close', self.outer.events)

    def test_environment_must_use_exact_new_cursor(self):
        with self.factory.committed_environment(lambda cr: SimpleNamespace(cr=cr)) as env:
            self.assertIs(env.cr, self.fresh)
        self.assertEqual(self.fresh.events[-1], 'close')
        with self.assertRaisesRegex(ValueError, 'fresh_environment_cursor'):
            with self.factory.committed_environment(lambda cr: SimpleNamespace(cr=self.outer)):
                self.fail('must not yield outer environment')


class TestDormantEntrypoint(unittest.TestCase):
    def test_addon_entrypoints_do_not_register_adapter(self):
        for name in ('src/__init__.py', 'src/models/__init__.py',
                     'src/services/__init__.py', 'src/hooks.py'):
            for node in ast.walk(ast.parse((ROOT / name).read_text())):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self.assertNotIn('llm_price_notice_odoo_adapter', ast.unparse(node))

    def test_explicit_odoo_factory_uses_clean_env_and_real_test_probe(self):
        outer = FakePgCursor(pid=11)
        fresh = FakePgCursor(pid=22)
        registry = Mock(cursor=Mock(return_value=fresh))
        env = SimpleNamespace(registry=registry, cr=outer,
                              context={'forged_attempt': 'ignored'})
        make_env = Mock(side_effect=lambda cr, uid, context: SimpleNamespace(cr=cr))
        odoo = ModuleType('odoo')
        odoo.api = SimpleNamespace(Environment=make_env)
        modules = ModuleType('odoo.modules')
        modules.module = SimpleNamespace(current_test='synthetic_test')
        with patch.dict(sys.modules, {'odoo': odoo, 'odoo.modules': modules}):
            boundary = a.odoo_boundary(env, service_uid=7, runtime_environment='stg')
            registry.cursor.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'test_bypass'):
                boundary.preflight(None, payload=PAYLOAD, envelope=())
            registry.cursor.assert_not_called()
            modules.module.current_test = None
            self.assertIs(boundary._test_mode(), False)
            with boundary._open_environment() as committed:
                self.assertIs(committed.cr, fresh)
            make_env.assert_called_once_with(fresh, 7, {})
            self.assertNotIn('commit', outer.events)
            self.assertNotIn('close', outer.events)

    def test_invalid_factory_configuration_denied_before_odoo_import(self):
        for uid, environment in ((True, 'stg'), (0, 'stg'), (7, 'unknown')):
            with self.assertRaisesRegex(ValueError, 'configuration'):
                a.odoo_boundary(None, service_uid=uid, runtime_environment=environment)


if __name__ == '__main__':
    unittest.main()
