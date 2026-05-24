import base64
import hashlib
import hmac
import logging
import os
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# The namespace secret is injected as a K8s Secret env (OQ#1 ruling (b)). It is
# deliberately single (no rotation _PREVIOUS): rotating it would re-derive a
# different hash for the same project → namespace drift → orphaned pods. A
# MISSING secret fails closed (see _get_namespace_secret) rather than falling
# back to any source-public default — otherwise anyone reading the source could
# compute the namespace hash (a public primitive).
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


def _compute_project_namespace_hash(project_id, secret):
    """Return 16-char lower-base32 of hmac_sha256(project_id, secret)[:10].

    Mirrors design §2.7 sign-off:
    - 10 bytes of HMAC-SHA256 → 80 bits → 16 base32 chars (no padding).
    - lower-cased so the value lands cleanly in a K8s namespace name
      ``paas-ws-pa-<hash>`` (DNS-1123, lower-case + digits + dash only).
    """
    digest = hmac.new(secret, str(project_id).encode(), hashlib.sha256).digest()
    return base64.b32encode(digest[:10]).decode('ascii').lower()


class AgentSession(models.Model):
    _name = 'woow_paas_platform.agent_session'
    _description = 'Per-project Agent Runtime Pod Lifecycle Record'
    _order = 'create_date desc'

    project_id = fields.Many2one(
        'project.project',
        required=True,
        index=True,
        ondelete='cascade',
    )
    project_namespace_hash = fields.Char(
        string='K8s Namespace Hash',
        readonly=True,
        index=True,
        help='hmac_sha256(project_id, secret)[:10] → base32 16 chars; '
             'used to derive helm release name agent-pa-{hash} and '
             'namespace paas-ws-pa-{hash}.',
    )

    state = fields.Selection(
        selection=[
            ('absent', 'Absent'),
            ('spawning', 'Spawning'),
            ('active', 'Active'),
            ('reaped', 'Reaped'),
            ('degraded', 'Degraded'),
            ('spawn_failed', 'Spawn Failed'),
        ],
        default='absent',
        index=True,
    )

    helm_release_name = fields.Char(readonly=True)
    helm_namespace = fields.Char(readonly=True)
    pod_ws_url = fields.Char(readonly=True)
    runtime_version = fields.Char()

    workspace_ids_snapshot = fields.Char(
        readonly=True,
        help='Comma-separated workspace_id list captured at the latest '
             'spawn-or-attach call.',
    )

    primary_workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        string='Primary Workspace (billing)',
        ondelete='set null',
    )

    quota_override_cpu = fields.Integer(string='CPU Quota Override (cores)')
    quota_override_memory_gi = fields.Integer(string='Memory Quota Override (Gi)')
    quota_override_storage_gi = fields.Integer(string='Storage Quota Override (Gi)')

    spawn_at = fields.Datetime(readonly=True)
    last_active = fields.Datetime(readonly=True)
    reaped_since = fields.Datetime(readonly=True)

    agent_template_id = fields.Many2one('woow_paas_platform.agent_template')
    mcp_snapshot_hash = fields.Char(readonly=True)
    last_error = fields.Text(readonly=True)

    # paas-operator state sync (design §5.5 — webhook primary + cron fallback)
    last_synced_at = fields.Datetime(
        readonly=True,
        help='Stamped whenever a webhook / drift cron syncs this session.',
    )
    last_state_event_ts = fields.Datetime(
        readonly=True,
        help='Timestamp of the last paas-operator state event (drift detection).',
    )

    _sql_constraints = [
        (
            'unique_project',
            'UNIQUE(project_id)',
            'One agent session per project (per-project pod model).',
        ),
        (
            'unique_namespace_hash',
            'UNIQUE(project_namespace_hash)',
            'Namespace hash must be unique across agent sessions.',
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        secret = self._get_namespace_secret()
        for vals in vals_list:
            if not vals.get('project_namespace_hash') and vals.get('project_id'):
                vals['project_namespace_hash'] = _compute_project_namespace_hash(
                    vals['project_id'], secret,
                )
        return super().create(vals_list)

    @api.model
    def _get_namespace_secret(self):
        """Return the namespace HMAC secret from the injected env, fail-closed.

        Reads ``AGENT_RUNTIME_NAMESPACE_SECRET`` (the K8s Secret env). A missing
        / empty value raises instead of using any default — the hash must not be
        derivable from source alone (F-PC-3 / C-5 / D-2).
        """
        raw = os.environ.get(_ENV_NAMESPACE_SECRET, '')
        if not raw:
            raise UserError(
                "Agent runtime namespace secret is not configured: set the "
                "%s environment variable (injected from the K8s Secret). The "
                "namespace hash is not derived without it (fail-closed)."
                % _ENV_NAMESPACE_SECRET
            )
        return raw.encode()

    @api.model
    def _cron_drift_detection(self):
        """Fallback poll for sessions the webhook may have missed (§5.5).

        Sessions in a live-ish state whose ``last_synced_at`` is stale
        (> 10 min) are re-synced against paas-operator's status endpoint.
        """
        from ..services.agent_runtime_client import get_agent_runtime_client
        client = get_agent_runtime_client(self.env)
        if not client:
            return
        threshold = fields.Datetime.now() - timedelta(minutes=10)
        stale = self.sudo().search([
            ('state', 'in', ['spawning', 'active', 'reaped', 'degraded']),
            '|',
            ('last_synced_at', '=', False),
            ('last_synced_at', '<', threshold),
        ])
        for session in stale:
            if not session.project_namespace_hash:
                continue
            try:
                status = client.get_status(session.project_namespace_hash)
            except Exception as exc:
                _logger.warning(
                    'Drift sync failed for agent_session %s: %s', session.id, exc,
                )
                continue
            vals = {'last_synced_at': fields.Datetime.now()}
            if status.get('state'):
                vals['state'] = status['state']
            if 'pod_ws_url' in status:
                vals['pod_ws_url'] = status['pod_ws_url'] or False
            session.write(vals)
