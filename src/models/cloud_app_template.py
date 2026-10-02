import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services.woow_odoo_readiness import WOOW_ODOO_CHART, chart_name_of

_logger = logging.getLogger(__name__)

# `admin_username_helm_path` 的合法形態：點分式、每段是 values 鍵名。
ADMIN_USERNAME_HELM_PATH_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$')

# #535：`helm_value_specs` 解析失敗的哨兵。與「template 本來就沒有 specs」必須分得開
# ——後者是正常狀態（例 `cloud_app_ha_paas`），前者代表無從得知哪些鍵是租戶填的憑證，
# 遮罩層只能 fail-closed 全遮。用哨兵而不是 None／{}，是因為那兩個值都可能是合法結果。
SPECS_UNPARSEABLE = object()

# #501：限量商品的**內建**全域實例上限（slug → 台數）。
#
# 這是**安全網不是設定值**——`ir.config_parameter` 有值就覆寫它（營運可不發版調整，
# 見 `_global_instance_cap`）。它存在的唯一理由是「全新 DB／營運還沒設定」時硬閘
# 仍然在：上限缺值若 fail-open，等於把「不超賣」這個不可逆的商業保證掛在「有人記得
# 去設定」上，而超賣的善後是人工退點。
#
# ⚠ 上限值刻意**不放** seed（`src/data/cloud_app_templates.xml` 是 noupdate=0，且
# 檔尾 healer 每次 `-u` 主動清 `ir.model.data.noupdate`，手改 DB 保不住）。「不寫進
# seed XML 的 model 欄位」也不行——那是靠遺漏成立的保證，誰補一行 `<field/>` 就靜默
# 失效。config_parameter 在結構上不可能被 `-u` 碰到。
#
# ⚠ 新增限量商品時改這裡，並在 `res_config_settings` 補一組營運欄位；守衛本身是
# slug-generic 的，不需要動。
_DEFAULT_GLOBAL_INSTANCE_CAPS = {
    'haos': 8,
}


