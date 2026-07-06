"""18.0.1.0.43 — #271 heal sidecar_backed rows whose url is not in-cluster.

The new @api.constrains('sidecar_backed', 'url') (sidecar-backed MCP must use
an in-cluster '.svc.cluster.local' http URL) would raise on ANY later write to
pre-existing bad rows (e.g. the 18.0.1.0.25 auto_created→sidecar_backed
backfill that may have carried a public url, or scenario E/F data). This
migration unsets sidecar_backed on those rows (they are really external MCPs —
detach from the CS lifecycle, preserve url / tool_ids). Idempotent.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.mcp_server']._heal_sidecar_backed_non_cluster_url()
