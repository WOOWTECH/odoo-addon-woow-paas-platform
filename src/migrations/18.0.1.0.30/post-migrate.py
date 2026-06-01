"""18.0.1.0.30 — one-time backfill: auto-enable agent runtime on ready platforms.

決策來源：docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md (決策 3).

If this environment already has agent runtime fully configured (PaaS Operator
url+api_key + a valid platform Default Agent Template), enable the new-project
default toggle and flip every existing project's use_agent_runtime to True so
existing projects match the new auto-enable behaviour. No-op (and safe) on
environments that are not yet configured — they only get the new-project
default once an admin finishes platform setup and turns the toggle on.
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
    backfill_existing_projects_use_agent_runtime,
)

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    flipped = backfill_existing_projects_use_agent_runtime(env)
    _logger.info(
        "18.0.1.0.30 auto-enable backfill: flipped %s existing project(s) "
        "(0 = nothing to flip: platform not configured, or all projects "
        "already enabled).", flipped,
    )
