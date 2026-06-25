import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Add skill.scope and backfill existing rows to 'user' BEFORE the ORM
    registers the required (NOT NULL) field — otherwise the upgrade aborts on
    pre-existing rows with NULL scope. Every live skill today is project-scoped,
    so 'user' is the correct backfill."""
    cr.execute("""
        ALTER TABLE woow_paas_platform_skill
        ADD COLUMN IF NOT EXISTS scope VARCHAR
    """)
    cr.execute("""
        UPDATE woow_paas_platform_skill
        SET scope = 'user'
        WHERE scope IS NULL
    """)
    _logger.info("skill.scope added + backfilled 'user' (%s rows)", cr.rowcount)