class SupplyGateError(ValidationError):
    """供給閘門（停售／售罄／每帳號上限）對「不能再收這一筆」的拒絕。

    **用途是讓測試斷言例外「型別」而非文案片段**——措辭調整不該讓守線變紅，而
    `assertRaises(ValidationError)` 又寬到分不出「被供給閘門擋下」與「別的驗證錯」。

    ⚠ **controller 不特別捕獲它**。`_create_service` 走的是 #520 那組
    `except (ValidationError, UserError)` 分支，把訊息原文回前端——那是刻意的：
    業務規則例外的訊息本來就是寫給使用者看的，統一成泛用字串會讓第一線無從排錯
    （#520 症狀 4），而真正帶內部技術名的 `AccessError` 已經被單獨收窄。

    本 class 曾一度規劃讓 controller 單獨捕獲以收窄訊息轉出面，該設計在 rebase 到
    #520 之後**未被採用**；docstring 一併更新，避免留下「已經這樣做了」的錯誤印象
    （註解把未達成的性質寫成已確立事實，本 PR 第一輪已犯過一次）。
    """


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

    # Workload 型態（#423 W5a）——決定就緒判定與啟停走哪條路。
    #
    # 為何是顯式欄位而不是從 chart 嗅探（Q3 拍板）：嗅探要嘛多打一次 API、
    # 要嘛把「叢集沒裝 KubeVirt」與「這個服務不是 VM」混為一談。平台對 VM 的
    # 就緒判定是 fail-closed，那個混淆會讓服務靜默卡在 deploying。顯式旗標
    # 讓失效可判讀（fail-loud 慣例）。
    #
    # 既有 template 全部落在預設 'container' → 零行為改變；required=True 對
    # 既有 row 由 Odoo 套 default，不需 migration（守線見
    # src/tests/test_haos_workload_type.py）。
    workload_type = fields.Selection(
        selection=[
            ('container', 'Container'),
            ('vm', 'Virtual Machine'),
        ],
        string='Workload Type',
        default='container',
        required=True,
        help='VM workloads decide readiness and start/stop differently from '
             'containers (see the #423 v1 design): readiness reads the VMI '
             'phase instead of the pod, and start/stop goes through runStrategy '
             'instead of replicas.',
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

    # Admin GUI Configuration（#357）
    # 與上方 mcp_enabled 正交，勿混用：mcp_enabled＝「外掛一顆 MCP sidecar container」
    # 的部署形態；has_admin_gui＝「image 內建 mcp-admin-core 的 admin GUI、需平台供給
    # admin_password」的供給契約。三個 standalone MCP template（mcp-odoo /
    # mcp-odoo-manage / mcp-n8n）MCP 內建於 body 故 mcp_enabled=False，卻都需要
    # admin_password；反之 cloud_app_n8n（plain n8n + n8n-mcp sidecar）mcp_enabled=True
    # 但沒有 admin-core GUI、不需要。用 mcp_enabled 當 admin_password 供給鏈的閘門會
    # 同時錯放與錯擋（#357 根因 C01）——本欄位是該供給鏈的 single source of truth，
    # model（cloud_service）與 controller（paas）兩端都必須讀它、不得自建 slug allowlist。
    has_admin_gui = fields.Boolean(
        string='Built-in Admin GUI',
        default=False,
        help='The image ships the mcp-admin-core admin GUI and needs the '
             'platform to supply admin_password (generated when the service is '
             'created, stored in cloud_service_secret, injected through Helm '
             'sensitive values). Orthogonal to "Enable MCP Sidecar": a template '
             'whose MCP is built into the body has mcp_enabled=False but '
             'has_admin_gui=True.',
    )

    # #449 密碼載體：**只影響 reset 路徑，不影響供給路徑**。
    # 供給側（建立服務時把 admin_password 經 sensitive_values 送進 chart）兩種載體
    # 完全相同——都是 config.sensitive.admin_password，由 has_admin_gui 這條供給鏈
    # 統一負責（_build_mcp_sensitive_values）。差別只在「密碼在 pod 裡活在哪」：
    #
    #   config_json（mcp-server 家族）：chart 的 seed-config 一次性把 Secret 落成
    #     PVC 上的 /data/config.json，之後應用只讀該檔。改 Secret 不改檔＝不改密碼，
    #     所以 reset 必須外科式 read-modify-write 那個檔（保留租戶自填的
    #     connection/tools/token_history），operator 走 #357 補償 saga。
    #   helm_secret（hermes-agent / open-design）：chart Secret 就是權威來源，pod 以
    #     env／initContainer（nginx htpasswd）在**啟動時**讀取，PVC 裡根本沒有
    #     config.json。對它做 exec patch 必炸＝#449 的 500（patch 失敗→備份沒建成
    #     →rollback 也失敗）。reset 走純 helm upgrade ＋ rollout restart。
    #
    # 選錯的後果是不對稱的：helm_secret 誤標成 config_json＝reset 必 500（#449 現況）；
    # config_json 誤標成 helm_secret＝helm upgrade 成功但 config.json 沒動、密碼靜默
    # 不生效（更糟，因為它假裝成功）。新 template 上架時以「chart 有沒有把 Secret
    # 落成 PVC 上的設定檔」判定，不要照 slug 猜。
    admin_password_carrier = fields.Selection(
        selection=[
            ('config_json',
             'config.json (config file inside the PVC, mcp-server family)'),
            ('helm_secret',
             'Helm Secret (managed by the chart, hermes-agent / open-design)'),
            ('app_managed',
             'Initial only (the app owns the password after first boot, omnigent)'),
        ],
        string='Password Carrier',
        default='config_json',
        required=True,
        help='Where admin_password authoritatively lives inside the pod, which '
             'decides the operator path a reset takes: config.json = a surgical '
             'read-modify-write of the config file on the PVC (preserving the '
             'connection/tools the tenant filled in); Helm Secret = a plain '
             'helm upgrade plus rollout restart. Initial only = the platform '
             'supplies the first password and shows it once; the app stores it '
             'in its own database afterwards, so the platform cannot reset it '
             '(the tenant changes it inside the app). Only meaningful on '
             'templates with has_admin_gui=True.',
    )

    # 「平台能不能重設／改這台服務的 admin 憑證」的唯一事實源：reset endpoint 的能力
    # gate（controllers/paas.py）、model 端 _reset_admin_password 的防線、以及前端
    # 「Admin Credentials」按鈕（payload template.admin_reset_supported 鏡射）三處都
    # 讀它，不得各自由 has_admin_gui／carrier 重新推導（#357 C01 同一紀律）。
    # app_managed（omnigent）：chart 只在第一次開機把 Secret 的密碼寫進 app 自己的
    # 資料庫，之後改 Secret 不改密碼——若照 helm_secret 走 reset，helm upgrade 會成功、
    # 密碼卻沒變＝「假裝成功」，比失敗更糟（同 config_json 誤標 helm_secret 的教訓）。
    admin_reset_supported = fields.Boolean(
        string='Platform Can Reset Admin Credentials',
        compute='_compute_admin_reset_supported',
        help='True when the platform supplies the admin credential AND stays '
             'its authority (config.json / Helm Secret carriers). False for '
             'apps that own the password after first boot (Initial only).',
    )

    @api.depends('has_admin_gui', 'admin_password_carrier')
    def _compute_admin_reset_supported(self):
        for record in self:
            record.admin_reset_supported = bool(
                record.has_admin_gui
                and record.admin_password_carrier != 'app_managed'
            )

    # 租戶可改的 admin 帳號：**帳號在 chart values 的哪個路徑**（點分式）。空值＝帳號由
    # chart 固定、平台不開放修改（mcp-server 家族的 admin-core GUI 帳號不是 values
    # 決定的）。本欄位同時是「能不能改帳號」的能力事實源（controller 判 400、payload
    # 鏡射給前端，與 has_admin_gui 同一紀律，不得在任何一端自建 slug 名單），也是寫入
    # 位置：改帳號＝把新值寫進 helm_values 的這個路徑，隨 reset 那條 helm upgrade ＋
    # rollout restart 生效，之後 redeploy 也從 helm_values 帶出同一個值。
    # 只對 admin_password_carrier=helm_secret 有意義：config_json 載體的帳號活在 PVC
    # 的 config.json 裡，改 values 不會生效。
    admin_username_helm_path = fields.Char(
        string='Admin Username Values Path',
        help='Dotted Helm values path of the admin login username when the '
             'tenant may change it (e.g. authProxy.basicAuth.username). Empty '
             'means the chart fixes the username and it cannot be changed. '
             'Only meaningful with the Helm Secret password carrier.',
    )

    # 租戶可自設**密碼**、但沒有帳號（code-server：只有一組密碼、沒有使用者名稱）。
    # admin_username_helm_path 有值的模板本來就能自設密碼（連同帳號）；本欄位補上
    # 「沒有帳號可改、只有密碼」的形態。與 admin_username_helm_path 同一條前提：
    # 平台供給憑證（has_admin_gui）且密碼活在 chart Secret（helm_secret）——
    # config_json 載體的密碼在 PVC 的 config.json 裡，走的是另一條 saga。
    # 刻意是顯式 seed 旗標、不是由 carrier 推導：hermes-agent 同為 helm_secret，
    # 但它的密碼同時是 MCP 端要填的憑證，是否開放自設是那個模板自己的決定。
    admin_password_settable = fields.Boolean(
        string='Tenant Can Set Own Password',
        default=False,
        help='Password-only services (no username): the tenant may set their '
             'own password from the service page, besides resetting it. '
             'Templates with an Admin Username Values Path can already set '
             'both. Requires "Built-in Admin GUI" and the Helm Secret carrier.',
    )

    # 「租戶能不能自設憑證（Admin Credentials 對話框）」的唯一事實源：controller 的
    # admin-credentials endpoint 能力 gate、model 端 _reset_admin_password 的防線、
    # payload template.admin_credentials_settable（前端 canEditAdminCredentials）
    # 三處都讀它（#357 C01／capability-gate-single-source-field 同一紀律）。
    # 能不能**改帳號**仍只看 admin_username_helm_path（payload admin_username_editable）。
    admin_credentials_settable = fields.Boolean(
        string='Tenant Can Set Admin Credentials',
        compute='_compute_admin_credentials_settable',
        help='True when the tenant may set their own admin credentials: the '
             'platform can reset them AND the template either exposes the '
             'username path or allows a password-only change.',
    )

    @api.depends('admin_reset_supported', 'admin_username_helm_path',
                 'admin_password_settable')
    def _compute_admin_credentials_settable(self):
        for record in self:
            record.admin_credentials_settable = bool(
                record.admin_reset_supported
                and (record.admin_username_helm_path
                     or record.admin_password_settable)
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

    # #341（Wave A）每帳號實例上限：model 層 create() 計數守衛（#345 補 advisory
    # lock 序列化，race-safe，見 cloud_service.py create()）。HA PaaS 於 seed 補
    # max_per_partner=1（#345 defense-in-depth）——其每帳號限一的主防護仍由 provision
    # 端點的 provision_partner_id partial unique index ＋ advisory lock 承擔，此 model
    # 層值為縱深一致守衛（與下方 help「HA PaaS 免費方案設 1」對齊）。
    max_per_partner = fields.Integer(
        string='Max Instances Per Account',
        default=0,
        help='Maximum number of active instances of this template a single '
             'partner (account) may hold; 0 = unlimited. The HA PaaS free plan '
             'sets this to 1.',
    )

    # #471：原本 is_hidden 一個旗標同時承載「不上架」與「既有實例整條鏈消失」兩種
    # 語意。對 HA PaaS（租戶入口是 App、不是 /woow）恰好無害，套到「暫時下架的一般
    # 商品」就變成破壞性的——租戶手上 state=running 的服務詳情頁打不開、rollback／
    # change-plan／MCP 設定全不可達（#464 實測：上架／下架是常見營運動作，不能因
    # 翻旗標就把既有實例打掉）。而「不可購買」這件事
    # 本來就已由 plan 層 storefront_hidden 承擔，兩層縱深早有前例。故拆成：
    #
    #   is_hidden          ＝ 曝光層。marketplace 不列、template 詳情/plans 不可讀、
    #                        generic create 拒收。**不動既有實例。**
    #   conceal_instances  ＝ 實例層。既有實例的讀取/操作端點全擋、HiddenChainMixin
    #                        全鏈（service／訂閱／發票／workspace）過濾。
    #
    # 上架／下架是常態營運動作，只翻 is_hidden 不該有破壞性副作用；要讓既有實例也
    # 消失是**額外**的、少數的決定（目前僅 HA PaaS）。
    is_hidden = fields.Boolean(
        string='Hidden',
        default=False,
        help='Exposure-layer switch: when true, this template is not listed in '
             'the marketplace, its detail/plans are unreadable, and generic '
             'create refuses it. **Existing services are unaffected** (still '
             'readable, still operable, still listed in the workspace) — to '
             'hide existing instances as well, turn on "Conceal Existing '
             'Instances" (conceal_instances, #471). Presentation-layer '
             'filtering, not an ACL (a tenant who knows the id can still call '
             'the JSON endpoints, and Odoo native /my and /report bypass this '
             'layer). Use it for products whose pricing is not settled or that '
             'are temporarily delisted.',
    )

    conceal_instances = fields.Boolean(
        string='Conceal Existing Instances',
        default=False,
        help='Instance-layer switch: when true, the **existing** services / '
             'subscriptions / invoices / workspaces of this template disappear '
             'from every /woow front-end list (workspace, dashboard, billing), '
             'and the read and action endpoints of existing services (detail, '
             'update, rollback, revisions, change-plan, skip-trial, addon '
             'check, MCP settings, admin password reset) all return Service not '
             'found. Used for platform auto-provisioned services whose tenant '
             'entry point is not /woow, such as HA PaaS (#336). Must be paired '
             'with is_hidden=True (#471). Presentation-layer filtering, not an '
             'ACL — a tenant who knows the id can still call Odoo native /my '
             'and /report.',
    )

    def _gateway_external_path(self):
        """chart 對外的 gateway 子路徑（seed 的 ``gateway.externalPath``）；沒有回 ''。

        #530 gateway 三個閘門（鑄造／供給／交付）的**唯一事實源**。原本平台側維護
        一份 ``{'hermes-agent': '/gateway'}`` 的 slug 對照表，與 spec 的「不得新建
        slug allowlist」字面相反，而且路徑存在兩份（chart 一份、平台一份）——兩邊
        各自看起來都對（chart 有那條 location、平台也組得出字串），只有租戶會拿到
        404。改讀 seed 之後路徑只有一份，seed 改動自動貫穿三個閘門。

        資料源刻意是 ``template.helm_default_values`` 而非 ``service.helm_values``：

        * ACL 上 ``cloud_app_template`` 對 ``base.group_user`` 是唯讀
          （ir.model.access.csv `1,0,0,0`），租戶改不動；``service.helm_values``
          混有租戶 config（雖有 ``_filter_allowed_helm_values`` 白名單擋著）。
        * 更關鍵的是**存量服務**：``helm_values`` 是 create 當下的快照，本欄新增
          ``gateway`` 鍵之前建立的服務其快照裡根本沒有這個鍵；但部署走
          ``reset_values``（chart 自身 values.yaml 為 base），那些服務的 pod 仍然
          有 ``/gateway``。讀 ``helm_values`` 會對這批回空 ⇒ 判準 fail-closed 成
          「這台沒有 gateway」，#530 對他們就靜默不存在。

        malformed／非預期形態一律回 ''（fail-closed）——寧可不交付，也不要交一條
        ``https://x.woowtech.iogateway`` 這種看起來像產品保證的死鏈。create 路徑
        本來就對 malformed ``helm_default_values`` fail-loud（controllers/paas.py
        ``_create_service``），所以 '' 實務上只會落在「這個 chart 沒有 gateway」。
        """
        self.ensure_one()
        try:
            values = json.loads(self.helm_default_values or '{}')
        except (ValueError, TypeError):
            return ''
        gateway = values.get('gateway') if isinstance(values, dict) else None
        path = gateway.get('externalPath') if isinstance(gateway, dict) else None
        if not isinstance(path, str) or not path.startswith('/'):
            return ''
        return path

    @api.constrains('is_hidden', 'conceal_instances')
    def _check_conceal_implies_hidden(self):
        """conceal_instances=True ⟹ is_hidden=True（#471 蘊含式，fail-loud）。

        反例語意矛盾：marketplace 賣得到、租戶買完服務立刻從前台消失且不可操作。
        兩個旗標拆開後最可能的誤用形態有二——(a) 新 template 只設 conceal 忘了設
        hidden；(b) 解封既有 conceal template 時只翻 is_hidden=False、留下
        conceal_instances=True，於是「上架了但買到的人看不到自己的服務」。兩者都
        在寫入當下就炸，不留給 code review 抓。

        解封 conceal template 的正確作法是**兩個欄位一起翻**。
        """
        for record in self:
            if record.conceal_instances and not record.is_hidden:
                raise ValidationError(
                    _('"Conceal Existing Instances" must be enabled together '
                      'with "Hidden" (template: %s). Hiding existing instances '
                      'while the template stays listed on the marketplace lets '
                      'a tenant buy a service they can neither see nor manage. '
                      'To list it, turn both switches off.')
                    % (record.name or record.slug or record.id)
                )

    @api.constrains('admin_username_helm_path', 'has_admin_gui',
                    'admin_password_carrier')
    def _check_admin_username_helm_path(self):
        """可改帳號的前提：平台供給 admin 憑證、且憑證活在 chart Secret。

        兩個誤用形態都是「假裝成功」：has_admin_gui=False 的模板根本沒有平台供給的
        憑證可改；config_json 載體的帳號活在 PVC 的 config.json，改 values 後 helm
        upgrade 照樣成功、帳號卻沒變。在寫入當下就炸，不留到租戶按下去才發現。
        路徑格式同時擋掉空段與奇怪字元，免得寫進 helm_values 變成另一棵樹。
        """
        for record in self:
            path = record.admin_username_helm_path
            if not path:
                continue
            name = record.name or record.slug or record.id
            if not record.has_admin_gui or record.admin_password_carrier != 'helm_secret':
                raise ValidationError(
                    _('"Admin Username Values Path" requires "Built-in Admin GUI" '
                      'and the Helm Secret password carrier (template: %s).') % name
                )
            if not ADMIN_USERNAME_HELM_PATH_RE.match(path):
                raise ValidationError(
                    _('"Admin Username Values Path" must be a dotted path such as '
                      'authProxy.basicAuth.username (template: %s).') % name
                )

    @api.constrains('admin_password_settable', 'has_admin_gui',
                    'admin_password_carrier')
    def _check_admin_password_settable(self):
        """自設密碼的前提同 _check_admin_username_helm_path：平台供給、密碼在 chart Secret。

        誤標在 config_json／app_managed 上都是「假裝成功」：helm upgrade 成功、租戶
        登入用的密碼卻沒變。寫入當下就炸。
        """
        for record in self:
            if not record.admin_password_settable:
                continue
            if not record.has_admin_gui or record.admin_password_carrier != 'helm_secret':
                raise ValidationError(
                    _('"Tenant Can Set Own Password" requires "Built-in Admin GUI" '
                      'and the Helm Secret password carrier (template: %s).')
                    % (record.name or record.slug or record.id)
                )

    # ------------------------------------------------------------------ #
    #  #501：供給閘門（全域實例上限 ＋ 營運停售）                          #
    # ------------------------------------------------------------------ #
    #
    # 與既有三軸正交，別混用（見 docs/decisions/patterns/
    # hidden-template-presentation-chain.md 的三軸分工表）：
    #   plan.storefront_hidden → 這個**方案**不可選購
    #   template.is_hidden     → 這個**商品**不上架（marketplace 不列、詳情不可讀）
    #   template.conceal_instances → 這個商品的**既有實例**也不可見／不可操作
    #   本組（#501）           → 商品**照常上架、照常可看**，只是現在買不到
    #
    # 售罄是**現算**不是存旗標：存旗標要指定「誰在刪除／狀態轉移／調高上限時把它
    # 翻回去」，那是本 repo 反覆踩過的形態（#389 的第二個寫入者沒一起改）。現算也
    # 讓上限調整立即生效。代價是顯示層與 create() 之間天生有 TOCTOU 窗（看到可買
    # → 下單 → 被別人搶先 → 拿到 ValidationError），那個窗存旗標只會更大。

    def _global_cap_param_key(self):
        """上限參數的 key。守衛與設定畫面必須指向同一個（守線見
        `test_global_instance_cap_501.TestSettingsFieldTargetsGuardParam`）。

        ⚠ key 由 `slug` 推導 ⇒ **改 slug 會靜默失去上限**（param 還在舊 key 底下，
        新 key 查無值 ⇒ 落回內建預設；若該 slug 不在內建表就變成不限）。刻意不加
        機制防這件事：slug 是跨 repo 的穩定業務鍵（chart 名、seed xmlid、測試都綁
        它），改名會先在別處炸開，不會只有這裡默默出事。改 slug 時請一併搬 param。
        """
        self.ensure_one()
        return 'woow_paas_platform.global_instance_cap.%s' % (self.slug or '')

    def _sales_paused_param_key(self):
        self.ensure_one()
        return 'woow_paas_platform.sales_paused.%s' % (self.slug or '')

    def _global_instance_cap(self):
        """本 template 的全域活躍實例上限；**0 ＝不限**（與 max_per_partner 同語意）。

        解析順序：`ir.config_parameter` → 內建預設 → 0。

        ⚠ 「填 0 ＝不限」在**設定畫面上不可表達**：`res.config.settings.set_values`
        對 Integer 是 `repr(value) if value else False`，而 `set_param(key, False)`
        會 unlink 該列 ⇒ 「填 0」與「從未設定」在儲存層是同一個狀態，於是回退到
        內建預設。要真的解除限量，只能在 Technical → Parameters 直接把值寫成
        `0`（這裡的 `if raw:` 讓顯式的 `'0'` 走得到 `int()`），或發版把 slug 從
        `_DEFAULT_GLOBAL_INSTANCE_CAPS` 移除。這與 max_per_partner 的「0＝不限」
        方向相反，是 Odoo 的儲存語意逼出來的，不是選擇。

        參數**壞掉**（非數字）或**負數**時退回內建預設而不是「不限」：這裡 fail-open
        的代價是超賣，而超賣不可逆。

        ⚠ 精確說是「**退回內建預設**」而不是「fail-closed」——兩者只對**已登記在
        `_DEFAULT_GLOBAL_INSTANCE_CAPS` 的 slug** 等價。未登記的商品（絕大多數）壞值
        會落到 `.get(slug, 0)` ＝ 不限，方向看似與本段相反。這是正確的：那些商品的
        內建預設本來就是「不限」，退回預設＝維持原狀，沒有任何保證被放寬。
        真正要守的是「限量商品不會因為參數壞掉而變成無限」，那一條成立。

        ⚠ 負數要與非數字走同一分支，這點原本寫成 `max(int(raw), 0)` 是錯的——負數被
        夾成 0，而 0 在本設計＝不限 ⇒ 不計數、前台永不售罄、硬閘整個關掉且零訊號。
        可達情境不是「填 -1 想表達不限」（意圖達成、無損害），而是**正負號打錯**：
        要填 8 打成 -8。營運意圖是設限，畫面顯示 -8，實際全開。同一種營運手滑，
        兩個形態的處理方向必須一致（PR #517 覆核 B2）。
        """
        self.ensure_one()
        raw = self.env['ir.config_parameter'].sudo().get_param(
            self._global_cap_param_key())
        if raw:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = None
            if value is None or value < 0:
                _logger.warning(
                    "全域實例上限參數 %s 不是非負整數（%r）；退回內建預設。",
                    self._global_cap_param_key(), raw)
            else:
                return value
        return _DEFAULT_GLOBAL_INSTANCE_CAPS.get(self.slug, 0)

    def _sales_paused(self):
        """營運手動停售；**缺值＝未停售**。

        語意與 `res.config.settings` 的 Boolean `config_parameter` 一致（框架把
        非空字串指派給 Boolean 欄位即 True；取消勾選時 `set_param(False)` 會
        unlink 該列）。兩邊同語意，設定畫面顯示什麼就是守衛看到什麼——刻意不自訂
        `'0'`／`'False'` 的正規化，那會讓畫面與守衛各判一次。

        缺值方向與上限**相反**是刻意的：停售缺值只造成「該手動收手卻沒收」，而
        超賣仍被上限硬閘擋住；反過來設會讓 `-u` 後或全新環境直接不可購買，且沒有
        任何訊號告訴營運去打開它——守線的預設值不該是「商城關門」。
        """
        self.ensure_one()
        return bool(self.env['ir.config_parameter'].sudo().get_param(
            self._sales_paused_param_key()))

    def _reject_reason_for_new_instance(self, count_getter):
        """再收一筆會不會被擋、擋的理由；``None`` ＝ 放行。

        **這是可購買性的唯一裁決點**。`CloudService.create()`（權威閘門）與
        `_format_template` 的 `availability`（顯示層）都呼叫它，差別只在計數的
        時點——create() 呼叫時那筆已經 INSERT 進去了，顯示層則要自己 `+1`。兩邊
        問的是同一個問題「總數變成 N 時會不會超過」，不是兩個表達式。

        `count_getter` 是 callable 而不是數值：沒有上限（絕大多數商品）或已停售
        時根本不必計數，惰性求值讓「要不要查」純屬效能決定，判準仍只有這一處。

        優先序 paused > unpinned_chart > sold_out：停售是明示的營運意志；未釘版是
        平台端的設定缺陷；售罄是推導出來的狀態。**未釘版必須排在 sold_out 之前**
        ——把設定缺陷報成「已售罄」是對租戶說謊，而且會讓營運去查根本不存在的名額
        問題（同型前例：#478 一個欄位承載兩個語意）。

        #552 層 4：`helm_chart_version` 留空 ⇒ 整條管線不帶 `--version` ⇒ helm 解析
        成 registry **當下的 latest**，平台不知道也沒記錄裝了哪一版。判空放在**這裡**
        （可購買性裁決點）而不是部署當下，是刻意的：

        * `_availability()` 讀它 ⇒ marketplace 直接顯示不可購買＝**購買前就擋**。
          擋在 `_execute_deploy` 等於「先收錢、建訂閱、寄確認信，再 error」。
        * `CloudService.create()` 的 `_enforce_supply_gate` 也讀它 ⇒ 自動涵蓋
          **兩個首裝入口**（marketplace 的 `_create_service` 與 HA PaaS 的
          `controllers/ha_paas.py`，後者不經 `_create_service`）。守單一入口會被
          第二個繞過；這裡是兩者的**共同咽喉**。
        """
        self.ensure_one()
        if self._sales_paused():
            return 'paused'
        if not self.helm_chart_version:
            return 'unpinned_chart'
        cap = self._global_instance_cap()
        if cap and count_getter() > cap:
            return 'sold_out'
        return None

    def _availability(self):
        """顯示層可購買性（`_format_template` 的 payload 欄位）。

        `+1` ＝「若現在再收一筆」。這是與 create() 的唯一差異，因為那邊呼叫時
        新的一列已經在計數裡了。
        """
        self.ensure_one()
        CloudService = self.env['woow_paas_platform.cloud_service']
        reason = self._reject_reason_for_new_instance(
            lambda: CloudService._count_active_for_template(self) + 1)
        return {'purchasable': reason is None, 'reason': reason or 'available'}

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
                        _("Plan(s) %s are already linked to %s(s) %s. "
                          "A subscription plan must belong to a single service "
                          "type — its product determines the invoice line text.")
                        % (
                            ', '.join(shared.mapped('name')),
                            label,
                            ', '.join(conflicts.mapped('name')),
                        )
                    )

    @api.model
    def _warn_unpinned_active_templates(self):
        """升級期活體檢查：有沒有**在售**商品因為沒釘 chart 版本而被層 4 擋掉。

        #552 層 4 之後，`helm_chart_version` 留空 ⇒ `_reject_reason_for_new_instance`
        回 `unpinned_chart` ⇒ 商品在 marketplace 直接不可購買。那個失效形態
        **完全靜默**：卡片消失、沒有例外、沒有 log。`_enforce_supply_gate` 只在
        真的有人嘗試開通時才會 log，而「沒人買得到」正好讓那條路永遠不會被走到。

        本方法由 `data/cloud_app_templates.xml` 檔尾的 `<function>` 在**每次 -u**
        呼叫（`post_init_hook` 是 install-only，涵蓋不到升級——同 docstring 見
        `hooks.py`）。它把「靜默下架」變成升級 log 裡一行指名道姓的 ERROR。

        ⚠ 刻意不 raise：本 repo 的 prod 用 initContainer 跑 `-u`，raise 會讓 pod
        卡在 Init ＝ 把資料狀態問題升級成停機。要不要硬擋是營運決策。
        """
        unpinned = self.search([('helm_chart_version', 'in', [False, ''])])
        if not unpinned:
            return
        active = unpinned.filtered('is_active')
        if active:
            _logger.error(
                "🔴 #552：%d 個**在售**商品沒有 helm_chart_version，層 4 會把它們"
                "擋在購買前（marketplace 上會直接消失，且不會有任何其他訊號）：%s。"
                "若這是升級當下看到的，代表 seed 值沒有落地——檢查 "
                "ir_model_data.noupdate（見本檔上方 healer 與 "
                "docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md），"
                "不要只看模組版號有沒有推進。",
                len(active), sorted(active.mapped('slug')),
            )
        if unpinned - active:
            _logger.warning(
                "#552：%d 個已下架商品沒有 helm_chart_version（不影響租戶，"
                "但重新上架前必須釘版）：%s",
                len(unpinned - active), sorted((unpinned - active).mapped('slug')),
            )

    # ------------------------------------------------------------------ #
    #  helm_value_specs 的解析與白名單（#1052：自 PaasController 搬入，收尾 #559） #
    # ------------------------------------------------------------------ #
    # 這三個 method 只讀 template 自己的欄位、不碰 request，本來就是 template 的
    # 語意；留在 controller 時 model 層要用它們得反向 import controller。
    # 遮罩（``PaasController._sanitize_helm_values``／``_password_spec_paths``）
    # 是 API 回應層的關切，刻意留在 controller。

    def _parse_helm_value_specs_or_fail(self):
        """Parse helm_value_specs, returning ``SPECS_UNPARSEABLE`` on failure.

        #535：遮罩層必須分得出「這個 template 本來就沒有 specs」與「specs 在那裡但
        我讀不懂」——前者代表沒有租戶填入的憑證可遮（正常，例如 ``cloud_app_ha_paas``），
        後者代表**無從得知哪些鍵是憑證**，只能 fail-closed 全遮。
        ``_parse_helm_value_specs`` 把兩者都壓成 ``{}``（顯示路徑要的優雅降級），
        所以判定端不能用它的回傳值分辨。這裡是單一 parser，兩個 wrapper 共用。
        """
        if not self or not self.helm_value_specs:
            return {}
        try:
            return json.loads(self.helm_value_specs)
        except (json.JSONDecodeError, TypeError):
            # Defense-in-depth: a stray literal newline inside a JSON string value
            # (e.g. a seed XML that wrapped a long label/description across lines)
            # makes strict json.loads raise. Silently returning {} here would nuke the
            # ENTIRE Application Settings section — including required fields the user
            # must fill — so the service can never launch. Degrade to a tolerant parse
            # (strict=False allows control chars) so the section still renders, and log
            # loudly so the malformed seed data gets noticed and cleaned.
            try:
                specs = json.loads(self.helm_value_specs, strict=False)
                _logger.error(
                    "helm_value_specs for template %s contains control characters "
                    "inside string values; parsed with strict=False as a fallback. "
                    "Fix the seed data so JSON strings are not split across lines.",
                    self.name,
                )
                return specs
            except (json.JSONDecodeError, TypeError):
                _logger.error("Invalid helm_value_specs for template %s", self.name)
                return SPECS_UNPARSEABLE

    def _is_woow_odoo_chart(self) -> bool:
        """#1130：本 template 是否消費自家 ``woow-odoo`` chart（woow-paas-charts#81）。

        判準是 ``helm_chart_name`` 的 chart 名，不是 slug：cutover 期間 slug ``odoo`` 的
        既有服務仍跑 Bitnami release（#1128），而 woow-odoo 專屬的 values 形狀（pull
        secret 物件形、``addons.selected``、``identity``、``conf.*``）、sensitive 路徑
        ``conf.adminPassword`` 與 expose 目標都綁 chart 契約。與 #1251 就緒判定共用
        ``chart_name_of``，兩邊不會各自長出一套 chart 名解析。
        """
        if not self:
            return False
        self.ensure_one()
        return chart_name_of(self.helm_chart_name) == WOOW_ODOO_CHART

    def _parse_helm_value_specs(self) -> dict[str, list]:
        """Parse this template's helm_value_specs JSON.

        Returns:
            dict with 'required' and 'optional' lists, or empty dict if no specs
            （解析失敗也壓成 ``{}``——顯示路徑要的優雅降級；要分辨失敗用
            ``_parse_helm_value_specs_or_fail``）
        """
        specs = self._parse_helm_value_specs_or_fail()
        return {} if specs is SPECS_UNPARSEABLE else specs

    def _filter_allowed_helm_values(self, values):
        """
        Filter Helm values to only include keys allowed by this template's value_specs.

        This prevents users from overriding critical system values like namespace,
        resource limits, or security settings that should be controlled by the template.

        Args:
            values (dict): User-provided Helm values

        Returns:
            tuple: (filtered_values dict, rejected_keys list)
        """
        if not values:
            return {}, []

        specs = self._parse_helm_value_specs()
        if not specs:
            # No specs defined - allow all values (backward compatibility)
            return values, []

        # Build set of allowed keys from specs
        allowed_keys = set()

        # Get keys from required and optional fields
        for field_list in [specs.get('required', []), specs.get('optional', [])]:
            for field in field_list:
                if isinstance(field, dict) and 'key' in field:
                    allowed_keys.add(field['key'])
                elif isinstance(field, str):
                    allowed_keys.add(field)

        if not allowed_keys:
            # No keys defined in specs - allow all
            return values, []

        # Filter values to only allowed keys
        filtered = {}
        rejected = []
        for key, value in values.items():
            if key in allowed_keys:
                filtered[key] = value
            else:
                rejected.append(key)

        if rejected:
            _logger.warning(
                "Rejected unauthorized Helm value keys %s for template %s",
                rejected, self.name
            )

        return filtered, rejected

    @api.model
    def _heal_sticky_helm_value_specs(self):
        """Upgrade healer for the fix/n8n-creation-issue seed correction.

        DBs first installed under the historical ``<data noupdate="1">`` block carry a
        sticky ``ir.model.data.noupdate=True`` on the cloud_app_* seeds, so the corrected
        ``helm_value_specs`` (clean single-line JSON) is silently skipped on ``-u``.
        Runtime still works because ``CloudAppTemplate._parse_helm_value_specs`` falls back
        to ``strict=False``, but the DB row stays dirty and logs an ERROR on every
        template fetch.

        This ``<function>`` runs on every ``-u`` regardless of noupdate. It (1) clears
        the sticky flag on the seeded template xmlids so future seed edits apply, and
        (2) rewrites any helm_value_specs that fails strict JSON (control chars wrapped
        inside string values) into clean strict JSON with whitespace collapsed, so the
        stored data converges to the fixed seed. Idempotent.

        See docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md.
        """
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
