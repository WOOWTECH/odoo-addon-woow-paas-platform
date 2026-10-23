"""Public pricing DTO, with exact decimal rates and no Odoo/network dependencies.

The upstream envelope is privileged: never cache or publish it verbatim. Only default/free
models and explicit public metadata survive. Rates are decimal *strings* (points per million
tokens, or points per call); quota balances remain Python integers in the accounting layer.
"""
from decimal import Decimal, InvalidOperation, localcontext
import re

POINTS_PER_USD = 32
# Staging source contract: docker.io/calciumion/new-api:v1.0.0-rc.44
# GetCreateCacheRatio in relay/helper/price.go defaults to 1.25; model/pricing.go
# omits create_cache_ratio from GET /api/pricing unless explicitly overridden.
# Keep this fallback scoped to that deployed source version and verify drift in acceptance audit.
CREATE_CACHE_RATIO_DEFAULT = Decimal('1.25')
ALLOWED_GROUPS = frozenset({'default', 'free'})
FREE_MODELS = frozenset({'qwen3-4b-aicoder', 'qwen3-4b-cluster1'})
FREE_LIMITS = {'per_minute': 10, 'per_day': 200, 'reset_timezone': 'Asia/Taipei'}
PRICE_FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'per_call')
_TAGS = {
    'text': 'text', 'chat': 'text', '文本': 'text', '对话': 'text',
    'vision': 'vision', '视觉': 'vision', '視覺': 'vision',
    'reasoning': 'reasoning', '推理': 'reasoning',
    'tools': 'tools', 'function calling': 'tools', '工具': 'tools',
    'embedding': 'embedding', 'embeddings': 'embedding', '嵌入': 'embedding',
    'image': 'image', '图像': 'image', '圖片': 'image',
    'audio': 'audio', '语音': 'audio', '語音': 'audio',
}


class PricingInvalid(ValueError):
    """A fixed error label only: upstream content must not reach logs or alerts."""


def _number(value):
    if type(value) not in (int, float, str, Decimal) or len(str(value)) > 64:
        raise PricingInvalid('pricing_number')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise PricingInvalid('pricing_number') from None
    if (not number.is_finite() or number < 0 or len(number.as_tuple().digits) > 24
            or (number and not -18 <= number.adjusted() <= 12)):
        raise PricingInvalid('pricing_number')
    return number


def _decimal_text(value):
    return format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else str(value)


def _notice_rate(value):
    """Keep source rates inside the downstream notice catalog's decimal bounds."""
    if type(value) is not str or not 0 < len(value) <= 64:
        raise PricingInvalid('notice_source_unknown_rate')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise PricingInvalid('notice_source_unknown_rate') from None
    if (not number.is_finite() or number < 0 or len(number.as_tuple().digits) > 24
            or not -64 <= number.as_tuple().exponent <= 64
            or (number and not -18 <= number.adjusted() <= 12)):
        raise PricingInvalid('notice_source_unknown_rate')
    return value


def model_price(row, group, group_ratio):
    """DESIGN §4.1: ratio, fixed, tiered and free; no float arithmetic or quota rounding."""
    price = dict.fromkeys(PRICE_FIELDS)
    if group == 'free':
        return 'ratio', dict.fromkeys(PRICE_FIELDS, '0')
    if row.get('billing_mode') == 'tiered_expr':
        return 'tiered', None
    if row.get('billing_mode') not in (None, '', 'ratio', 'fixed'):
        raise PricingInvalid('pricing_mode')
    ratio = _number(group_ratio)
    # A local context bounds precision independently from callers' Decimal settings.
    with localcontext() as ctx:
        ctx.prec = 80
        if type(row.get('quota_type')) is not int:
            raise PricingInvalid('pricing_mode')
        if row['quota_type'] == 1:
            price['per_call'] = _decimal_text(_number(row.get('model_price')) * ratio * POINTS_PER_USD)
            return 'fixed', price
        if type(row.get('quota_type')) is not int or row['quota_type'] != 0:
            raise PricingInvalid('pricing_mode')
        input_price = 2 * _number(row.get('model_ratio')) * ratio * POINTS_PER_USD
        price['input'] = _decimal_text(input_price)
        price['output'] = _decimal_text(input_price * _number(row.get('completion_ratio')))
        if row.get('cache_ratio') is not None:
            price['cache_read'] = _decimal_text(input_price * _number(row['cache_ratio']))
        # rc.44's pricing endpoint hides the runtime 1.25 default when there is no
        # per-model override. Preserve an explicit row value, otherwise disclose it.
        create_cache_ratio = row.get('create_cache_ratio')
        if create_cache_ratio is None:
            create_cache_ratio = CREATE_CACHE_RATIO_DEFAULT
        price['cache_write'] = _decimal_text(input_price * _number(create_cache_ratio))
        return 'ratio', price


