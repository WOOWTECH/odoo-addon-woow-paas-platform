"""Pure pricing contract tests; run directly, no Odoo or external service required."""
import importlib.util
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

spec = importlib.util.spec_from_file_location('llm_pricing', Path(__file__).resolve().parents[1] / 'src/services/llm_pricing.py')
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)


def row(**values):
    return dict({'model_name': 'provider/model', 'quota_type': 0, 'model_ratio': 0.5,
                 'completion_ratio': 4, 'cache_ratio': 0.1, 'create_cache_ratio': 1.25,
                 'enable_groups': ['default'], 'tags': 'text,vision,reasoning', 'vendor_id': 1}, **values)


def envelope(*rows, **values):
    return dict({'data': list(rows), 'group_ratio': {'default': 1.3, 'free': 0},
                 'vendors': [{'id': 1, 'name': 'Provider', 'private': 'hidden'}]}, **values)


class TestPricing(unittest.TestCase):
    def test_ratio_and_cache_write_are_exact_points(self):
        item = pricing.normalize_pricing(envelope(row()))['items'][0]
        self.assertEqual(item['price'], {'input': '41.6', 'output': '166.4', 'cache_read': '4.16',
                                         'cache_write': '52', 'per_call': None})
        self.assertEqual(item['vendor'], 'Provider')
        self.assertEqual(item['tags'], ['reasoning', 'text', 'vision'])
        self.assertNotIn('private', str(item))

    def test_fixed_and_tiered_modes(self):
        mode, price = pricing.model_price(row(quota_type=1, model_price='0.003'), 'default', '1.3')
        self.assertEqual((mode, price['per_call']), ('fixed', '0.1248'))
        self.assertIsNone(price['input'])
        self.assertEqual(pricing.model_price(row(billing_mode='tiered_expr'), 'default', 1.3), ('tiered', None))

    def test_small_rates_preserve_precision_even_with_external_decimal_context(self):
        with localcontext() as ctx:
            ctx.prec = 3
            _, price = pricing.model_price(row(model_ratio='0.000000000001', completion_ratio='0.123456789'), 'default', '1.3')
        self.assertEqual(price['input'], '0.0000000000832')
        self.assertEqual(price['output'], '0.0000000000102716048448')

    def test_two_free_models_zero_regardless_of_paid_fields(self):
        for name in pricing.FREE_MODELS:
            result = pricing.normalize_pricing(envelope(row(model_name=name, enable_groups=['free'])))
            item = result['items'][0]
            self.assertEqual(set(item['price'].values()), {'0'})
            self.assertEqual(item['free_limits']['per_day'], 200)

    def test_internal_rows_and_zero_cost_endpoints_never_enter_public_dto(self):
        rows = [row(), row(model_name='private/intern', enable_groups=['internal']),
                row(model_name='zero', model_ratio=0), row(model_name='or/free:free'),
                row(model_name='fixed-zero', quota_type=1, model_price=0)]
        result = pricing.normalize_pricing(envelope(*rows))
        self.assertEqual([item['model'] for item in result['items']], ['provider/model'])

    def test_group_invariant_is_checked_before_rows_even_when_empty(self):
        for groups in ({'default': 1.3, 'internal': 1}, {'internal': 1}, {'default': 1.2}, {'free': .1}):
            with self.subTest(groups=groups), self.assertRaises(pricing.PricingInvalid):
                pricing.normalize_pricing(envelope(group_ratio=groups))

    def test_missing_group_or_unknown_free_model_fail_closed(self):
        for data in (envelope(row(), group_ratio={}), envelope(row(model_name='other', enable_groups=['free'])),
                     envelope(row(model_name='qwen3-4b-cluster1', enable_groups=['default']))):
            with self.assertRaises(pricing.PricingInvalid):
                pricing.normalize_pricing(data)

    def test_bad_and_nonfinite_numbers_do_not_publish_partial_catalogue(self):
        for value in (True, None, '', 'NaN', float('inf'), '-0.1', {}, '1e100', Decimal('NaN')):
            with self.subTest(value=value), self.assertRaises(pricing.PricingInvalid):
                pricing.normalize_pricing(envelope(row(), row(model_name='bad', model_ratio=value)))

    def test_missing_optional_cache_rate_is_not_misrepresented_as_free(self):
        item = pricing.normalize_pricing(envelope(row(cache_ratio=None, create_cache_ratio=None)))['items'][0]
        self.assertIsNone(item['price']['cache_write'])
        self.assertIsNone(item['price']['cache_read'])

    def test_duplicate_unknown_mode_or_raw_list_is_rejected(self):
        for data in (envelope(row(), row()), envelope(row(billing_mode='future')), [row()]):
            with self.assertRaises(pricing.PricingInvalid):
                pricing.normalize_pricing(data)


if __name__ == '__main__':
    unittest.main()
