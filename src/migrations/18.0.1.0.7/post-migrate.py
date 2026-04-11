import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Remove deprecated monthly_price field."""
    _logger.info("Post-migration 18.0.1.0.7: dropping monthly_price column")

    cr.execute("""
        ALTER TABLE woow_paas_platform_cloud_app_template
        DROP COLUMN IF EXISTS monthly_price;
    """)
