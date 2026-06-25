import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Add subscription-related columns before module update."""
    _logger.info("Pre-migration 18.0.1.0.7: adding subscription columns")

    cr.execute("""
        ALTER TABLE woow_paas_platform_cloud_service
        ADD COLUMN IF NOT EXISTS subscription_id INTEGER;
    """)
    cr.execute("""
        ALTER TABLE woow_paas_platform_cloud_service
        ADD COLUMN IF NOT EXISTS pre_suspend_state VARCHAR;
    """)
    cr.execute("""
        ALTER TABLE woow_paas_platform_smart_home
        ADD COLUMN IF NOT EXISTS subscription_id INTEGER;
    """)
    cr.execute("""
        ALTER TABLE woow_paas_platform_smart_home
        ADD COLUMN IF NOT EXISTS pre_suspend_state VARCHAR;
    """)
    cr.execute("""
        ALTER TABLE woow_paas_platform_smart_home
        ADD COLUMN IF NOT EXISTS original_service_url VARCHAR;
    """)
    # Many2many relation table for template ↔ plan is auto-created by ORM