def project_default_notice_increase(payload, *, source_version):
    """Project a proposed 1→1.3 default tariff, without attesting its source.

    The caller must separately prove that this is a fresh, complete view for
    every affected account and that group overrides, options and the deployed
    source version match. Unknown cache rates and unsupported modes fail closed.
    This pure result is not a publication or a pricing permit.
    """
    if source_version != 'v1.0.0-rc.44':
        raise PricingInvalid('notice_source_version')
    if (type(payload) is not dict or type(payload.get('group_ratio')) is not dict
            or type(payload.get('data')) is not list):
        raise PricingInvalid('notice_source_shape')
    groups, rows = payload['group_ratio'], payload['data']
    extra = set(groups) - ALLOWED_GROUPS
    usable = payload.get('usable_group')
    if (not ALLOWED_GROUPS <= set(groups) or len(extra) > 1
            or any(type(group) is not str
                   or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', group) is None for group in groups)
            or type(usable) is not dict or set(usable) != set(groups)
            or (extra and usable.get(next(iter(extra))) != 'User group')
            or not 0 < len(rows) <= 10000):
        raise PricingInvalid('notice_source_scope')
    own_group = next(iter(extra)) if extra else None
    if own_group is not None:
        _number(groups[own_group])
    allowed_enabled = (['default'], ['free']) + (([own_group],) if own_group is not None else ())
    current, target = _number(groups['default']), Decimal('1.3')
    if current != 1 or _number(groups['free']) != 0:
        raise PricingInvalid('notice_source_ratio')
    before, after, seen = {}, {}, set()
    for row in rows:
        if type(row) is not dict or type(row.get('enable_groups')) is not list:
            raise PricingInvalid('notice_source_shape')
        enabled = row['enable_groups']
        if enabled not in allowed_enabled:
            raise PricingInvalid('notice_source_scope')
        name = row.get('model_name')
        if type(name) is not str or re.fullmatch(r'[A-Za-z0-9_.:/+-]{1,200}', name) is None:
            raise PricingInvalid('notice_source_model')
        if name in seen:
            raise PricingInvalid('notice_source_model')
        seen.add(name)
        if enabled != ['default']:
            continue
        if name in FREE_MODELS or name.endswith(':free') or len(before) >= 100:
            raise PricingInvalid('notice_source_model')
        mode, old = model_price(row, 'default', current)
        new_mode, new = model_price(row, 'default', target)
        if mode != new_mode or mode not in ('ratio', 'fixed'):
            raise PricingInvalid('notice_source_mode')
        if row.get('billing_mode') not in (None, '', mode):
            raise PricingInvalid('notice_source_mode')
        fields = ('per_call',) if mode == 'fixed' else ('input', 'output', 'cache_read', 'cache_write')
        for key in fields:
            _notice_rate(old[key])
            _notice_rate(new[key])
        if Decimal(old['per_call' if mode == 'fixed' else 'input']) <= 0:
            raise PricingInvalid('notice_source_zero_rate')
        before[name] = dict(mode=mode, rates={k: old[k] for k in fields})
        after[name] = dict(mode=mode, rates={k: new[k] for k in fields})
    if not before:
        raise PricingInvalid('notice_source_scope')
    return ({'default': dict(sorted(before.items()))},
            {'default': dict(sorted(after.items()))})


def normalize_pricing(payload):
    """Validate the service account's group boundary *before* looking at any model rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get('group_ratio'), dict):
        raise PricingInvalid('pricing_shape')
    groups = payload['group_ratio']
    if not set(groups) <= ALLOWED_GROUPS:
        raise PricingInvalid('pricing_groups')
    # D5 and D11: accepting a silently changed multiplier would advertise the wrong tariff.
    if 'default' in groups and _number(groups['default']) != Decimal('1.3'):
        raise PricingInvalid('pricing_ratio')
    if 'free' in groups and _number(groups['free']) != 0:
        raise PricingInvalid('pricing_ratio')
    rows = payload.get('data')
    if not isinstance(rows, list):
        raise PricingInvalid('pricing_shape')
    vendors = {}
    vendor_rows = payload.get('vendors') or []
    if not isinstance(vendor_rows, list):
        raise PricingInvalid('pricing_shape')
    for vendor in vendor_rows:
        if isinstance(vendor, dict) and type(vendor.get('id')) is int and isinstance(vendor.get('name'), str):
            vendors[vendor['id']] = vendor['name'][:120]
    items, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('enable_groups'), list):
            raise PricingInvalid('pricing_shape')
        allowed = set(g for g in row['enable_groups'] if isinstance(g, str)) & ALLOWED_GROUPS
        if not allowed:
            continue
        # A model exposed to both key groups cannot have one truthful public price:
        # the same request may be paid or free depending on the caller's key.
        if allowed == ALLOWED_GROUPS:
            raise PricingInvalid('pricing_groups')
        name = row.get('model_name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:/+-]{1,200}', name):
            raise PricingInvalid('pricing_model')
        group = 'free' if name in FREE_MODELS and 'free' in allowed else 'default'
        if group not in allowed or group not in groups:
            raise PricingInvalid('pricing_groups')
        # D11/D20: only the two self-hosted Qwen endpoints are free. Do not silently turn a
        # different free-group endpoint into a paid listing or expose cost-zero paid models.
        if name in FREE_MODELS and group != 'free':
            raise PricingInvalid('pricing_groups')
        if group == 'default' and name.endswith(':free'):
            continue
        mode, price = model_price(row, group, groups[group])
        if group == 'default' and price and price['per_call' if mode == 'fixed' else 'input'] == '0':
            continue
        if name in seen:
            raise PricingInvalid('pricing_duplicate')
        seen.add(name)
        tags = row.get('tags') or []
        if isinstance(tags, str):
            tags = re.split(r'[,，;]', tags)
        if not isinstance(tags, list):
            tags = []
        tags = sorted({_TAGS[tag.strip().lower()] for tag in tags
                       if isinstance(tag, str) and tag.strip().lower() in _TAGS})
        vendor_id = row.get('vendor_id')
        item = {'model': name, 'vendor': vendors.get(vendor_id) if type(vendor_id) is int else None,
                'tags': tags, 'group': group, 'pricing_mode': mode, 'price': price}
        if group == 'free':
            item['free_limits'] = dict(FREE_LIMITS)
        items.append(item)
    return {'items': sorted(items, key=lambda item: (item['group'] != 'free', item['model'])),
            'state': 'ok'}
