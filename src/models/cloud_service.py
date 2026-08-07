import json
import logging
import secrets
import uuid

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .cloud_app_template import SupplyGateError

_logger = logging.getLogger(__name__)

# #336（Wave A）：計數「活躍實例」時排除的刪除終態。CloudService 刪除走真 unlink
# （record 離場），故 state 值集內唯一「即將釋放、不應佔用配額」的過渡態
# 為 'deleting'（Task 0 校驗：值集無 deleted／terminated）。
TERMINAL_STATES = ('deleting',)

# provision「每帳號限一」的 advisory-lock namespace（比照 project_project 的
# _REACH_LOCK_NS reach-lock；任意固定 int、全 module 唯一即可）。'HAPA' = 0x48415041。
_HA_PROVISION_LOCK_NS = 0x48415041

# #501/#518 全域實例上限的**衝突列** UPDATE。
#
# ⚠ 不能只用 advisory lock。Odoo 的 cursor 是 **REPEATABLE READ**
# （`odoo/sql_db.py` 的 `set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`，
# 該 class docstring 明文選用），所以「取鎖之後重新計數」在同一個 tx 內**拿不到新
# snapshot**：輸家取得鎖後看到的仍是取鎖前的舊資料，照樣放行。實測（#518）：cap=1、
# 兩並發，兩邊取鎖後計數都是 1，最終落 2 列——鎖確實序列化了執行，但在它唯一存在的
# 理由上零貢獻。#345 的 docstring 寫「READ COMMITTED」是與事實相反的前提。
#
# 正解是讓並發的第二個 tx 撞上 **write-write 衝突**：兩者都 UPDATE 同一列時，
# PostgreSQL 在 RR 下對後到者回 `could not serialize access due to concurrent
# update`（SQLSTATE 40001），Odoo 的 `retrying()` 會以**新 tx／新 snapshot** 重試
# （最多 5 次），重算後的計數才是對的。UPDATE 的 row lock 同時提供互斥，所以
# advisory lock 在這裡是冗餘的，已移除——少一個機制少一份誤解。
#
# 衝突列選 template row：全域上限的語意範圍就是 template，粒度剛好，也不會誤傷
# 無關交易。`max_per_partner` 的語意範圍是 (template, partner)，不能共用這一列
# （會把它的併發粒度粗化），其修法另見 #518。
_GLOBAL_CAP_CONFLICT_SQL = (
    "UPDATE woow_paas_platform_cloud_app_template "
    "SET write_date = (clock_timestamp() AT TIME ZONE 'UTC') WHERE id = %s"
)

# ------------------------------------------------------------------------- #
#  #489：轉移狀態的 age timeout（F-423-1 Step 2）                             #
# ------------------------------------------------------------------------- #
#
# `deploying` / `upgrading` 在此之前**沒有任何時間上界**：cron 每分鐘、前台每
# 5 秒都只是「再問一次 operator」，沒有一方會說「問太久了，這條不會好了」。
# 永遠不會就緒的服務因此無限期停在轉移狀態，而它與「正在正常部署」在 Odoo 側
# 外觀完全相同（#456 兩筆、#423 W5a 的 VM RBAC 缺權皆屬此類）。
#
# ⚠ 門檻**寧鬆勿緊**，因為誤殺不可自癒：兩層 poll 都只在 deploying /
# upgrading / initializing 才呼叫，一旦寫進 `error` 就再也沒有任何路徑會把它
# poll 回 `running`——即使叢集裡的服務在門檻之後其實就緒了。誤殺＝本來會好的
# 服務被打成 error、租戶得重新下單；晚報＝維運晚 N 分鐘知道。兩者不對稱。
#
# 預設值的實證依據（見 PR 論證）：
#   * 容器型 30 分 —— 本 repo 沒有容器型的分段計時實測，取 #489 建議值 15 分
#     的兩倍餘裕。這段時間涵蓋 image pull（跨 registry 的大 image）＋ PVC
#     provision ＋ pod 排程。
#   * VM 型 120 分 —— #423 W6 stg e2e 第二輪實測「下單→running 98s」
#     （registry/OCI 路徑，import 58s）；design §5.2 對 http source 的估計是
#     30–60 分（**二手估計，未實測**）。對未實測的最慢合法路徑取兩倍餘裕。
#     CDI 磁碟 import 是這裡最大的變數，故 VM 必須有自己的一檔。
#
# 兩者皆可用 ir.config_parameter 覆寫——正確值依叢集規格／網路而異，prod 與
# stg 未必相同，不該要改 code 才能調。
DEPLOY_TIMEOUT_MINUTES_DEFAULT = 30
VM_DEPLOY_TIMEOUT_MINUTES_DEFAULT = 120

_DEPLOY_TIMEOUT_PARAM = 'woow_paas_platform.deploy_timeout_minutes'
_VM_DEPLOY_TIMEOUT_PARAM = 'woow_paas_platform.vm_deploy_timeout_minutes'

# age timeout 管轄的狀態。**`initializing` 刻意不在內**：post-deploy init 有
# 自己的上界（`init_retries`，5 次後翻 error），兩套機制夾擊同一個狀態只會讓
# 失敗歸因更難（#489 明列）。
_TIMEOUT_GOVERNED_STATES = ('deploying', 'upgrading')

# 逾時訊息的分型提示：把 `_is_release_ready` 的 detail 對應到「維運該往哪查」。
# 目的是讓人不必進叢集就知道方向——detail 說「卡在哪」，hint 說「去查什麼」。
#
# 以 detail 的子字串比對（順序即優先序）。detail 字串日後若改動，
# `TestTimeoutDiagnosis` 會紅——這個耦合是刻意用測試守住的，而不是靠註解。
# 未命中時不附 hint（fail-soft：訊息仍有前綴與 detail，只是少了方向）。
_TIMEOUT_HINTS = (
    ('pod lookup failed',
     'the operator could not list pods — check its RBAC and the API server'),
    ('no pods found',
     'no Pod object was ever created — check the namespace ResourceQuota (a '
     'rejected Pod never materialises) and that the chart labels its pods '
     'with app.kubernetes.io/instance'),
    ('no active pods',
     'pods exist but none reached Running — check pod events (scheduling, '
     'image pull) and node capacity'),
    ('pods not ready',
     'pods are Running but never became ready — check container logs and the '
     'readiness probe'),
    ('include_vm',
     'the platform never received VM status — the operator is likely too old '
     'to report it'),
    ('VMI lookup failed',
     'the operator could not read the VirtualMachineInstance — check its RBAC '
     'for virtualmachineinstances'),
    ('no VMI yet',
     'no VirtualMachineInstance was created — check the DataVolume / CDI '
     'importer, the disk import may have stalled'),
    ('VMI not ready',
     'the VM is running but never reported Ready — check the guest boot '
     'inside the VM'),
    ('helm release status is',
     'the helm release never reached "deployed" — a killed rollback leaves it '
     'stuck in pending-*; inspect `helm history` for the release'),
)

# 停權中的服務被 redeploy 後會卡在 `deploying`（#482）：VM 被**正確地** Halted
# 著，所以永遠不會 ready。此時上面那些 hint 全是誤導（叫人去查 pod / quota，
# 而真因是停權），故停權時**取代**而非附加。
_SUSPENDED_HINT = (
    'the service is suspended — its workload is intentionally halted, so it '
    'can never become ready while a deploy/upgrade is pending (see #482)'
)

# #497 一次性收斂的判別式：停權旗標仍在，但 state 已被 #493 合併前的舊碼汙染成
# 這些「對租戶不可用」的狀態之一。`initializing` 在內是因為容器型被 upgrade 喚醒後
# poll 會把它從 `deploying` 推進去（poll 不看 `_is_halted`）。
#
# **`running` 刻意不在內**：那批同樣帶 stale 旗標，但誤動的代價是把正在跑的服務關掉
# ——交給 lifecycle cron 分支 2 分流（訂閱有效 → resume 清旗標且零停機）。
_STUCK_SUSPENDED_STATES_497 = ('deploying', 'upgrading', 'initializing', 'error')

