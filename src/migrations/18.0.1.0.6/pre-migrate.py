"""Clear noupdate flag for Odoo template so noupdate=0 XML data applies."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("Clearing noupdate flag for cloud_app_odoo to allow XML data update")

    cr.execute(
        """
        UPDATE ir_model_data
        SET noupdate = false
        WHERE module = 'woow_paas_platform'
          AND name = 'cloud_app_odoo'
          AND noupdate = true
        """
    )
    if cr.rowcount:
        _logger.info("Cleared noupdate flag for cloud_app_odoo (%d rows)", cr.rowcount)
    else:
        _logger.info("cloud_app_odoo already has noupdate=false, no change needed")
