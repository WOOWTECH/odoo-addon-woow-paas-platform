"""Offline consistency checks for synthetic New API pricing and option snapshots.

This module performs no I/O. Its result is explicitly provisional: the inputs
are caller supplied and do not establish freshness, completeness, or authority.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

try:
    from . import llm_pricing
except ImportError:  # Allow direct offline loading without importing the Odoo addon package.
    _spec = importlib.util.spec_from_file_location(
        'llm_pricing', Path(__file__).with_name('llm_pricing.py'))
    llm_pricing = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(llm_pricing)


_REQUIRED_OPTIONS = frozenset({
    'GroupGroupRatio', 'ModelPrice', 'billing_setting.billing_mode', 'CompletionRatioMeta',
})
_RATIO_OPTIONS = frozenset({'ModelRatio', 'CompletionRatio', 'CacheRatio'})


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise llm_pricing.PricingInvalid('notice_source_duplicate_option')
        result[key] = value
    return result


def _option_object(options, key):
    raw = options[key]
    if type(raw) is not str:
        raise llm_pricing.PricingInvalid('notice_source_option_json')
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicates,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise llm_pricing.PricingInvalid('notice_source_option_json') from None
    if type(parsed) is not dict:
        raise llm_pricing.PricingInvalid('notice_source_option_json')
    return parsed


def _same_number(left, right):
    try:
        return llm_pricing._number(left) == llm_pricing._number(right)
    except llm_pricing.PricingInvalid:
        return False


def _check_map(options, key, field_by_model):
    values = _option_object(options, key)
    for model, expected in field_by_model.items():
        if model not in values or not _same_number(values[model], expected):
            raise llm_pricing.PricingInvalid('notice_source_option_mismatch')
    return values


def project_notice_source(pricing_payload, option_rows, *, source_version):
    """Validate caller supplied snapshots, then return a provisional projection."""
    if source_version != 'v1.0.0-rc.44':
        raise llm_pricing.PricingInvalid('notice_source_version')
    if type(pricing_payload) is not dict or type(option_rows) is not list:
        raise llm_pricing.PricingInvalid('notice_source_shape')

    options = {}
    for item in option_rows:
        if (type(item) is not dict or set(item) != {'key', 'value'}
                or type(item['key']) is not str or not item['key']):
            raise llm_pricing.PricingInvalid('notice_source_option_shape')
        if item['key'] in options:
            raise llm_pricing.PricingInvalid('notice_source_duplicate_option')
        options[item['key']] = item['value']
    if not _REQUIRED_OPTIONS <= options.keys():
        raise llm_pricing.PricingInvalid('notice_source_missing_option')

    # rc.44 pricing uses GroupGroupRatio at runtime. Any nonempty override makes
    # the public group ratio insufficient to describe tenant-effective prices.
    if _option_object(options, 'GroupGroupRatio'):
        raise llm_pricing.PricingInvalid('notice_source_group_group_ratio')

    rows = pricing_payload.get('data')
    if type(rows) is not list:
        raise llm_pricing.PricingInvalid('notice_source_shape')
    paid = [row for row in rows if type(row) is dict and row.get('enable_groups') == ['default']]
    ratio_rows = [row for row in paid if type(row.get('quota_type')) is int
                  and row['quota_type'] == 0]
    fixed_rows = [row for row in paid if type(row.get('quota_type')) is int
                  and row['quota_type'] == 1]
    if len(ratio_rows) + len(fixed_rows) != len(paid):
        raise llm_pricing.PricingInvalid('notice_source_mode')
    if ratio_rows and not _RATIO_OPTIONS <= options.keys():
        raise llm_pricing.PricingInvalid('notice_source_missing_option')
    ratio_names = {row.get('model_name') for row in ratio_rows}
    if ratio_rows:
        _check_map(options, 'ModelRatio',
                   {r.get('model_name'): r.get('model_ratio') for r in ratio_rows})
        completion_values = _check_map(
            options, 'CompletionRatio',
            {r.get('model_name'): r.get('completion_ratio') for r in ratio_rows})
        cache_values = _check_map(
            options, 'CacheRatio',
            {r.get('model_name'): r.get('cache_ratio') for r in ratio_rows
             if r.get('cache_ratio') is not None})
    else:
        completion_values, cache_values = {}, {}
    prices = _option_object(options, 'ModelPrice')
    modes = _option_object(options, 'billing_setting.billing_mode')
    metadata = _option_object(options, 'CompletionRatioMeta')
    if ratio_names & prices.keys():
        raise llm_pricing.PricingInvalid('notice_source_fixed_price')
    for row in paid:
        name = row.get('model_name')
        fixed = type(row.get('quota_type')) is int and row['quota_type'] == 1
        if fixed and (name not in prices or not _same_number(prices[name], row.get('model_price'))):
            raise llm_pricing.PricingInvalid('notice_source_fixed_price')
        if (modes.get(name, 'ratio') not in ('ratio', 'fixed')
                or row.get('billing_mode') not in (None, '', 'ratio', 'fixed')
                or (not fixed and (modes.get(name, 'ratio') != 'ratio'
                                   or row.get('billing_mode') == 'fixed'))):
            raise llm_pricing.PricingInvalid('notice_source_mode')
        if fixed:
            continue
        meta = metadata.get(name)
        if meta is not None and (type(meta) is not dict or 'ratio' not in meta
                                 or not _same_number(meta['ratio'], completion_values[name])):
            raise llm_pricing.PricingInvalid('notice_source_completion_meta')
        if row.get('cache_ratio') is None or name not in cache_values:
            raise llm_pricing.PricingInvalid('notice_source_cache')

    before, after = llm_pricing.project_default_notice_increase(
        pricing_payload, source_version=source_version)
    try:
        canonical = json.dumps(
            {'pricing': pricing_payload, 'options': option_rows, 'source_version': source_version},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise llm_pricing.PricingInvalid('notice_source_digest') from None
    return {
        'status': 'provisional_untrusted',
        'authority': 'untrusted_synthetic_input',
        'before': before,
        'after': after,
        'digest': hashlib.sha256(canonical).hexdigest(),
    }
