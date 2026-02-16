"""Fix helm_value_specs format for cloud app templates.

Migrates from incorrect JSON Schema format:
    {"properties": {"key": {"type": "string", ...}}}

To the frontend-expected spec format:
    {"required": [...], "optional": [...]}
"""
import json
import logging

_logger = logging.getLogger(__name__)

SPECS_BY_SLUG = {
    'postgresql': json.dumps({
        "required": [
            {"key": "auth.username", "label": "Database Username", "type": "text", "default": "woow"},
            {"key": "auth.database", "label": "Database Name", "type": "text", "default": "woowdb"},
        ],
        "optional": [],
    }),
    'n8n': json.dumps({
        "required": [],
        "optional": [
            {"key": "config.database.type", "label": "Database Type", "type": "select",
             "default": "sqlite", "options": ["sqlite", "postgres"]},
        ],
    }),
    'redis': json.dumps({
        "required": [],
        "optional": [
            {"key": "architecture", "label": "Architecture", "type": "select",
             "default": "standalone", "options": ["standalone", "replication"]},
        ],
    }),
    'odoo': json.dumps({
        "required": [
            {"key": "odooEmail", "label": "Administrator Email", "type": "text",
             "default": "user@example.com"},
            {"key": "odooPassword", "label": "Administrator Password", "type": "password"},
        ],
        "optional": [
            {"key": "loadDemoData", "label": "Load Demo Data", "type": "boolean", "default": False},
        ],
    }),
}


def migrate(cr, version):
    if not version:
        return

    _logger.info("Migrating helm_value_specs format for cloud app templates")

    for slug, specs in SPECS_BY_SLUG.items():
        cr.execute(
            """
            UPDATE woow_paas_platform_cloud_app_template
            SET helm_value_specs = %s
            WHERE slug = %s
            """,
            (specs, slug),
        )
        if cr.rowcount:
            _logger.info("Updated helm_value_specs for template '%s'", slug)
        else:
            _logger.warning("Template '%s' not found, skipping", slug)
