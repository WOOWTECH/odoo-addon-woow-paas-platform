"""Run the actual narrowly scoped circuit exemption without ORM/network."""
import ast
import contextvars
from contextlib import contextmanager
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock

from test_llm_status_monitor_worker import nac

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

    def test_exception_restores_original_guard_context(self):
        with self.assertRaises(RuntimeError):
            with probe(self.client):
                raise RuntimeError('test')
        self.assertIsNone(ns['_STATUS_PROBE'].get())
        self.assertIsInstance(self.guard.before('anon', None), nac.NotExecuted)


if __name__ == '__main__':
    unittest.main()
