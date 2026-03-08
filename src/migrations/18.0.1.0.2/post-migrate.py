"""Fix Odoo template PostgreSQL image: bitnami/postgresql → bitnamilegacy/postgresql.

The bitnami/postgresql:17.7.0 image is no longer available (ImagePullBackOff).
Update to bitnamilegacy/postgresql:17.6.0-debian-12-r4 which is the legacy
registry equivalent.
"""
import json
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("Fixing Odoo template PostgreSQL image to bitnamilegacy")

    cr.execute(
        """
        SELECT id, helm_default_values
        FROM woow_paas_platform_cloud_app_template
        WHERE slug = 'odoo'
        """,
    )
    row = cr.fetchone()
    if not row:
        _logger.warning("Odoo template not found, skipping")
        return

    template_id, raw_values = row
    try:
        values = json.loads(raw_values)
    except (json.JSONDecodeError, TypeError):
        _logger.warning("Invalid helm_default_values for Odoo template, skipping")
        return

    pg_image = values.get("postgresql", {}).get("image", {})
    old_repo = pg_image.get("repository", "")
    old_tag = pg_image.get("tag", "")

    if old_repo == "bitnamilegacy/postgresql" and old_tag == "17.6.0-debian-12-r4":
        _logger.info("Odoo template PostgreSQL image already up to date")
        return

    values.setdefault("postgresql", {}).setdefault("image", {})
    values["postgresql"]["image"]["registry"] = "docker.io"
    values["postgresql"]["image"]["repository"] = "bitnamilegacy/postgresql"
    values["postgresql"]["image"]["tag"] = "17.6.0-debian-12-r4"

    cr.execute(
        """
        UPDATE woow_paas_platform_cloud_app_template
        SET helm_default_values = %s
        WHERE id = %s
        """,
        (json.dumps(values), template_id),
    )
    _logger.info(
        "Updated Odoo template PostgreSQL image: %s:%s → bitnamilegacy/postgresql:17.6.0-debian-12-r4",
        old_repo, old_tag,
    )
