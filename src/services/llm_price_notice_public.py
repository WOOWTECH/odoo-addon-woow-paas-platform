"""Strict, side-effect-free projection of committed price notices for the AI page.

This is a display contract, not approval, publication, SMTP or pricing evidence.
The source must be a separately committed publication row, never a draft row.
"""
from decimal import Decimal, InvalidOperation
import re


_ID = re.compile(r'pc_[a-f0-9]{32}\Z')
_MODEL = re.compile(r'[A-Za-z0-9_.:/+-]{1,200}\Z')
_RATE = re.compile(r'[0-9]+(?:\.[0-9]+)?\Z')
_FIELDS = {
    'ratio': ('input', 'output', 'cache_read', 'cache_write'),
    'fixed': ('per_call',),
}
_PUBLIC_KEYS = frozenset({
    'schema', 'id', 'version', 'title', 'body_text', 'not_before',
    'timezone', 'unit', 'changes',
})


def _text(value, maximum):
    return type(value) is str and 0 < len(value) <= maximum


def _rate(value):
    if not (_text(value, 64) and _RATE.fullmatch(value)):
        raise ValueError('invalid_notice_rate')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('invalid_notice_rate') from None
    if (not number.is_finite() or len(number.as_tuple().digits) > 24
            or (number and number.adjusted() > 12)):
        raise ValueError('invalid_notice_rate')
    return value


def _one(row):
    schema = row.get('schema') if type(row) is dict else None
    if (type(row) is not dict or not set(row) <= _PUBLIC_KEYS
            or (schema is not None and (type(schema) is not int or schema != 1))
            or row.get('timezone') not in (None, 'Asia/Taipei')
            or row.get('unit') not in (None, 'points_per_million_tokens_or_per_call')
            or not _text(row.get('id'), 120)
            or not _ID.fullmatch(row['id'])
            or not _text(row.get('title'), 256)
            or not _text(row.get('body_text'), 65536)
            or type(row.get('not_before')) is not int
            or not 0 < row['not_before'] <= 4102444800):
        raise ValueError('invalid_public_notice')
    changes = row.get('changes')
    if type(changes) is not list or not 0 < len(changes) <= 100:
        raise ValueError('invalid_public_notice')
    public_changes = []
    for change in changes:
        if (type(change) is not dict or set(change) != {'model', 'mode', 'before', 'after'}
                or type(change.get('model')) is not str
                or not _MODEL.fullmatch(change['model'])
                or change.get('mode') not in _FIELDS):
            raise ValueError('invalid_public_notice')
        fields = _FIELDS[change['mode']]
        before, after = change.get('before'), change.get('after')
        if (type(before) is not dict or type(after) is not dict
                or set(before) != set(fields) or set(after) != set(fields)):
            raise ValueError('invalid_public_notice')
        public_changes.append({
            'model': change['model'], 'mode': change['mode'],
            'before': {field: _rate(before.get(field)) for field in fields},
            'after': {field: _rate(after.get(field)) for field in fields},
        })
    result = {key: row[key] for key in ('id', 'title', 'body_text', 'not_before')}
    result['changes'] = public_changes
    version = row.get('version')
    if version is not None:
        if not _text(version, 120):
            raise ValueError('invalid_public_notice')
        result['version'] = version
    return result


def project_public_notices(rows):
    """Return only canonical public fields, or no notices on malformed storage."""
    if type(rows) is not list or len(rows) > 10:
        return []
    try:
        projected = [_one(row) for row in rows]
        if len({item['id'] for item in projected}) != len(projected):
            return []
        return projected
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return []
