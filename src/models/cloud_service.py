import json
import logging
import secrets
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# #336（Wave A）：計數「活躍實例」時排除的刪除終態。CloudService 刪除走真 unlink
# （record 離場），故 state 值集內唯一「即將釋放、不應佔用配額」的過渡態
# 為 'deleting'（Task 0 校驗：值集無 deleted／terminated）。
TERMINAL_STATES = ('deleting',)

# provision「每帳號限一」的 advisory-lock namespace（比照 project_project 的
# _REACH_LOCK_NS reach-lock；任意固定 int、全 module 唯一即可）。'HAPA' = 0x48415041。
_HA_PROVISION_LOCK_NS = 0x48415041


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
    provision_partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Provision Owner（每帳號限一）',
        index=True,
        help='僅平台自動開通的「每帳號限一」服務（如 HA PaaS）填值，供 partial '
             'unique index 保證同一 partner 至多一份。一般服務留空（#336）。',
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

    # Support Project — Phase A M:N reverse (shared project_cloud_service_rel)
    project_ids = fields.Many2many(
        comodel_name='project.project',
        relation='project_cloud_service_rel',
        column1='cloud_service_id',
        column2='project_id',
        string='Support Projects',
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
    # #357: mcp-admin-core /api/auth/login 明文比對（auth/middleware.py:46）。
    # 平台 CS 建立時生成 → 注入 chart config.sensitive.admin_password → seed-config
    # initContainer 寫入 /data/config.json → GUI 登入。一次性顯示：建立/reset
    # 成功 response 回明文，後續 GET service-detail 走 _sanitize_helm_values 遮
    # config.sensitive.* → 平台不再提供明文（除 helm_values 同欄位 DB 內仍明文
    # 儲存供後續 helm upgrade 同步）。revealed_at 給 UI 顯示「已揭示」狀態，
    # 但不提供明文回取 API。
    admin_password = fields.Char(
        string='MCP Admin Password',
        readonly=True,
        groups='base.group_system',
        help='#357: Platform-generated admin password for the MCP admin GUI. '
        '明文只在 create/reset 成功 response 回傳一次，之後不再提供明文回取。'
        '實際值同步在 helm_values.config.sensitive.admin_password。',
    )
    admin_password_revealed_at = fields.Datetime(
        string='MCP Admin Password Revealed At',
        readonly=True,
        help='#357: 最近一次明文揭示給用戶的時間。UI 用此顯示狀態、'
        '無明文回取 API。',
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

    def init(self):
        """建 partial unique index：同一 provision_partner_id 至多一列（#336 每帳號
        限一的 DB backstop）。比照 skill.py init 的 partial-unique-index 慣例——Odoo
        原生 hook，每次 install / -u 自動跑、冪等（IF NOT EXISTS），涵蓋新裝與升級
        兩條路徑，免 migration + post_init 三件套。不加 state 條件——排除 deleting
        會放行 delete/create race（並發第二筆 INSERT commit 撞 IntegrityError →
        provision controller catch → 回既有）。"""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                woow_paas_cloud_service_provision_partner_uniq
            ON woow_paas_platform_cloud_service (provision_partner_id)
            WHERE provision_partner_id IS NOT NULL
        """)

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

    # #357 — admin_password 重置
    # ------------------------------------------------------------------ #

    def action_reset_admin_password(self):
        """#357 外科式 reset mcp admin password（保留 GUI 自填 connection/tools）。

        流程：
        1. 重生成 ``secrets.token_urlsafe(24)`` 強隨機密碼
        2. deep-merge 進 ``helm_values.config.sensitive.admin_password``
        3. 寫新密碼到 ``self.admin_password`` 欄位（同 n8n_owner_password 模式）
        4. 戳 ``admin_password_revealed_at = now``
        5. 呼叫 operator 新端點 ``POST /api/releases/{ns}/{name}/reset-config``
           → 內部：read-modify-write /data/config.json（保留其他 leaf）
                   → helm upgrade（Secret 對齊）
                   → rollout_restart_deployment（Recreate 觸發 ConfigStore cache 重建）
        6. 成功 → ``state='upgrading'``（讓 cron/前端 poll 觀察 pod）
                   **回傳明文密碼**（controller 包成 response.data）
        7. 失敗 → ``state='error'`` + error_message、**不回明文**

        chart seed-if-absent 在 /data/config.json 存在時 skip seed，純 helm upgrade
        無法讓新密碼生效；故走 operator 的 read-modify-write 走 surgical 路線。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )

        if not (self.helm_release_name and self.helm_namespace):
            raise UserError(
                "Service has no helm release yet — deploy first before resetting password."
            )

        # Generate a strong random password (~32 chars url-safe base64).
        new_password = secrets.token_urlsafe(24)

        # deep-merge into helm_values.config.sensitive.admin_password.
        values = json.loads(self.helm_values) if self.helm_values else {}
        values.setdefault('config', {}).setdefault('sensitive', {})['admin_password'] = new_password

        # Build payload for operator reset-config endpoint. chart Secret slot is
        # /seed/secret/config.secret.json → operator only needs the new
        # sensitive fragment; reuse_values=True keeps the rest of the chart values.
        operator_values = {'config': {'sensitive': {'admin_password': new_password}}}

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError("PaaS Operator not configured. Contact administrator.")

        try:
            client.reset_release_config(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                chart=self.template_id.helm_chart_name,
                version=self.template_id.helm_chart_version,
                values=operator_values,
                reuse_values=True,
            )
        except PaaSOperatorError as e:
            _logger.error(
                "reset-admin-password failed for service %s (id=%s): %s",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(
                f"Reset failed: {e.detail or e.message}"
            ) from e

        # Persist new state + persist the new password on the record.
        self.write({
            'helm_values': json.dumps(values),
            'admin_password': new_password,
            'admin_password_revealed_at': fields.Datetime.now(),
            'state': 'upgrading',
            'error_message': False,
        })
        return new_password

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
            cpu_limit, memory_limit, storage_limit = self._compute_ns_quota(
                self.workspace_id,
            )
            try:
                client.create_namespace(
                    namespace=helm_namespace,
                    cpu_limit=cpu_limit,
                    memory_limit=memory_limit,
                    storage_limit=storage_limit,
                    workspace_id=self.workspace_id.id or None,
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
            # MCP server cloud services (hermes/odoo/n8n) — single mcp-server
            # chart, persistence.storageClass. Explicit entries (default already
            # falls back here) to document intent and guard against default drift.
            'mcp-hermes': [
                ('persistence', 'storageClass'),
            ],
            'mcp-odoo': [
                ('persistence', 'storageClass'),
            ],
            'mcp-n8n': [
                ('persistence', 'storageClass'),
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
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """Scale down Helm release to 0 replicas."""
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_suspend service=%s name=%s state=%s sub=%s",
            self.id, self.name, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=0,
                )
                _logger.info(
                    "[CLOUD_SVC] _do_suspend scaled to 0 service=%s release=%s ns=%s",
                    self.id, self.helm_release_name, self.helm_namespace,
                )
            except Exception as e:
                _logger.warning(
                    "[CLOUD_SVC] _do_suspend scale failed service=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error', 'error_message': f'Suspend failed: {e}'})
                return

        self.write({
            'pre_suspend_state': self.state if self.state in ('running', 'error') else 'running',
            'state': 'suspended',
        })
        _logger.info("[CLOUD_SVC] _do_suspend done service=%s new_state=suspended", self.id)

    def _do_resume(self):
        """Scale up Helm release to restore replicas."""
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_resume service=%s name=%s state=%s pre_suspend=%s",
            self.id, self.name, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.scale_release(
                    self.helm_release_name, self.helm_namespace, replicas=1,
                )
                _logger.info(
                    "[CLOUD_SVC] _do_resume scaled to 1 service=%s release=%s ns=%s",
                    self.id, self.helm_release_name, self.helm_namespace,
                )
            except Exception as e:
                _logger.warning(
                    "[CLOUD_SVC] _do_resume scale failed service=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error', 'error_message': f'Resume failed: {e}'})
                return

        restore_state = self.pre_suspend_state or 'running'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })
        _logger.info(
            "[CLOUD_SVC] _do_resume done service=%s new_state=%s",
            self.id, restore_state,
        )

    def _compute_ns_quota(self, workspace):
        """#278 A1 權威值：以 workspace 內『非 deleting』存活 service 加總算 quota。

        domain 已排除 state='deleting' → 呼叫時機在 state 翻 deleting /
        super().unlink() 之後即天然反映刪除後存活集。回傳字串 quantity
        （與 operator NamespaceQuotaUpdateRequest 及既有 create_namespace 一致）。
        floor 為 8 vCPU / 8Gi / 100Gi（workspace 清空後 survivor 為空亦不 skip）。
        """
        Svc = self.env['woow_paas_platform.cloud_service'].sudo()
        live = Svc.search([
            ('workspace_id', '=', workspace.id),
            ('state', '!=', 'deleting'),
        ])
        vcpu = sum(live.mapped('allocated_vcpu'))
        ram = sum(live.mapped('allocated_ram_gb'))
        storage = sum(live.mapped('allocated_storage_gb'))
        return (
            str(max(vcpu * 3, 8)),
            f"{max(int(ram * 3), 8)}Gi",
            f"{max(storage * 3, 100)}Gi",
        )

    def _push_ns_quota(self, workspace, helm_namespace):
        """best-effort：重算並 PUT ns quota；operator 未設定 / 失敗只 log，
        絕不 abort 刪除。"""
        if not helm_namespace:
            return
        from ..services.paas_operator import get_paas_operator_client
        client = get_paas_operator_client(self.env)
        if not client:
            return
        cpu, mem, storage = self._compute_ns_quota(workspace)
        try:
            client.update_namespace_quota(helm_namespace, cpu, mem, storage)
        except Exception as e:
            _logger.warning(
                "quota recompute PUT failed for %s: %s", helm_namespace, e,
            )

    def _do_cleanup(self):
        """Helm uninstall — permanently remove the service.

        Order matters: unlink sidecar-backed user-scope MCP → helm uninstall
        → state='deleting'. Re-ordering opens a time window where mcp_server
        rows reference a torn-down sidecar URL (silent network failures in
        AI chat tool calls).

        The ``sidecar_backed`` filter is load-bearing: only sidecar-backed
        user-scope MCP follows the service lifecycle; user-provided external
        MCP rows have URLs independent of this service and must survive the
        teardown.
        """
        self.ensure_one()
        _logger.info(
            "[CLOUD_SVC] _do_cleanup service=%s name=%s release=%s ns=%s",
            self.id, self.name, self.helm_release_name, self.helm_namespace,
        )

        # Step 1 — unlink sidecar-backed user MCP first, before the sidecar
        # is torn down. M:N: project may also bind other cloud_services whose
        # sidecar-backed MCP must SURVIVE — narrow by URL prefix to clear only
        # rows pointing at *this* service's sidecar. External-URL user MCP
        # (sidecar_backed=False) stays. Services without a sidecar identity
        # fall back to the broad filter (URL-prefix unknown).
        url_prefix = self._sidecar_url_prefix()
        user_mcps = self._collect_sidecar_user_mcps(url_prefix)
        if user_mcps:
            user_mcps.sudo().unlink()
            _logger.info(
                "[CLOUD_SVC] _do_cleanup unlinked %s user-scope MCP server(s) "
                "for service=%s", len(user_mcps), self.id,
            )

        from ..services.paas_operator import get_paas_operator_client

        # Step 2 — helm uninstall (physical sidecar teardown).
        client = get_paas_operator_client(self.env)
        if client and self.helm_release_name and self.helm_namespace:
            try:
                client.uninstall_release(
                    self.helm_namespace, self.helm_release_name,
                    subdomain=self.subdomain,
                )
                _logger.info(
                    "[CLOUD_SVC] _do_cleanup helm uninstalled service=%s",
                    self.id,
                )
            except Exception as e:
                _logger.warning(
                    "[CLOUD_SVC] _do_cleanup helm uninstall failed service=%s: %s",
                    self.id, e,
                )
                self.write({
                    'state': 'error',
                    'error_message': f'Cleanup failed: {e}',
                })
                return

        # Step 3 — mark state so cron/UI know teardown is in progress.
        self.write({'state': 'deleting'})

        # Step 4 — #278 A1：本 record 已 state=deleting、被 domain 排除，
        # 以存活集重算 ns quota 並權威覆寫（best-effort，不影響刪除）。
        self._push_ns_quota(self.workspace_id, self.helm_namespace)

    def _cleanup_helm(self):
        """Idempotent helm uninstall for the direct-unlink / workspace-cascade
        path (F-ENV-5).

        Returns True when the release is gone — physically uninstalled,
        already-absent (operator 404, e.g. controller already ran its own
        uninstall before calling unlink), or operator unconfigured (dev/test,
        no live release to tear down). Returns False ONLY when a configured
        operator was asked to uninstall and genuinely failed (release still
        live). Does NOT write state — callers decide their own policy
        (``_do_cleanup``→state='error', ``unlink``→raise). KEEPS
        ``helm_release_name`` on failure so a retry still has the anchor
        (analogous to SecurityAccess keeping ``tunnel_id``); clearing it would
        make the helm release un-findable = a permanent orphan
        (cf-ns-orphan-no-account-gc).
        """
        self.ensure_one()
        if not (self.helm_release_name and self.helm_namespace):
            return True
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
        )
        client = get_paas_operator_client(self.env)
        if not client:
            return True  # operator unconfigured — no live release to tear down
        try:
            client.uninstall_release(
                self.helm_namespace, self.helm_release_name,
                subdomain=self.subdomain,
            )
            return True
        except PaaSOperatorError as e:
            if getattr(e, 'status_code', None) == 404:
                return True  # already gone — idempotent success
            _logger.warning(
                "[CLOUD_SVC] _cleanup_helm failed service=%s: %s — kept for retry",
                self.id, e,
            )
            return False
        except Exception as e:
            _logger.warning(
                "[CLOUD_SVC] _cleanup_helm failed service=%s: %s — kept for retry",
                self.id, e,
            )
            return False

    def write(self, vals):
        """#279 Path B: a CloudService's ``workspace_id`` / ``project_ids`` binding
        change shifts which workspaces its bound projects can reach, so re-render the
        agent isolation legs. project.write (Path A) covers ``cloud_service_ids``
        changes made from the project side; this covers changes made from the service
        side, which fire no project hook even though ``project.workspace_ids``
        recomputes.
        """
        # [F: revoke-txn low] A deployed CS's namespace identity (paas-ws-{hash}) is
        # fixed at create; retargeting workspace_id would leave the pod in the OLD ns
        # while agent egress is re-pointed at the new workspace → silent reach break to
        # the still-bound CS. Reject reassignment on a live-namespace service.
        if 'workspace_id' in vals:
            for cs in self:
                if (cs.helm_namespace and cs.workspace_id.id
                        and vals['workspace_id'] != cs.workspace_id.id):
                    raise UserError(
                        "不可變更已部署 CloudService 的 workspace（命名空間身分固定）。"
                    )
        tracked = 'workspace_id' in vals or 'project_ids' in vals
        projects = self.mapped('project_ids') if tracked else self.browse()
        old = {p.id: set(p.workspace_ids.ids) for p in projects}
        res = super().write(vals)
        if tracked:
            # Union of projects bound before AND after (a project_ids write can add or
            # drop bindings); each reconciles against its own pre-write reach set.
            for project in (projects | self.mapped('project_ids')):
                project._reconcile_agent_runtime_reach(old.get(project.id, set()))
        return res

    @api.model
    def _backfill_reach_isolation_p5(self):
        """#279 P5 one-time backfill (invoked from migration 18.0.1.0.42).

        (a) CloudService namespaces — apply the O4 baseline NP bundle + re-stamp
            workspace-id/env labels (leg-1 egress selector + baseline NP4 key on them)
            on every CS namespace created before O4/O1.
        (b) Agent-runtime namespaces — re-stamp the env label (+ reach-ws) so a legacy
            agent ns matches NP4's env predicate once #280 removes the chart from-less
            (else the running agent silently loses reach to its bound CS).

        Idempotent, re-runnable, per-ns logged, best-effort (one failure never aborts
        the sweep — a re-run + the enforcing cron converge the rest). MUST run before
        #280. Returns a per-leg ok/fail tally.
        """
        from ..services.paas_operator import get_paas_operator_client
        from ..services.agent_runtime_client import get_agent_runtime_client
        ns_client = get_paas_operator_client(self.env)
        agent_client = get_agent_runtime_client(self.env)

        cs_done = cs_fail = ar_done = ar_fail = 0

        # (a) CloudService namespaces — addressed by their stored helm_namespace.
        if ns_client:
            services = self.sudo().search([
                ('helm_namespace', '!=', False),
                ('workspace_id', '!=', False),
            ])
            for svc in services:
                try:
                    ns_client.backfill_baseline_netpol(
                        svc.helm_namespace, svc.workspace_id.id)
                    cs_done += 1
                except Exception as exc:
                    cs_fail += 1
                    _logger.warning(
                        "P5 backfill: CS ns %s failed: %s", svc.helm_namespace, exc)
        else:
            _logger.warning("P5 backfill: PaaS operator unconfigured — CS leg skipped")

        # (b) Agent-runtime namespaces — addressed by hash (operator derives the ns).
        if agent_client:
            Session = self.env['woow_paas_platform.agent_session'].sudo()
            sessions = Session.search([
                ('project_namespace_hash', '!=', False),
                ('project_id.use_agent_runtime', '=', True),
            ])
            for session in sessions:
                try:
                    agent_client.restamp_labels(
                        session.project_namespace_hash,
                        reach_workspace_ids=session.project_id.workspace_ids.ids)
                    ar_done += 1
                except Exception as exc:
                    ar_fail += 1
                    _logger.warning(
                        "P5 backfill: agent ns (hash %s) failed: %s",
                        session.project_namespace_hash, exc)
        else:
            _logger.warning(
                "P5 backfill: agent-runtime operator unconfigured — agent leg skipped")

        _logger.info(
            "P5 backfill done: CS %s ok / %s fail; agent %s ok / %s fail",
            cs_done, cs_fail, ar_done, ar_fail)
        return {"cs_done": cs_done, "cs_fail": cs_fail,
                "ar_done": ar_done, "ar_fail": ar_fail}

    @api.model
    def _allocated_vals_from(self, template, plan):
        """#332：規格顯示值——plan cs_* 優先、0 值 fallback template.min_*。"""
        return {
            'allocated_vcpu': plan.cs_vcpu or template.min_vcpu,
            'allocated_ram_gb': plan.cs_ram_gb or template.min_ram_gb,
            'allocated_storage_gb': plan.cs_storage_gb or template.min_storage_gb,
        }

    @api.model
    def _apply_plan_spec_overlay(self, values, template, plan):
        """#332 runtime overlay：把選定 plan 的 cs_* 疊進 helm values。

        刻意不改 template seed（避開 sticky noupdate）；cs_* 為 0 的維度一律
        不碰＝與現狀 bit-for-bit 相同。就地疊加並回傳同一 dict。

        workers／memory 用精確字串替換（裁定② 2026-07-12 by Eugene）：seed 內
        為 `echo 'workers = 0'`（非裸行），故 body.replace + 逐 marker「全 script
        合計恰 1 處」的 count 斷言 fail-loud——base 漂移（0 或多處）即 raise、
        不得靜默 no-op。
        """
        if plan.cs_vcpu:
            res = values.setdefault('resources', {})
            for key in ('requests', 'limits'):
                res.setdefault(key, {})['cpu'] = str(plan.cs_vcpu)
        if plan.cs_ram_gb:
            mem = (f'{int(plan.cs_ram_gb)}Gi'
                   if float(plan.cs_ram_gb).is_integer()
                   else f'{int(plan.cs_ram_gb * 1024)}Mi')
            res = values.setdefault('resources', {})
            for key in ('requests', 'limits'):
                res.setdefault(key, {})['memory'] = mem
        if plan.cs_storage_gb:
            values.setdefault('persistence', {})['size'] = f'{plan.cs_storage_gb}Gi'
        scripts = values.get('customPostInitScripts')
        if plan.cs_odoo_workers and scripts:
            n = plan.cs_odoo_workers
            replacements = [('workers = 0', f'workers = {n}')]
            if plan.cs_ram_gb:
                # workers>0 ⟹ cs_ram_gb>0（A6-1 constraint 保證），per-worker
                # memory 上限＝官方建議比例（hard 0.85／soft 0.70）。
                ram_bytes = int(plan.cs_ram_gb * 1024 ** 3)
                replacements += [
                    ('limit_memory_hard = 0',
                     f'limit_memory_hard = {int(ram_bytes * 0.85 / n)}'),
                    ('limit_memory_soft = 0',
                     f'limit_memory_soft = {int(ram_bytes * 0.70 / n)}'),
                ]
            for marker, repl in replacements:
                total = sum(body.count(marker) for body in scripts.values())
                if total != 1:
                    raise ValueError(
                        "customPostInitScripts 內 '%s' 預期恰 1 處、實得 %d"
                        "（base 已漂移，fail-loud 不靜默 no-op）" % (marker, total)
                    )
                for name, body in list(scripts.items()):
                    if marker in body:
                        scripts[name] = body.replace(marker, repl)
        return values

    @api.model
    def _find_active_for_partner(self, template, partner):
        """回傳該 partner 名下、該 template 的活躍實例（排除刪除終態）。

        供 max_per_partner enforcement 與開通 API 冪等（#337）共用。
        排除的刪除終態＝module-level TERMINAL_STATES（Task 0 校驗＝('deleting',)）。
        """
        return self.sudo().search([
            ('template_id', '=', template.id),
            ('workspace_id.owner_id.partner_id', '=', partner.id),
            ('state', 'not in', list(TERMINAL_STATES)),
        ])

    @api.model_create_multi
    def create(self, vals_list):
        """#336：每帳號實例上限 enforcement（#345 補並發防護）。

        放 model create（controller、#337 開通 API、shell 等所有入口共用），
        controller 不另做早驗——避免同一規則兩份漂移。計數口徑＝該 partner 名下、
        該 template、state 不在刪除終態的 CloudService 數。enforcement 對所有入口
        （含測試 su env）一律生效、無 su/group 豁免（否則 TransactionCase 假綠）。

        #345 並發防護：原為 check-after-act（super().create() 後才 count、無鎖），
        兩並發 create 同 (template, partner) 各見自身 count=1 雙雙放行 → 可繞上限。
        改為 count 前先取 (template_id, partner_id) 為鍵的 advisory xact lock 序列化
        （比照 provision 端點 _HA_PROVISION_LOCK_NS 慣例，見 ha_paas.py）。順序
        INSERT→lock→count 仍正確：READ COMMITTED 下，輸家取得鎖後的 count SELECT
        用新 snapshot，看得到贏家已 commit 的列 → count>limit 觸發 rollback（撤回
        自身 INSERT）。xact lock 於本 tx 結束（commit/rollback）自動釋放。
        max_per_partner=0 的常態不進迴圈＝零鎖開銷。多筆 vals：先收集 distinct
        (template, partner) 排序後依序取鎖，消除跨組交錯的 lock-ordering 死鎖。

        DB unique-index 無法表達「每 (partner, template) ≤N」的一般 N 上限
        （partial unique index 只能保證「至多一列」，見 provision_partner_id），
        故泛化上限只能靠 advisory lock；單連線 TransactionCase 無法真並發，既有
        功能測試僅證非回歸、鎖的競態正確性靠與 provision 先例一致性 + code review。
        """
        records = super().create(vals_list)
        # 收集需 enforcement 的 (template_id, partner_id)，排序去重後依序取鎖：
        # 排序消除多筆跨組的 lock-ordering 死鎖；max_per_partner=0 常態集合為空、零開銷。
        lock_pairs = sorted({
            (rec.template_id.id, rec.workspace_id.owner_id.partner_id.id)
            for rec in records
            if rec.template_id.max_per_partner and rec.workspace_id.owner_id
        })
        for tpl_id, partner_id in lock_pairs:
            self.env.cr.execute(
                'SELECT pg_advisory_xact_lock(%s, %s)', (tpl_id, partner_id))
        for rec in records:
            limit = rec.template_id.max_per_partner
            if limit and rec.workspace_id.owner_id:
                partner = rec.workspace_id.owner_id.partner_id
                count = len(self._find_active_for_partner(rec.template_id, partner))
                if count > limit:
                    raise ValidationError(_(
                        '「%(tpl)s」每個帳號最多 %(limit)s 個實例，'
                        '您已達上限。', tpl=rec.template_id.name, limit=limit))
        return records

    def unlink(self):
        """Cleanup sidecar-backed user-scope MCP before deleting the service.

        Catches every unlink path — controllers calling ``service.unlink()``
        directly after their own helm uninstall, cron taking down a stuck
        service, manual ORM ops — without forcing callers to remember to
        call ``_do_cleanup`` first. ``project.cloud_service_id`` is
        ``ondelete='set null'``, so a naked ``unlink()`` would leave
        ``project_id`` populated on sidecar-backed MCP rows while their
        backing sidecar is gone — zombie URL. ``_do_cleanup`` does the same
        step but only triggers on the lifecycle path; this override is the
        catch-all guard.

        The ``sidecar_backed`` filter keeps user-provided external MCP rows
        intact — their URL is physically independent of this service.
        Idempotent against ``_do_cleanup``: when both run, the second finds
        the matching recordset already empty.

        M:N (Phase A): each cloud_service narrows by its own URL prefix so
        tearing down service A does not collateral-unlink service B's
        sidecar-backed MCP rows on a project that binds both.

        #279 Path B (revoke): capture bound projects BEFORE delete; once the record
        (and its M:N rows) are gone their ``workspace_ids`` recompute — dropping
        workspaces only reachable via this service — so each is reconciled as a pure
        revoke (best-effort; the enforcing cron is authoritative).
        """
        # #318: 在 super().unlink() 前捕獲仍 in_progress 的關聯訂閱。
        # subscription_id 是 ondelete='set null'（只保護反向），刪 service
        # 不會動訂閱 → 孤兒訂閱會被計費 cron 續扣。super().unlink() 後
        # record 離場但訂閱 record 存活，逐一結算（取消＋按比例退點）。
        # 僅結算 in_progress；lifecycle-cleanup / dunning 已取消者跳過。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        affected = self.mapped('project_ids')
        old = {p.id: set(p.workspace_ids.ids) for p in affected}
        # #278 A1：pre-delete 捕獲 (workspace, ns)，super().unlink() 後這些
        # record 離場、workspace_ids 重算，即以存活集重算 ns quota。
        quota_targets = [
            (s.workspace_id, s.helm_namespace)
            for s in self if s.helm_namespace
        ]
        for service in self:
            # F-ENV-5 helm fail-loud guard: the helm release / namespace must
            # never outlive the record. The workspace-cascade routing path
            # (Workspace.unlink → service_ids.unlink) relies on THIS to tear
            # down helm, since the DB FK cascade bypasses Python unlink. The
            # controller delete path that already uninstalled helm hits an
            # idempotent 404 here (treated as success). On a real failure the
            # row + release are kept and the deletion is aborted for retry.
            if service.helm_release_name and service.helm_namespace:
                if not service._cleanup_helm():
                    raise UserError(
                        "Helm uninstall failed; release '%s' is still live in "
                        "namespace '%s' (billed compute / leaked namespace). "
                        "The service record was kept so the deletion can be "
                        "retried. Please try again shortly." % (
                            service.helm_release_name, service.helm_namespace,
                        )
                    )
            user_mcps = service._collect_sidecar_user_mcps(
                service._sidecar_url_prefix()
            )
            if user_mcps:
                user_mcps.sudo().unlink()
                _logger.info(
                    "[CLOUD_SVC] unlink() unlinked %s user-scope MCP server(s) "
                    "for service id=%s", len(user_mcps), service.id,
                )
        res = super().unlink()
        # #318: teardown 成功後才結算訂閱（取消＋按比例退點）。放在
        # super().unlink() 之後，故 helm fail-loud raise 時不會執行（全交易
        # rollback、record 保留、訂閱不動）。訂閱是獨立 record，不隨 service
        # 刪除而消失。
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Service deleted')
        for workspace, helm_namespace in quota_targets:
            self._push_ns_quota(workspace, helm_namespace)
        for project in affected.exists():
            project._reconcile_agent_runtime_reach(old.get(project.id, set()))
        return res

    def _collect_sidecar_user_mcps(self, url_prefix):
        """Return sidecar-backed user-scope MCP rows attached via M:N
        projects whose URL belongs to this service's sidecar (or all of
        them when no URL prefix is available, e.g. pre-deploy fixtures).
        """
        self.ensure_one()
        candidates = self.project_ids.mapped('mcp_server_ids').filtered(
            lambda s: s.scope == 'user' and s.sidecar_backed
        )
        if url_prefix is None:
            return candidates
        return candidates.filtered(
            lambda s: (s.url or '').startswith(url_prefix)
        )

    def _sidecar_url_prefix(self):
        """Return the URL prefix used by sidecar-backed user-scope MCP rows
        whose backing sidecar is *this* cloud_service, or ``None`` if this
        service has no sidecar identity (no helm_release_name/namespace).

        ``None`` lets callers fall back to the legacy filter that prunes any
        sidecar-backed user-scope row — required so services created without
        helm metadata (older fixtures, pre-deploy state) still drop their
        MCPs on teardown.
        """
        self.ensure_one()
        if not self.helm_release_name or not self.helm_namespace:
            return None
        return (
            f'http://{self.helm_release_name}-mcp.'
            f'{self.helm_namespace}.svc.cluster.local'
        )
