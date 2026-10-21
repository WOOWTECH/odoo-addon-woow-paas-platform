"""Execute the real monitor methods with a memory transaction adapter; no Odoo."""
import ast
from contextlib import contextmanager
from datetime import datetime
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
PKG = '_status_monitor_tests'
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT / 'src/services')]
sys.modules[PKG] = pkg
nac = importlib.import_module(PKG + '.newapi_client')
policy = importlib.import_module(PKG + '.llm_status_monitor')
invariants = importlib.import_module(PKG + '.llm_status_invariants')


@contextmanager
def probe(client):
    yield


def load_worker():
    path = ROOT / 'src/models/llm_status_monitor.py'
    tree = ast.parse(path.read_text())
    model = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    methods = [n for n in model.body if isinstance(n, ast.FunctionDef)]
    for node in methods:
        node.decorator_list = []
    worker = ast.ClassDef(name='Worker', bases=[], keywords=[], body=methods, decorator_list=[])
    ns = dict(nac=nac, policy=policy, validate_status=invariants.validate_status,
              status_probe=probe, llm_config=lambda env: env.config,
              llm_unconfigured=lambda env, config: config['unconfigured'],
              run_llm_cron=lambda env, xmlid, work, **kw: work(kw['config'], env.tick),
              STATUS_CRON_XMLID='woow_paas_platform.ir_cron_llm_status_monitor',
              LlmAlert=types.SimpleNamespace(STATUS_INVALID='status_invalid'),
              _logger=MagicMock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[worker], type_ignores=[])),
                 str(path), 'exec'), ns)
    return ns['Worker']


Worker = load_worker()


class MemoryWorker(Worker):
    def __init__(self):
        self.env = MagicMock()
        self.env.config = {'unconfigured': False}
        self.env.tick = types.SimpleNamespace(deadline=9999999999)
        self.client = MagicMock()
        self.client.base_url = 'http://newapi-proxy.new-api-stg.svc.cluster.local:8080'
        self.env.__getitem__.return_value._llm_client.return_value = self.client
        self.claim = {'status_generation': 1, 'url': self.client.base_url}
        self.events = []
        self.receipt = None
        self.owned = True
        self.env.__getitem__.return_value._llm_alert.side_effect = self.alert

    def _status_claim(self, deadline):
        self.events.append('claim')
        return self.claim

    def _status_finish(self, claim, result, deadline):
        self.events.append('persist')
        if not self.owned:
            return False
        self.receipt = result
        return result

    def alert(self, *args, **kwargs):
        self.events.append('alert')
        if self.receipt is None:
            raise AssertionError('alert before receipt')


def valid_data():
    return {'quota_per_unit': 500000, 'password_login_enabled': True,
            'turnstile_check': False, 'password_login_encryption_enabled': False,
            'enable_batch_update': False, 'enable_data_export': True,
            'register_enabled': False, 'self_use_mode_enabled': False,
            'quota_display_type': 'CUSTOM', 'custom_currency_exchange_rate': 32,
            'checkin_enabled': False}


