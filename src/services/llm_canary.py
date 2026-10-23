"""Canary work identity and scheduling decisions; no Odoo or external I/O."""
from dataclasses import dataclass, field
from datetime import datetime

INTERVAL_SECONDS = 1800
LEASE_SECONDS = 45
KEY_LIFETIME_SECONDS = 600
_AUTHORITY = object()
RESULTS = frozenset({'ok', 'create_failed', 'probe_failed', 'interrupted',
                     'cleanup_pending', 'config_changed', 'account_unavailable'})


@dataclass(frozen=True)
class CanaryClaim:
    generation: int
    request_id: str
    account_id: int
    partner_id: int
    fingerprint: str
    _authority: object = field(default=None, repr=False, compare=False)


def issue_claim(generation, work):
    # Only trusted Python code can supply object identity. RPC JSON and sudo
    # alone cannot confer this capability; DB fencing remains mandatory too.
    return CanaryClaim(generation, work['request_id'], work['account_id'],
                       work['partner_id'], work['fingerprint'], _AUTHORITY)


def authorized(claim):
    return type(claim) is CanaryClaim and claim._authority is _AUTHORITY


def due(started, now):
    return not started or (now - started).total_seconds() >= INTERVAL_SECONDS


def owns(row, claim, now):
    work = row.get('canary_work') or {}
    until = row.get('canary_lease_until')
    return (authorized(claim) and isinstance(until, datetime) and now < until
            and row.get('canary_generation') == claim.generation
            and work.get('request_id') == claim.request_id
            and work.get('account_id') == claim.account_id
            and work.get('partner_id') == claim.partner_id
            and work.get('fingerprint') == claim.fingerprint)


def outcome(row, result, now, *, completed):
    """One failed health sample per 30-minute window, even during cleanup retries."""
    if result not in RESULTS:
        raise ValueError('invalid_canary_result')
    last = row.get('last_canary_at')
    started = row.get('canary_started_at')
    fresh = not last or (started and last < started) or due(last, now)
    if not completed and not fresh:
        return None
    failures = row.get('canary_failures') or 0
    if result == 'ok':
        failures = 0
    elif fresh:
        failures += 1
    return {'result': result, 'failures': failures, 'alert': failures >= 2,
            'sample_at': now if fresh or result == 'ok' else last}
