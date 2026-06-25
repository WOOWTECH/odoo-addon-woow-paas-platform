"""18.0.1.0.26 — Phase A: project.cloud_service_id 1:N → cloud_service_ids M:N.

Step 1 (design §7.0.2): create the shared M2M relation table
``project_cloud_service_rel`` before Odoo's registry boot tries to read /
write it through the new fields on ``project.project.cloud_service_ids`` and
``cloud_service.project_ids``.

Idempotent: CREATE TABLE IF NOT EXISTS — re-running ``-u`` is a noop.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        """
        CREATE TABLE IF NOT EXISTS project_cloud_service_rel (
            project_id INTEGER NOT NULL
                REFERENCES project_project(id) ON DELETE CASCADE,
            cloud_service_id INTEGER NOT NULL
                REFERENCES woow_paas_platform_cloud_service(id) ON DELETE CASCADE,
            PRIMARY KEY (project_id, cloud_service_id)
        )
        """
    )
    cr.execute(
        """
        CREATE INDEX IF NOT EXISTS
            project_cloud_service_rel_cloud_service_id_idx
        ON project_cloud_service_rel (cloud_service_id)
        """
    )
    _logger.info(
        "Migration 18.0.1.0.26 pre: project_cloud_service_rel table ensured."
    )
