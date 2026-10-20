"""Strict, pure normalization for AP admin-log pages used by LLM reconciliation.

This module does not decide whether a log proves an operation or whether a
query has global coverage. It normalizes rc.44 log envelopes, fully validates
type-1 ``user.quota_add``, and exposes other type-1 actions and type-4 system
rows through fixed safe classifications. The AP query parameter is ``p``,
while rc.44 serializes the response field as ``page``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from . import llm_redact

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
MAX_OTHER_BYTES = 65536
MAX_PAGE_SIZE = 100
_REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_SAFE_ACTION = re.compile(r'[a-z][a-z0-9_.]{0,63}\Z')


class EvidenceError(ValueError):
    """Fixed reason only; upstream values never appear in error messages."""


def _integer(value, *, minimum=INT64_MIN):
    if type(value) is not int or not minimum <= value <= INT64_MAX:
        raise EvidenceError('invalid_reconcile_log')
    return value


def _request_id(value):
    if (not isinstance(value, str) or not _REQUEST_ID.fullmatch(value)
            or llm_redact.redact(value) != value):
        raise EvidenceError('invalid_reconcile_log')
    return value


def _object_no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError('invalid_reconcile_log')
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise EvidenceError('invalid_reconcile_log')


def _other_object(value):
    if isinstance(value, str):
        try:
            if len(value.encode('utf-8')) > MAX_OTHER_BYTES:
                raise EvidenceError('invalid_reconcile_log')
            value = json.loads(value, object_pairs_hook=_object_no_duplicate_keys,
                               parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise EvidenceError('invalid_reconcile_log') from None
    if not isinstance(value, dict):
        raise EvidenceError('invalid_reconcile_log')
    return value


@dataclass(frozen=True)
class NormalizedLog:
    log_id: int
    log_type: int
    user_id: int
    created_at: int
    request_id: str
    classification: str
    action: str | None = None
    admin_id: int | None = None
    target_user_id: int | None = None
    requested_quota: int | None = None
    quota_from: int | None = None
    quota_to: int | None = None


@dataclass(frozen=True)
class NormalizedPage:
    page: int
    page_size: int
    total: int
    items: tuple[NormalizedLog, ...]
    has_more: bool


def _normalize_log(row):
    if not isinstance(row, dict):
        raise EvidenceError('invalid_reconcile_log')
    log_id = _integer(row.get('id'), minimum=1)
    log_type = _integer(row.get('type'), minimum=0)
    user_id = _integer(row.get('user_id'), minimum=1)
    created_at = _integer(row.get('created_at'), minimum=0)
    request_id = _request_id(row.get('request_id'))

    if log_type == 4:
        # rc.44 LogTypeSystem; check-in is one producer, not the only one.
        return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                             'type4_system')
    if log_type != 1:
        return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                             'unsupported_log_type')

    raw_other = row.get('other')
    if raw_other is None or raw_other == '':
        return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                             'unsupported_type1_operation')
    other = _other_object(raw_other)
    op_present, admin_present = 'op' in other, 'admin_info' in other
    op, admin = other.get('op'), other.get('admin_info')
    if (op_present and not isinstance(op, dict)) or (admin_present and not isinstance(admin, dict)):
        raise EvidenceError('invalid_reconcile_log')
    admin_id = _integer(admin.get('admin_id'), minimum=1) if admin_present else None
    if op is None:
        # rc.44 also writes ordinary top-up logs for redemption/payment flows;
        # those carry content_parts but no admin operation envelope.
        return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                             'unsupported_type1_operation', admin_id=admin_id)
    action = op.get('action')
    if not isinstance(action, str) or len(action) > 64 or not _SAFE_ACTION.fullmatch(action):
        raise EvidenceError('invalid_reconcile_log')
    if action != 'user.quota_add':
        # Unknown operation names are not returned; only a fixed classification
        # and typed admin/log envelope leave this boundary.
        return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                             'unsupported_type1_operation', admin_id=admin_id)

    if admin_id is None:
        raise EvidenceError('invalid_reconcile_log')
    params = op.get('params')
    if not isinstance(params, dict) or params.get('mode') != 'add':
        raise EvidenceError('invalid_reconcile_log')
    target = _integer(params.get('target_user_id'), minimum=1)
    amount = _integer(params.get('requested_quota'), minimum=1)
    logged_quota = _integer(params.get('quota'), minimum=1)
    before = _integer(params.get('from'))
    after = _integer(params.get('to'))
    if (user_id != target or logged_quota != amount or after - before != amount):
        raise EvidenceError('invalid_reconcile_log')
    return NormalizedLog(log_id, log_type, user_id, created_at, request_id,
                         'type1_quota_add', action='user.quota_add', admin_id=admin_id,
                         target_user_id=target, requested_quota=amount,
                         quota_from=before, quota_to=after)


def normalize_page(data, *, requested_page, requested_page_size):
    """Normalize one complete AP PageInfo (``page/page_size/total/items``).

    The request query uses ``p``; rc.44 ``common.PageInfo`` echoes it as
    ``page``. The page must echo the requested page and size, and contain
    exactly the number of rows implied by ``total`` for that offset. This
    catches truncated and extra-row payloads. It makes no claim that ``total``
    is stable across page requests or that ordering prevents rows moving.
    """
    page = _integer(requested_page, minimum=1)
    page_size = _integer(requested_page_size, minimum=1)
    if page > 999999 or page_size > MAX_PAGE_SIZE or not isinstance(data, dict):
        raise EvidenceError('invalid_reconcile_page')
    response_page = _integer(data.get('page'), minimum=1)
    response_size = _integer(data.get('page_size'), minimum=1)
    total = _integer(data.get('total'), minimum=0)
    items = data.get('items')
    if (response_page != page or response_size != page_size or not isinstance(items, list)):
        raise EvidenceError('invalid_reconcile_page')
    offset = (page - 1) * page_size
    expected = min(page_size, max(total - offset, 0))
    if len(items) != expected:
        raise EvidenceError('invalid_reconcile_page')
    normalized = tuple(_normalize_log(row) for row in items)
    return NormalizedPage(page, page_size, total, normalized, offset + len(items) < total)


def duplicate_log_ids(pages: Iterable[NormalizedPage]) -> frozenset[int]:
    """Return normalized IDs repeated within/across pages; never infers coverage."""
    seen, duplicates = set(), set()
    for page in pages:
        for item in page.items:
            if item.log_id in seen:
                duplicates.add(item.log_id)
            seen.add(item.log_id)
    return frozenset(duplicates)
