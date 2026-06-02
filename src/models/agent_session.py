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

    # ----------------------------------------------------------------- #
    # Snapshot / resolve — single source shared by chat (controller) and #
    # on-publish push (#111). Down-sunk from ai_assistant.py (spec ①).   #
    # ----------------------------------------------------------------- #
    @api.model
    def _platform_default_agent_template(self):
        """Platform default template if valid, else empty recordset.

        Valid = config_parameter set + exists + is_public + published +
        project-unbound (cross-tenant MCP/skill leak guard)."""
        AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        default_id = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_agent_template_id')
        if not default_id:
            return AgentTemplate.browse()
        try:
            candidate = AgentTemplate.browse(int(default_id)).exists()
        except (ValueError, TypeError):
            return AgentTemplate.browse()
        if candidate and candidate.is_public \
                and candidate.state == 'published' \
                and not candidate.project_ids:
            return candidate
        return AgentTemplate.browse()

    @api.model
    def _resolve_agent_template(self, project):
        """project-bound published → project-bound any → project.agent_template_id
        → platform default. Last fallback so a project's own binding wins."""
        AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        template = AgentTemplate.search([
            ('project_ids', 'in', project.id),
            ('state', '=', 'published'),
        ], limit=1)
        if not template:
            template = AgentTemplate.search([
                ('project_ids', 'in', project.id),
            ], limit=1)
        if not template and project.agent_template_id:
            template = project.agent_template_id
        if not template:
            template = self._platform_default_agent_template()
        return template or AgentTemplate.browse()

    @api.model
    def _build_mcp_snapshot(self, project, template):
        """Serialise the template's bound MCP servers (name → url/transport)."""
        servers = {}
        for mcp in template.mcp_server_ids:
            servers[mcp.name] = {'url': mcp.url or '', 'transport': mcp.transport}
        return {'servers': servers}

    @api.model
    def _build_skill_snapshot(self, project, template):
        """Serialise the template's published skills + system skills for the pod
        (name/version/sha256/description; system shadowed by same-name bound)."""
        Skill = self.env['woow_paas_platform.skill'].sudo()
        bound = template.skill_ids.filtered(lambda s: s.state == 'published')
        bound_names = set(bound.mapped('name'))
        system = Skill.search([
            ('scope', '=', 'system'),
            ('state', '=', 'published'),
            ('name', 'not in', list(bound_names)),
        ])
        return [
            {'name': skill.name, 'version': skill.version,
             'sha256': skill.bundle_sha256,
             'description': skill.description or ''}
            for skill in (bound + system)
        ]
