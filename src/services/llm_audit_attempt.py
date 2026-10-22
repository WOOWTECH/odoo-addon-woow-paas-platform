"""Dormant I3 logical-attempt evidence from #1900's normalized proxy events.

One intent plus at most one outcome is one attempt, not two quota credits.
No return value proves a new-api credit, identifies an Odoo order, establishes
the proxy store generation, or establishes complete audit coverage. In
particular a forwarded HTTP response is only a correlation candidate; an
independent new-api type=1 log and a unique order claim remain necessary.

This module has no Odoo, transport, persistence, alert, or scheduler entry.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid


_FIELDS = frozenset(('seq', 'attempt_id', 'kind', 'at', 'target_user_id',
                     'quota', 'result', 'status', 'request_id'))
_RESULTS = frozenset(('forwarded', 'connect', 'reset', 'timeout', 'cancelled', 'not_sent'))
_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z', re.ASCII)
_CREDENTIAL = re.compile(
    r'nap_|sk-|bearer|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[0-9a-f]{64}',
    re.I | re.ASCII)
_MAX_SEQ = 2 ** 63 - 1


class AuditAttemptError(ValueError):
    """Fixed, non-secret error for malformed or contradictory evidence."""

    def __init__(self):
        super().__init__('invalid_audit_attempt')


@dataclass(frozen=True)
class AuditAttempt:
    attempt_id: str
    target_user_id: int
    quota: int
    intent_seq: int
    outcome_seq: int | None
    outcome_result: str | None
    upstream_status: int | None
    request_id: str | None
    classification: str


def _require(value):
    if not value:
        raise AuditAttemptError()


def _number(value, low, high):
    return type(value) is int and low <= value <= high


def _event(row, kind):
    _require(type(row) is dict and set(row) == _FIELDS and row['kind'] == kind)
    _require(_number(row['seq'], 1, _MAX_SEQ))
    attempt = row['attempt_id']
    try:
        parsed = uuid.UUID(attempt) if type(attempt) is str and len(attempt) == 36 else None
    except ValueError:
        parsed = None
    _require(parsed is not None and parsed.version == 4 and str(parsed) == attempt)
    stamp = row['at']
    _require(type(stamp) is str and len(stamp) == 27 and stamp.endswith('Z'))
    try:
        parsed_stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        raise AuditAttemptError() from None
    _require(parsed_stamp.tzinfo == timezone.utc and
             parsed_stamp.isoformat(timespec='microseconds').replace('+00:00', 'Z') == stamp)
    if kind == 'intent':
        _require(_number(row['target_user_id'], 1, 2147483647) and
                 _number(row['quota'], 1, 156250000) and
                 row['result'] is False and type(row['status']) is int and
                 row['status'] == 0 and row['request_id'] is False)
        return
    _require(type(row['target_user_id']) is int and row['target_user_id'] == 0 and
             type(row['quota']) is int and row['quota'] == 0 and
             type(row['result']) is str and row['result'] in _RESULTS)
    if row['result'] == 'forwarded':
        _require(_number(row['status'], 100, 599))
        request_id = row['request_id']
        _require(request_id is False or
                 (type(request_id) is str and _REQUEST.fullmatch(request_id) and
                  not _CREDENTIAL.search(request_id)))
    else:
        _require(type(row['status']) is int and row['status'] == 0 and
                 row['request_id'] is False)


def collapse_attempt(intent, outcome=None):
    """Conservatively describe one proxy attempt; never claim quota or order.

    The two rows may cross page boundaries. Absence of an outcome means
    uncertainty, even after a snapshot is complete: the proxy might have died
    after sending the request but before writing its outcome.
    """
    _event(intent, 'intent')
    if outcome is not None:
        _event(outcome, 'outcome')
        _require(outcome['attempt_id'] == intent['attempt_id'] and
                 outcome['seq'] > intent['seq'])
    result = outcome['result'] if outcome else None
    request_id = outcome['request_id'] or None if outcome else None
    if result == 'not_sent':
        classification = 'not_sent'
    elif result == 'forwarded' and request_id:
        classification = 'correlation_candidate'
    else:
        classification = 'uncertain'
    return AuditAttempt(
        attempt_id=intent['attempt_id'], target_user_id=intent['target_user_id'],
        quota=intent['quota'], intent_seq=intent['seq'],
        outcome_seq=outcome['seq'] if outcome else None,
        outcome_result=result, upstream_status=outcome['status'] if outcome else None,
        request_id=request_id, classification=classification)
