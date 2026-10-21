"""Run the actual narrowly scoped circuit exemption without ORM/network."""
import ast
import contextvars
import json
from contextlib import contextmanager
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch

from test_llm_status_monitor_worker import nac, MemoryWorker, Worker, valid_data

PATH = Path(__file__).resolve().parents[1] / 'src/models/llm_runtime_state.py'
tree = ast.parse(PATH.read_text())
helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'status_probe')
guard_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LlmCallGuard')
guard_class.bases = []
ns = dict(contextmanager=contextmanager, nac=nac, STATE_MODEL='runtime',
          _STATUS_PROBE=contextvars.ContextVar('status_probe_test', default=None),
          _PROBING=contextvars.ContextVar('probe_test', default=False))
exec(compile(ast.fix_missing_locations(ast.Module(body=[helper, guard_class], type_ignores=[])),
             str(PATH), 'exec'), ns)
Guard, probe = ns['LlmCallGuard'], ns['status_probe']


class TestStatusProbe(unittest.TestCase):
    def setUp(self):
        self.env = MagicMock()
        self.state = self.env.__getitem__.return_value.sudo.return_value
        self.state._llm_circuit_state.return_value = ('open', 60)
        self.guard = Guard(self.env, nac.ROLE_CRON)
        self.client = types.SimpleNamespace(_guard=self.guard)

    def test_open_circuit_stays_open_but_one_anon_probe_is_allowed(self):
        self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)
        with probe(self.client):
            self.assertIsNone(self.guard.before('anon', None))
            self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)
        self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)
        self.state._llm_circuit_record.assert_not_called()

    def test_wrong_identity_other_client_and_web_are_not_exempted(self):
        with probe(self.client):
            self.assertIsInstance(self.guard.before('ap', None), nac.NotExecuted)
            other = Guard(self.env, nac.ROLE_CRON)
            self.assertIsInstance(other.before('anon', None), nac.NotExecuted)
            self.assertIsNone(self.guard.before('anon', None))
        with self.assertRaises(ValueError):
            with probe(types.SimpleNamespace(_guard=Guard(self.env, nac.ROLE_WEB))):
                self.fail('web probe must be refused')

    def test_probe_after_never_records_success_or_failure(self):
        for result in (nac.Ok(data=valid_data()), nac.Unknown('read_timeout')):
            with self.subTest(kind=result.kind):
                self.state._llm_circuit_record.reset_mock()
                with probe(self.client):
                    self.assertIsNone(self.guard.before('anon', None))
                    self.guard.after('anon', result)
                    self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)
                self.state._llm_circuit_record.assert_not_called()
                self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)

    def test_normal_after_still_records_success_and_failure(self):
        for result in (nac.Ok(), nac.Unknown('read_timeout')):
            with self.subTest(kind=result.kind):
                self.state._llm_circuit_record.reset_mock()
                self.guard.after('anon', result)
                self.state._llm_circuit_record.assert_called_once_with(
                    *nac.circuit_effect('anon', result))

    def test_probe_does_not_suppress_another_guards_bookkeeping(self):
        other = Guard(self.env, nac.ROLE_CRON)
        with probe(self.client):
            self.assertIsInstance(other.before('anon', None), nac.NotExecuted)
            other.after('anon', nac.Ok())
        self.state._llm_circuit_record.assert_called_once_with(nac.CIRCUIT_SUCCESS, None)

    def test_real_client_status_and_worker_agree_on_unwrapped_data(self):
        for data, expected in ((valid_data(), 'ok'),
                               ({'success': True, 'data': valid_data()}, 'invalid')):
            with self.subTest(result=expected):
                session = MagicMock()
                session.request.return_value = types.SimpleNamespace(
                    status_code=200, headers={},
                    content=json.dumps({'success': True, 'data': data}).encode())
                client = nac.NewApiClient(nac.PROXY_URL_STG, 'probe', b'S' * 40,
                                          nac.ROLE_CRON, guard=self.guard,
                                          session_factory=lambda: session)
                worker = MemoryWorker()
                worker.env.__getitem__.return_value._llm_client.return_value = client
                worker.claim['url'] = client.base_url
                self.state._llm_circuit_record.reset_mock()
                with patch.dict(Worker._status_tick.__globals__, status_probe=probe):
                    worker._cron_llm_status_monitor()
                self.assertEqual(worker.receipt, expected)
                session.request.assert_called_once()
                self.assertTrue(session.request.call_args.args[1].endswith('/api/status'))
                self.state._llm_circuit_record.assert_not_called()

    def test_exception_restores_original_guard_context(self):
        with self.assertRaises(RuntimeError):
            with probe(self.client):
                raise RuntimeError('test')
        self.assertIsNone(ns['_STATUS_PROBE'].get())
        self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)


if __name__ == '__main__':
    unittest.main()
