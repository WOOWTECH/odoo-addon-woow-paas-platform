"""An absolute SQL budget on one owned Odoo cursor, including ORM and flush.

No global cursor class is changed. Callers without a deadline keep the original
transaction behavior. Cleanup statements remain possible after expiration.
"""
import math
import re
import time
from contextlib import contextmanager
from datetime import datetime

_CLEANUP = re.compile(r'^(?:ROLLBACK TO SAVEPOINT|RELEASE SAVEPOINT) "[A-Za-z0-9_-]+"$')
_FINISH_MARGIN = 0.05


class SqlBudgetExpired(TimeoutError):
    """Fixed, credential-free failure; retry from the last durable receipt."""


def _remaining(deadline):
    milliseconds = int((deadline - time.monotonic() - _FINISH_MARGIN) * 1000)
    if milliseconds < 1:
        raise SqlBudgetExpired()
    return milliseconds


@contextmanager
def deadline_cursor(factory, deadline=None, *, lock_seconds=5.0):
    """Own a cursor whose every SQL statement shares the caller's deadline.

    The instance execute/commit hooks stay installed through context exit so
    ORM flush and commit hooks cannot acquire a fresh per-statement budget.
    Rollback/close do not need a live work budget. A committed sink receipt
    remains durable even if a later sink runs out of time.
    """
    if deadline is None:
        with factory() as cr:
            yield cr
        return
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(lock_seconds) not in (int, float)
            or not math.isfinite(lock_seconds) or lock_seconds <= 0):
        raise ValueError('invalid_sql_budget')
    _remaining(deadline)
    cr = factory()
    original_execute, original_commit = cr.execute, cr.commit
    prior = {name: cr.__dict__.get(name)
             for name in ('execute', 'commit', '_llm_sql_deadline')}
    owned = {name: name in cr.__dict__ for name in prior}

    def arm():
        remaining = _remaining(deadline)
        setup = 10
        for _attempt in range(3):
            timeout = remaining - setup
            if timeout < 1:
                raise SqlBudgetExpired()
            original_execute('SET LOCAL lock_timeout = %s',
                             ('%sms' % min(max(1, int(lock_seconds * 1000)), timeout),),
                             log_exceptions=False)
            original_execute('SET LOCAL statement_timeout = %s',
                             ('%sms' % timeout,), log_exceptions=False)
            actual = _remaining(deadline)
            if timeout <= actual:
                return
            setup = max(setup, remaining - actual + 5)
            remaining = actual
        raise SqlBudgetExpired()

    def execute(query, *args, **kwargs):
        if isinstance(query, str) and _CLEANUP.fullmatch(query):
            return original_execute(query, *args, **kwargs)
        arm()
        if len(args) >= 2:
            args = (args[0], False) + args[2:]
            kwargs.pop('log_exceptions', None)
        else:
            kwargs['log_exceptions'] = False
        result = original_execute(query, *args, **kwargs)
        _remaining(deadline)
        return result

    def commit():
        arm()
        return original_commit()

    try:
        cr.execute, cr.commit = execute, commit
        cr._llm_sql_deadline = deadline
        with cr:
            yield cr
            _remaining(deadline)
    finally:
        # Restore exactly the instance state, including TestCursor shims.
        for name, value in prior.items():
            if owned[name]:
                setattr(cr, name, value)
            else:
                cr.__dict__.pop(name, None)


def trigger_cron(cron, at=None):
    """Queue within the bounded transaction without an unbounded notify cursor.

    Odoo's scheduler polls ir.cron.trigger normally. Unbounded callers retain
    native _trigger, including its immediate postcommit notification. Bounded
    calls preserve native active/future semantics, but rely on normal polling.
    """
    if getattr(cron.env.cr, '_llm_sql_deadline', None) is None:
        return cron._trigger(at=at) if at is not None else cron._trigger()
    cron.ensure_one()
    now = datetime.utcnow().replace(microsecond=0)
    at = now if at is None else at
    if not isinstance(at, datetime):
        raise ValueError('invalid_cron_trigger_time')
    Trigger = cron.env['ir.cron.trigger'].sudo()
    if not cron.sudo().active and at <= now:
        return Trigger
    return Trigger.create({'cron_id': cron.id, 'call_at': at})
