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

    # VM 型服務的開通進度（#423 W5a，design §7.4）。
    #
    # 為何需要：VM 首次開通要把一整顆磁碟映像 import 進 PVC，耗時遠長於容器
    # 型的 pull image。沒有進度時服務只是停在 `deploying`，租戶（與維運）
    # 看不出是「正在跑」還是「卡住了」。CDI 原生提供百分比，poll 時順手帶回。
    #
    # 這**不是**狀態機的一部分：它純供顯示，就緒判定完全不看它
    # （DataVolume 查詢失敗只會讓進度消失，不該擋住一台已 Ready 的 VM）。
    import_progress = fields.Char(
        string='Import Progress',
        help='VM 磁碟映像的 import 進度（例 "42.31%"）；僅供顯示，不參與就緒判定',
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
            # Upgrade in-place — preserves PVCs and data.
            # #411: 走全量 values 組裝 helper（與 fresh install 同一條管線）＋
            # reset_values 語意（chart values.yaml 為 base，全量 values 疊上）。
            # 既存 release 升級不再以 reuse_values 沿用舊 coalesced values，
            # chart 升版新增的預設能自然補位。見 docs/decisions/patterns/
            # helm-reset-values-unified-pipeline.md。
            template = self.template_id
            values = self._assemble_deploy_values()['values']
            sensitive_values = self._build_mcp_sensitive_values()
            try:
                release_info = client.upgrade_release(
                    namespace=self.helm_namespace,
                    release_name=self.helm_release_name,
                    chart=template.helm_chart_name,
                    version=template.helm_chart_version,
                    values=values,
                    sensitive_values=sensitive_values or None,
                    reset_values=True,
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

    def _reset_admin_password(self):
        """#357 外科式 reset mcp admin password（保留 GUI 自填 connection/tools）。

        密碼只短暫存在記憶體與 ``cloud_service_secret``；不寫回 ``helm_values``。
        先用 current user 的存取規則驗證 service，再 sudo 讀寫該 service 的 secret。

        #357 C05：**必須維持 ``_`` 開頭（private）**。本方法尾端回傳明文新密碼，
        而授權（workspace role=admin）只存在 controller 層。命名為 ``action_*``
        時它是 RPC 可達的：任何 workspace 成員（含 role=user/guest——cloud_service
        的 ACL 對 group_user 全開、ir.rule 只驗「是該 workspace 成員」）可繞過
        controller 的 role gate 直接 ``POST /web/dataset/call_kw``，RPC 回傳值
        即明文新密碼，同時把真 admin 的密碼換掉（提權 + DoS）。這是「controller
        filter 擋不住 ORM 直呼」的實例，見 docs/decisions/patterns/
        paas-workspace-member-acl-tightening.md §Controller filter 不夠。

        Odoo 18 由 ``get_public_method``（odoo/service/model.py:43
        ``if name.startswith('_') ... raise AccessError``）擋下 ``_`` 開頭方法的
        遠端呼叫，**兩條** RPC 路徑均已覆蓋：JSON-RPC 走
        odoo/addons/web/controllers/dataset.py:34（call_kw 與 call_button）、
        XML-RPC 走 odoo/service/model.py:71（execute_cr）。
        ⚠️ 勿照 17.0 經驗判斷：17.0 用的是 ``check_method_name``，該函式在 18.0
        雖仍定義於 models.py:155 但**已無任何呼叫點＝遺留死碼**（本 repo 是 18.0，
        於 18.0 容器內 grep 實證）。

        改回 public 命名（或加上會被 RPC 觸及的 wrapper）即重新打開該洞，
        ``TestResetAdminPasswordAuthorization`` 會紅。
        """
        self.ensure_one()
        from ..services.paas_operator import (
            get_paas_operator_client,
            PaaSOperatorError,
            PaaSOperatorTimeoutError,
        )

        # Hold a row lock across the operator saga.  A second reset waits for
        # the first request to commit, then observes state='upgrading' below
        # instead of interleaving Secret/config rollback with a newer reset.
        #
        # #357 C11: this serialises the success path only. On a client timeout
        # the lock is released while the operator's saga keeps running — it is a
        # sync handler, so the client hanging up cancels nothing — and state
        # stays 'running' because a timeout stores nothing. The retry message
        # below therefore asks the tenant to wait rather than press again at
        # once, which would race the in-flight saga.
        self.env.cr.execute(
            "SELECT id FROM woow_paas_platform_cloud_service WHERE id = %s FOR UPDATE",
            [self.id],
        )
        self.invalidate_recordset()
        if self.state == 'upgrading':
            # #357 C11: a successful reset stamps 'upgrading', so this is the
            # message a tenant hits when they reset, lose the password, and
            # immediately try again. The generic text below reads as breakage;
            # _cron_check_deploying_services polls every minute and flips it
            # back to 'running', so say that instead.
            raise UserError(
                "Service is finishing an update (usually under a minute). "
                "Please wait and try again."
            )
        if self.state != 'running':
            raise UserError(
                "Service must be running before resetting its admin password."
            )

        if not (self.helm_release_name and self.helm_namespace):
            raise UserError(
                "Service has no helm release yet — deploy first before resetting password."
            )

        # #357 C03：不檢查既有 secret。reset 是「重新生成並下發」而非「讀舊密碼」，
        # 舊 secret 缺席（legacy 服務建立時平台尚未供給 admin_password）不是錯誤——
        # 這正是租戶要 reset 的情境。_set_admin_password_secret 會 create-or-write。

        # C20: the single generation point — never inline token_urlsafe here.
        new_password = self._generate_admin_password()
        # #411: values 走全量組裝 helper（與 deploy/redeploy/reconfigure 同一條
        # 管線）。此路徑帶 template.helm_chart_version，reset password 會夾帶
        # chart 升版；改 reset_values 語意讓 chart 新增預設補位，不再以
        # --reuse-values 沿用舊 coalesced values（同一 bug class，見 #411）。
        values = self._assemble_deploy_values()['values']
        operator_sensitive_values = self._build_mcp_sensitive_values(new_password)

        # #461 可診斷性：reset 夾帶 chart 升版時，operator 的 helm upgrade 會真的
        # 等新 pod（pod template 變了），可能撞上 --timeout 15s 而 fail-safe 失敗。
        # 這種情境下「重按」**不會**收斂（每次都夾帶同一次升版），正解是先 redeploy
        # 讓 chart 升版落地。租戶側引導記在 F-461-2；這行是維運判讀的線索。
        target_chart_version = self.template_id.helm_chart_version
        # 兩邊都要有值才算「漂移」：helm_chart_version 為空代表平台**不知道**線上是
        # 哪一版（legacy 服務），印「False -> 0.1.5」是誤導而非線索。
        if (target_chart_version and self.helm_chart_version
                and target_chart_version != self.helm_chart_version):
            _logger.info(
                "reset-admin-password for service %s (id=%s) also carries a chart "
                "upgrade %s -> %s; if it fails on timeout, redeploy first (F-461-2)",
                self.name, self.id, self.helm_chart_version, target_chart_version,
            )

        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError("PaaS Operator not configured. Contact administrator.")

        try:
            client.reset_release_config(
                namespace=self.helm_namespace,
                release_name=self.helm_release_name,
                chart=self.template_id.helm_chart_name,
                version=self.template_id.helm_chart_version,
                values=values,
                sensitive_values=operator_sensitive_values or None,
                reset_values=True,
                # #449：告訴 operator 這個 chart 把密碼放在哪。**供給面兩者相同**
                # （都是 config.sensitive.admin_password，上一行的 sensitive_values
                # 不因載體而異）；差別只在 operator 要不要 exec patch
                # /data/config.json。hermes-agent / open-design 沒有那個檔，走預設
                # 值必 500（#449）。判別器＝template 欄位，不得在此自建 slug／chart
                # allowlist——與 has_admin_gui 同一紀律（#357 C01）。
                admin_password_carrier=self.template_id.admin_password_carrier,
            )
        except PaaSOperatorTimeoutError as e:
            # #461 C11′ (commit-on-secret) — this branch survived the async
            # rewrite, but its *reason* changed. Read this before touching it.
            #
            # OLD C11: the DB may only hold a password that really took effect,
            # because the operator waited for the rollout and rolled the Secret
            # BACK when it failed. Storing on an ambiguous outcome could therefore
            # record a password the cluster had already discarded.
            #
            # C11′: the operator no longer waits for the rollout, so there is no
            # rollback after 2xx either. The commit point moved to "the operator
            # answered 2xx" ⇒ the new password IS the authoritative value in the
            # chart Secret / config.json and nothing reverts it. Everything that
            # can still fail happens BEFORE that answer and is compensated (helm
            # --atomic rolls itself back; a failed config.json patch restores the
            # backup) ⇒ on any error the cluster still holds the OLD password ⇒
            # not storing is still exactly right.
            #
            # What this branch now means: the budget ladder makes the platform's
            # 75s strictly larger than the operator's own 65s hard cap
            # (RESET_HARD_CAP_SECONDS), so a client timeout is no longer the
            # expected outcome of a slow-but-healthy reset — it means the operator
            # blew its own ceiling, or the 2xx was lost in flight. The cluster may
            # then hold a password we never stored, so "Reset failed" would still
            # be a lie the tenant acts on.
            #
            # Recovery is unchanged and does not need the old password: state stays
            # 'running' (nothing was written), so the tenant can press Reset again;
            # it mints a fresh password and re-commits. Deliberately no pending-
            # password column — storing an unconfirmed credential buys only this
            # narrow window and costs a second live secret plus a mid-request
            # commit that would break this handler's row lock (design §4.4).
            _logger.error(
                "reset-admin-password timed out for service %s (id=%s): %s — "
                "operator saga may still be in flight; new password NOT stored",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError(
                "Reset status unknown — the request timed out before the service "
                "confirmed it. The reset may still have taken effect, so the "
                "password was not saved. Wait a few minutes, then press Reset "
                "again to issue a new password."
            ) from e
        except PaaSOperatorError as e:
            _logger.error(
                "reset-admin-password failed for service %s (id=%s): %s",
                self.name, self.id, e.detail or e.message,
            )
            raise UserError("Reset failed. Check service status and retry.") from e

        self._set_admin_password_secret(new_password)

        self.write({
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

        # #411: values 組裝走單一真相源 helper（與 redeploy / reconfigure /
        # reset admin password 共用同一條全量管線）。expose_config 亦由 helper
        # 重算，取代 create 時存在 deploy_params 的等價值（收斂雙寫）。
        assembled = self._assemble_deploy_values()
        merged_values = assembled['values']
        expose_config = assembled['expose_config']

        # sensitive_values 與 mcp_api_key 屬本 install 路徑專屬（非部署 values），
        # 另外組。mcp_api_key 首裝 patch sidecar 用，仍自 deploy_params 取。
        sensitive_values = self._build_mcp_sensitive_values()
        params = json.loads(self.deploy_params) if self.deploy_params else {}
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
                sensitive_values=sensitive_values or None,
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

    # ------------------------------------------------------------------ #
    #  #411 — 全量 values 組裝（單一真相源）                              #
    # ------------------------------------------------------------------ #

    def _assemble_deploy_values(self):
        """組裝部署用的全量「非敏感」helm values ＋ expose_config（單一真相源）。

        Fresh install 與既存 release 升級**共用這一條**組裝管線，讓 helm 在
        ``reset_values`` 語意下（chart 自身 values.yaml 為 base）每次都把平台
        管理的所有值補齊 —— chart 升版新增的預設能自然 coalesce 生效，終結
        ``reuse_values`` bug class（既存 release 升級路徑靜默漏掉 chart 新預設；
        見 #411 / #413 與 docs/decisions/patterns/helm-reset-values-unified-pipeline.md）。

        組裝順序（後者疊在前者上）：

        1. ``svc.helm_values`` —— template 預設（``template.helm_default_values``）
           ＋ user config ＋ MCP api key ＋ plan overlay，於 service create 時已
           merge 定案（controllers/paas.py ``_deploy_service``）。此處先剝掉
           ``config.sensitive`` 子樹（``_strip_mcp_sensitive_values``）。
        2. storage_class injector（PVC 生命週期）
        3. image_pull_secrets injector（私有 registry）
        4. allowed_origins injector（open-design 瀏覽器 Origin 白名單，#380）
        5. dashboard_public_url injector（hermes-agent MCP OAuth 回呼，#424①）
        6. apiserver_cidrs injector（open-design／hermes-agent console egress，#404）
        7. odoo addon selection（initContainer + addons_path）

        **純唯讀、不寫 DB、不呼叫 operator** —— 盤點腳本可安全 dry-run 呼叫，
        取 ``['values']`` 去比對 ``helm get values <rel>``（stored、非敏感）。
        ``sensitive_values``（admin_password）不在此組：它是敏感值、且 stored
        release values 本就不含它，由各呼叫點自組（reset 路徑帶新密碼）。

        Returns:
            dict: {
                'values': 全量非敏感 helm values（dict）,
                'expose_config': ingress expose dict 或 None（Cloudflare tunnel；
                                僅 fresh install 用，upgrade endpoint 無 expose
                                參數，升級路徑忽略此鍵）,
            }
        """
        self.ensure_one()
        template = self.template_id
        values = self._strip_mcp_sensitive_values(
            json.loads(self.helm_values) if self.helm_values else {}
        ) or {}

        # Inject StorageClass for PVC lifecycle management
        storage_class = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete'
        )
        if storage_class:
            values = self._inject_storage_class(
                values, template.slug, storage_class,
            )

        # Inject imagePullSecrets for private registry access
        registry_secret = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.registry_secret_name', 'paas-registry'
        )
        if registry_secret:
            values = self._inject_image_pull_secrets(
                values, registry_secret,
            )

        # Inject the service's own public origin for charts that gate on the
        # browser Origin header (open-design daemon 403s a public Origin unless
        # whitelisted verbatim; the chart never learns the tenant host — the
        # platform exposes services via a Cloudflare tunnel, not the chart's
        # ingress). No-op for other templates. See #380.
        if self.subdomain:
            paas_domain = self.env['ir.config_parameter'].sudo().get_param(
                'woow_paas_platform.paas_domain', 'woowtech.io',
            )
            public_base_url = f"https://{self.subdomain}.{paas_domain}"
            values = self._inject_allowed_origins(
                values, template.slug, public_base_url,
            )
            # Inject the service's own public URL for charts whose app must hand
            # that URL to a third party (hermes-agent dashboard is the OAuth
            # redirect_uri target for external MCP servers). No-op elsewhere.
            # See #424①.
            values = self._inject_dashboard_public_url(
                values, template.slug, public_base_url,
            )

        # Inject the kube-apiserver egress allowlist for charts whose pods run
        # kubectl from under a tenant NetworkPolicy (open-design console).
        # No-op for other templates. See #404.
        apiserver_cidrs = self._get_apiserver_cidrs()
        if apiserver_cidrs:
            values = self._inject_apiserver_cidrs(
                values, template.slug, apiserver_cidrs,
            )

        # #423 W5a：VM 型的 runStrategy 必須與服務狀態同源。
        #
        # 為什麼一定要寫進 values：suspend/resume 走的是 operator 的外科式
        # `kubectl patch vm`（不動 helm release），所以 live 物件與 helm 的
        # stored manifest 會漂移。下一次 helm upgrade 若 render 出 chart 預設
        # 的 `Always`，就會**把停權中（欠費）的 VM 喚醒**——沉默失效，且會演
        # 變成計費爭議。把當前狀態餵進 values 讓兩條路徑同源。
        # 守線測試：src/tests/test_haos_suspend_resume_runstrategy.py
        if template.workload_type == 'vm':
            values.setdefault('vm', {})['runStrategy'] = (
                'Halted' if self._is_halted() else 'Always'
            )

        # Inject Odoo addon selection (initContainer + addons_path)
        if self.odoo_addon_ids and template.slug == 'odoo':
            image_vals = values.get('image', {})
            registry = image_vals.get('registry', 'docker.io')
            repo = image_vals.get('repository', '')
            tag = image_vals.get('tag', 'latest')
            image_ref = f"{registry}/{repo}:{tag}"
            addon_values = self._build_odoo_addon_helm_values(image_ref)
            # Deep-merge addon values into the assembled values
            for key, val in addon_values.items():
                if key in values and isinstance(values[key], dict) and isinstance(val, dict):
                    values[key].update(val)
                else:
                    values[key] = val

        return {
            'values': values,
            'expose_config': self._build_expose_config(),
        }

    def _build_expose_config(self):
        """Cloudflare tunnel ingress expose 設定（fresh install 用）。

        Template 開 ingress 且 service 有 subdomain 才 expose；與 service create
        時 (controllers/paas.py ``_deploy_service``) 及 action_redeploy fresh 分支
        的算法一致，收斂為單一真相源。upgrade endpoint 無 expose 參數 —— ingress
        於首裝建立後升級不重設（Cloudflare tunnel 已在），故升級路徑不用此值。
        """
        self.ensure_one()
        if self.template_id.ingress_enabled and self.subdomain:
            return {'enabled': True, 'subdomain': self.subdomain}
        return None

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
            # #423 W5a: HAOS（VM 型）。chart 的 DataVolume 用扁平
            # persistence.storageClass，與 mcp-* 同鍵名，故預設 fallback 本來
            # 就會命中——列出顯式 entry 是記錄意圖並防 default 漂移（同 mcp-*
            # 的既有做法）。
            # ⚠ chart 端此鍵 required：woow-k3s 有兩個 default StorageClass
            # （local-path 與 longhorn），留空會讓 DataVolume 落點不確定，
            # 可能掉到 node-local 的 local-path（節點故障即資料失聯）。故
            # chart 讓空值直接 render 失敗，平台這邊也絕不可傳空字串。
            'haos': [
                ('persistence', 'storageClass'),
            ],
            # #424①: hermes-agent（open-design 派生 chart）走扁平
            # persistence.storageClass —— 契約明訂這是平台注入點，per-PVC 的
            # persistence.data.storageClass 留空時由 chart 繼承扁平鍵（與 open-design
            # 同款）。與 mcp-* 同理列出顯式 entry：記錄意圖並防 default 漂移。
            # 若 Phase A Step 0 翻案要開內建 DB，這裡必須同 PR 補
            # ('postgresql', 'persistence', 'storageClass') /
            # ('redis', 'persistence', 'storageClass') —— 那兩顆 PVC 不在扁平鍵的
            # 繼承鏈上。lock-step 由 src/tests/test_hermes_agent_seed_424.py 的
            # test_db_disabled_lockstep_storage_class 守。
            'hermes-agent': [
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

    @staticmethod
    def _inject_allowed_origins(values, template_slug, origin):
        """Inject the service's own public origin into charts that gate on the
        browser Origin header.

        The open-design daemon rejects a PUBLIC tenant Origin unless it is
        whitelisted verbatim (its same-origin fallback only trusts
        loopback/private-LAN hosts), and a bare "*" CRASHES it — so the chart
        cannot self-default this (it never learns the tenant host: the platform
        exposes services via a Cloudflare tunnel, not the chart's ingress). The
        platform, which alone knows the host, supplies it here. No-op for charts
        without such a value path. See woow-paas-charts open-design 0.1.1 +
        issue #380.

        Args:
            values: Helm values dict (modified in-place and returned)
            template_slug: Template identifier (e.g., 'open-design')
            origin: The service's own origin, e.g. "https://<sub>.<domain>"

        Returns:
            Modified values dict
        """
        path_map = {
            'open-design': [('config', 'allowedOrigins')],
        }
        paths = path_map.get(template_slug)
        if not paths:
            return values
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = origin
        return values

    @staticmethod
    def _inject_dashboard_public_url(values, template_slug, base_url):
        """把服務自身的對外 URL 注入需要「知道自己公開位址」的 chart。

        #424①：hermes-agent 的 dashboard（容器內 :9119）是 MCP OAuth 授權流程的
        回呼入口——agent 要把 ``redirect_uri`` 交給外部 MCP server，該 URL 必須是
        **租戶從瀏覽器看得到的公開位址**，不是 pod 內的 loopback。chart 自己學不到
        這個值（平台以 Cloudflare tunnel 對外，不走 chart 的 ingress），與
        ``_inject_allowed_origins``（#380）同一 family，由唯一知道 host 的平台補上。

        對外形態＝subpath（Eugene 2026-07-27 拍板 Q1；上游原生支援已實測確認——
        ``X-Forwarded-Prefix`` 會一併重建 302 Location／OAuth ``redirect_uri``／
        cookie Path／SPA 資產路徑）：``https://{subdomain}.{paas_domain}/dashboard``，
        由 chart 的 auth-proxy sidecar 轉給 127.0.0.1:9119。

        ⚠ 注入值**必須已含 ``/dashboard`` 前綴**：上游直接拿這個值當 OAuth
        ``redirect_uri`` 的權威來源，只給根位址的話回呼會打到 webui（``/``）而不是
        dashboard，授權流程靜默失敗。

        無此 value path 的 chart 一律 no-op。

        Args:
            values: Helm values dict（就地修改並回傳）
            template_slug: Template identifier（例 'hermes-agent'）
            base_url: 服務自身對外根位址，例 "https://<sub>.<domain>"

        Returns:
            Modified values dict
        """
        # slug → (value paths, 該 chart 的對外子路徑)
        path_map = {
            'hermes-agent': ([('config', 'dashboardPublicUrl')], '/dashboard'),
        }
        entry = path_map.get(template_slug)
        if not entry or not base_url:
            return values
        paths, subpath = entry
        public_url = '%s%s' % (base_url.rstrip('/'), subpath)
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = public_url
        return values

    @staticmethod
    def _parse_cidr_list(raw):
        """把逗號分隔的 CIDR 字串 parse 成 list（去空白、濾空項）。

        ir.config_parameter 只能存字串，但 chart 的 apiServerCidrs 是 CIDR
        字串陣列，故需要這層轉換。

        Args:
            raw: 逗號分隔字串，例如 "10.43.0.1/32, 192.168.10.21/32"

        Returns:
            CIDR 字串 list（輸入為空 → 空 list）
        """
        if not raw:
            return []
        return [c.strip() for c in raw.split(',') if c.strip()]

    def _get_apiserver_cidrs(self):
        """讀取 console → kube-apiserver 的 egress 白名單（cluster-specific）。

        預設 ``10.43.0.1/32`` 與 chart 預設一致（k3s 的 kubernetes Service
        ClusterIP），保持 cluster-agnostic。**woow-k3s 這類 CNI 在 kube-proxy
        DNAT «之後» 比對 egress 的叢集**，連 ClusterIP 會被 DNAT 成 apiserver
        的真實 endpoint（control-plane node IP），該 IP 不在白名單就被 deny —
        這些 node /32 屬 cluster-specific 值，由 ops 在各環境以 config
        parameter 設定，不寫死進 code / git seed。見 #404。

        Returns:
            CIDR 字串 list
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.apiserver_cidrs', '10.43.0.1/32',
        )
        return self._parse_cidr_list(raw)

    @staticmethod
    def _inject_apiserver_cidrs(values, template_slug, cidrs):
        """Inject the kube-apiserver egress allowlist into charts whose pods must
        reach the apiserver from under a tenant NetworkPolicy.

        The open-design console runs ``kubectl`` (connect.sh) to locate and exec
        into the daemon pod. Its NP egress opens ONLY the CIDRs listed in
        ``networkPolicy.apiServerCidrs``; the chart defaults to the kubernetes
        Service ClusterIP alone, which is silently insufficient on clusters whose
        CNI matches egress POST-DNAT (the packet's real destination becomes a
        control-plane node IP). Those node IPs are cluster-specific, so the chart
        cannot self-default them — the platform supplies them here. No-op for
        charts without such a value path. See issue #404 + woow-paas-charts
        open-design 0.1.3 (``values.yaml`` apiServerCidrs).

        Args:
            values: Helm values dict (modified in-place and returned)
            template_slug: Template identifier (e.g., 'open-design')
            cidrs: List of CIDR strings, e.g. ['10.43.0.1/32', '192.168.10.21/32']

        Returns:
            Modified values dict
        """
        path_map = {
            'open-design': [('networkPolicy', 'apiServerCidrs')],
            # #424①: hermes-agent 的 console 與 open-design 同構（同一顆 baked
            # console image、同一支 connect.sh kubectl exec 進 daemon 容器），故
            # 同樣需要 apiserver egress 白名單，鍵名亦沿用（chart 由 open-design 派生）。
            'hermes-agent': [('networkPolicy', 'apiServerCidrs')],
        }
        paths = path_map.get(template_slug)
        if not paths or not cidrs:
            return values
        for path in paths:
            d = values
            for key in path[:-1]:
                d = d.setdefault(key, {})
            d[path[-1]] = list(cidrs)
        return values

    @staticmethod
    def _generate_admin_password():
        """#357 C20：mcp admin password 的**唯一**生成點。

        ``secrets.token_urlsafe(24)`` 給 ~32 chars url-safe base64，強度足夠
        mcp-admin-core 的明文比對（auth/middleware.py 的 secrets.compare_digest）。
        只在建立 / reset 成功的 response 回傳一次，後續 API 一律走
        ``PaasController._sanitize_helm_values`` 遮蔽 config.sensitive.*。

        收斂理由：原本 controller（自己 import secrets 生一份）與 model create()
        各寫一次 ``token_urlsafe(24)``，同一條安全屬性散在兩處＝改強度時容易只改
        一邊，且「密碼唯一性」測試找不到單一標的。所有生成路徑都必須呼叫這裡。
        """
        return secrets.token_urlsafe(24)

    def _get_secret_record(self):
        self.ensure_one()
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.id)],
            limit=1,
        )

    def _set_admin_password_secret(self, admin_password):
        self.ensure_one()
        Secret = self.env['woow_paas_platform.cloud_service_secret'].sudo()
        secret = self._get_secret_record()
        vals = {'admin_password': admin_password, 'updated_at': fields.Datetime.now()}
        if secret:
            secret.write(vals)
            return secret
        return Secret.create(dict(vals, service_id=self.id))

    def _build_mcp_sensitive_values(self, admin_password=None):
        self.ensure_one()
        if not self.template_id.has_admin_gui:
            return {}
        if admin_password is None:
            secret = self._get_secret_record()
            if not secret:
                # #357 C03（方案 A）：secret 缺席回 {}，不 raise、不亂生密碼。
                # 供給鏈接上前建立的 legacy 服務沒有 secret，redeploy 會走到這裡。
                # 回 {} 讓 helm 沿用現有 Secret：這批服務的 chart Secret 本來就是
                # config.secret.json="{}"（平台從沒供給過 admin_password）→「{} → {}」
                # 零變化；chart deployment.yaml 的 config.json 是 write-only-if-absent，
                # 既有 config.json 不動；chart 對 admin_password 無 self-generate
                # （randAlphaNum 只給 app=n8n 的 authToken）→ 不會把密碼洗成空。
                # 租戶要取得密碼走 reset 流程（唯一同時換 Secret + patch config.json 者）。
                return {}
            admin_password = secret.admin_password
        return {'config': {'sensitive': {'admin_password': admin_password}}}

    @staticmethod
    def _strip_mcp_sensitive_values(values):
        if not values or not isinstance(values, dict):
            return values
        result = json.loads(json.dumps(values))
        config = result.get('config')
        if isinstance(config, dict):
            config.pop('sensitive', None)
        return result

    def _has_mcp_admin_secret(self):
        self.ensure_one()
        return bool(self.template_id.has_admin_gui and self._get_secret_record())

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

    # ------------------------------------------------------------------ #
    #  就緒判定：兩層共用的單一事實源（#423 W5a）                          #
    # ------------------------------------------------------------------ #

    def _needs_vm_status(self):
        """本服務的就緒判定是否需要 operator 回傳 VM 資訊。

        呼叫 operator ``get_status`` 時必須據此帶 ``include_vm``。**漏帶會讓
        VM 服務永遠停在 ``deploying``**：operator 在未查詢時回 ``vmis: None``，
        而下方 :meth:`_is_release_ready` 對 VM 是 fail-closed。
        """
        self.ensure_one()
        return self.template_id.workload_type == 'vm'

    def _is_halted(self):
        """這台 VM 是否應該處於停機（``runStrategy: Halted``）狀態。

        🔴 **不可以只看 ``state == 'suspended'``。** reconfigure 路徑
        （``controllers/paas.py``）**先**把 state 寫成 ``upgrading``、19 行後
        才組裝 values，屆時守衛讀到的已經不是 ``suspended`` → 組出 ``Always``
        → 以 ``reset_values=True`` 送出 → **停權中（欠費）的 VM 被喚醒**。
        （對照組：``action_redeploy`` 與 ``_reset_admin_password`` 都是先組裝
        再寫 state，只有 reconfigure 順序相反。）

        改讀 ``pre_suspend_state`` 作為不受呼叫順序影響的單一事實源：它只在
        :meth:`_do_suspend` 設、:meth:`_do_resume` 清，語意穩定，且在
        「state 已被中途改寫但服務仍處於停權」的窗口內仍為真。

        兩者取聯集而非擇一：``state == 'suspended'`` 涵蓋 pre_suspend_state
        因故為空的情形，``pre_suspend_state`` 涵蓋 state 被中途改寫的情形。
        """
        self.ensure_one()
        return self.state == 'suspended' or bool(self.pre_suspend_state)

    def _is_release_ready(self, status):
        """判定「這個 release 就緒了嗎」——**model 與 controller 兩層共用**。

        這段邏輯原本在 model（``_poll_and_update_status``）與 controller
        （``PaasController._update_service_status``）各有一份完全同型的複製品。
        #423 設計審查抓到：只修其中一份，租戶只要打開前台服務詳情頁，
        controller 那份就會把還在 import 的 VM 標成 ``running``——cron 修好的
        東西被前台查詢覆寫回去。故抽成單一事實源，兩層都呼叫這裡。

        （post-deploy-init 兩層仍各自實作，那是既存且有實質差異的另一件事，
        不在本次收斂範圍。）

        Args:
            status: operator ``get_status`` 的回應 dict

        Returns:
            ``(ready: bool, detail: str)`` —— detail 供 log 判讀為何未就緒
        """
        self.ensure_one()

        if self._needs_vm_status():
            vm_ready, detail = self._is_vm_ready(status)
            if not vm_ready:
                return vm_ready, detail
            # MINOR-2：VM 就緒後仍要看容器面。template 日後若啟用 MCP sidecar、
            # 或 chart 加了輔助 Deployment，只看 VMI 會在那些容器還沒起來時就
            # 報就緒。
            #
            # ⚠ 這裡原本寫「容器端對『pods 為空』回 True，所以這個合取不會讓純
            # VM 卡住」——那句話的**前提是錯的**（F-423-1 Step 1 實測推翻）：
            # KubeVirt 的 VM 跑在 virt-launcher pod 裡，而 haos chart 把
            # `app.kubernetes.io/instance` 標在 VirtualMachine 的 pod template 上，
            # virt-launcher 因此**帶著** operator selector 用的那個標籤（實測
            # `virt-launcher-svc-…-haos` 回報 `2/2`）。所以純 VM 的 `pods` 從來
            # 不是空的，容器閘是**憑自身條件**通過的，不靠任何 fail-open。
            # 這個事實在 Step 1 之後從「碰巧成立」變成「必要條件」。
            return self._is_container_ready(status)
        return self._is_container_ready(status)

    @staticmethod
    def _is_container_ready(status):
        """容器型就緒判定。

        **fail-closed**：沒有看到「至少一顆 Running 且容器全 ready 的 pod」，就不算
        就緒。``pods`` 為空的兩個成因處置相同，只在理由字串上區分：

        1. **查詢本身失敗** —— ``pod_retrieval_error`` 有值（RBAC 掉了、API server
           抖動…）。operator 此時仍回 **HTTP 200**、``pods`` 保持空陣列
           （``api/releases.py``），所以呼叫端不讀這個欄位就會誤讀成第 2 種（#484）。
        2. **查詢成功、真的一顆 pod 都沒有** —— ``pod_retrieval_error`` 是 ``None``。

        第 2 種原本回 True（fail-open，F-423-1 / design §4.1），理由是「容器型有一個
        合法的短暫狀態是 helm release 已 deployed 但 pod 還沒被 controller 建出來，
        改 fail-closed 會讓所有現存租戶服務多卡一輪 poll」。**F-423-1 Step 1
        (2026-07-31) 翻掉了它**，因為評估推翻了這個取捨的兩邊：

        - **代價被高估**：poll 只撈 deploying / upgrading / initializing，所以「多卡
          一輪」只發生在**正在轉移**的服務上，對已 ``running`` 的現存機隊零影響；
          而一輪是前台 5 秒、cron 60 秒。
        - **收益被低估**：``pods`` 為空**不必然是短暫的**。ResourceQuota 拒絕 pod
          建立時，ReplicaSet 根本產不出 Pod 物件（``FailedCreate``），空陣列會**永久
          持續**；而誤報是**黏住**的（寫進 ``running`` 後沒有任何東西會再 poll 它），
          15 個 template 有 13 個 ``post_deploy_init_type='none'``、連 post-deploy
          init 那層巧合的網都沒有。#456 就是實錄：兩筆 hermes-agent 永久壞掉，
          DB 與 UI 卻都顯示 Running/ONLINE。

        ⚠ 注意這裡**只**對「空陣列」改判。``pods`` 非空但沒有 Running 的（例如排程
        失敗卡 Pending、或 maxPods 滿）本來就回 False，那條路沒有改動。

        殘餘風險與封頂：若某 chart 的 pod 不帶 ``app.kubernetes.io/instance``
        （operator 的 selector），服務會從「假就緒」變成「永久卡 deploying」。合併前
        已逐一實證現行 15 個 template 涵蓋的 9 支 chart 都帶該標籤；長期由 #489 的
        部署 age timeout 封頂。
        """
        if status.get('pod_retrieval_error'):
            # 硬失效：查詢失敗＝狀態未知。不會自己好 → 必須是 HARD
            # （`_log_not_ready` 會升 warning），否則正式環境的 INFO 級 log
            # 一行都看不到。
            return False, (
                f"HARD: pod lookup failed: {status['pod_retrieval_error']}"
            )

        pods = status.get('pods') or []
        # Only Running-phase pods — terminators would block readiness forever
        active_pods = [p for p in pods if p.get('phase') == 'Running']
        if not pods:
            # 查得到、但一顆 pod 都沒有。可能是合法的建立中窗口，也可能是永久的
            # （quota 擋住 Pod 物件建立）。兩者在這一層**分不出來**，所以 HARD——
            # 分辨要靠時間，那是 #489 的職責。
            return False, 'HARD: no pods found for this release'
        if not active_pods:
            return False, 'no active pods'
        ready = all(
            '/' in pod.get('ready', '0/0')
            and pod.get('ready', '0/0').split('/')[0] == pod.get('ready', '0/0').split('/')[1]
            for pod in active_pods
        )
        return ready, 'pods ready' if ready else 'pods not ready'

    @staticmethod
    def _is_vm_ready(status):
        """VM 型就緒判定——**fail-closed**（#423 design §4.1）。

        與容器型的關鍵差異：**virt-launcher pod Running ≠ guest 開機完成**。
        pod 一起來就翻 running 會讓租戶拿到一台還在 import 磁碟的機器。故改讀
        VMI 的 ``phase`` ＋ ``Ready`` condition。

        fail-closed 的三種情形（全部「維持 deploying」而非翻 running）：

        1. ``vm_included`` 為 False —— 呼叫端漏帶 ``include_vm``。這是程式錯誤，
           但寧可卡住也不能誤報就緒。
        2. ``vmi_retrieval_error`` 有值 —— 查詢失敗，狀態未知。
        3. ``vmis`` 為 None（未查詢）或空 list（還沒建出來）。

        ⚠ ``datavolume_retrieval_error`` **不**參與就緒判定：DataVolume 只供
        進度顯示，它掛掉頂多讓進度條消失，不該擋住一台已經 Ready 的 VM。
        """
        if not status.get('vm_included'):
            # 硬失效：呼叫端漏帶 include_vm，或 operator 版本太舊沒有這個欄位。
            # 兩者都不會自己好，且外觀與「還在 import」完全相同 → 必須是
            # warning，否則正式環境的 INFO 級 log 一行都看不到（MINOR-1）。
            return False, (
                'HARD: operator was not asked for VM status (include_vm '
                'missing, or operator too old to echo it) — refusing to '
                'report ready'
            )
        if status.get('vmi_retrieval_error'):
            # 硬失效：多半是 RBAC 缺 virtualmachineinstances 權限。
            return False, (
                f"HARD: VMI lookup failed: {status['vmi_retrieval_error']}"
            )

        vmis = status.get('vmis')
        if not vmis:
            # 正常過渡：VMI 還沒建出來（磁碟 import 中）。
            return False, 'no VMI yet'

        not_ready = [
            v.get('name') for v in vmis
            if not (v.get('phase') == 'Running' and v.get('ready'))
        ]
        if not_ready:
            return False, f"VMI not ready: {', '.join(filter(None, not_ready))}"
        return True, 'VMI ready'

    def _log_not_ready(self, detail):
        """把「為何還沒就緒」記進 log，硬失效升 warning（MINOR-1）。

        為什麼分級很重要：VM 卡在 ``deploying`` 有兩種成因，在 Odoo 側**外觀
        完全相同**——
          * 正常過渡：磁碟還在 import（可能數十分鐘），
          * 硬失效：operator RBAC 缺 ``virtualmachineinstances`` 權限、或
            平台漏帶 ``include_vm``。
        後者不會自己好，而 ``_cron_check_deploying_services`` 沒有 age 逾時
        機制，所以若兩者都寫 debug，正式環境（INFO 級）不會留下任何線索，
        只能靠人去猜。這一個字決定 W6 上線時能不能直接從 log 定位（#315
        同型教訓）。
        """
        self.ensure_one()
        if detail.startswith('HARD:'):
            _logger.warning(
                "Service %s (id=%s) blocked from becoming ready: %s",
                self.name, self.id, detail,
            )
        else:
            _logger.debug(
                "Service %s not ready yet: %s", self.name, detail,
            )

    @staticmethod
    def _extract_import_progress(status):
        """取 CDI DataVolume 的 import 進度字串（供前台進度顯示，design §7.4）。

        回 None 代表沒有進度可顯示——**呼叫端不得據此判斷就緒**
        （``datavolume_retrieval_error`` 時這裡就會回 None）。

        多顆 DataVolume 時取「仍在 import 的那顆」而非第一顆：v1 每台 VM 只有
        一顆 disk，但 chart 日後若加第二顆（例如獨立 data volume），先完成的
        那顆會是 100% 而遮住仍在跑的，進度條就會卡在 100% 不動。優先取
        ``ImportInProgress``，再退回百分比最小者。
        """
        dvs = status.get('datavolumes') or []
        in_progress = [
            dv for dv in dvs if dv.get('phase') == 'ImportInProgress'
        ]
        candidates = in_progress or dvs

        def _pct(dv):
            raw = (dv.get('progress') or '').rstrip('%')
            try:
                return float(raw)
            except ValueError:
                return None

        usable = [
            (pct, dv.get('progress'))
            for dv in candidates
            for pct in [_pct(dv)]
            if pct is not None
        ]
        if usable:
            return min(usable)[1]

        # 沒有可解析的百分比就退回第一個非 N/A 的原始字串（保守）。
        for dv in candidates:
            progress = dv.get('progress')
            if progress and progress != 'N/A':
                return progress
        return None

    def _store_import_progress(self, status):
        """把 import 進度寫進記錄供前台顯示（兩層 poll 共用）。

        只在值真的改變時 write，避免每次 poll 都髒一次記錄。就緒後清空
        （避免卡片一直掛著 "100.0%"）。
        """
        self.ensure_one()
        progress = self._extract_import_progress(status)
        if (self.import_progress or None) != progress:
            self.sudo().write({'import_progress': progress or False})

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
                # VM 型必須帶 include_vm，否則 operator 不回 vmis，
                # 而 _is_vm_ready 是 fail-closed → 服務永遠卡 deploying。
                include_vm=self._needs_vm_status(),
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
        release_status = release.get('status', '')
        helm_revision = release.get('revision', self.helm_revision)

        # Re-fetch for optimistic locking
        service = self.browse(self.id)
        if not service.exists() or service.state != original_state:
            return

        # 進度先落地（與就緒判定無關，且未就緒時才是最有用的時候）。
        service._store_import_progress(status)

        if release_status == 'deployed':
            all_ready, detail = service._is_release_ready(status)
            if not all_ready:
                service._log_not_ready(detail)

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

        # MINOR-4（既存設計，本 PR 未改）：resume 後直接寫回 pre_suspend_state。
        # 對 VM 型而言，patch runStrategy=Always 只是「叫它開機」，guest 真正
        # 可用還要數分鐘，所以這段期間 UI 會顯示 running 但服務尚未就緒。
        # 容器型的 resume 成本低、窗口短，故沿用同一路徑；VM 型是否改成回
        # deploying 交給 poll 收斂，列為 v1.1 評估（design §7.2）。
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
        normalized_vals_list = []
        for vals in vals_list:
            normalized = dict(vals)
            raw_values = normalized.get('helm_values')
            if raw_values:
                try:
                    normalized['helm_values'] = json.dumps(
                        self._strip_mcp_sensitive_values(json.loads(raw_values))
                    )
                except (json.JSONDecodeError, TypeError):
                    pass
            normalized_vals_list.append(normalized)

        records = super().create(normalized_vals_list)
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
        for rec in records.filtered(lambda r: r.template_id.has_admin_gui):
            if not rec._get_secret_record():
                secret_password = self.env.context.get('mcp_admin_password')
                if not secret_password:
                    # #357 C20：走單一生成點，不在此處自行 token_urlsafe。
                    secret_password = self._generate_admin_password()
                rec._set_admin_password_secret(secret_password)
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
