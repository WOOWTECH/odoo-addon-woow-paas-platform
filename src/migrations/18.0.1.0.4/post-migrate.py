"""Backfill reference_id for existing smart_home records."""
import logging
import uuid

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("Backfilling reference_id for smart_home records")

    cr.execute(
        """
        ALTER TABLE woow_paas_platform_smart_home
        ADD COLUMN IF NOT EXISTS reference_id VARCHAR
        """
    )

    cr.execute(
        """
        SELECT id FROM woow_paas_platform_smart_home
        WHERE reference_id IS NULL OR reference_id = ''
        """
    )
    rows = cr.fetchall()

    for (record_id,) in rows:
        cr.execute(
            """
            UPDATE woow_paas_platform_smart_home
            SET reference_id = %s
            WHERE id = %s
            """,
            (str(uuid.uuid4()), record_id),
        )

    _logger.info("Backfilled reference_id for %d smart_home records", len(rows))