class TestStatusMonitorWorker(unittest.TestCase):
    def test_unconfigured_returns_before_claim_client_or_event(self):
        worker = MemoryWorker()
        worker.env.config['unconfigured'] = True
        self.assertFalse(worker._cron_llm_status_monitor())
        self.assertEqual(worker.events, [])
        worker.env.__getitem__.assert_not_called()

    def test_one_anonymous_status_and_no_alert_on_success(self):
        worker = MemoryWorker()
        worker.client.status.return_value = nac.Ok(data=valid_data())
        worker._cron_llm_status_monitor()
        self.assertEqual(worker.events, ['claim', 'persist'])
        self.assertEqual(worker.receipt, 'ok')
        worker.client.status.assert_called_once()
        worker.client.ap_health.assert_not_called()

    def test_drift_and_transport_failures_are_persisted_before_alert(self):
        bad = valid_data()
        bad['register_enabled'] = True
        for response in (nac.Ok(data=bad), nac.Ok(data=None), nac.Unknown('non_json'),
                         nac.NotExecuted('connect_timeout'), nac.Definite(code='UNAUTHENTICATED')):
            worker = MemoryWorker()
            worker.client.status.return_value = response
            worker._cron_llm_status_monitor()
            self.assertIn(worker.receipt, ('invalid', 'unavailable'))
            self.assertEqual(worker.events, ['claim', 'persist', 'alert'])

    def test_client_configuration_exception_and_secret_safe_alert(self):
        worker = MemoryWorker()
        worker.env.__getitem__.return_value._llm_client.side_effect = RuntimeError('SECRET hostile text')
        worker._cron_llm_status_monitor()
        self.assertEqual(worker.receipt, 'unavailable')
        self.assertEqual(worker.events, ['claim', 'persist', 'alert'])
        self.assertNotIn('SECRET', repr(worker.env.__getitem__.return_value._llm_alert.call_args))

    def test_no_lease_means_no_http_and_no_alert(self):
        worker = MemoryWorker()
        worker.claim = None
        worker._cron_llm_status_monitor()
        worker.client.status.assert_not_called()
        self.assertEqual(worker.events, ['claim'])

    def test_late_old_worker_cannot_publish_or_alert(self):
        worker = MemoryWorker()
        worker.owned = False
        worker.client.status.return_value = nac.Ok(data=valid_data())
        worker._cron_llm_status_monitor()
        self.assertIsNone(worker.receipt)
        self.assertEqual(worker.events, ['claim', 'persist'])

    def test_changed_client_endpoint_is_never_contacted(self):
        worker = MemoryWorker()
        worker.claim = dict(worker.claim, url='http://different.invalid:8080')
        worker._cron_llm_status_monitor()
        worker.client.status.assert_not_called()
        self.assertEqual(worker.receipt, 'unavailable')

    def test_models_catalogue_consumes_effective_guard_before_cache(self):
        path = ROOT / 'src/models/llm_swr.py'
        tree = ast.parse(path.read_text())
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == '_llm_models_payload')
        method.decorator_list = []
        ns = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                     str(path), 'exec'), ns)
        model = MagicMock()
        model.env.__getitem__.return_value.sudo.return_value._llm_read.return_value = {
            'guard_tripped': True}
        self.assertEqual(ns['_llm_models_payload'](model)['state'], 'maintenance')
        model._llm_swr_get.assert_not_called()

    def test_inactive_xml_and_registry_share_the_exact_entry(self):
        import xml.etree.ElementTree as ET
        records = ET.parse(ROOT / 'src/data/llm_cron.xml').getroot()
        record = records.find("./data/record[@id='ir_cron_llm_status_monitor']")
        self.assertEqual(record.find("field[@name='active']").get('eval'), 'False')
        self.assertEqual(record.find("field[@name='interval_number']").text, '5')
        tree = ast.parse((ROOT / 'src/models/llm_common.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and
                    any(isinstance(t, ast.Name) and t.id == 'LLM_CRONS' for t in n.targets))
        registry = ast.literal_eval(node.value)
        self.assertEqual(len(registry), 19)
        self.assertEqual(registry['woow_paas_platform.ir_cron_llm_status_monitor'],
                         ('woow_paas_platform.llm_runtime_state', '_cron_llm_status_monitor'))

    def test_odoo_harness_does_not_assume_uncommitted_cross_cursor_visibility(self):
        path = ROOT / 'src/tests/test_llm_status_monitor.py'
        tree = ast.parse(path.read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        forbidden = {'configure', 'reset_llm_config'}
        self.assertFalse(any(isinstance(n.func, ast.Name) and n.func.id in forbidden for n in calls))
        self.assertFalse(any(isinstance(n.func, ast.Attribute) and
                             n.func.attr in {'commit', 'set_param', 'write', '_llm_read'} for n in calls))
        source = path.read_text()
        for seam in ("'_environment'", "'_now'", "'deadline_cursor'", "'_begin'"):
            self.assertIn(seam, source)

    def test_alert_failure_keeps_failure_receipt(self):
        worker = MemoryWorker()
        worker.client.status.return_value = nac.Unknown('non_json')
        worker.env.__getitem__.return_value._llm_alert.side_effect = RuntimeError('SECRET')
        worker._cron_llm_status_monitor()
        self.assertEqual(worker.receipt, 'unavailable')


if __name__ == '__main__':
    unittest.main()
