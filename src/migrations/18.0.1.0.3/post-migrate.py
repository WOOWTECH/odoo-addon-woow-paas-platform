"""Update n8n template: add basic auth, timezone, log level specs.

Adds required basic auth fields and optional timezone/log level fields
to helm_value_specs. Updates helm_default_values with basic auth active
flag and default timezone/log level.
"""
import json
import logging

_logger = logging.getLogger(__name__)

N8N_HELM_VALUE_SPECS = {
    "required": [
        {
            "key": "main.secret.N8N_BASIC_AUTH_USER",
            "label": "Basic Auth Username",
            "type": "text",
            "default": "admin",
        },
        {
            "key": "main.secret.N8N_BASIC_AUTH_PASSWORD",
            "label": "Basic Auth Password",
            "type": "password",
        },
    ],
    "optional": [
        {
            "key": "config.database.type",
            "label": "Database Type",
            "type": "select",
            "default": "sqlite",
            "options": ["sqlite", "postgres"],
        },
        {
            "key": "main.config.GENERIC_TIMEZONE",
            "label": "Timezone",
            "type": "select",
            "default": "Asia/Taipei",
            "options": [
                "Asia/Taipei",
                "Asia/Tokyo",
                "Asia/Shanghai",
                "Asia/Hong_Kong",
                "America/New_York",
                "America/Los_Angeles",
                "Europe/London",
                "UTC",
            ],
        },
        {
            "key": "main.config.N8N_LOG_LEVEL",
            "label": "Log Level",
            "type": "select",
            "default": "info",
            "options": ["info", "warn", "error", "debug"],
        },
    ],
}


def migrate(cr, version):
    if not version:
        return

    _logger.info("Updating n8n template with basic auth and config specs")

    cr.execute(
        """
        SELECT id, helm_default_values
        FROM woow_paas_platform_cloud_app_template
        WHERE slug = 'n8n'
        """,
    )
    row = cr.fetchone()
    if not row:
        _logger.warning("n8n template not found, skipping")
        return

    template_id, raw_values = row
    try:
        values = json.loads(raw_values) if raw_values else {}
    except (json.JSONDecodeError, TypeError):
        _logger.warning("Invalid helm_default_values for n8n template, using empty dict")
        values = {}

    # Add basic auth and config defaults to nested structure
    values.setdefault("main", {})
    values["main"].setdefault("config", {})
    values["main"]["config"].setdefault("GENERIC_TIMEZONE", "Asia/Taipei")
    values["main"]["config"].setdefault("N8N_LOG_LEVEL", "info")
    values["main"]["config"]["N8N_BASIC_AUTH_ACTIVE"] = "true"
    values["main"].setdefault("secret", {})
    values["main"]["secret"].setdefault("N8N_BASIC_AUTH_USER", "admin")

    cr.execute(
        """
        UPDATE woow_paas_platform_cloud_app_template
        SET helm_value_specs = %s,
            helm_default_values = %s
        WHERE id = %s
        """,
        (json.dumps(N8N_HELM_VALUE_SPECS), json.dumps(values), template_id),
    )
    _logger.info("Updated n8n template: added basic auth + timezone + log level specs")
