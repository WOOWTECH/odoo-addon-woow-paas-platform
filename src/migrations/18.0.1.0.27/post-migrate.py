"""18.0.1.0.27 — Phase C: agent_session webhook-sync fields.

Adds ``last_synced_at`` + ``last_state_event_ts`` to
``woow_paas_platform_agent_session`` (design §5.5 — webhook primary + cron
fallback state sync).

Both columns are additive + NULLABLE with no backfill: existing rows keep
NULL and are picked up by the drift cron on the next pass. The ORM also
auto-creates these columns during the module update; this migration makes the
intent explicit and is idempotent (ADD COLUMN IF NOT EXISTS).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        ALTER TABLE woow_paas_platform_agent_session
            ADD COLUMN IF NOT EXISTS last_synced_at timestamp,
            ADD COLUMN IF NOT EXISTS last_state_event_ts timestamp
        """
    )
    _logger.info(
        "18.0.1.0.27: ensured agent_session.last_synced_at + "
        "last_state_event_ts columns (additive, nullable, no backfill)."
    )
