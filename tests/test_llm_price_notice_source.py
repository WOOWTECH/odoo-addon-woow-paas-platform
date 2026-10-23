"""Offline source consistency checks; inputs are synthetic and unauthoritative."""
import importlib.util
import json
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'llm_pricing_source', Path(__file__).resolve().parents[1]
    / 'src/services/llm_pricing_source.py')
source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source)


def pricing_row(**changes):
    return {
        'model_name': 'provider/model', 'quota_type': 0, 'model_ratio': '1',
        'completion_ratio': '4', 'cache_ratio': '0.1', 'create_cache_ratio': '1.25',
        'enable_groups': ['default'],
        **changes,
    }


def pricing_payload(*rows):
    return {
        'data': list(rows or (pricing_row(),)),
        'group_ratio': {'default': 1, 'free': 0},
        'usable_group': {'default': '預設', 'free': '免費'},
    }


def option_rows(**changes):
    values = {
        'GroupGroupRatio': {},
        'ModelRatio': {'provider/model': 1},
        'CompletionRatio': {'provider/model': 4},
        'CacheRatio': {'provider/model': 0.1},
        'ModelPrice': {},
        'billing_setting.billing_mode': {},
        'CompletionRatioMeta': {'provider/model': {'ratio': 4}},
    }
    values.update(changes)
    return [{'key': key, 'value': json.dumps(value)} for key, value in values.items()]


def raw_option(key, value):
    options = option_rows()
    next(item for item in options if item['key'] == key)['value'] = value
    return options


def project(payload=None, options=None, version='v1.0.0-rc.44'):
    return source.project_notice_source(payload or pricing_payload(),
                                        option_rows() if options is None else options,
                                        source_version=version)


class TestPricingSourceProof(unittest.TestCase):
    def test_projection_is_explicitly_untrusted_and_has_stable_digest(self):
        first = project()
        second = project()
        self.assertEqual(first['status'], 'provisional_untrusted')
        self.assertEqual(first['authority'], 'untrusted_synthetic_input')
        self.assertEqual(first['before']['default']['provider/model']['rates']['input'], '64')
        self.assertEqual(first['after']['default']['provider/model']['rates']['input'], '83.2')
        self.assertEqual(first['digest'], second['digest'])

    def test_stale_group_ratio_options_are_ignored(self):
        options = option_rows()
        options.extend([
            {'key': 'group_ratio_setting.group_ratio', 'value': '{"default":99}'},
            {'key': 'group_ratio_setting.group_group_ratio', 'value': '{"default":{"vip":5}}'},
        ])
        self.assertEqual(project(options=options)['status'], 'provisional_untrusted')

    def test_duplicate_option_key_is_rejected(self):
        options = option_rows() + [{'key': 'ModelRatio', 'value': '{"provider/model":1}'}]
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_missing_required_option_is_rejected(self):
        options = [item for item in option_rows() if item['key'] != 'CacheRatio']
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_malformed_option_json_is_rejected(self):
        options = raw_option('ModelRatio', '{"provider/model":1,}')
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_duplicate_member_inside_json_is_rejected(self):
        options = raw_option('ModelRatio', '{"provider/model":{"ratio":1,"ratio":1}}')
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_nonempty_group_group_ratio_is_rejected(self):
        options = option_rows(GroupGroupRatio={'default': {'vip': 1}})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_model_ratio_option_mismatch_is_rejected(self):
        options = option_rows(ModelRatio={'provider/model': 2})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_completion_ratio_option_mismatch_is_rejected(self):
        options = option_rows(CompletionRatio={'provider/model': 3})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_cache_ratio_option_mismatch_is_rejected(self):
        options = option_rows(CacheRatio={'provider/model': 0.2})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_fixed_model_price_override_is_rejected(self):
        options = option_rows(ModelPrice={'provider/model': 0.5})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_fixed_mode_matches_model_price_option(self):
        fixed_row = pricing_row(quota_type=1, model_price='0.003', billing_mode='fixed')
        options = option_rows(ModelPrice={'provider/model': '0.003'})
        options = [item for item in options if item['key'] not in {
            'ModelRatio', 'CompletionRatio', 'CacheRatio',
        }]
        result = project(payload=pricing_payload(fixed_row), options=options)
        self.assertEqual(result['before']['default']['provider/model'], {
            'mode': 'fixed', 'rates': {'per_call': '0.096'},
        })
        self.assertEqual(result['after']['default']['provider/model']['rates']['per_call'], '0.1248')

    def test_fixed_mode_missing_or_different_model_price_is_rejected(self):
        fixed_row = pricing_row(quota_type=1, model_price='0.003', billing_mode='fixed')
        for model_prices in ({}, {'provider/model': '0.004'}):
            with self.subTest(model_prices=model_prices):
                options = option_rows(ModelPrice=model_prices)
                options = [item for item in options if item['key'] not in {
                    'ModelRatio', 'CompletionRatio', 'CacheRatio',
                }]
                with self.assertRaises(source.llm_pricing.PricingInvalid):
                    project(payload=pricing_payload(fixed_row), options=options)

    def test_tiered_option_and_row_modes_are_rejected(self):
        options = option_rows(**{'billing_setting.billing_mode': {'provider/model': 'tiered_expr'}})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(payload=pricing_payload(pricing_row(billing_mode='tiered_expr')))

    def test_completion_ratio_meta_mismatch_is_rejected(self):
        options = option_rows(CompletionRatioMeta={'provider/model': {'ratio': 5}})
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(options=options)

    def test_unlocked_model_may_be_absent_from_completion_ratio_meta(self):
        options = option_rows(CompletionRatioMeta={})
        self.assertEqual(project(options=options)['status'], 'provisional_untrusted')

    def test_wrong_source_version_is_rejected(self):
        with self.assertRaises(source.llm_pricing.PricingInvalid):
            project(version='unknown')

    def test_non_json_canonical_input_fails_closed(self):
        payload = pricing_payload()
        payload['extra'] = object()
        with self.assertRaisesRegex(source.llm_pricing.PricingInvalid, 'notice_source_digest'):
            project(payload=payload)


if __name__ == '__main__':
    unittest.main()
