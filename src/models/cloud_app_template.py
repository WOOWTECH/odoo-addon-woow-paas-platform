from odoo import api, fields, models
from odoo.exceptions import ValidationError


class CloudAppTemplate(models.Model):
    _name = 'woow_paas_platform.cloud_app_template'
    _description = 'Cloud Application Template'
    _order = 'name'

    # Basic Info
    name = fields.Char(
        string='Name',
        required=True,
        help='Application name (e.g., PostgreSQL, n8n)',
    )
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-safe identifier for the application',
    )
    icon = fields.Binary(
        string='Icon',
        help='Application icon/logo',
    )
    description = fields.Char(
        string='Description',
        help='Short description (~100 characters)',
    )
    full_description = fields.Text(
        string='Full Description',
        help='Detailed description with features and use cases',
    )
    category = fields.Selection(
        selection=[
            ('ai_llm', 'AI & LLM'),
            ('automation', 'Automation'),
            ('database', 'Database'),
            ('analytics', 'Analytics'),
            ('devops', 'DevOps'),
            ('web', 'Web'),
            ('container', 'Container'),
            ('mcp', 'MCP Server'),
        ],
        string='Category',
        help='Application category for filtering',
    )
    tags = fields.Char(
        string='Tags',
        help='JSON array of tags (e.g., ["postgresql", "database", "sql"])',
    )
    agent_context_keys = fields.Text(
        string='Agent Context Keys',
        help='JSON list of helm_values key paths (dot notation) that are '
             'safe to expose to the project AI agent, e.g. '
             '["replicaCount", "image.tag"]. Empty = expose no '
             'configuration (fail-closed). Extracted values still pass '
             'the secret sanitizer as a second layer.',
    )
    # Subscription plans available for this template
    subscription_plan_ids = fields.Many2many(
        'sh.subscription.plan',
        'woow_template_plan_rel',
        'template_id',
        'plan_id',
        string='Subscription Plans',
        help='Available subscription plans for this template',
    )

    documentation_url = fields.Char(
        string='Documentation URL',
        help='Link to official documentation',
    )

    # Helm Chart Configuration
    helm_repo_url = fields.Char(
        string='Helm Repository URL',
        required=True,
        help='URL of the Helm chart repository',
    )
    helm_chart_name = fields.Char(
        string='Helm Chart Name',
        required=True,
        help='Name of the Helm chart in the repository',
    )
    helm_chart_version = fields.Char(
        string='Helm Chart Version',
        required=True,
        help='Version of the Helm chart to deploy',
    )
    helm_default_values = fields.Text(
        string='Default Helm Values',
        help='JSON object with default values for Helm chart',
    )
    helm_value_specs = fields.Text(
        string='Helm Value Specs',
        help='JSON schema defining configurable values for users',
    )

    # Service Configuration
    default_port = fields.Integer(
        string='Default Port',
        default=80,
        help='Default internal port for the service',
    )
    ingress_enabled = fields.Boolean(
        string='Ingress Enabled',
        default=True,
        help='Whether to expose service via ingress',
    )

    # Resource Requirements
    min_vcpu = fields.Integer(
        string='Minimum vCPU',
        default=1,
        help='Minimum number of vCPU cores required',
    )
    min_ram_gb = fields.Float(
        string='Minimum RAM (GB)',
        default=1.0,
        help='Minimum RAM in GB required',
    )
    min_storage_gb = fields.Integer(
        string='Minimum Storage (GB)',
        default=5,
        help='Minimum storage in GB required',
    )

    # MCP Sidecar Configuration
    mcp_enabled = fields.Boolean(
        string='Enable MCP Sidecar',
        default=False,
        help='Enable MCP sidecar container for this application template',
    )
    mcp_sidecar_image = fields.Char(
        string='MCP Sidecar Image',
        help='Docker image for the MCP sidecar container (e.g., ghcr.io/czlonkowski/n8n-mcp:latest)',
    )
    mcp_sidecar_port = fields.Integer(
        string='MCP Sidecar Port',
        default=3001,
        help='Port the MCP sidecar listens on',
    )
    mcp_transport = fields.Selection(
        selection=[('sse', 'SSE'), ('streamable_http', 'Streamable HTTP')],
        string='MCP Transport',
        default='streamable_http',
        help='MCP transport protocol for sidecar communication',
    )
    mcp_endpoint_path = fields.Char(
        string='MCP Endpoint Path',
        default='/mcp',
        help='URL path for the MCP endpoint on the sidecar',
    )
    mcp_sidecar_env = fields.Text(
        string='MCP Sidecar Environment',
        help='JSON object of environment variables for the MCP sidecar container',
    )
    mcp_api_key_helm_path = fields.Char(
        string='MCP API Key Helm Path',
        help='Dot-path in Helm values where the auto-generated API key should be set '
             '(e.g., main.secret.N8N_API_KEY). The last segment is used as the env var '
             'name for the MCP sidecar. When set, a unique API key is generated per '
             'service and injected into both the main container (via Helm) and the '
             'MCP sidecar (as env var).',
    )

    # Post-Deploy Initialization
    post_deploy_init_type = fields.Selection(
        selection=[
            ('none', 'None'),
            ('n8n', 'n8n (Owner Setup + API Key)'),
            ('odoo', 'Odoo (Update Module List)'),
        ],
        string='Post-Deploy Init Type',
        default='none',
        help='Type of post-deployment initialization to run automatically',
    )
    post_deploy_init_email = fields.Char(
        string='Init Owner Email',
        default='admin@woowtech.io',
        help='Default email for the application owner user created during init',
    )

    # Status
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this template is available for deployment',
    )

    max_per_partner = fields.Integer(
        string='每帳號實例上限',
        default=0,
        help='同一 partner（帳號）最多可持有的本範本活躍實例數；'
             '0＝不限制。HA PaaS 免費方案設 1。',
    )

    @api.constrains('subscription_plan_ids')
    def _check_no_cross_service_type_plans(self):
        """Symmetrical to smart_home_template — a plan belongs to one service type only.

        Triggered when an admin edits a CloudAppTemplate; rejects the change if
        any of the chosen plans is already attached to a SmartHomeTemplate or a
        SecurityAccessTemplate. Without this check, the other-side constrains
        alone wouldn't catch edits made on the CS side.

        Three-way symmetric check with smart_home_template and
        security_access_template; all fire on write so the conflict is
        caught regardless of which side is edited last.
        """
        if not self:
            return
        for record in self:
            if not record.subscription_plan_ids:
                continue
            for other_model, label in [
                ('woow_paas_platform.smart_home_template', 'Smart Home Template'),
                ('woow_paas_platform.security_access_template',
                 'Security Access Template'),
            ]:
                conflicts = self.env[other_model].search([
                    ('subscription_plan_ids', 'in',
                     record.subscription_plan_ids.ids),
                ])
                if conflicts:
                    shared = (record.subscription_plan_ids
                              & conflicts.subscription_plan_ids)
                    raise ValidationError(
                        "Plan(s) %s are already linked to %s(s) %s. "
                        "A subscription plan must belong to a single service "
                        "type — its product determines the invoice line text."
                        % (
                            ', '.join(shared.mapped('name')),
                            label,
                            ', '.join(conflicts.mapped('name')),
                        )
                    )

    @api.model
    def _heal_sticky_helm_value_specs(self):
        """Upgrade healer for the fix/n8n-creation-issue seed correction.

        DBs first installed under the historical ``<data noupdate="1">`` block carry a
        sticky ``ir.model.data.noupdate=True`` on the cloud_app_* seeds, so the corrected
        ``helm_value_specs`` (clean single-line JSON) is silently skipped on ``-u``.
        Runtime still works because ``PaasController._parse_helm_value_specs`` falls back
        to ``strict=False``, but the DB row stays dirty and logs an ERROR on every
        template fetch.

        This ``<function>`` runs on every ``-u`` regardless of noupdate. It (1) clears
        the sticky flag on the seeded template xmlids so future seed edits apply, and
        (2) rewrites any helm_value_specs that fails strict JSON (control chars wrapped
        inside string values) into clean strict JSON with whitespace collapsed, so the
        stored data converges to the fixed seed. Idempotent.

        See docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md.
        """
        import json
        import re

        def _clean(node):
            if isinstance(node, str):
                return re.sub(r'\s+', ' ', node).strip()
            if isinstance(node, list):
                return [_clean(x) for x in node]
            if isinstance(node, dict):
                return {k: _clean(v) for k, v in node.items()}
            return node

        # (1) Clear sticky noupdate on this module's cloud_app_template seeds.
        stuck = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'woow_paas_platform'),
            ('model', '=', 'woow_paas_platform.cloud_app_template'),
            ('noupdate', '=', True),
        ])
        if stuck:
            stuck.write({'noupdate': False})

        # (2) Repair any dirty helm_value_specs in place.
        for template in self.search([]):
            raw = template.helm_value_specs
            if not raw:
                continue
            try:
                json.loads(raw)  # already strict-valid → leave untouched
                continue
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                parsed = json.loads(raw, strict=False)
            except (json.JSONDecodeError, TypeError):
                continue  # malformed beyond control-char wrapping; leave for a human
            template.helm_value_specs = json.dumps(_clean(parsed), ensure_ascii=False)
