"""Strict proxy-control audit v1 pages. No ORM, transport or I3 inference."""
from datetime import datetime, timezone
import re
import uuid

MAX_SEQ = 2 ** 63 - 1
ENV = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z', re.ASCII)
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z', re.ASCII)
REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z', re.ASCII)
CREDENTIAL = re.compile(
    r'nap_|sk-|bearer|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[0-9a-f]{64}', re.I | re.ASCII)
RESULTS = frozenset(('forwarded', 'connect', 'reset', 'timeout', 'cancelled', 'not_sent'))


class AuditPageError(ValueError):
    def __init__(self):
        super().__init__('invalid_audit_page')


def require(value):
    if not value:
        raise AuditPageError()


def integer(value, low=0, high=MAX_SEQ):
    return type(value) is int and low <= value <= high


def environment(value):
    require(type(value) is str and ENV.fullmatch(value))
    return value


def normalize_page(data, expected_env, after, through=None, limit=100):
    """Return (fixed highwater, normalized events). Unknown/partial != empty.

    at is retained as canonical UTC text (including microseconds), not an ordering/coverage
    watermark. No raw JSON/credential-shaped identifiers are persisted.
    """
    environment(expected_env)
    require(integer(after) and (through is None or integer(through, after)))
    require(integer(limit, 1, 100))
    require(type(data) is dict and set(data) == {'env', 'highwater', 'events'})
    require(data['env'] == expected_env and integer(data['highwater'], after))
    head = data['highwater']
    require(through is None or head == through)
    events = data['events']
    require(type(events) is list and len(events) == min(limit, head - after))
    normalized, seen = [], set()
    for offset, row in enumerate(events, 1):
        require(type(row) is dict)
        require(integer(row.get('seq'), 1) and row['seq'] == after + offset)
        attempt = row.get('attempt_id')
        try:
            parsed = uuid.UUID(attempt) if type(attempt) is str and len(attempt) == 36 else None
        except ValueError:
            parsed = None
        require(parsed is not None and parsed.version == 4 and str(parsed) == attempt)
        kind = row.get('kind')
        require(type(kind) is str and kind in ('intent', 'outcome'))
        require((attempt, kind) not in seen)
        seen.add((attempt, kind))
        at = row.get('at')
        require(type(at) is str and len(at) <= 40 and STAMP.fullmatch(at))
        try:
            moment = datetime.fromisoformat(at.replace('Z', '+00:00'))
            require(moment.tzinfo is not None)
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError):
            raise AuditPageError() from None
        values = {'seq': row['seq'], 'attempt_id': attempt, 'kind': kind, 'at': moment.isoformat(timespec='microseconds') + 'Z',
                  'target_user_id': 0, 'quota': 0, 'result': False, 'status': 0, 'request_id': False}
        common = {'seq', 'attempt_id', 'kind', 'at'}
        if kind == 'intent':
            require(set(row) == common | {'target_user_id', 'quota'})
            require(integer(row['target_user_id'], 1, 2147483647)
                    and integer(row['quota'], 1, 156250000))
            values.update(target_user_id=row['target_user_id'], quota=row['quota'])
        else:
            require(set(row) <= common | {'result', 'status', 'request_id'})
            result = row.get('result')
            require(type(result) is str and result in RESULTS)
            if result == 'forwarded':
                require(integer(row.get('status'), 100, 599))
                request = row.get('request_id')
                require(request is None or (type(request) is str and REQUEST.fullmatch(request)
                                           and not CREDENTIAL.search(request)))
                values.update(status=row['status'], request_id=request or False)
            else:
                require('status' not in row and 'request_id' not in row)
            values['result'] = result
        normalized.append(values)
    return head, normalized