# 殘留記號的固定前綴：state 已收斂但 workload 沒能確認停機時寫進 `error_message`。
# 停權服務照跑就是計費繞過，所以這個窗口必須能被 prod rollout 的 checklist 一條
# search 盤出來，而不是散在 log 裡靠人翻。文字改動會讓 #497 的測試紅——它是對維運的
# 契約，不該靜默漂移。
_UNCONFIRMED_WORKLOAD_MARKER_497 = '#497 已收斂但無法確認 workload 已停機'


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
        groups='base.group_system',
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
    last_release_sync_at = fields.Datetime(
        string='Last Release Sync At',
        help=(
            "最後一次從 operator 取得 release 事實（chart 版本／revision）的時間。"
            "注意語意：是「最後一次**問過**」，不是「最後一次**值有變**」——"
            "租戶要知道的是眼前這個版本號多新，而不是它上次改是什麼時候。\n\n"
            "⚠ 這個欄位由 #627 (i) 的每日 reconciliation cron 每天 stamp 一次，"
            "**代價是 `write_date` 從此每天變動**：在此之前，`write_date` 停留在"
            "「最後一次平台自己動這筆服務」的時間，可以拿來判斷「這筆帳本多久沒被"
            "碰過」。#552／#627 的調查就用過那個訊號兩次（判定某次 helm upgrade "
            "不是 Odoo 發起的、以及證明升版沒有順手回寫帳本）。**那個診斷能力從本"
            "版起消失** —— 要判斷「平台最後一次主動操作這筆服務」請改看 "
            "`last_upgraded_at` / `deployed_at`，不要再用 `write_date`。"
        ),
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
            # #552 層 4：redeploy 帶 `template.helm_chart_version` 做 helm upgrade，
            # 空值 ⇒ 不帶 `--version` ⇒ **升到 registry 當下的 latest**，而
            # `reset_values=True` 又讓 values 以新版 chart 的預設重新渲染。這條路
            # 沒有 role gate（後台 button ＋ `call_kw`），一次誤按就是不可逆的跳版。
            #
            # ⚠ 這裡用 `UserError` 而不是靜默略過或背景失敗：本方法是**同步**的，
            # 例外會直接回到呼叫端（後台 UI／RPC）。實查全 repo，`action_redeploy`
            # 沒有任何 cron／自動化呼叫端（只有 `cloud_service_views.xml` 的兩顆
            # button），所以擋下來一定有人看得到，不會變成靜默失敗。
            #
            # 刻意**不**退回 `self.helm_chart_version`：redeploy 的語意就是「重新
            # 套用商品當前釘的版本」（允許夾帶升版），改讀服務自己的版本會把它
            # 悄悄變成另一個功能。要放行就去把 template 的版本填上。
            if not template.helm_chart_version:
                raise UserError(
                    f"無法重新部署：商品「{template.name}」尚未指定 chart 版本。"
                    f"直接部署會抓 registry 當下的最新版（不可逆的跳版）。"
                    f"請先在商品設定填入版本再重試。"
                )
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

            # #482: 停權中的服務不得被 upgrade 拉出停權。寫成 'deploying' 會讓
            # _do_resume 的 guard 擋掉復權（租戶付款也回不來），而 poll 又永遠
            # 等不到一台本該停著的服務就緒 —— 兩頭卡死。必須在 write 之前判定，
            # 因為 write 本身會改掉 state 這個判斷來源。
            halted = self._is_halted()

            # Set to 'deploying' — cron + frontend polling will handle
            # the rest (pod readiness check → init → running)
            #
            # #489：`last_upgraded_at` 是這條路徑的時間錨。這裡寫的 state 是
            # `deploying`（首裝路徑的狀態）但做的是 helm **upgrade**——不記錨的
            # 話，age timeout 會退回 `deployed_at`（首裝時間），一台跑了三個月
            # 的服務按下重新部署就會在第一輪 poll 被判逾時。
            self.write({
                'state': 'suspended' if halted else 'deploying',
                'error_message': False,
                'init_error': False,
                'init_retries': 0,
                'helm_revision': release_info.get('revision', self.helm_revision),
                # #552: redeploy runs a real helm upgrade carrying
                # `template.helm_chart_version` (possibly empty ⇒ registry latest),
                # so it can move the deployed chart version. Record what the
                # operator resolved; `or self.helm_chart_version` keeps the old
                # value when talking to an operator that does not report it yet.
                # Per-service field only — see `_execute_deploy` for why the
                # template record must never be written back to.
                'helm_chart_version': (
                    release_info.get('chart_version') or self.helm_chart_version
                ),
                'last_upgraded_at': fields.Datetime.now(),
            })
            # #482: upgrade 用 chart 預設把 replicas 渲染回去了，收回停機。
            # 放在 write 之後 —— 這個 helper 失敗時要寫 error_message，順序反過來
            # 會被上面那行 'error_message': False 清掉。
            self._rescale_halted_after_upgrade(client)

            # Trigger cron so state transitions without frontend polling.
            # 停權服務不觸發：poll 掃 deploying/upgrading/initializing，把停權
            # 服務餵進去只會讓它被判定成「還沒就緒」而空轉。
            if not halted:
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
        # #530：Reset 一次換發兩把憑證。它是租戶唯一的換發入口，也是**存量服務取得
        # gateway key 的唯一路徑**（刻意不做從叢集回讀的 backfill——那會違反「憑證
        # 權威來源在平台端」的不變式，且要新增 operator 端點）。只換一把的話 UI 那句
        # 「Reset 會一併換掉 Gateway API Key」就是謊話。
        # 沒有 gateway 的 chart 傳 None：`_set_admin_password_secret` 的補鑄同樣受
        # `_chart_has_gateway` 閘門管，這裡硬生一把會繞過那道閘門（鑄造／供給／交付
        # 三個閘門必須同源，否則就是 review finding 3 那條隱性契約再長回來）。
        new_gateway_api_key = (
            self._generate_admin_password() if self._chart_has_gateway() else None
        )
        # #411: values 走全量組裝 helper（與 deploy/redeploy/reconfigure 同一條
        # 管線）。此路徑帶 template.helm_chart_version，reset password 會夾帶
        # chart 升版；改 reset_values 語意讓 chart 新增預設補位，不再以
        # --reuse-values 沿用舊 coalesced values（同一 bug class，見 #411）。
        values = self._assemble_deploy_values()['values']
        operator_sensitive_values = self._build_mcp_sensitive_values(
            new_password, gateway_api_key=new_gateway_api_key,
        )

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
            # #552: the response is no longer discarded — it carries the chart
            # version helm resolved for this upgrade (both operator branches,
            # config_json and helm_secret, report it).
            reset_result = client.reset_release_config(
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

        # ================= 提交點（C11′ commit-on-secret）之後 =================
        #
        # #525 B3：從這裡開始，「新密碼已生效」已經是**既成事實**——operator 回了
        # 2xx，新憑證就是叢集的權威值，而且不會再被回退（helm --atomic 自己回捲、
        # config.json patch 失敗會還原備份，能失敗的全在 2xx 之前）。
        #
        # 因此提交點之後的每一步都只是**記帳**（落庫、stamp state、停權回收），
        # 不是「reset 有沒有成功」的判準。讓記帳的例外往外拋 ⇒ controller 回
        # `success: False` ⇒ 告訴租戶「沒換」。那句話是假的，代價也不對稱：
        # 明文只顯示這一次、平台刻意沒有回讀端點（憑證一律一次性），租戶因此
        # **永久拿不到**唯一能登入 admin GUI 的那把密碼，還會拿已失效的舊密碼
        # 反覆重試。所以這兩段的例外一律不外拋（`OperationalError` 除外，見下）。
        #
        # 🔴 這裡**刻意不補** `cr.rollback()`。#525 把本處列為 B 級（外觀與 #520
        # 同構、因果相反）正是這個理由：回捲會把已落庫的新密碼退回舊值，而叢集
        # 已經是新值 ⇒ 從「一致但租戶不知情」變成「DB 與叢集不一致」，管理員連
        # admin GUI 都登不進去，之後的 redeploy 還會拿 DB 的舊值去覆寫叢集。
        #
        # 用 `cr.savepoint()` 而非裸 try：Python 層例外會在 ORM cache 留下待
        # flush 的髒值，不清掉的話它會在本 request 稍後（甚至 controller 之外的
        # commit）再炸一次 → 變成 500 並回捲整筆交易，正好是我們要避免的形態。
        # `_FlushingSavepoint.rollback` 會先 `cr.clear()` 再 ROLLBACK TO SAVEPOINT。
        #
        # 兩段分開包，是因為**回捲單位不同**：憑證落庫必須在記帳失敗時存活下來
        # （它是與叢集一致的那個事實）；記帳則整組成立或整組不成立。
        try:
            with self.env.cr.savepoint():
                # #530：兩把憑證一起落庫。2xx 代表兩者都已是叢集權威值。
                self._set_admin_password_secret(
                    new_password, gateway_api_key=new_gateway_api_key,
                )
        except OperationalError:
            # #518：並發／連線類 DB 例外（SerializationFailure 40001 等）必須傳
            # 出去讓 Odoo 的 `retrying()` 看見；此刻交易已 aborted，吞掉只會讓
            # 後續 flush 再炸。重播整個 handler 會重跑一次 reset（另鑄一把新密碼
            # 重新下發），結果仍然是一致的。
            raise
        except Exception:
            _logger.exception(
                "[#525 B3] reset-admin-password could NOT store the new "
                "credentials for service %s (id=%s) although the operator "
                "already applied them — cluster and DB now disagree; the live "
                "password is being returned to the tenant so they can still "
                "sign in, but a later redeploy would re-supply the old value",
                self.name, self.id,
            )

        # `last_upgraded_at` 同 action_redeploy：reset 走的是同一條 helm
        # upgrade 管線，不記錨就會讓 #489 的 age timeout 拿首裝時間當起點。
        #
        # #482 縱深：這條路對 suspended 服務**不可達**（本方法開頭就要求
        # state == 'running'），但 reset 同樣是一次 reset_values 重渲染。二階
        # 情境是服務先被 reconfigure 汙染（suspended → upgrading → poll →
        # running）而 pre_suspend_state 仍在：此時 _is_halted() 仍為真＝它其實
        # 還在停權中，reset 完必須把它收回停機，而不是留下一台跑著的欠費服務。
        try:
            with self.env.cr.savepoint():
                halted = self._is_halted()
                self.write({
                    'state': 'suspended' if halted else 'upgrading',
                    'error_message': False,
                    # #552: reset carries `template.helm_chart_version` into helm
                    # upgrade (see the F-461-2 drift log above — this path really
                    # does change the chart), so it is one of the four places the
                    # ledger must be refreshed. `or self.helm_chart_version` =
                    # backward compatibility with an operator whose reset response
                    # has no `chart_version` yet.
                    # Per-service field only — never `template.helm_chart_version`.
                    #
                    # #525 B3：帳本更新與 state stamp 同屬「記帳」，一起進 savepoint。
                    # 它失敗時整組回捲（帳本維持舊值＝與失敗前一致），但**不會**
                    # 波及上一段已落庫的憑證，也不會翻轉「密碼已生效」的回應。
                    'helm_chart_version': (
                        (reset_result or {}).get('chart_version')
                        or self.helm_chart_version
                    ),
                    'last_upgraded_at': fields.Datetime.now(),
                })
                self._rescale_halted_after_upgrade(client)
        except OperationalError:
            raise
        except Exception:
            # 記帳整組回捲 ⇒ state 留在 'running'。代價是前端少一輪 upgrading
            # 輪詢（服務仍會自己收斂），遠小於讓租戶永久拿不到已生效的密碼。
            _logger.exception(
                "[#525 B3] reset-admin-password bookkeeping failed for service "
                "%s (id=%s) AFTER the new credentials took effect — the reset "
                "itself stands and the password is being returned; platform-side "
                "state/last_upgraded_at/helm_chart_version may be stale",
                self.name, self.id,
            )

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
            # #552 層 4 —— **backstop，不是主閘門**。主閘門在可購買性裁決點
            # （`cloud_app_template._reject_reason_for_new_instance`），擋在收錢之前。
            #
            # 🔴 **這不是過渡期保險，是永久涵蓋——不要當成清得掉的舊碼。**
            # 主閘門掛在 `CloudService.create()` 上，所以它只看得到**新建**的列。
            # 有一條當前就存在、可重複觸發的路徑會讓一台服務走進 `_execute_deploy`
            # 而**完全不經過 create**：
            #
            #     controllers/ha_paas.py 的 provision 重試 —— 既有服務處於 `error`
            #     時，它 `existing.sudo().write({'state': 'pending'})` 再觸發 deploy
            #     cron。**是 write 不是 create** ⇒ `_enforce_supply_gate` 不會跑，
            #     cron 隨後直接呼叫本方法。這條路上**只有這道 backstop 接得住**。
            #
            # （另一種同樣接不到主閘門的列：閘門上線前就已經躺在 `pending` 的服務。
            # 那個確實是過渡期的，但它**不是**本 backstop 存在的唯一理由。）
            #
            # 而 install 不帶 `--version` 就是 registry latest。
            #
            # 🔴 **位置在 `try` 之內是必要條件，不是風格**。放在 try 外面時，例外
            # 會直接穿過本方法，落進 `_cron_process_pending_deploys` 的泛用
            # `except Exception` —— 訊息被換成「Unexpected error during
            # deployment. Check server logs.」，租戶與第一線都無從排錯（#520 症狀
            # 4 同型）。實測踩過一次：`state` 確實變成 error，但 `error_message`
            # 完全不提版本，看起來就像基礎設施故障。
            #
            # ⚠ 例外型別同理**必須**是 `PaaSOperatorError`：底下那個 handler 才會
            # 把 detail 寫進 `error_message`。兩個條件（位置＋型別）少一個都會讓
            # 這道 backstop 退化成「擋下來了，但沒人知道為什麼」。
            if not template.helm_chart_version:
                raise PaaSOperatorError(
                    'chart version not pinned',
                    detail=(
                        f'商品「{template.name}」尚未指定部署版本，為避免部署到'
                        f'未知的 chart 版本已中止開通。請在 seed 釘版後重新發版'
                        f'（#552）。'
                    ),
                )

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
                # #552: record the chart version helm ACTUALLY resolved. With an
                # empty `template.helm_chart_version` no `--version` is sent and
                # helm picks the registry's latest — the ledger used to keep the
                # (empty) template value forever, so the tenant's service-detail
                # page showed a version nobody had verified.
                #
                # ⚠ Written to **cloud_service** (per-service), never to
                # `template.helm_chart_version`: the seed is `noupdate=0` and the
                # healer at the tail of `data/cloud_app_templates.xml` clears
                # `ir.model.data.noupdate` on every `-u`, so a hand-written
                # template value is silently reverted by the next release (same
                # discipline as `cloud_app_template.py` `_DEFAULT_GLOBAL_INSTANCE_CAPS`).
                #
                # `or self.helm_chart_version` keeps backward compatibility with
                # an operator that predates the field: unknown ⇒ keep, never blank.
                'helm_chart_version': (
                    release_info.get('chart_version') or self.helm_chart_version
                ),
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
        # #559：helm_values 有 groups=，屬性直讀會 AccessError。走唯一入口。
        values = self._strip_mcp_sensitive_values(self._helm_values_dict()) or {}

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
        public_base_url = self._public_base_url()
        if public_base_url:
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
        """#357 C20：平台供給憑證的**唯一**生成點（admin password ＋ #530 gateway key）。

        ``secrets.token_urlsafe(24)`` 給 ~32 chars url-safe base64，強度足夠
        mcp-admin-core 的明文比對（auth/middleware.py 的 secrets.compare_digest），
        也**必然滿足** hermes gateway 對 api server key 的 16 字元下限（#530：低於
        下限時上游會靜默不登記 api server 平台，健康信號直接消失——無聲失效）。
        只在建立 / reset 成功的 response 回傳一次，後續 API 一律走
        ``PaasController._sanitize_helm_values`` 遮蔽。

        收斂理由：原本 controller（自己 import secrets 生一份）與 model create()
        各寫一次 ``token_urlsafe(24)``，同一條安全屬性散在兩處＝改強度時容易只改
        一邊，且「密碼唯一性」測試找不到單一標的。所有生成路徑都必須呼叫這裡
        ——#530 的 gateway key 同樣走這裡，不另開第二套隨機來源（兩套會分歧成
        兩種強度，而弱的那把不會有任何測試盯著）。
        """
        return secrets.token_urlsafe(24)

    @staticmethod
    def _build_gateway_public_url(external_path, base_url):
        """把 chart 的 gateway 子路徑接到服務的公開根位址上（純字串正規化）。

        #530：hermes-agent 的 gateway（容器內 :8642）以 auth-proxy 的 ``/gateway``
        子路徑對外。租戶要把這條網址填進 MCP 的 Gateway 區，所以**由後端組**而非
        丟一個根位址讓前端或租戶自己拼——同一個坑已經在 dashboard 那區發生過
        （租戶填成服務根網址、漏掉子路徑而永遠連不上）。

        ``external_path`` 由 ``CloudAppTemplate._gateway_external_path()`` 從 seed
        讀出，**平台側不再維護 slug→路徑對照表**（見該 method 的 docstring 與 spec
        「不得新建 slug allowlist」）。本 method 因此只剩拼接與 rstrip 正規化：純
        函式、沒有能力語意——「這個 chart 有沒有 gateway」由 external_path 空不空
        表達，判準只有一處。

        Args:
            external_path: chart 對外的 gateway 子路徑（例 '/gateway'）；'' ＝沒有
            base_url: 服務自身對外根位址，例 "https://<sub>.<domain>"

        Returns:
            完整 gateway URL；任一輸入為空時回 ''。
        """
        if not external_path or not base_url:
            return ''
        return '%s%s' % (base_url.rstrip('/'), external_path)

    def _public_base_url(self):
        """服務自身的對外根位址（Cloudflare tunnel 曝光）；無 subdomain 回 ''。

        `_assemble_deploy_values` 的三個 injector 與 #530 的 gateway URL 交付都要
        這個值，收斂成一處避免 domain 組法漂移。
        """
        self.ensure_one()
        if not self.subdomain:
            return ''
        paas_domain = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io',
        )
        return 'https://%s.%s' % (self.subdomain, paas_domain)

    def _chart_has_gateway(self):
        """本服務的 chart 有沒有對外 gateway ⇒ 該不該有 gateway API key。

        #530 的**鑄造／供給**閘門（`_set_admin_password_secret` 補鑄、
        `_build_mcp_sensitive_values` 送出 `auth.apiServerKey`、Reset 換發），與
        **交付**閘門 `_gateway_public_url()` 同源但條件較寬：交付另要求服務有對外
        根位址（subdomain），沒有 subdomain 的服務仍然需要 key（gateway 在叢集內
        仍在跑），只是平台沒有網址可以交給租戶。

        為什麼鑄造也要判、而不是「先鑄好、之後 chart 支援了就能用」：`has_admin_gui`
        的 8 個模板裡只有 hermes-agent 有 gateway，其餘 7 個 chart 會收到一個平台
        並不打算給它的 `auth.apiServerKey`。今天無害純粹是因為 mcp-server／
        open-design chart 沒有宣告 `auth` 區塊、也沒有 values.schema.json——是**隱性
        契約**：哪天 mcp-server 補了 `auth:`（很合理的演進），這幾顆就會無聲吃下一
        把不該給它的憑證。存量服務要補鑄有既定路徑（Reset 是唯一換發入口，本來就
        會重跑本閘門），所以「先鑄起來放」買不到任何東西。
        """
        self.ensure_one()
        return bool(self.template_id._gateway_external_path())

    def _gateway_public_url(self):
        """本服務的 gateway 完整網址（chart 無 gateway 或服務無 subdomain 時回 ''）。

        #530 的**交付**判準本體：controller 的 `_gateway_credentials` 用它決定要不要
        把明文憑證交給租戶，service payload 的 `has_gateway`（前端文案的 gate）也鏡射
        它。三處讀同一個 method，不各自重算——否則「畫面說會換發、回應裡卻沒有」這種
        漂移兩邊都不會報錯。
        """
        self.ensure_one()
        external_path = self.template_id._gateway_external_path()
        if not external_path:
            # 短路：絕大多數 template 沒有 gateway，先擋掉才不會為了清單裡的每一列
            # 服務都去查一次 paas_domain（_public_base_url 走 ir.config_parameter）。
            return ''
        return self._build_gateway_public_url(external_path, self._public_base_url())

    def _get_secret_record(self):
        self.ensure_one()
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.id)],
            limit=1,
        )

    def _set_admin_password_secret(self, admin_password, gateway_api_key=None):
        """建立／更新本服務的憑證記錄（create-or-write，secret 的**唯一**寫入點）。

        #530：``gateway_api_key`` 未指定、記錄上還沒有值、**且 chart 真的有對外
        gateway**（``_chart_has_gateway``）時在這裡補鑄。收斂在這一處而不是讓每條
        呼叫路徑各自記得補——「有憑證記錄 ∧ chart 有 gateway ⟹ 兩把憑證都在」是狀態
        不變式，逐路徑 stamp 必然漏（見 pitfall
        reaped-since-single-path-stamp-gc-immunity：gate 欄位只在單一路徑 stamp，
        其他路徑到達同狀態的列就永久缺值）。Reset 顯式帶新值以強制換發（它同樣先
        判 ``_chart_has_gateway``，沒有 gateway 就傳 None 而不是硬換一把）。
        """
        self.ensure_one()
        Secret = self.env['woow_paas_platform.cloud_service_secret'].sudo()
        secret = self._get_secret_record()
        vals = {'admin_password': admin_password, 'updated_at': fields.Datetime.now()}
        if (gateway_api_key is None and not (secret and secret.gateway_api_key)
                and self._chart_has_gateway()):
            gateway_api_key = self._generate_admin_password()
        if gateway_api_key:
            vals['gateway_api_key'] = gateway_api_key
        if secret:
            secret.write(vals)
            return secret
        return Secret.create(dict(vals, service_id=self.id))

    def _build_mcp_sensitive_values(self, admin_password=None, gateway_api_key=None):
        """組出送進 chart 的敏感值（**不寫回 helm_values**，另走 operator 的分離通道）。

        #530：gateway API key 走 ``auth.apiServerKey``——chart 憑證解析優先序的第一段
        就是「顯式 values 值」，所以平台供給的 per-service key 會勝過 chart 自產。
        它有**自己的**閘門 ``_chart_has_gateway``（seed 的 gateway.externalPath），
        不與 admin_password 的 ``has_admin_gui`` 共用：has_admin_gui=True 的 8 個模板
        裡只有 hermes-agent 有 gateway，共用閘門會把這個鍵送給另外 7 個並不吃它的
        chart（今天無害只因那些 chart 沒宣告 auth 區塊——隱性契約，見
        ``_chart_has_gateway`` 的 docstring）。兩個閘門都不是 slug allowlist。
        """
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
            if gateway_api_key is None:
                gateway_api_key = secret.gateway_api_key
        values = {'config': {'sensitive': {'admin_password': admin_password}}}
        # #530：沒有 key 時**整個鍵不送**，而不是送空字串。chart 的第一段優先序拿到
        # 顯式空值就不會落到第二段「查既有 Secret」，等於把 chart 自產、agent 正在
        # 用的那把洗掉 → gateway 當場拒絕所有請求。存量服務走 Reset 換發。
        # `_chart_has_gateway` 是 fail-closed 的第二道：鑄造閘門已經讓沒有 gateway 的
        # chart 不會有 key，但存量列（閘門上線前鑄的）仍可能有值，供給面必須自己判，
        # 不能靠「上游應該不會給我髒資料」。
        if gateway_api_key and self._chart_has_gateway():
            values['auth'] = {'apiServerKey': gateway_api_key}
        return values

    @staticmethod
    def _strip_mcp_sensitive_values(values):
        """剝掉不得落庫（helm_values）的敏感子樹。

        ⚠ 這層是 **path-aware** 的：新增一條敏感值通道時**必須**在這裡補對應路徑，
        否則那個值會留在 ``helm_values`` 並經服務詳情 API 明文流出，而只掃既有路徑
        的測試不會紅（#530 就是這個形狀：``auth.apiServerKey`` 不在 config.sensitive
        底下）。與 ``PaasController._sanitize_helm_values`` 的遮罩層是兩道獨立防線，
        新增路徑要**兩邊都補**。
        """
        if not values or not isinstance(values, dict):
            return values
        result = json.loads(json.dumps(values))
        config = result.get('config')
        if isinstance(config, dict):
            config.pop('sensitive', None)
        # #530 gateway API key。只 pop 這一個鍵、不動整個 auth 子樹——同層還有
        # 非憑證欄位（PostgreSQL 家族 seed 的 auth.username／auth.database）。
        auth = result.get('auth')
        if isinstance(auth, dict):
            auth.pop('apiServerKey', None)
        return result

    # ------------------------------------------------------------------
    # #559：helm_values 的欄位級授權（groups='base.group_system'）之後，
    # 平台程式碼存取它的**唯二**入口。
    #
    # 為什麼欄位要有 groups=：`helm_values` 裝的是租戶親手填的憑證
    # （odooPassword／smtpPassword／auth.*ApiKey）。#535 把服務詳情 API 的遮罩
    # 改成 spec-driven，但那道遮罩在 **controller 層**；欄位本身沒有 groups= 時，
    # 租戶可以直接打 `POST /web/dataset/call_kw` 送 `read(['helm_values'])`
    # **繞過整個 controller 遮罩層**拿到明文。實測坐實（#559）：最低權限
    # （workspace role=guest）的 `base.group_user` 租戶讀得到完整明文，而同一條
    # 路徑上讀 `n8n_owner_password`（有 groups=）會被 AccessError 擋下
    # ——鑑別項證明了「有 groups= 就會被擋」在這條路徑上確實生效。
    #
    # 為什麼要這兩個 helper：groups= 擋的不只是 RPC，**連平台自己的 ORM 屬性存取
    # 也一併擋掉**（實測：`_format_service` 的 `service.helm_values` 直接拋
    # AccessError，租戶的服務詳情頁與 reconfigure 全掛）。租戶**應該**看得到自己
    # 的設定——只是要走 BFF、經 `_sanitize_helm_values` 遮罩後才出去。所以合法路徑
    # 需要提權，而提權集中在這裡、不散落成一堆 `.sudo()`。
    #
    # ⚠ 呼叫端的義務：這兩個 helper **只提升欄位級權限，不做記錄級授權**。
    # 呼叫前必須已經確認呼叫者有權碰這筆 service（controller 端是
    # `workspace.check_user_access()` ＋ ir.rule 在 browse 後的第一次讀）。
    # 不要在「還沒授權的 recordset」上呼叫它們——那才是
    # docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md 講的那種洞。
    # ------------------------------------------------------------------

    def _helm_values_dict(self):
        """讀出 helm_values 並解析成 dict（空值／未設定回 ``{}``）。

        解析失敗時**照原樣拋出**，與改動前各呼叫點的行為一致（那些點原本就是
        裸 ``json.loads``）——不在這裡吞掉，免得壞掉的 JSON 變成靜默的空設定。
        """
        self.ensure_one()
        raw = self.sudo().helm_values
        return json.loads(raw) if raw else {}

    def _apply_helm_values(self, values, extra_vals=None):
        """寫回 helm_values（會套用與 ``create()`` 相同的敏感值剝除）。

        ``extra_vals`` 裡的其他欄位**刻意不提權**、走呼叫者自己的身分寫入，
        這樣 ir.rule 對它們照常生效——提權只落在 helm_values 這一格。
        """
        self.ensure_one()
        if extra_vals:
            self.write(extra_vals)
        stripped = self._strip_mcp_sensitive_values(values)
        self.sudo().write({'helm_values': json.dumps(stripped)})

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

    def _cron_reconcile_release_facts(self):
        """#627 (i)：抓「非由 Odoo 發起」的 chart 變動——**唯一會看 running 的路徑**。

        ## 為什麼需要獨立一條

        #552 層① 的第五條回寫路徑是 poll，設計目的就是抓「有人直接對叢集下
        `helm upgrade`、Odoo 完全不知道」。但 poll 的兩個入口與
        ``_cron_check_deploying_services`` 的 domain **都只收轉移狀態**
        （deploying／upgrading／initializing／deleting）——而那種變動發生時服務正是
        ``running`` ⇒ **要抓的場景與能執行的場景不相交**（#627 缺陷 (i)，stg
        `svc-bd9d6ed3` 漂移 3 天無人發現）。

        ## 與 ``_poll_and_update_status`` 的根本差異：**絕不碰 state**

        那個函式會依 release status 推狀態機（deployed→running、failed→error…）。
        對 ``running`` 服務跑它，等於讓一條對帳工作去改生命週期狀態——叢集暫時不健康
        就會把租戶的服務打成 ``error``，而 error 沒有任何路徑會自己 poll 回來。
        ⇒ **本 cron 只做「讀 → 比對 → 回寫 release 事實」**，一行 state 都不寫。
        repo-lint 的 ``poll-release-facts-single-source`` 判準 5 機械強制這件事。

        ## 頻率：每日一次，先不要調快

        漂移的後果是**帳面錯誤不是服務中斷** ⇒ 不需要分鐘級偵測。要不要縮短請看下面
        那條 log 累積出來的資料，**不要用直覺**——目前對「漂移多常發生」的全部認識
        只有 stg 一筆軼事。

        ## Log：**改 0 筆也要印**，而且要印出掃描範圍與是哪幾筆

        只在有改動時才記，會讓「一直沒漂移」與「cron 根本沒跑」的日誌畫面完全相同；
        只印筆數不印範圍，則會讓「suspended 沒漂移」與「suspended 根本沒被看」相同。
        兩者是同一個家族，差別只在它升到了範圍那一層。

        ## ⚠ 代價：`write_date` 從此每天變動

        本 cron 每天都會 stamp ``last_release_sync_at``（即使沒有漂移），而那是一次
        真正的 ORM write ⇒ ``write_date`` 每天都會動。**在此之前 `write_date` 可以
        用來判斷「這筆帳本多久沒被平台碰過」，#552／#627 的調查用過它兩次**（判定
        某次 helm upgrade 不是 Odoo 發起的、以及證明升版沒有順手回寫）。那個診斷
        能力從本版起消失——要判斷「平台最後一次主動操作」請改看 ``last_upgraded_at``
        ／``deployed_at``。這個取捨是刻意接受的（#627 留言有記）。
        """
        from ..services.paas_operator import get_paas_operator_client

        services = self.search([('state', '=', 'running')])

        # 掃描範圍要能被讀出來：只印「掃描 N 筆」會讓「範圍外的沒漂移」與「範圍外的
        # 根本沒被看」畫面相同。逐狀態點名，不是只給總數。
        out_of_scope = {}
        for svc in self.search([('state', '!=', 'running')]):
            out_of_scope[svc.state] = out_of_scope.get(svc.state, 0) + 1
        scope_note = (
            '另有 %s 不在本 cron 範圍' % ' / '.join(
                '%s %d' % (state, count)
                for state, count in sorted(out_of_scope.items()))
            if out_of_scope else '無其他狀態的服務')

        client = get_paas_operator_client(self.env)
        if not client:
            # 沒有 client 時**大聲說**：靜默 return 會讓「operator 沒設定」與
            # 「掃過了都沒漂移」的日誌畫面相同，正是本 cron 要避免的形態。
            _logger.warning(
                "reconcile-release-facts: operator client 未設定，本輪未執行"
                "（掃描範圍本應為 %d 筆 running，%s）", len(services), scope_note)
            return

        changed, failed = [], []
        for service in services:
            try:
                # 逐筆 savepoint：一筆失敗不得中斷整批。用 savepoint 而非裸 try 是
                # 因為 OperationalError 之後 cursor 已不可用，後續會連帶全部失敗。
                with self.env.cr.savepoint():
                    status = client.get_status(
                        namespace=service.helm_namespace,
                        release_name=service.helm_release_name,
                        include_vm=service._needs_vm_status(),
                    )
                    facts = service._release_facts(status.get('release', {}))
                    # 舊值必須**在寫入之前**收起來：寫完之後 `service[field]` already
                    # 是新值，log 就只印得出「現在是什麼」而不是「從什麼變成什麼」，
                    # 而後者才是判斷漂移頻率與幅度的那份母體資料。
                    drift = {
                        field: (service[field], value)
                        for field, value in facts.items()
                        if field != 'last_release_sync_at'
                        and service[field] != value
                    }
                    # 有沒有漂移都寫同一個 dict：`drift` 只用於 log。
                    #
                    # 為什麼不在無漂移時只寫 `last_release_sync_at` 那一格：那會在這裡
                    # 出現一個 release 事實欄位的**字面鍵**，也就是 #627 的成因形態
                    # （逐路徑列欄位），repo-lint 判準 2 會擋。而且沒有好處——版本欄位
                    # 寫進相同的值本來就沒有語意變化，`write_date` 反正每天都會動
                    # （見本方法 docstring 的代價段）。
                    service.write(facts)
                    if drift:
                        changed.append((service, drift))
            except Exception as exc:
                failed.append((service, exc))

        _logger.info(
            "reconcile-release-facts: 掃描 %d 筆 running（%s），%d 筆漂移已修正"
            "%s", len(services), scope_note, len(changed),
            ("，%d 筆讀取失敗" % len(failed)) if failed else "")
        for service, drift in changed:
            _logger.info(
                "  id=%s %s  %s", service.id, service.name,
                '  '.join(
                    '%s %s→%s' % (field, old or '(空)', new or '(空)')
                    for field, (old, new) in sorted(drift.items())))
        for service, exc in failed:
            _logger.warning(
                "  id=%s %s 讀取失敗：%s（本筆略過，不影響其餘）",
                service.id, service.name, exc)

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

    def _rescale_halted_after_upgrade(self, client):
        """upgrade 之後把仍在停權中的 workload 重新收回停機（#482）。

        三條 upgrade 路徑（``action_redeploy`` / controller ``_update_service``
        / ``_reset_admin_password``）都以 ``reset_values=True`` 重新渲染 chart，
        而平台組出的 values **完全不帶 replicas** —— 容器型的 replicas 鍵名因
        chart 而異（bitnami 是 ``primary.replicaCount``、n8n 又是另一套），平台
        對任意第三方 chart 無法猜鍵名。VM 型能在 values 注入 ``vm.runStrategy``
        只是因為 haos 是自家 chart、鍵固定。

        於是 chart 預設的 ``replicas: 1`` 會把停權（欠費）中的容器直接喚醒：
        2026-07-31 docker-desktop 實測（真 operator + helm 3.13.3 + 真 chart），
        n8n 的 Deployment 與 postgresql 的 StatefulSet 都 0→1，且 pod 實測
        Running/ready=true —— 喚醒是實質的，不只是欄位。
        ``subscription_lifecycle_cron`` 每天只跑一次，最壞情況白燒約一天。

        收斂走與 :meth:`_do_suspend` 相同的外科式 ``scale_release``：與 chart
        無關，且對 VM 型冪等（operator 把 ``replicas=0`` 映射成
        ``runStrategy: Halted``——依賴 operator #488 的三 workload 語意）。

        非停權服務一律 no-op —— 這裡誤動就是把正常服務關掉。失敗不 raise
        （upgrade 本身已經成功，讓整個操作看起來失敗是誤導），但**必須留下
        可觀察的痕跡**：停權服務照跑就是計費繞過，靜默吞掉等於把帳務問題藏起來。

        Returns:
            bool: ``True`` ＝ 這台的 workload 已確認被送去停機（或本來就不該停機）；
            ``False`` ＝ **沒能**收回（operator 不可達、或連 release 座標都沒有）。
            三條 upgrade 路徑忽略回傳值——它們的 error_message 已足夠。#497 的
            migration 用它決定要不要在 ``error_message`` 留可機械盤點的殘留記號。
        """
        self.ensure_one()
        if not self._is_halted():
            return True
        if not (client and self.helm_release_name and self.helm_namespace):
            return False
        try:
            client.scale_release(
                self.helm_release_name, self.helm_namespace, replicas=0,
            )
            _logger.info(
                "[CLOUD_SVC] _rescale_halted_after_upgrade scaled to 0 "
                "service=%s release=%s ns=%s",
                self.id, self.helm_release_name, self.helm_namespace,
            )
            return True
        except Exception as e:
            _logger.warning(
                "[CLOUD_SVC] _rescale_halted_after_upgrade failed service=%s: %s "
                "— suspended workload may still be running (billing bypass)",
                self.id, e,
            )
            self.write({
                'error_message': (
                    f'Service is suspended but could not be scaled back down '
                    f'after the upgrade: {e}'
                ),
            })
            return False

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

    # ------------------------------------------------------------------ #
    #  轉移狀態的 age timeout（#489 / F-423-1 Step 2）                     #
    # ------------------------------------------------------------------ #

    def _is_timeout_governed(self):
        """本服務目前的狀態是否受 age timeout 管轄。

        兩層 poll 共用這個判準（controller 那層要用它決定 pending-* 要不要記
        stuck 理由），避免狀態清單在兩處雙寫後無聲漂移。
        """
        self.ensure_one()
        return self.state in _TIMEOUT_GOVERNED_STATES

    def _transition_anchor(self):
        """本輪狀態轉移的起算時間；``None`` ＝ 無法判定。

        取 ``deployed_at`` 與 ``last_upgraded_at`` 的**較晚者**，而不是依
        ``state`` 擇一。理由是寫入這兩個欄位的路徑與 state 不是一一對應：
        ``action_redeploy`` 寫的是 ``deploying``（首裝路徑的狀態）卻是 upgrade
        語意，依 state 擇一會取到**首裝**時間——一台部署了三個月的服務按下重新
        部署，第一輪 poll 就被判逾時。取較晚者天然涵蓋所有入口。

        （本 PR 一併把三個漏記錨的轉移入口補上 ``last_upgraded_at``：
        ``action_redeploy`` 的 upgrade 分支、``_reset_admin_password``、
        controller 的 rollback。少一個都會讓那條路徑用陳舊的錨誤殺。）

        回 ``None`` 的唯一情形是兩個欄位都空——只發生在 ``_execute_deploy``
        的 helm install 執行期間（install 回來就寫 ``deployed_at``，同一個
        transaction 內）。**此時不逾時**：沒有依據就不裁決，而誤殺不可自癒。
        這與「參數缺值走內建預設」的 fail-safe 方向鎖不衝突——那條鎖的是**設定
        漏設**（可預期、可補），這裡是**事實未知**（無從推算）。
        """
        self.ensure_one()
        stamps = [t for t in (self.deployed_at, self.last_upgraded_at) if t]
        return max(stamps) if stamps else None

    def _transition_timeout_minutes(self):
        """本服務的逾時門檻（分鐘）。VM 型與容器型各一檔。

        缺值、空白、非整數、非正數一律**退回內建預設**而不是停用逾時——否則
        這道保護會因為漏設一個參數而靜默失效（#404 apiServerCidrs 的教訓）。
        """
        self.ensure_one()
        is_vm = self._needs_vm_status()
        key = _VM_DEPLOY_TIMEOUT_PARAM if is_vm else _DEPLOY_TIMEOUT_PARAM
        default = (
            VM_DEPLOY_TIMEOUT_MINUTES_DEFAULT if is_vm
            else DEPLOY_TIMEOUT_MINUTES_DEFAULT
        )

        raw = self.env['ir.config_parameter'].sudo().get_param(key)
        # 未設定是正常的（走內建預設，不吵）。**設了但看不懂**才要吵——否則
        # 有人把門檻打錯字（"30m"、"0"）會靜默被忽略，而他以為自己調成功了。
        unset = raw in (None, False, '') or not str(raw).strip()
        try:
            minutes = int(str(raw).strip())
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            if not unset:
                _logger.warning(
                    "%s is %r (not a positive integer of minutes) — falling "
                    "back to the built-in default of %s minutes",
                    key, raw, default,
                )
            return default
        return minutes

    def _timeout_diagnosis(self, detail):
        """把就緒判定的 ``detail`` 翻成「維運該往哪查」的一句話。

        回空字串代表沒有對應的方向可給（fail-soft：訊息仍帶前綴與 detail）。
        """
        self.ensure_one()
        if self._is_halted():
            return _SUSPENDED_HINT
        for needle, hint in _TIMEOUT_HINTS:
            if needle in (detail or ''):
                return hint
        return ''

    def _check_transition_timeout(self, detail):
        """轉移狀態逾時就翻 ``error``；回傳「是否已翻」。

        **只變狀態與訊息**——不刪任何叢集資源、不觸發 rollback。卡住的 release
        原樣留在叢集裡供診斷，維運看完再決定要 redeploy 還是清掉。

        呼叫端契約：必須在 poll 的 optimistic locking（re-fetch ＋ 比對
        ``original_state``）**之後**呼叫，且 ``self`` 是 re-fetch 過的 record。
        逾時寫入走同一套鎖，否則會蓋掉別的 process 剛寫進去的狀態。
        """
        self.ensure_one()
        if self.state not in _TIMEOUT_GOVERNED_STATES:
            return False

        anchor = self._transition_anchor()
        if not anchor:
            _logger.warning(
                "Service %s (id=%s) is %s but has neither deployed_at nor "
                "last_upgraded_at — age timeout cannot be evaluated",
                self.name, self.id, self.state,
            )
            return False

        minutes = self._transition_timeout_minutes()
        age = fields.Datetime.now() - anchor
        if age.total_seconds() <= minutes * 60:
            return False

        hint = self._timeout_diagnosis(detail)
        message = f"Deployment timed out after {minutes} minutes: {detail}"
        if hint:
            message = f"{message} — {hint}"

        # error 分兩種來源，log 要能一眼分辨：這條是「平台判定逾時」，
        # `release_status == 'failed'` 那條是「operator 明確回報失敗」。
        _logger.error(
            "Service %s (id=%s) timed out in state %s after %s minutes "
            "(anchor=%s): %s",
            self.name, self.id, self.state, minutes, anchor, message,
        )
        self.write({'state': 'error', 'error_message': message})
        return True

    @api.model
    def _woow_migrate_refresh_transition_anchor_489(self):
        """把當下處於轉移狀態的服務的錨刷成 now（#489 上線的一次性窗口）。

        **為什麼需要**：合併前有三條轉移入口不記錨（``action_redeploy`` 的
        upgrade 分支、``_reset_admin_password``、controller 的 rollback），經它們
        進入 ``deploying`` / ``upgrading`` 的服務，錨是 ``deployed_at``＝**首裝**
        時間。`-u` 完成後第一次 cron 會拿那個陳舊的錨去判 age，於是**正在正常
        部署**的服務也會被判逾時——而誤殺不可自癒（poll 只撈轉移狀態，寫進
        ``error`` 後沒有任何路徑會把它 poll 回 ``running``）。

        本 method 給它們一個新鮮的起點，把那個窗口降到零。**代價**：真正已經
        卡住的那批（#456 / #482 那類）會晚一個門檻週期才被抓到——一次性，且
        它們本來就已經卡了不知道多久。

        只動 ``last_upgraded_at``，不動 ``deployed_at``——後者的語意是「首次
        部署時間」，還掛在前台 Overview 上，改它會說謊。錨取兩者較晚者，所以
        刷新 ``last_upgraded_at`` 就足以讓錨變新鮮。

        **只動轉移中的服務**：``running`` / ``suspended`` / ``error`` 一律不碰
        （它們的 ``last_upgraded_at`` 是真實的升級歷史，改了就是竄改紀錄）。

        **冪等**：無條件把「當下轉移中」那一批的錨寫成 now，不累加、不依賴前次
        執行結果。重跑的效果只取決於執行當下的 state 集合。

        ``active_test=False``：本 model 目前沒有 ``active`` 欄位（此 context 是
        no-op），但漏掉任何一筆就等於那筆被誤殺，所以明確寫死不靠 model 的現況。
        """
        stuck = self.sudo().with_context(active_test=False).search([
            ('state', 'in', list(_TIMEOUT_GOVERNED_STATES)),
        ])
        if not stuck:
            _logger.info(
                "#489 migration: no service in a transitional state — "
                "nothing to re-anchor"
            )
            return stuck

        stuck.write({'last_upgraded_at': fields.Datetime.now()})
        _logger.info(
            "#489 migration: re-anchored %d service(s) in a transitional "
            "state so the new deploy age timeout starts counting from now: %s",
            len(stuck),
            ', '.join(f"{s.name}(id={s.id},state={s.state})" for s in stuck),
        )
        return stuck

    @api.model
    def _woow_migrate_converge_stuck_suspended_497(self):
        """把 #482 合併前就卡死的停權服務收斂回 ``suspended``（一次性窗口）。

        **為什麼需要**：#493 從源頭修掉了「停權服務被 redeploy／reconfigure 汙染
        state」，但那只對**新**操作有效。合併前就已經被汙染的 row 仍在，而 #489／
        #491 的 age timeout 一上線就會把其中 ``deploying`` / ``upgrading`` 那批逐一
        打成 ``error``。那批的 ``pre_suspend_state`` 仍在＝:meth:`_is_halted` 仍為真
        ＝**它其實還在停權中**，於是撞上 ``subscription_lifecycle_cron`` 的分支落差：
        訂閱已恢復（``in_progress``）走分支 2，而分支 2 要求 ``state ==
        'suspended'`` 才 resume → 既不 resume 也不再 suspend ＝ **租戶付了錢、服務
        永遠回不來，且沒有任何排程會修好它**（#497）。

        **收斂集合**：``pre_suspend_state`` 有值（停權的權威旗標）且 state 落在
        :data:`_STUCK_SUSPENDED_STATES_497`。``initializing`` 也在內——容器型被
        upgrade 喚醒後（#482 的 A/B 實證），poll 會判 pod ready 而把它從
        ``deploying`` 推進 ``initializing``（:meth:`_poll_and_update_status` 不看
        :meth:`_is_halted`）。

        **不誤傷**：``pre_suspend_state`` 全 repo 只在 :meth:`_do_suspend` 寫、
        :meth:`_do_resume` 清，沒有 seed、沒有其他寫入點——所以「真部署失敗」的
        ``error`` 服務該欄位恆為 ``False``，天然落在集合外。不再疊訂閱狀態這種條件：
        那會漏掉欠費那批（它們同樣卡死，容器型還可能在燒錢）。

        **``running`` 刻意不收**：容器型被喚醒後 poll 會一路把它標成 ``running``，
        那批同樣帶著 stale 旗標，但這裡誤動的代價是**把正在跑的服務關掉**。它們交給
        放寬後的 lifecycle cron 分支 2 處理（訂閱有效 → ``_do_resume`` 清掉旗標、
        scale 1 對在跑的服務冪等＝零停機；欠費 → 分支 3／4 照舊 suspend），最壞
        延遲一個 cron 週期。

        🔴 **收斂 state 必須連 workload 一起收**：:meth:`_do_suspend` 開頭就
        ``if self.state == 'suspended': return``，所以一旦把 state 寫成
        ``suspended``，cron 的欠費分支便永遠早退——唯一還會 ``scale_release(0)`` 的
        路徑會被這個 migration 自己關掉。故先走 #493 的
        :meth:`_rescale_halted_after_upgrade`（外科式、與 chart 無關、對 VM 型冪等）
        再寫 state。

        **operator 失敗一律不 raise**：migration 拋例外＝整支 ``-u`` 失敗＝平台起不
        來。state 收斂是本地事實修正、不依賴叢集，所以 scale 失敗也照收斂（不收斂
        的話這筆 row 留在 ``error``，自癒路徑仍然是斷的）。代價是 workload 可能還在
        跑，因此**必須留下可機械盤點的記號**：``error_message`` 以
        :data:`_UNCONFIRMED_WORKLOAD_MARKER_497` 起頭，prod rollout 的 checklist 用
        它掃出「收斂了但未確認停機」的那批。

        **冪等**：收斂後的 row 是 ``suspended``，不再落在判別式內，重跑即空集合。
        """
        stuck = self.sudo().with_context(active_test=False).search([
            ('pre_suspend_state', '!=', False),
            ('state', 'in', list(_STUCK_SUSPENDED_STATES_497)),
        ])
        if not stuck:
            _logger.info(
                "#497 migration: no stuck suspended service — nothing to converge"
            )
            return stuck

        from ..services.paas_operator import get_paas_operator_client

        client = get_paas_operator_client(self.env)
        unconfirmed = self.browse()
        for service in stuck:
            has_coordinates = bool(
                client and service.helm_release_name and service.helm_namespace
            )
            confirmed = service._rescale_halted_after_upgrade(client)
            vals = {'state': 'suspended'}
            if confirmed:
                # age timeout 留下的「部署逾時」訊息會把維運指向叢集，而真因是停權。
                vals['error_message'] = False
            elif has_coordinates:
                # helper 已把具體例外寫進 error_message；套上固定前綴讓它可被盤點。
                vals['error_message'] = (
                    f'{_UNCONFIRMED_WORKLOAD_MARKER_497}：{service.error_message}'
                )
            else:
                vals['error_message'] = (
                    f'{_UNCONFIRMED_WORKLOAD_MARKER_497}：'
                    'operator client 或 release 座標不可用'
                )
            if not confirmed:
                unconfirmed |= service
            service.write(vals)

        _logger.info(
            "#497 migration: converged %d stuck suspended service(s) back to "
            "'suspended': %s",
            len(stuck),
            ', '.join(f"{s.name}(id={s.id})" for s in stuck),
        )
        if unconfirmed:
            _logger.warning(
                "#497 migration: %d service(s) converged but their workload "
                "could not be confirmed stopped (billing bypass risk) — %s",
                len(unconfirmed),
                ', '.join(f"{s.name}(id={s.id})" for s in unconfirmed),
            )
        return stuck

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

    def _release_facts(self, release):
        """operator 回報的 release 事實 → 帳本回寫欄位。**兩份 poll 的單一來源。**

        #627：poll 有**兩份實作** —— `PaasController._update_service_status`
        （租戶打 list／detail 端點時走）與本模組的 `_poll_and_update_status`
        （**cron** 走）。原本兩份各自組自己的 write vals，結果 controller 那份
        寫了 `helm_chart_version`、cron 這份只寫 `helm_revision`，而唯一斷言
        「`release_facts` 必須到得了每個 write 分支」的測試**整份只驅動
        controller** ⇒ cron 那份整份沒有回寫也全綠（#552 層① 的第五條回寫路徑
        因此在無人值守時失效）。

        ⇒ **欄位集合收斂在這裡**：兩份實作都必須經由本方法取得 write vals。日後
        新增第三個 release 事實欄位時，兩份會同時跟上，不會再各漏各的。

        ⚠ 這裡刻意**只收斂「寫什麼」，不收斂「什麼時候寫」** —— 兩份的控制流
        （post-deploy init 的兩套實作、404／`deleting` 分支的 savepoint 差異，見
        `tests/test_poll_teardown_rollback_525.py`）差異是另一件事，收斂它的爆炸
        半徑遠大於本修法，見 #627 的討論。

        `or self.helm_chart_version`＝舊 operator 不回這個欄位（或回空字串）時
        **保留帳本原值**：未知不得覆蓋成空。
        """
        self.ensure_one()
        return {
            'helm_revision': release.get('revision', self.helm_revision),
            'helm_chart_version': (
                release.get('chart_version') or self.helm_chart_version
            ),
            # #627 (i)：`last_release_sync_at` 也走這個咽喉 ⇒ **三個寫入者**
            # （兩份 poll ＋ reconciliation cron）自動全部 stamp，不必逐路徑補。
            # 這正是本方法存在的理由，也避免這個新欄位重演 #627。
            #
            # ⚠ 語意是「最後一次**問過** operator」，不是「最後一次值有變」——
            # 所以無漂移時 reconciliation cron 仍會寫它（見該 cron 的 docstring）。
            # ⚠ 必須 `fields.Datetime.now()`（UTC naive）不能 `datetime.now()`
            # （本機時區）；repo-lint 的 transition-anchor-utc 已把本欄位納入覆蓋。
            'last_release_sync_at': fields.Datetime.now(),
        }

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
                # #525 B5：與 controller 的 `_update_service_status` 同形，缺陷
                # 也同形——`unlink()` 中途拋錯 → 例外逸出這個 except handler
                # （同層 sibling 的 `except Exception` 接不到）→ 被唯一呼叫端
                # `_cron_check_deploying_services` 迴圈的 catch-all 吞掉 → cron
                # 照常 commit 刪一半的狀態。
                #
                # ⚠ **今天這條分支不可達**：唯一的生產呼叫端 cron 的 domain 是
                # `state in ('deploying','upgrading','initializing')`（本檔
                # `_cron_check_deploying_services`），不含 `deleting`，所以
                # `self.state == 'deleting'` 在被呼叫到時恆為 False。這裡的
                # savepoint 是**結構防護**：domain 一旦補上 `deleting`（controller
                # 側的 `_list_services` 就是這樣列的），殘留立刻可達，而那時不會
                # 有人記得回來補這一行。形態同
                # `subscription_lifecycle_cron._cron_enforce_subscription_lifecycle`
                # 的 per-item savepoint。
                with self.env.cr.savepoint():
                    self.unlink()
            else:
                _logger.warning("Error polling status for %s: %s", self.name, e)
            return
        except Exception as e:
            _logger.warning("Error polling status for %s: %s", self.name, e)
            return

        release = status.get('release', {})
        release_status = release.get('status', '')
        # Re-fetch for optimistic locking
        service = self.browse(self.id)
        if not service.exists() or service.state != original_state:
            return

        # #627：欄位集合來自 `_release_facts`（與 controller 那份同一個來源），
        # **不要**在下面的 write 分支裡各自列欄位——那正是 cron 這份漏掉
        # `helm_chart_version` 的方式。
        release_facts = service._release_facts(release)

        # 進度先落地（與就緒判定無關，且未就緒時才是最有用的時候）。
        service._store_import_progress(status)

        # #489：「這一輪 poll 沒能讓服務離開轉移狀態」的理由，供 age timeout
        # 組診斷訊息。None ＝ 這一輪有進展（就緒或明確失敗），不必判逾時。
        stuck_detail = None

        if release_status == 'deployed':
            all_ready, detail = service._is_release_ready(status)
            if not all_ready:
                service._log_not_ready(detail)
                stuck_detail = detail

            if all_ready:
                template = service.template_id
                needs_init = (
                    template.post_deploy_init_type
                    and template.post_deploy_init_type != 'none'
                )

                if needs_init and original_state == 'deploying':
                    service.write({
                        'state': 'initializing',
                        **release_facts,
                        'error_message': False,
                    })
                    service._model_run_post_deploy_init(client)

                elif needs_init and original_state == 'initializing':
                    service._model_run_post_deploy_init(client)

                elif needs_init and original_state == 'upgrading' and service.addon_update_pending:
                    service.write({
                        'state': 'initializing',
                        **release_facts,
                        'error_message': False,
                        'init_retries': 0,
                        'init_error': False,
                    })
                    service._model_run_post_deploy_init(client)

                else:
                    service.write({
                        'state': 'running',
                        **release_facts,
                        'error_message': False,
                    })

        elif release_status == 'failed':
            service.write({
                'state': 'error',
                **release_facts,
                'error_message': release.get('description', 'Deployment failed'),
            })

        elif service._is_timeout_governed():
            # `pending-install` / `pending-upgrade` / `pending-rollback` /
            # `unknown`。既有碼對這些一律靜置等下一輪——**但它們同樣會永久卡**：
            # helm 的 subprocess 被 SIGKILL 在 rollback 途中就會把 release 留在
            # `pending-rollback`，沒有任何東西會再推它（同
            # `docs/reference/gotchas/helm-atomic-failure-double-timeout.md` 的實錄）。故一併
            # 納入封頂：狀態不動，只是讓 age timeout 有話可說。
            # （限縮在受管狀態，避免給 `deleting` / `initializing` 添 log 噪音。）
            #
            # ⚠ 刻意**不**加 `HARD:` 前綴：`pending-install` 在正常首裝時就會被
            # 撞到（helm 還在跑、cron 已經在 poll），升 warning 會讓每一次正常
            # 部署都刷一行「硬失效」——那正好稀釋掉 HARD 的語意（它專指「不會
            # 自己好」）。卡住與否由時間分辨，逾時本身會印 error 級。
            stuck_detail = f"helm release status is {release_status or 'unknown'}"
            service._log_not_ready(stuck_detail)

        if stuck_detail:
            service._check_transition_timeout(stuck_detail)

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
                merged_helm_values = None
                if template.mcp_api_key_helm_path:
                    try:
                        # #559：走唯一入口（欄位有 groups=，屬性直讀會 AccessError）
                        current_values = self._helm_values_dict()
                        api_key_nested = _unflatten_dotpath_keys(
                            {template.mcp_api_key_helm_path: real_api_key}
                        )
                        merged_helm_values = _deep_merge(current_values, api_key_nested)
                    except (json.JSONDecodeError, TypeError):
                        _logger.warning("Failed to update helm_values with real API key for service %s", self.name)
                if merged_helm_values is not None:
                    self._apply_helm_values(merged_helm_values, extra_vals=update_vals)
                else:
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
            # #497 縱深：已經在停權中的服務（旗標仍在、state 被別的路徑改寫過）再走
            # 一次 suspend 時，**不覆寫**既有的停權前狀態。這裡讀的是**當下**的
            # state，而卡死 row 的當下 state 是 age timeout 寫的 'error' —— 覆寫後
            # 租戶付款復權會拿回一台顯示 error 的服務（癒後帶傷）。首次停權（旗標為
            # 空）仍如實記下當下狀態。
            'pre_suspend_state': self.pre_suspend_state or (
                self.state if self.state in ('running', 'error') else 'running'
            ),
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
        # #482 縱深：guard 讀 _is_halted() 的聯集，而非只認 state == 'suspended'。
        #
        # 舊 guard 讓任何把 state 改寫掉的路徑（upgrade 寫 deploying/upgrading）
        # 都變成單向門：服務仍在停權中，卻再也回不來 —— 租戶付了款也復不了權，
        # 只能人工 write({'state': 'suspended'}) 再 resume（#482 的脫困 runbook）。
        # 主修已讓那三條路不再汙染 state；這裡保證**即使**將來又有新路徑改寫
        # state，或要處理既有已卡死的資料，復權仍然走得通。這是縱深，不是主修的
        # 替代品。
        if not self._is_halted():
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
        # ⚠ 這裡**刻意沒有**全域上限守衛。曾經有過（#501 B3，擋「先建成 `deleting`
        # 再 write 回計數態」的繞道），已移除，理由記在 #519 與 PR #517 的第二輪覆核：
        #
        # 1. 它會誤傷刪除失敗的善後路徑。`_delete_service` 先寫 `state='deleting'`，
        #    uninstall 失敗後在 except 分支寫 `state='error'`——那是「從終態寫回非
        #    終態」，守衛判定為進入者。而裁決點 `_reject_reason_for_new_instance`
        #    第一件事是 paused 檢查、**在計數之前就 return** ⇒ 營運一勾「暫停販售」，
        #    任何刪除失敗都會 raise、整筆 rollback，`error_message` 遺失、狀態退回，
        #    租戶看到的是「目前暫停販售」。與設定畫面 help 明載的「只擋新下單，不影響
        #    已開通的服務」直接矛盾。
        # 2. 它擋不完。`write({'template_id': …})` 不帶 state ⇒ 守衛不求值；
        #    `state` 是 nullable Selection（無 `required=True`），插一個
        #    `write({'state': False})` 當跳板即可整段跳過。
        #
        # 根因是租戶對本 model 有直接 write 權（`ir.model.access.csv`，#519）。用
        # write() 守衛打地鼠會引入誤傷卻仍留下繞道；正解是收緊 ACL，一旦租戶沒有
        # 直接 write 權，所有繞過路徑一次關閉。**create() 的閘門是防意外、不是安全
        # 邊界**——不要在這裡補守衛來製造「已經守住」的錯覺。
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

    @api.model
    def _count_active_for_template(self, template):
        """該 template 的**全站**活躍實例數（#501 全域上限與售罄現算共用）。

        口徑與 `_find_active_for_partner` 同源（`state not in TERMINAL_STATES`，
        而 TERMINAL_STATES 只有 `deleting`）——`suspended` 與 `error` 照樣佔名額，
        因為它們隨時可能被復原，位置沒有真的釋放出來。兩邊口徑漂移會讓同一台服務
        在兩個上限裡算法不同。

        ⚠ `sudo()` 是**正確性前提，不是效能選擇**：ir.rule
        `cloud_service_rule_workspace_member` 把可見範圍限制在「呼叫者是成員的
        workspace」，而全域計數必然跨租戶——不 sudo 就會少算，守衛靜默 fail-open。
        這比 `_find_active_for_partner` 更依賴 sudo（那邊同 partner 常在同一個
        workspace，這邊必然不是）。
        回傳的是計數而非 recordset，跨租戶的 row 從不外流給呼叫端。
        """
        return self.sudo().search_count([
            ('template_id', '=', template.id),
            ('state', 'not in', list(TERMINAL_STATES)),
        ])

    @api.model
    def _claim_global_cap_slots(self, records):
        """對每個「有上限的 template」寫一次衝突列，讓並發第二者拿到 40001。

        機制與取捨見 `_GLOBAL_CAP_CONFLICT_SQL` 的註解。排序去重是為了讓多筆
        vals 跨 template 時的加鎖順序在所有 tx 中一致（避免死鎖）；沒有上限的
        template 完全不進來——否則全站每次建服務都在同一列上排隊，代價會落到
        與限量無關的商品身上。

        用原生 SQL 而非 ORM `write()`：只需要製造 row 版本變更，不要觸發
        template 的 constrains／cache 失效／`write_date` 之外的任何副作用。
        """
        capped_ids = sorted({
            rec.template_id.id for rec in records
            if rec.template_id._global_instance_cap()
        })
        for tpl_id in capped_ids:
            self.env.cr.execute(_GLOBAL_CAP_CONFLICT_SQL, (tpl_id,))

    @api.model
    def _enforce_supply_gate(self, records):
        """逐筆重跑供給閘門（停售／售罄），超過就 raise 讓整筆撤回。

        判準的唯一定義點是 `cloud_app_template._reject_reason_for_new_instance`
        ——marketplace payload 的 availability 呼叫的是同一個，不是第二份判準。
        這裡傳的計數**已含剛落庫的這筆**（順序：寫入 → 取衝突列 → count →
        超過就 raise），所以判定式是嚴格大於。

        供給閘門先於 `max_per_partner` 判：「這個商品現在還賣不賣」邏輯上先於
        「你還能不能再買一台」，訊息也比較貼近租戶當下的處境。
        """
        for rec in records:
            template = rec.template_id
            reason = template._reject_reason_for_new_instance(
                lambda tpl=template: self._count_active_for_template(tpl))
            if reason == 'paused':
                raise SupplyGateError(_(
                    '「%(tpl)s」目前暫停販售。', tpl=template.name))
            if reason == 'unpinned_chart':
                # #552 層 4：這是**平台端設定缺陷**不是營運狀態，所以 log 成 error
                # ——租戶看到的是下面那句，但營運要在 log 裡看得到是哪個 template。
                # 只在真的有人嘗試開通時才 log（判準本身每次 marketplace 渲染都會
                # 跑一次，寫在裁決點會洗版）。
                _logger.error(
                    "供給閘門擋下開通：template %s (id=%s, slug=%s) 沒有 "
                    "helm_chart_version ⇒ helm 會解析成 registry 當下的 latest。"
                    "請在 seed 釘版後重新發版（#552）。",
                    template.name, template.id, template.slug,
                )
                raise SupplyGateError(_(
                    '「%(tpl)s」目前無法開通：平台尚未指定此商品的部署版本。'
                    '請聯繫客服。', tpl=template.name))
            if reason == 'sold_out':
                raise SupplyGateError(_(
                    '「%(tpl)s」目前已售罄，暫時無法開通新的實例。',
                    tpl=template.name))

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

        ⚠⚠ **上面那段「READ COMMITTED」是錯的前提**（#345 起沿用，#518 證偽）。
        Odoo 的 cursor 是 **REPEATABLE READ**，取鎖**不會**刷新 snapshot，所以
        「輸家取鎖後的 count 看得到贏家已 commit 的列」不成立——實測兩並發各自
        只看得到自己那筆、雙雙放行。`max_per_partner` 因此在並發下仍可被繞過；
        它的修法（衝突列粒度需為 (template, partner)）另見 #518，本方法未修。
        HA PaaS 由 `provision_partner_id` 的 partial unique index 兜底，不受影響。

        #501 供給閘門（全域實例上限＋營運停售）改用**衝突列**而非 advisory lock：
        `_claim_global_cap_slots` 對 template row 做 UPDATE，並發第二者在 RR 下拿到
        SQLSTATE 40001，交給 `retrying()` 以新 tx／新 snapshot 重試（見
        `_GLOBAL_CAP_CONFLICT_SQL` 的完整論證與實測）。判準本身在
        `cloud_app_template._reject_reason_for_new_instance`，marketplace 的
        availability payload 呼叫的是同一個——單點裁決，不做兩份漂移。

        ⚠ 衝突列只有 template 一維，代價是**同一 template 的全站下單被序列化**：
        輸家會阻塞到贏家整個 tx 結束（不只計數那一瞬，還包含建訂閱等後續工作），
        且會多付一次重試。HAOS 是 VM 型、上限個位數、下單頻率極低，可接受。
        **不要把這個形制直接套到熱門的容器型 template**——那需要先量測 create tx
        的實際長度，或改用更細的分片鍵。

        ⚠ 這條路徑要求呼叫端**不得吞掉並發例外**：`SerializationFailure` 是
        `Exception` 子類，controller 若用裸 `except Exception` 收掉，`retrying()`
        永遠看不到、不會重試，而且當下 PG 交易已 aborted，後續 flush 會再炸。
        `_create_service` 已顯式重拋（見該處註解）。savepoint 幫不上忙——40001 之後
        整個 tx 必須中止，`ROLLBACK TO SAVEPOINT` 繼續跑只會帶著同一個陳舊 snapshot。

        ⚠ 本方法對所有入口一律生效、無 su/group 豁免（否則 TransactionCase 假綠，
        見 docs/decisions/patterns/su-env-exempt-guard-false-green.md）。
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
        self._claim_global_cap_slots(records)
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
        self._enforce_supply_gate(records)
        for rec in records:
            limit = rec.template_id.max_per_partner
            if limit and rec.workspace_id.owner_id:
                partner = rec.workspace_id.owner_id.partner_id
                count = len(self._find_active_for_partner(rec.template_id, partner))
                if count > limit:
                    raise SupplyGateError(_(
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
