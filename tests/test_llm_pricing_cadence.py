"""Offline timing regression using real catalogue/key methods and cron XML.

The clock and persistence are in memory: this is not Odoo scheduler/DB evidence.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CRON_XMLID = 'woow_paas_platform.ir_cron_llm_pricing_refresh'


class LlmError(Exception):
    pass


def methods(path, class_name, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    body = [node for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in body:
        node.decorator_list = []
    cls = ast.ClassDef(name=class_name, bases=[], keywords=[], body=body, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 path, 'exec'), namespace)
    return namespace[class_name]


def cache_worker():
    path = ROOT / 'src/models/llm_swr.py'
    tree = ast.parse(path.read_text())
    # Execute the production TTL table and freshness predicate, without importing Odoo.
    nodes = [node for node in tree.body if (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and (target.id.startswith('KIND_') or target.id == 'SWR_KINDS')
            for target in node.targets)) or (
        isinstance(node, ast.FunctionDef) and node.name in ('is_stale', 'iso'))]
    now = [datetime(2026, 10, 26, 4, 20, 30)]
    ns = dict(_utcnow=lambda: now[0], timezone=timezone,
              pricing_profile_for_config=lambda config: '', llm_config=lambda env: {},
              PricingInvalid=ValueError, PRICING_CRON_XMLID=CRON_XMLID,
              CACHE_MODEL='woow_paas_platform.llm_swr_cache')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), ns)
    cls = methods('src/models/llm_swr.py', 'LlmSwrCache',
                  {'_llm_models_payload', '_llm_ensure_pricing_refresh_cadence'}, ns)
    worker = cls()
    worker.env = MagicMock()
    worker.env.__getitem__.return_value.sudo.return_value._llm_read.return_value = {
        'guard_tripped': False}
    worker._llm_swr_get = MagicMock()
    return worker, now, ns


def key_worker(cache):
    cls = methods('src/models/llm_api_key.py', 'LlmApiKey', {'_llm_default_models'},
                  dict(LlmError=LlmError, ERR_LLM_UNAVAILABLE='llm_unavailable',
                       _MODEL_ID_RE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]*')))
    worker = cls()
    worker.env = {'woow_paas_platform.llm_swr_cache': cache}
    return worker


def cron_seed():
    root = ET.parse(ROOT / 'src/data/llm_cron.xml').getroot()
    record = root.find(".//record[@id='ir_cron_llm_pricing_refresh']")
    return root, {field.attrib['name']: field.text for field in record.findall('field')}


class TestPricingCadence(unittest.TestCase):
    def setUp(self):
        self.cache, self.now, self.ns = cache_worker()
        self.key = key_worker(self.cache)
        self.row = dict(payload={'state': 'ok', 'items': [
            {'model': 'provider/model', 'group': 'default'}]},
            fetched_at=self.now[0], last_result='ok')
        self.cache._llm_swr_get.side_effect = lambda *args: self.row

    def assert_available(self):
        self.assertEqual(self.cache._llm_models_payload()['state'], 'ok')
        self.assertEqual(self.key._llm_default_models(), ['provider/model'])

    def assert_closed(self):
        self.assertEqual(self.cache._llm_models_payload(),
                         {'state': 'maintenance', 'items': [], 'data_as_of': None})
        with self.assertRaisesRegex(LlmError, 'llm_unavailable'):
            self.key._llm_default_models()

    def test_healthy_refresh_keeps_catalogue_and_key_creation_available_during_poll_drift(self):
        _, seed = cron_seed()
        self.assertEqual(seed['interval_type'], 'minutes')
        cadence = timedelta(minutes=int(seed['interval_number']))
        start = self.now[0]
        # Include a full 60-second scheduler poll and 30-second tick budget.
        # Repeat to catch cumulative drift rather than only the first expiry.
        for cycle in range(4):
            completed = self.row['fetched_at'] + cadence + timedelta(seconds=90)
            self.now[0] = completed - timedelta(seconds=1)
            with self.subTest(cycle=cycle, elapsed=self.now[0] - start):
                self.assert_available()
            self.row['fetched_at'] = completed
            self.now[0] = completed
            self.assert_available()

    def test_reported_request_times_with_short_poll_drift(self):
        _, seed = cron_seed()
        cadence = timedelta(minutes=int(seed['interval_number']))
        for request, last, delayed_refresh in (
                ('04:30:34', '04:20:30', '04:30:36'),
                ('04:50:53', '04:40:50', '04:50:58')):
            with self.subTest(request=request):
                # Synthetic prior timestamps; the request/next-refresh times come from the report.
                previous = datetime.fromisoformat('2026-10-26T' + last)
                self.now[0] = datetime.fromisoformat('2026-10-26T' + request)
                next_old = datetime.fromisoformat('2026-10-26T' + delayed_refresh)
                delay = next_old - previous - timedelta(minutes=10)
                completed = previous
                while completed + cadence + delay <= self.now[0]:
                    completed += cadence + delay
                self.row['fetched_at'] = completed
                self.assert_available()

    def test_ttl_boundary_and_outage_still_fail_closed(self):
        fetched = self.row['fetched_at']
        self.assertEqual(self.ns['SWR_KINDS']['pricing'][0], 600)
        self.now[0] = fetched + timedelta(seconds=600)
        self.assert_available()
        self.now[0] += timedelta(microseconds=1)
        self.assert_closed()
        self.row['fetched_at'] = self.now[0]
        self.row['last_result'] = 'connect'
        self.assert_closed()
        self.row['last_result'] = 'ok'
        self.row['payload'] = None
        self.assert_closed()
        self.row = None
        self.assert_closed()

    def test_guard_still_closes_fresh_prices(self):
        self.cache.env.__getitem__.return_value.sudo.return_value._llm_read.return_value = {
            'guard_tripped': True}
        self.assert_closed()

    def test_existing_noupdate_cron_is_updated_on_module_update(self):
        root, seed = cron_seed()
        self.assertIsNotNone(root.find("data[@noupdate='1']/record[@id='ir_cron_llm_pricing_refresh']"))
        # Must run outside noupdate; changing seed alone cannot update installed DBs.
        functions = root.findall("function[@name='_llm_ensure_pricing_refresh_cadence']")
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0].attrib['model'], 'woow_paas_platform.llm_swr_cache')
        cron = MagicMock(interval_number=10, interval_type='minutes', active=False,
                         code='model._cron_llm_pricing_refresh()')
        cron.model_id.model = 'woow_paas_platform.llm_swr_cache'
        cron.write.side_effect = lambda values: [setattr(cron, key, value) for key, value in values.items()]
        self.cache.env.ref.return_value = cron
        for _ in range(2):
            self.cache._llm_ensure_pricing_refresh_cadence()
        self.cache.env.ref.assert_called_with(CRON_XMLID, raise_if_not_found=False)
        cron.write.assert_called_once_with({'interval_number': 5})
        self.assertEqual(cron.interval_number, int(seed['interval_number']))
        self.assertFalse(cron.active)

    def test_upgrade_preserves_replacement_cron_with_same_xmlid(self):
        for code, model in (('model.other_job()', 'woow_paas_platform.llm_swr_cache'),
                            ('model._cron_llm_pricing_refresh()', 'other.model')):
            with self.subTest(code=code, model=model):
                cron = MagicMock(interval_number=10, interval_type='minutes', code=code)
                cron.model_id.model = model
                self.cache.env.ref.return_value = cron
                self.cache._llm_ensure_pricing_refresh_cadence()
                cron.write.assert_not_called()

    def test_upgrade_preserves_operator_custom_cadence_and_missing_record(self):
        for number, unit in ((1, 'minutes'), (5, 'minutes'), (2, 'hours'), (20, 'minutes')):
            with self.subTest(number=number, unit=unit):
                cron = MagicMock(interval_number=number, interval_type=unit,
                                 code='model._cron_llm_pricing_refresh()')
                cron.model_id.model = 'woow_paas_platform.llm_swr_cache'
                self.cache.env.ref.return_value = cron
                self.cache._llm_ensure_pricing_refresh_cadence()
                cron.write.assert_not_called()
        self.cache.env.ref.return_value = False
        self.cache._llm_ensure_pricing_refresh_cadence()


if __name__ == '__main__':
    unittest.main()
