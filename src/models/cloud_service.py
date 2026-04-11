import json
import logging
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _unflatten_dotpath_keys(flat_dict):
    """Convert flat dot-path keys to nested dict.

    Example: {"a.b.c": 1} → {"a": {"b": {"c": 1}}}
    """
    result = {}
    for key, value in flat_dict.items():
        parts = key.split('.')
        if len(parts) == 1:
            result[key] = value
            continue
        d = result
        for part in parts[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value
    return result


def _deep_merge(base, override):
    """Deep merge override into base. Override values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Cloud Service Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all services.',
        ),
        (
            'unique_reference_id',
            'UNIQUE(reference_id)',
            'Reference ID must be unique.',
        ),
    ]

    # Relationships
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this service belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_app_template',
        string='Application Template',
        required=True,
        ondelete='restrict',
        help='The application template used to create this service',
    )

    # Identity
    name = fields.Char(
        string='Service Name',
        required=True,
        help='User-defined name for this service instance',
    )
    reference_id = fields.Char(
        string='Reference ID',
        index=True,
        readonly=True,
        default=lambda self: str(uuid.uuid4()),
        help='Immutable unique identifier for this service',
    )
    deployment_id = fields.Char(
        string='Deployment ID',
        help='External deployment system identifier',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('initializing', 'Initializing'),
            ('running', 'Running'),
            ('error', 'Error'),
            ('upgrading', 'Upgrading'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        help='Current state of the service deployment',
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if deployment failed',
    )

    # Post-deploy initialization tracking
    init_retries = fields.Integer(
        string='Init Retries',
        default=0,
        help='Number of post-deploy initialization attempts',
    )
    init_error = fields.Text(
        string='Init Error',
        help='Last error from post-deploy initialization',
    )
    addon_update_pending = fields.Boolean(
        string='Addon Update Pending',
        default=False,
        help='Whether addon selection changed during last upgrade and needs post-deploy init',
    )

    # Network
    subdomain = fields.Char(
        string='Subdomain',
        help='Subdomain for accessing this service (combined with PaaS domain, e.g., myapp.woowtech.io)',
    )
    custom_domain = fields.Char(
        string='Custom Domain',
        help='Custom domain configured by user',
    )
    internal_port = fields.Integer(
        string='Internal Port',
        help='Internal port where the service is listening',
    )

    # Helm Release Info
    helm_release_name = fields.Char(
        string='Helm Release Name',
        help='Name of the Helm release in Kubernetes',
    )
    helm_namespace = fields.Char(
        string='Helm Namespace',
        help='Kubernetes namespace where the service is deployed',
    )
    helm_values = fields.Text(
        string='Helm Values',
        help='JSON object with Helm values used for deployment',
    )
    helm_revision = fields.Integer(
        string='Helm Revision',
        default=1,
        help='Current Helm release revision number',
    )
    helm_chart_version = fields.Char(
        string='Chart Version',
        help='Version of the Helm chart currently deployed',
    )

    # Resources
    allocated_vcpu = fields.Integer(
        string='Allocated vCPU',
        help='Number of vCPU cores allocated to this service',
    )
    allocated_ram_gb = fields.Float(
        string='Allocated RAM (GB)',
        help='RAM in GB allocated to this service',
    )
    allocated_storage_gb = fields.Integer(
        string='Allocated Storage (GB)',
        help='Storage in GB allocated to this service',
    )

    # Support Project
    project_ids = fields.One2many(
        comodel_name='project.project',
        inverse_name='cloud_service_id',
        string='Support Projects',
    )

    # User MCP Servers
    user_mcp_server_ids = fields.One2many(
        comodel_name='woow_paas_platform.mcp_server',
        inverse_name='cloud_service_id',
        string='MCP Servers',
        domain=[('scope', '=', 'user')],
    )

    # MCP Sidecar
    mcp_auth_token = fields.Char(
        string='MCP Auth Token',
        readonly=True,
        groups='base.group_system',
        help='Authentication token for the MCP sidecar, auto-generated during deployment',
    )
    n8n_api_key = fields.Char(
        string='N8N API Key',
        readonly=True,
        groups='base.group_system',
        help='API key for n8n instance, generated during post-deploy init',
    )
    n8n_owner_email = fields.Char(
        string='N8N Owner Email',
        readonly=True,
        groups='base.group_system',
    )
    n8n_owner_password = fields.Char(
        string='N8N Owner Password',
        readonly=True,
        groups='base.group_system',
    )

    # Timestamps
    deployed_at = fields.Datetime(
        string='Deployed At',
        help='Timestamp when service was first deployed',
    )
    last_upgraded_at = fields.Datetime(
        string='Last Upgraded At',
        help='Timestamp of the most recent upgrade',
    )

    # Odoo Addons
    odoo_addon_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_odoo_addon',
        relation='cloud_service_odoo_addon_rel',
        column1='service_id',
        column2='addon_id',
        string='Odoo Addons',
        help='Odoo addons selected for this service',
    )

    # Deploy metadata (JSON blob consumed by _execute_deploy cron)
    deploy_params = fields.Text(
        string='Deploy Params',
        groups='base.group_system',
        help='JSON parameters for pending deployment (expose_config, mcp_api_key, etc.)',
    )

    # ------------------------------------------------------------------ #
    #  Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_redeploy(self):
        """Re-deploy the service, preserving existing data.

        If the Helm release already exists, performs a helm upgrade
        (data-safe — PVCs are preserved). If no release exists yet
        (e.g. initial deploy failed), resets to pending for a fresh install.
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError("PaaS Operator not configured. Contact administrator.")

        # Check whether a Helm release already exists
        release_exists = False
        if self.helm_release_name and self.helm_namespace:
            try:
                client.get_release(
                    namespace=self.helm_namespace,
                    release_name=self.helm_release_name,
                )
                release_exists = True
            except PaaSOperatorError:
                release_exists = False

        if release_exists:
            # Upgrade in-place — preserves PVCs and data
            template = self.template_id
            values = json.loads(self.helm_values) if self.helm_values else None
            try:
                release_info = client.upgrade_release(
                    namespace=self.helm_namespace,
                    release_name=self.helm_release_name,
                    chart=template.helm_chart_name,
                    version=template.helm_chart_version,
                    values=values,
                    reuse_values=True,
                )
            except PaaSOperatorError as e:
                raise UserError(
                    f"Upgrade failed: {e.detail or e.message}"
                ) from e

            # Set to 'deploying' — cron + frontend polling will handle
            # the rest (pod readiness check → init → running)
            self.write({
                'state': 'deploying',
                'error_message': False,
                'init_error': False,
                'init_retries': 0,
                'helm_revision': release_info.get('revision', self.helm_revision),
            })
            # Trigger cron so state transitions without frontend polling
            cron = self.env.ref(
                'woow_paas_platform.ir_cron_check_deploying_services',
                raise_if_not_found=False,
            )
            if cron:
                cron._trigger()
        else:
            # No release yet — do a full fresh deploy via cron
            deploy_params = {}
            template = self.template_id
            if template.ingress_enabled and self.subdomain:
                deploy_params['expose_config'] = {
                    'enabled': True,
                    'subdomain': self.subdomain,
                }

            self.write({
                'state': 'pending',
                'error_message': False,
                'init_error': False,
                'init_retries': 0,
                'deploy_params': json.dumps(deploy_params) if deploy_params else False,
            })

            cron = self.env.ref(
                'woow_paas_platform.ir_cron_deploy_cloud_services',
                raise_if_not_found=False,
            )
            if cron:
                cron._trigger()

        return True

    # ------------------------------------------------------------------ #
    #  Cron: process pending deployments                                  #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_process_pending_deploys(self):
        """Called by ir.cron to deploy services still in 'pending' state.

        Processes one service per cron run to let Odoo manage the transaction.
        If more pending services remain, re-triggers the cron for immediate pickup.
        """
        service = self.search([('state', '=', 'pending')], limit=1)
        if not service:
            return

        try:
            service._execute_deploy()
        except Exception:
            _logger.exception(
                "Deploy failed for service %s (id=%s)",
                service.name, service.id,
            )
            service.write({
                'state': 'error',
                'error_message': 'Unexpected error during deployment. Check server logs.',
            })

        # If more pending services exist, re-trigger for next run
        remaining = self.search_count([('state', '=', 'pending')])
        if remaining:
            self.env.ref('woow_paas_platform.ir_cron_deploy_cloud_services')._trigger()

    def _execute_deploy(self):
        """Execute the actual deployment for a single pending service."""
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
            PaaSOperatorConnectionError,
        )

        _logger.info("Deploying service %s (id=%s)", self.name, self.id)
        self.write({'state': 'deploying'})

        client = get_paas_operator_client(self.env)
        if not client:
            self.write({
                'state': 'error',
                'error_message': 'PaaS Operator not configured. Contact administrator.',
            })
            return

        template = self.template_id
        helm_namespace = self.helm_namespace
        helm_release_name = self.helm_release_name
        merged_values = json.loads(self.helm_values) if self.helm_values else {}

        # Inject StorageClass for PVC lifecycle management
        storage_class = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete'
        )
        if storage_class:
            merged_values = self._inject_storage_class(
                merged_values, template.slug, storage_class,
            )

        # Inject imagePullSecrets for private registry access
        registry_secret = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.registry_secret_name', 'paas-registry'
        )
        if registry_secret:
            merged_values = self._inject_image_pull_secrets(
                merged_values, registry_secret,
            )

        # Inject Odoo addon selection (initContainer + addons_path)
        if self.odoo_addon_ids and template.slug == 'odoo':
            image_vals = merged_values.get('image', {})
            registry = image_vals.get('registry', 'docker.io')
            repo = image_vals.get('repository', '')
            tag = image_vals.get('tag', 'latest')
            image_ref = f"{registry}/{repo}:{tag}"
            addon_values = self._build_odoo_addon_helm_values(image_ref)
            # Deep-merge addon values into merged_values
            for key, val in addon_values.items():
                if key in merged_values and isinstance(merged_values[key], dict) and isinstance(val, dict):
                    merged_values[key].update(val)
                else:
                    merged_values[key] = val

        # Recover deploy params stored at creation time
        params = json.loads(self.deploy_params) if self.deploy_params else {}
        expose_config = params.get('expose_config')
        mcp_api_key = params.get('mcp_api_key')

        try:
            # Create namespace (409 = already exists, OK)
            all_services = self.search([
                ('workspace_id', '=', self.workspace_id.id),
                ('state', '!=', 'deleting'),
            ])
            total_vcpu = sum(s.allocated_vcpu for s in all_services)
            total_ram = sum(s.allocated_ram_gb for s in all_services)
            total_storage = sum(s.allocated_storage_gb for s in all_services)
            try:
                client.create_namespace(
                    namespace=helm_namespace,
                    cpu_limit=str(max(total_vcpu * 3, 8)),
                    memory_limit=f"{max(int(total_ram * 3), 8)}Gi",
                    storage_limit=f"{max(total_storage * 3, 100)}Gi",
                )
            except PaaSOperatorError as e:
                if e.status_code != 409:
                    self.write({
                        'state': 'error',
                        'error_message': f'Failed to create namespace: {e.detail or e.message}',
                    })
                    return

            # Install Helm release
            _logger.info(
                "Helm install: %s in %s (chart=%s, expose=%s)",
                helm_release_name, helm_namespace, template.helm_chart_name, expose_config,
            )
            release_info = client.install_release(
                namespace=helm_namespace,
                release_name=helm_release_name,
                chart=template.helm_chart_name,
                repo_url=template.helm_repo_url,
                version=template.helm_chart_version,
                values=merged_values,
                create_namespace=True,
                expose=expose_config,
            )

            # Patch MCP sidecar if enabled
            if template.mcp_enabled and template.mcp_sidecar_image:
                mcp_auth_token = str(uuid.uuid4())
                sidecar_config = self._build_mcp_sidecar_config(
                    template, mcp_auth_token, mcp_api_key,
                    helm_release_name=helm_release_name,
                )
                try:
                    client.patch_sidecar(
                        namespace=helm_namespace,
                        release_name=helm_release_name,
                        sidecar_config=sidecar_config,
                    )
                    self.write({'mcp_auth_token': mcp_auth_token})
                    _logger.info("MCP sidecar patched for %s", self.name)
                except PaaSOperatorError as e:
                    _logger.warning("Failed to patch MCP sidecar: %s", e)

            # Helm install done — update release info and clean up deploy params
            self.write({
                'helm_revision': release_info.get('revision', 1),
                'deployed_at': fields.Datetime.now(),
                'deploy_params': False,
            })
            _logger.info("Service %s (id=%s) helm install complete", self.name, self.id)

        except PaaSOperatorConnectionError as e:
            _logger.error("Operator connection error: %s", e)
            self.write({
                'state': 'error',
                'error_message': 'Unable to connect to deployment service. Please try again later.',
            })

        except PaaSOperatorError as e:
            _logger.error("Operator error: %s", e)
            self.write({
                'state': 'error',
                'error_message': f'Deployment failed: {e.detail or e.message}',
            })

    def _build_odoo_addon_helm_values(self, image_ref):
        """Build Helm values for Odoo addon selection via initContainer.

        Args:
            image_ref: Full image reference (e.g., registry/repo:tag)
                       used for the initContainer (same as main container).

        Returns:
            Dict of Helm values to deep-merge, or empty dict if no addons.
        """
        if not self.odoo_addon_ids:
            return {}

        # Separate addons by source type
        copy_addons = []  # need initContainer copy from /all-addons/
        for addon in self.odoo_addon_ids:
            if addon.source_type in ('third_party', 'custom'):
                copy_addons.append(addon.technical_name)

        values = {}
        selected_addons_path = '/opt/bitnami/odoo/selected-addons'

        if copy_addons:
            addon_list = ' '.join(copy_addons)
            values['initContainers'] = [{
                'name': 'addon-selector',
                'image': image_ref,
                'imagePullPolicy': 'IfNotPresent',
                'command': ['/bin/bash', '-c'],
                'args': [
                    f'echo "[addon-selector] Copying addons..."\n'
                    f'for addon in {addon_list}; do\n'
                    f'  if [ -d "/all-addons/$addon" ]; then\n'
                    f'    cp -r "/all-addons/$addon" /mnt/selected-addons/\n'
                    f'    echo "  + $addon"\n'
                    f'  else\n'
                    f'    echo "  ! $addon not found"\n'
                    f'  fi\n'
                    f'done\n'
                    f'CONF=/bitnami/odoo/conf/odoo.conf\n'
                    f'if [ -f "$CONF" ] && ! grep -q "selected-addons" "$CONF"; then\n'
                    f'  sed -i "s|^addons_path.*|&,{selected_addons_path}|" "$CONF"\n'
                    f'  echo "[addon-selector] Updated addons_path"\n'
                    f'fi\n'
                    f'echo "[addon-selector] Done"',
                ],
                'resources': {
                    'requests': {'cpu': '100m', 'memory': '128Mi'},
                    'limits': {'cpu': '500m', 'memory': '256Mi'},
                },
                'volumeMounts': [
                    {'name': 'selected-addons', 'mountPath': '/mnt/selected-addons'},
                    {'name': 'odoo-data', 'mountPath': '/bitnami/odoo'},
                ],
            }]
            values['extraVolumes'] = [{'name': 'selected-addons', 'emptyDir': {}}]
            values['extraVolumeMounts'] = [
                {'name': 'selected-addons', 'mountPath': selected_addons_path},
            ]

        # Fallback for first boot (odoo.conf doesn't exist yet in initContainer)
        addons_path_script = (
            f'#!/bin/bash\n'
            f'CONF=/opt/bitnami/odoo/conf/odoo.conf\n'
            f'if [ -f "$CONF" ] && ! grep -q "selected-addons" "$CONF"; then\n'
            f'  sed -i "s|^addons_path.*|&,{selected_addons_path}|" "$CONF"\n'
            f'fi'
        )
        values.setdefault('customPostInitScripts', {})
        values['customPostInitScripts']['00-addons-path.sh'] = addons_path_script

        return values

    def _remove_addon_helm_values(self, values):
        """Remove only addon-selector related entries from helm values.

        Used when all addons are deselected. Precisely removes addon-specific
        items without affecting other extraVolumes/initContainers.
        """
        if 'initContainers' in values:
            values['initContainers'] = [
                c for c in values['initContainers']
                if c.get('name') != 'addon-selector'
            ]
        if 'extraVolumes' in values:
            values['extraVolumes'] = [
                v for v in values['extraVolumes']
                if v.get('name') != 'selected-addons'
            ]
        if 'extraVolumeMounts' in values:
            values['extraVolumeMounts'] = [
                m for m in values['extraVolumeMounts']
                if m.get('name') != 'selected-addons'
            ]
        if 'customPostInitScripts' in values:
            values['customPostInitScripts'].pop('00-addons-path.sh', None)

    @staticmethod
    def _inject_storage_class(values, template_slug, storage_class):
        """Inject StorageClass into helm values for PVC lifecycle management.

        Different Helm charts use different value paths for storageClass.
        This method sets the correct paths based on the template slug.

        Args:
            values: Helm values dict (will be modified in-place and returned)
            template_slug: Template identifier (e.g., 'odoo', 'postgresql')
            storage_class: StorageClass name to inject

        Returns:
            Modified values dict
        """
        # Mapping of template slug to storageClass value paths
        path_map = {
            'odoo': [
                ('persistence', 'storageClass'),
                ('postgresql', 'primary', 'persistence', 'storageClass'),
            ],
            'postgresql': [
                ('primary', 'persistence', 'storageClass'),
            ],
            'n8n': [
                ('persistence', 'storageClass'),
            ],
            'redis': [
                ('master', 'persistence', 'storageClass'),
                ('replica', 'persistence', 'storageClass'),
            ],
        }

        paths = path_map.get(template_slug, [('persistence', 'storageClass')])

        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = storage_class

        return values

    @staticmethod
    def _inject_image_pull_secrets(values, secret_name):
        """Inject imagePullSecrets into helm values for private registry access.

        Uses global.imagePullSecrets which is supported by all Bitnami charts
        and propagates to all containers including initContainers.

        Args:
            values: Helm values dict (modified in-place and returned)
            secret_name: Name of the dockerconfigjson K8s Secret

        Returns:
            Modified values dict
        """
        values.setdefault('global', {})
        existing = values['global'].get('imagePullSecrets', [])
        if secret_name not in existing:
            existing.append(secret_name)
        values['global']['imagePullSecrets'] = existing
        return values

    def _build_mcp_sidecar_config(
        self, template, auth_token, api_key=None, helm_release_name=None,
    ):
        """Build MCP sidecar container config for PaaS Operator patch endpoint."""
        sidecar_port = template.mcp_sidecar_port or 3000

        env_vars = [
            {'name': 'MCP_MODE', 'value': 'http'},
            {'name': 'AUTH_TOKEN', 'value': auth_token},
            {'name': 'N8N_API_URL', 'value': f'http://localhost:{template.default_port}'},
            {'name': 'PORT', 'value': str(sidecar_port)},
            {'name': 'AUTH_RATE_LIMIT_MAX', 'value': '500'},
            {'name': 'AUTH_RATE_LIMIT_WINDOW', 'value': '900000'},
        ]

        if template.mcp_api_key_helm_path:
            env_name = template.mcp_api_key_helm_path.rsplit('.', 1)[-1]
            if helm_release_name:
                secret_name = f'{helm_release_name}-n8n-app-secret'
                env_vars.append({
                    'name': env_name,
                    'valueFrom': {
                        'secretKeyRef': {
                            'name': secret_name,
                            'key': env_name,
                        }
                    }
                })
            elif api_key:
                env_vars.append({'name': env_name, 'value': api_key})

        if template.mcp_sidecar_env:
            try:
                custom_env = json.loads(template.mcp_sidecar_env)
                reserved_keys = {'MCP_MODE', 'AUTH_TOKEN', 'PORT', 'N8N_API_URL'}
                if template.mcp_api_key_helm_path:
                    reserved_keys.add(template.mcp_api_key_helm_path.rsplit('.', 1)[-1])
                for key, value in custom_env.items():
                    if key not in reserved_keys:
                        env_vars.append({'name': key, 'value': str(value)})
            except (json.JSONDecodeError, TypeError):
                _logger.warning("Invalid MCP sidecar env JSON in template %s", template.name)

        return {
            'container': {
                'name': 'mcp-sidecar',
                'image': template.mcp_sidecar_image,
                'ports': [{'containerPort': sidecar_port}],
                'env': env_vars,
                'resources': {
                    'requests': {'memory': '128Mi', 'cpu': '100m'},
                    'limits': {'memory': '256Mi', 'cpu': '200m'},
                },
                'livenessProbe': {
                    'httpGet': {'path': '/health', 'port': sidecar_port},
                    'initialDelaySeconds': 15,
                    'periodSeconds': 30,
                },
                'readinessProbe': {
                    'httpGet': {'path': '/health', 'port': sidecar_port},
                    'initialDelaySeconds': 10,
                    'periodSeconds': 10,
                },
            },
        }

    # ------------------------------------------------------------------ #
    #  Cron: check deploying / upgrading / initializing services         #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_check_deploying_services(self):
        """Poll operator for services in transitional states.

        This cron ensures deploying → running transitions happen even without
        frontend polling (e.g. when redeploy is triggered from backend views).
        """
        services = self.search([
            ('state', 'in', ['deploying', 'upgrading', 'initializing']),
        ])
        if not services:
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if not client:
            return

        for service in services:
            try:
                service._poll_and_update_status(client)
            except Exception:
                _logger.exception(
                    "Error checking status for service %s (id=%s)",
                    service.name, service.id,
                )

    def _poll_and_update_status(self, client):
        """Poll operator and transition state for a single service.

        Mirrors the logic in PaasController._update_service_status but works
        in model context (usable from cron without HTTP request).
        """
        self.ensure_one()
        from ..services.paas_operator import PaaSOperatorError

        original_state = self.state

        try:
            status = client.get_status(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
            )
        except PaaSOperatorError as e:
            if e.status_code == 404 and self.state == 'deleting':
                self.unlink()
            else:
                _logger.warning("Error polling status for %s: %s", self.name, e)
            return
        except Exception as e:
            _logger.warning("Error polling status for %s: %s", self.name, e)
            return

        release = status.get('release', {})
        pods = status.get('pods', [])
        release_status = release.get('status', '')
        helm_revision = release.get('revision', self.helm_revision)

        # Re-fetch for optimistic locking
        service = self.browse(self.id)
        if not service.exists() or service.state != original_state:
            return

        if release_status == 'deployed':
            # Only Running-phase pods — terminators would block readiness forever
            active_pods = [p for p in pods if p.get('phase') == 'Running']
            all_ready = all(
                '/' in pod.get('ready', '0/0')
                and pod.get('ready', '0/0').split('/')[0] == pod.get('ready', '0/0').split('/')[1]
                for pod in active_pods
            ) if active_pods else (not pods)

            if all_ready:
                template = service.template_id
                needs_init = (
                    template.post_deploy_init_type
                    and template.post_deploy_init_type != 'none'
                )

                if needs_init and original_state == 'deploying':
                    service.write({
                        'state': 'initializing',
                        'helm_revision': helm_revision,
                        'error_message': False,
                    })
                    service._model_run_post_deploy_init(client)

                elif needs_init and original_state == 'initializing':
                    service._model_run_post_deploy_init(client)

                elif needs_init and original_state == 'upgrading' and service.addon_update_pending:
                    service.write({
                        'state': 'initializing',
                        'helm_revision': helm_revision,
                        'error_message': False,
                        'init_retries': 0,
                        'init_error': False,
                    })
                    service._model_run_post_deploy_init(client)

                else:
                    service.write({
                        'state': 'running',
                        'helm_revision': helm_revision,
                        'error_message': False,
                    })
                    if original_state != 'running':
                        service._model_auto_create_mcp_server()

        elif release_status == 'failed':
            service.write({
                'state': 'error',
                'helm_revision': helm_revision,
                'error_message': release.get('description', 'Deployment failed'),
            })

    # ------------------------------------------------------------------ #
    #  Post-deploy init (model context)                                  #
    # ------------------------------------------------------------------ #

    def _model_run_post_deploy_init(self, client):
        """Execute post-deploy initialization in model context (for cron).

        Handles n8n owner setup + API key, and Odoo module list refresh.
        """
        self.ensure_one()
        template = self.template_id

        if template.post_deploy_init_type == 'n8n':
            self._model_init_n8n(client)
        elif template.post_deploy_init_type == 'odoo':
            self._model_init_odoo(client)

    def _model_init_n8n(self, client):
        """n8n post-deploy init: owner setup + API key generation."""
        self.ensure_one()
        template = self.template_id

        owner_email = self.n8n_owner_email or template.post_deploy_init_email or 'admin@woowtech.io'
        owner_password = self.n8n_owner_password
        if not owner_password:
            owner_password = 'W' + str(uuid.uuid4()).upper()

        try:
            result = client.init_n8n(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                owner_email=owner_email,
                owner_password=owner_password,
            )

            if result.get('success'):
                real_api_key = result['api_key']
                update_vals = {
                    'n8n_api_key': real_api_key,
                    'state': 'running',
                    'init_retries': 0,
                    'init_error': False,
                }
                if template.mcp_api_key_helm_path and self.helm_values:
                    try:
                        current_values = json.loads(self.helm_values)
                        api_key_nested = _unflatten_dotpath_keys(
                            {template.mcp_api_key_helm_path: real_api_key}
                        )
                        updated_values = _deep_merge(current_values, api_key_nested)
                        update_vals['helm_values'] = json.dumps(updated_values)
                    except (json.JSONDecodeError, TypeError):
                        _logger.warning("Failed to update helm_values with real API key for service %s", self.name)
                self.write(update_vals)
                _logger.info("n8n init succeeded for service %s (via cron)", self.name)
                try:
                    self._model_auto_create_mcp_server()
                except Exception as e:
                    _logger.warning("Auto-create MCP server failed for %s: %s", self.name, e)
            else:
                self._record_init_failure(result.get('error', 'Unknown error'))

        except Exception as e:
            self._record_init_failure(str(e))

    def _model_init_odoo(self, client):
        """Odoo post-deploy init: module list refresh."""
        self.ensure_one()
        try:
            result = client.init_odoo(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
            )
            if result.get('success'):
                self.write({
                    'state': 'running',
                    'init_retries': 0,
                    'init_error': False,
                    'addon_update_pending': False,
                })
                _logger.info("Odoo init succeeded for service %s (via cron)", self.name)
                try:
                    self._model_auto_create_mcp_server()
                except Exception as e:
                    _logger.warning("Auto-create MCP server failed for %s: %s", self.name, e)
            else:
                self._record_init_failure(result.get('error', 'Unknown error'))
        except Exception as e:
            self._record_init_failure(str(e))

    def _record_init_failure(self, error_msg):
        """Record a post-deploy init failure, moving to error after 5 retries."""
        retries = (self.init_retries or 0) + 1
        vals = {
            'init_retries': retries,
            'init_error': error_msg,
        }
        if retries >= 5:
            vals.update({
                'state': 'error',
                'error_message': f'Initialization failed after {retries} retries: {error_msg}',
            })
        self.write(vals)
        _logger.warning(
            "Init attempt %d failed for service %s: %s",
            retries, self.name, error_msg,
        )

    # ------------------------------------------------------------------ #
    #  MCP auto-create (model context)                                   #
    # ------------------------------------------------------------------ #

    def _model_auto_create_mcp_server(self):
        """Auto-create MCP Server when transitioning to running (model context)."""
        self.ensure_one()
        template = self.template_id
        if not template.mcp_enabled or not template.mcp_sidecar_image:
            return

        McpServer = self.env['woow_paas_platform.mcp_server'].sudo()
        existing = McpServer.search([
            ('cloud_service_id', '=', self.id),
            ('auto_created', '=', True),
        ], limit=1)
        if existing:
            return

        mcp_url = self._model_build_mcp_endpoint_url()

        server = McpServer.create({
            'name': f"{self.name} MCP",
            'url': mcp_url,
            'transport': template.mcp_transport or 'streamable_http',
            'scope': 'user',
            'cloud_service_id': self.id,
            'auto_created': True,
            'api_key': self.mcp_auth_token,
            'description': f"Auto-created MCP server for {self.name}",
        })

        _logger.info(
            "Auto-created MCP Server '%s' (id=%s) for cloud service '%s' (via cron)",
            server.name, server.id, self.name,
        )
        server.action_sync_tools_safe()

    def _model_build_mcp_endpoint_url(self):
        """Build MCP endpoint URL (model context, no request needed)."""
        self.ensure_one()
        template = self.template_id
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001

        if self.helm_release_name and self.helm_namespace:
            return (
                f"http://{self.helm_release_name}-mcp"
                f".{self.helm_namespace}.svc.cluster.local"
                f":{sidecar_port}{endpoint_path}"
            )

        if self.subdomain:
            IrConfigParameter = self.env['ir.config_parameter'].sudo()
            paas_domain = IrConfigParameter.get_param(
                'woow_paas_platform.paas_domain', 'woowtech.io',
            )
            return f"https://{self.subdomain}.{paas_domain}{endpoint_path}"

        return f"http://localhost:{sidecar_port}{endpoint_path}"

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """Scale down Helm release to 0 replicas."""
        self.ensure_one()
        if self.state == 'suspended':
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=0,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to scale down service %s: %s", self.name, e,
                )

        self.write({
            'pre_suspend_state': self.state if self.state in ('running', 'error') else 'running',
            'state': 'suspended',
        })

    def _do_resume(self):
        """Scale up Helm release to restore replicas."""
        self.ensure_one()
        if self.state != 'suspended':
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=1,
                )
            except Exception as e:
                _logger.warning(
                    "Failed to scale up service %s: %s", self.name, e,
                )

        restore_state = self.pre_suspend_state or 'running'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })

    def _do_cleanup(self):
        """Helm uninstall — permanently remove the service."""
        self.ensure_one()

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.uninstall_release(
                    self.helm_release_name, self.helm_namespace,
                )
            except Exception as e:
                _logger.warning(
                    "Cleanup failed for service %s: %s", self.name, e,
                )
        self.write({'state': 'deleting'})
