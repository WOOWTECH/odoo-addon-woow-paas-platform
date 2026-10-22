"""Pure decisions for the inactive status monitor; no I/O or shared guard writes.

The DB adapter supplies UTC wall time and current environment fingerprint.
A receipt is trusted for at most 360s from request start, including transport.
Once armed by a real cron invocation, disabling that cron never disables this
freshness guard. Unarmed installations retain their existing behavior.
"""
from datetime import datetime

INTERVAL_SECONDS = 300
LEASE_SECONDS = 30
FRESH_SECONDS = 360


def guard_reason(row, now, fingerprint):
    if not row.get('status_armed'):
        return None
    if row.get('status_fingerprint') != fingerprint:
        return 'llm_status_config_changed'
    if row.get('status_result') != 'ok':
        return 'llm_status_invalid'
    start, end = row.get('status_started_at'), row.get('status_completed_at')
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or not 0 <= (now - start).total_seconds() <= FRESH_SECONDS
            or not 0 <= (end - start).total_seconds() < LEASE_SECONDS
            or end > now):
        return 'llm_status_stale'
    return None


def effective_guard(row, now, fingerprint):
    result = dict(row)
    reason = guard_reason(row, now, fingerprint)
    if reason:
        result['guard_tripped'] = True
        # Preserve opaque existing reasons; never persist this overlay back.
        prior = row.get('guard_reasons') or ''
        result['guard_reasons'] = prior + (',' if prior else '') + reason
        result['guard_since'] = row.get('guard_since') or now
    return result


def due(row, now):
    start = row.get('status_started_at')
    if not start:
        return True
    elapsed = (now - start).total_seconds()
    interval = LEASE_SECONDS if row.get('status_result') == 'pending' else INTERVAL_SECONDS
    return elapsed < 0 or elapsed >= interval


def owns(row, generation, now):
    start = row.get('status_started_at')
    return (row.get('status_generation') == generation
            and row.get('status_result') == 'pending' and bool(start)
            and 0 <= (now - start).total_seconds() < LEASE_SECONDS)
