"""Offline acceptance-audit regression tests using synthetic pricing exports."""
import json
import importlib.util
from decimal import Decimal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / 'scripts/audit/check-llm-pricing.py'
PRICING_SPEC = importlib.util.spec_from_file_location('llm_pricing', ROOT / 'src/services/llm_pricing.py')
PRICING = importlib.util.module_from_spec(PRICING_SPEC)
PRICING_SPEC.loader.exec_module(PRICING)


def fixture(row_write_ratio=None, option_write_ratio=None):
    row = {'model_name': 'provider/model', 'quota_type': 0, 'model_ratio': 0.5,
           'completion_ratio': 4, 'cache_ratio': 0.1, 'enable_groups': ['default']}
    if row_write_ratio is not None:
        row['create_cache_ratio'] = row_write_ratio
    write_ratio = row_write_ratio if row_write_ratio is not None else (
        option_write_ratio if option_write_ratio is not None else 1.25)
    input_price = 2 * row['model_ratio'] * 1.3 * 32
    rows = [row] + [{'model_name': name, 'quota_type': 0, 'model_ratio': 1,
                     'completion_ratio': 1, 'enable_groups': ['free']}
                    for name in PRICING.FREE_MODELS]
    upstream = {'data': rows, 'group_ratio': {'default': 1.3, 'free': 0}, 'vendors': []}
    public = PRICING.normalize_pricing(upstream)
    # Model the BFF result for the selected effective CreateCacheRatio.
    paid = next(item for item in public['items'] if item['model'] == 'provider/model')
    paid['price']['cache_write'] = PRICING._decimal_text(Decimal(str(input_price)) * Decimal(str(write_ratio)))
    options = {'ModelRatio': {'provider/model': '0.5'},
               'CompletionRatio': {'provider/model': '4'},
               'CacheRatio': {'provider/model': '0.1'},
               'CreateCacheRatio': ({'provider/model': str(option_write_ratio)}
                                    if option_write_ratio is not None else {})}
    return upstream, public, options


class TestPricingAudit(unittest.TestCase):
    def run_audit(self, data):
        with tempfile.TemporaryDirectory(prefix='llm06-audit-test-') as directory:
            paths = []
            for name, content in zip(('pricing.json', 'models.json', 'options.json'), data):
                path = Path(directory) / name
                path.write_text(json.dumps(content))
                paths.append(path)
            return subprocess.run([sys.executable, str(AUDIT), '--pricing', str(paths[0]),
                '--models', str(paths[1]), '--options', str(paths[2]), '--expected-paid', '1'],
                capture_output=True, text=True, timeout=10)

    def test_default_cache_write_price_is_required_and_accepted(self):
        result = self.run_audit(fixture())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_create_cache_option_overrides_default(self):
        result = self.run_audit(fixture(row_write_ratio=1.5, option_write_ratio=1.5))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_option_and_row_override_disagreement_fails_audit(self):
        result = self.run_audit(fixture(row_write_ratio=1.25, option_write_ratio=1.5))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('CreateCacheRatio option/row disagreement', result.stdout)


if __name__ == '__main__':
    unittest.main()
