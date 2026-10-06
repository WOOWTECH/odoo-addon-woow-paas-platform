import json
import logging
import re
import unicodedata

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

# ---------------------------------------------------------------------------
# helm_value_specs 的 ``format``：租戶在服務設定裡填的是一行純文字（HelmValueForm
# 只有 text／password／number／boolean／select），但 chart 要的形態更嚴格。
#
# - 存檔（create／reconfigure）時 ``_validate_spec_formats`` 擋下不合法的值，回
#   租戶看得懂的錯誤訊息；
# - 部署組裝（``CloudService._assemble_deploy_values``）時 ``_apply_spec_formats``
#   把存在 helm_values 裡的那行文字轉成 chart 形態（例：逗號清單 → list）。
#   helm_values 本身保留租戶打的原文，設定頁顯示與再存檔都不會變形。
#
# 第一批用在 code-server 的 git 設定（QA CS-02／CS-03）：chart 把這三個值**原樣**
# 寫進 /etc/gitconfig（``name = {{ . }}``、``[credential "{{ . }}"]``），所以換行、
# 引號、反斜線、``;``／``#``（gitconfig 註解）都會把整份 gitconfig 弄壞，git 從此
# 每個指令都報錯——這裡就是那道閘。
#
# ``domain``（Headscale 的 ngrok.domain）：一個 FQDN，存檔時擋下整條網址／埠／路徑，
# 部署組裝時正規化成小寫（見 ``parse_domain``）。
SPEC_FORMAT_GIT_HOSTS = 'git_hosts'
SPEC_FORMAT_GIT_NAME = 'git_name'
SPEC_FORMAT_EMAIL = 'email'
SPEC_FORMAT_DOMAIN = 'domain'

_GIT_HOSTS_MAX = 10
_GIT_NAME_MAX = 100
_EMAIL_MAX = 254
_DOMAIN_MAX = 253
_HOST_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
_DOMAIN_LABEL_RE = re.compile(r'^%s\Z' % _HOST_LABEL)
_DOMAIN_RE = re.compile(r'^%s(?:\.%s)+\Z' % (_HOST_LABEL, _HOST_LABEL))
_GIT_HOST_RE = re.compile(
    r'^(?P<host>%s(?:\.%s)*)(?::(?P<port>[0-9]{1,5}))?\Z' % (_HOST_LABEL, _HOST_LABEL))
# gitconfig 未加引號的值：控制字元（含換行）、引號、反斜線會改變解析；``;``／``#``
# 開始註解（後面整段被吃掉）；``<``／``>`` git 自己會從作者身分裡剝掉。
_GIT_VALUE_FORBIDDEN_RE = re.compile(r'[\x00-\x1f\x7f"\\;#<>]')
# 上面那組只擋 ASCII。chart 把值 render 進 YAML literal block（``name = {{ . }}``），
# 而 YAML 1.1 解析器（helm／kubectl 用的 goyaml.v2、PyYAML）把 U+0085（NEL）、
# U+2028／U+2029 也當**換行**——``x<NEL>[core]`` 一樣會長出一段新的 gitconfig
# section（round-3 review 實測）。所以另外依 Unicode 類別擋掉所有不可見的字元：
# Cc（C0／C1 控制字元，含 NEL）、Cf（格式字元，含 bidi 控制 U+202A–202E／
# U+2066–2069、U+200E／200F、零寬字元、BOM）、Zl／Zp（行／段落分隔）、Cs（代理
# 對）、Co（私用區）、Cn（未指派，含 U+FFFE／FFFF）。一般空白（Zs，例全形空白）
# 照收——名字裡本來就可能有空白，YAML 也不把它當換行。
_GIT_VALUE_FORBIDDEN_CATEGORIES = frozenset(('Cc', 'Cf', 'Zl', 'Zp', 'Cs', 'Co', 'Cn'))


def _git_value_has_forbidden_char(value):
    """gitconfig 未加引號的值裡有會改變解析、或看不見的字元 → True。"""
    return bool(_GIT_VALUE_FORBIDDEN_RE.search(value)) or any(
        unicodedata.category(ch) in _GIT_VALUE_FORBIDDEN_CATEGORIES for ch in value)


# Git Token 適用主機的分隔符：只認逗號（含全形「，」）。空白**不是**分隔符——
# ``git example.com`` 這種打錯會被當成兩台主機（``https://git``＝pod DNS search
# path 上叫 git 的任何東西），Git Token 就交出去了（round-3 review）。
_GIT_HOSTS_SEPARATOR_RE = re.compile(r'[,\uff0c]')
_EMAIL_RE = re.compile(
    r'^[A-Za-z0-9.!$%&\'*+/=?^_`{|}~-]+@'
    r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z')


def parse_git_hosts(raw):
    """``git.tokenHosts``：**逗號**分隔的主機 → chart 要的 https URL list。

    ``github.com, git.example.com:3000`` → ``['https://github.com',
    'https://git.example.com:3000']``。可帶 ``https://`` 前綴、結尾 ``/``；大小寫
    不分、重複的去掉、順序保留。只收 https——Git Token 是租戶的憑證，不送明文
    http。空字串／None → ``(None, None)``＝不設，chart 預設（github.com）補位。

    Git Token 交給誰由這份清單決定，所以寧可擋下也不猜：

    - 只認逗號（含全形「，」）當分隔符；一個項目中間有空白／換行
      （``git example.com``、``github.com gitlab.com``）→ ``hosts_whitespace``，
      不會被拆成兩台主機；
    - 主機名至少要有一個點（``github.com``、``git.example.com``、IP）；單一
      label（``gitea``、``git:3000``、``localhost``）→ ``hosts_single_label``——
      它會經 pod 的 DNS search path 解析到 namespace 裡同名的 Service 之類，
      等於把 Token 交給不知道是誰的東西。

    Returns:
        (list | None, (error code, params) | None)——錯誤訊息由
        ``CloudAppTemplate._spec_format_message`` 翻成租戶語言（``_()`` 要在有
        ``self.env`` 的 frame 裡呼叫才找得到語言）。
    """
    if raw is None:
        return None, None
    if isinstance(raw, str):
        items = _GIT_HOSTS_SEPARATOR_RE.split(raw)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return None, ('not_text', {})
    hosts = []
    for item in items:
        if not isinstance(item, str):
            return None, ('not_text', {})
        entry = item.strip()
        if not entry:
            continue
        if any(ch.isspace() for ch in entry):
            return None, ('hosts_whitespace', {'entry': entry})
        low = entry.lower()
        if low.startswith('https://'):
            low = low[len('https://'):]
        elif '://' in low:
            return None, ('hosts_not_https', {'entry': entry})
        low = low.rstrip('/')
        match = _GIT_HOST_RE.match(low)
        port = match and match.group('port')
        if (not match or len(match.group('host')) > 253
                or (port is not None and not 0 < int(port) < 65536)):
            return None, ('hosts_invalid', {'entry': entry})
        if '.' not in match.group('host'):
            return None, ('hosts_single_label', {'entry': entry})
        url = 'https://%s' % low
        if url not in hosts:
            hosts.append(url)
    if len(hosts) > _GIT_HOSTS_MAX:
        return None, ('hosts_too_many', {'max': _GIT_HOSTS_MAX})
    return (hosts or None), None


def parse_git_name(raw):
    """``git.userName``：一行、≤ 100 字、沒有會弄壞 gitconfig 的字元。空 → 不設。

    擋 ``" \\ ; # < >``、ASCII 控制字元，以及 YAML 會當換行的 U+0085／U+2028／
    U+2029 與其他看不見的字元（bidi 控制、零寬字元等，見
    ``_GIT_VALUE_FORBIDDEN_CATEGORIES``）。
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, ('not_text', {})
    name = raw.strip()
    if not name:
        return None, None
    if len(name) > _GIT_NAME_MAX or _git_value_has_forbidden_char(name):
        return None, ('name_invalid', {'max': _GIT_NAME_MAX})
    return name, None


def parse_email(raw):
    """``git.userEmail``：一般 email 形態（≤ 254 字、網域至少一個點）。空 → 不設。"""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, ('not_text', {})
    email = raw.strip()
    if not email:
        return None, None
    if (len(email) > _EMAIL_MAX or not _EMAIL_RE.match(email)
            or _git_value_has_forbidden_char(email)):
        return None, ('email_invalid', {})
    return email, None


def parse_domain(raw):
    """``domain``：一個完整網域名稱（FQDN）。空 → 不設（選填欄位）。

    第一個用途是 Headscale 的 ``ngrok.domain``：chart 把它組成
    ``ngrok http … --url https://<domain>`` 與 Headscale 的 ``server_url``。租戶最常
    貼錯的形態是整條網址（``https://xxx.ngrok-free.app/``）或帶埠，這些要在**存檔**
    時就擋下並說清楚——靠 chart 在 render 時 ``fail`` 會重演 EMQX 0.1.1「升級失敗、
    服務進 error」。

    規則：ASCII、每段 1–63 字（字母數字與 ``-``，不得以 ``-`` 開頭或結尾）、至少兩段、
    總長 ≤ 253、最後一段不得全是數字（擋 IP）；不收 scheme、路徑、query、埠、
    ``user@``、萬用字元、空白。大小寫不分：正規化成小寫；結尾的一個 ``.``（FQDN 根）
    去掉。

    Returns:
        (str | None, (error code, params) | None)
    """
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, ('not_text', {})
    value = raw.strip()
    if not value:
        return None, None
    if not value.isascii():
        # 先擋再 lower()：有些非 ASCII 字元 lower() 之後會變成 ASCII（例 U+212A
        # KELVIN SIGN → k），等於把租戶打的同形字悄悄換成另一個網域。IDN 請填 punycode。
        return None, ('domain_invalid', {'entry': value})
    if '://' in value:
        return None, ('domain_scheme', {'entry': value})
    if any(ch in value for ch in '/?#@'):
        return None, ('domain_path', {'entry': value})
    if ':' in value:
        return None, ('domain_port', {'entry': value})
    low = value.lower()
    if low.endswith('.'):
        low = low[:-1]
    if len(low) > _DOMAIN_MAX:
        return None, ('domain_too_long', {'max': _DOMAIN_MAX})
    if not _DOMAIN_RE.match(low) or low.rsplit('.', 1)[-1].isdigit():
        if '.' not in low and _DOMAIN_LABEL_RE.match(low):
            return None, ('domain_single_label', {'entry': value})
        return None, ('domain_invalid', {'entry': value})
    return low, None


SPEC_FORMAT_PARSERS = {
    SPEC_FORMAT_GIT_HOSTS: parse_git_hosts,
    SPEC_FORMAT_GIT_NAME: parse_git_name,
    SPEC_FORMAT_EMAIL: parse_email,
    SPEC_FORMAT_DOMAIN: parse_domain,
}

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

    # 對外位址（chart 的 ngrok sidecar；charts/emqx 0.1.0 起、charts/headscale）——
    # 「這個商品有沒有一個平台要回讀並顯示給租戶的對外位址」的**唯一事實源**。
    # （18.0.1.0.205 以前叫 has_public_mqtt_address、只給 EMQX 用；P1 泛化後改名。
    # 範本由 seed 擁有（noupdate=0 ＋ 檔尾 healer），`-u` 時 seed 會把 EMQX 的新旗標
    # 寫回 True，所以改名不需要 migration。）
    #
    # 為什麼是 template 欄位而不是在哪裡判 `slug == 'emqx'`：capability gate 一律
    # 收斂成單一欄位（docs/decisions/patterns/capability-gate-single-source-field.md，
    # repo-lint 第 7 條機械守著前端不得用 slug 做集合成員判斷）。model 的
    # `CloudService._chart_has_public_tunnel_address`、poll 的探測閘門、service payload
    # 與前端文案四處都讀它。
    #
    # 位址怎麼來：平台的 Cloudflare 通道只載 HTTP/WS（而且會剝掉 Tailscale 控制協議
    # 的 Upgrade header），原生 MQTT 與 Headscale 控制面都走租戶自己的 ngrok agent
    # （chart 的 sidecar）。agent 實際拿到的位址（隨機指派的 TCP 位址、帳號的 dev
    # domain）只有它自己知道，它每 60 秒把當前位址印到自己的 container log
    # （`WOOW_PUBLIC_URL=tcp://…`／`https://…`），operator 的
    # `GET /api/releases/{ns}/{release}/public-address` 從那裡讀回來。
    #
    # ⚠ 旗標開著不代表一定有位址：租戶沒開、agent 還沒連上、pod 還在起來，都是正常
    # 的「沒有位址」狀態（`cloud_service.public_tunnel_url` 為空），不是錯誤。
    has_public_tunnel_address = fields.Boolean(
        string='Advertises a public tunnel address',
        default=False,
        help='The chart runs an ngrok tunnel sidecar that advertises the service\'s '
             'public address (tcp://host:port or https://host) on its own container '
             'log, and the platform reads it back and shows it on the service page. '
             'Empty address is a normal state (the tenant has not enabled it, or the '
             'agent has not connected yet).',
    )
    # 服務頁那一格的標題（例 EMQX「對外 MQTT 位址」、Headscale「對外控制網址」）。
    # 這是範本 record 的**資料值**：seed 直接寫中文原文、**不可翻譯、不進 .po**
    # （docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）。空值時前端
    # 用程式碼字串 `_t("Public address")`（那個走 .po）。只是顯示字，**不是**能力判準
    # ——gate 一律讀上面的旗標。
    public_tunnel_label = fields.Char(
        string='Public Tunnel Address Label',
        help='Label of the public tunnel address on the service page, e.g. "對外 MQTT '
             '位址". A data value written in the seed as-is (not translated). Empty '
             'shows the generic "Public address". Display only; whether the row '
             'exists is decided by "Advertises a public tunnel address".',
    )

    # #530 一次性憑證視窗的 gateway 兩格提示（P3）。空值＝沿用前端內建的 hermes-agent
    # 文案（「填進 MCP 連線設定的 Gateway API Server 區」）；其他有 gateway 的模板
    # （例 LiteLLM：key 是 master key、URL 是 OpenAI 相容 Base URL）在 seed 寫自己的
    # 說明。範本 record 的**資料值**：seed 直接寫中文原文、不可翻譯、不進 .po
    # （i18n-scope-data-values-vs-code-strings.md）；前端內建的 hermes 文案是程式碼字串，
    # 照常 `_t()`＋po。只是顯示字：交付與否仍只看 `_gateway_external_path()`
    # （seed 的 gateway.externalPath）。
    gateway_api_key_hint = fields.Text(
        string='Gateway API Key Hint',
        help='Hint under the "Gateway API Key" row of the one-time credentials dialog '
             '(a data value written in the seed as-is, not translated). Empty keeps the '
             'built-in hermes-agent text (MCP connection settings).',
    )
    gateway_url_hint = fields.Text(
        string='Gateway API URL Hint',
        help='Hint under the "Gateway API URL" row of the one-time credentials dialog '
             '(a data value written in the seed as-is, not translated). Empty keeps the '
             'built-in hermes-agent text (MCP connection settings).',
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
    #
    # Odoo（與 n8n）不適用本欄位：密碼權威在 app 自己的 DB，helm upgrade 換不到
    # （woow-odoo 的 conf.adminPassword 只在首次 -i base 套用），而本欄位兩種可重設
    # 載體的前提都是「reset 一定 helm upgrade」。它們走下方 app_admin_reset_kind 的
    # operator exec 契約（#1186）。
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

    # #1370／#1186：app 自管密碼、但平台能 exec 進容器直接改 app DB 的模板（Odoo 的
    # base.user_admin、n8n 的 owner）。與 has_admin_gui／admin_password_carrier 是**兩條
    # 不同的契約**，刻意不共用：
    #   - 不重用 has_admin_gui：那會觸發 config.sensitive.admin_password 的供給鏈，
    #     woow-odoo tenant schema（additionalProperties:false）直接擋。
    #   - 不擴充 admin_password_carrier：那條的前提是「一定 helm upgrade」，而這兩個
    #     app 的密碼權威在 app DB，helm upgrade 換不到（woow-odoo 的 conf.adminPassword
    #     只在首次 -i base 套用；n8n 的 owner 由 post-deploy init 建在 n8n DB）。
    # 值決定 operator 走哪支 exec 腳本（odoo/admin-password、n8n/owner-password 或
    # omnigent/admin-password）。只做 Reset（平台鑄造、一次性顯示），不開放租戶自設。
    # omnigent_exec（#1370 後續）：omnigent 的初始密碼仍走 has_admin_gui 供給鏈
    # （admin_password_carrier=app_managed，chart 只在首次開機建 admin），之後的 Reset 由
    # operator exec 改 omnigent 自己的 DB——所以它是唯一允許與 has_admin_gui 並存的 kind，
    # 且只限 app_managed（見 _check_app_admin_reset_kind）。
    app_admin_reset_kind = fields.Selection(
        selection=[
            ('none', 'None'),
            ('odoo_exec', 'Odoo administrator (operator exec)'),
            ('n8n_exec', 'n8n owner (operator exec)'),
            ('omnigent_exec', 'Omnigent administrator (operator exec)'),
        ],
        string='App Admin Password Reset',
        default='none',
        required=True,
        help='The app keeps the administrator password in its own database, and the '
             'platform can still reset it by running a script inside the app container '
             '(no helm upgrade, no restart): Odoo resets base.user_admin and clears its '
             'TOTP; n8n resets the owner account and turns off its MFA; Omnigent resets '
             'its admin account (signed-in sessions are kept). Mutually exclusive with '
             '"Built-in Admin GUI", except Omnigent with the "Initial only" carrier.',
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

    @api.depends('has_admin_gui', 'admin_password_carrier', 'app_admin_reset_kind')
    def _compute_admin_reset_supported(self):
        for record in self:
            record.admin_reset_supported = bool(
                (record.has_admin_gui
                 and record.admin_password_carrier != 'app_managed')
                # #1370：app DB 由 operator exec 重設（Odoo／n8n）。
                or (record.app_admin_reset_kind or 'none') != 'none'
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

    @api.constrains('app_admin_reset_kind', 'has_admin_gui', 'admin_password_carrier')
    def _check_app_admin_reset_kind(self):
        """#1370：exec 重設與 admin GUI 供給鏈互斥。

        兩者同時開著時 controller 只會走其中一條，另一條的承諾（例如 config.json
        的外科式 reset）就靜默失效——寧可在 seed／後台存檔時就擋下。

        唯一例外：omnigent_exec ＋ admin_password_carrier=app_managed。app_managed 的
        has_admin_gui 只負責**初始**密碼供給、本來就沒有 reset 路可以被遮蔽；Reset 只有
        exec 這一條。
        """
        for record in self:
            kind = record.app_admin_reset_kind or 'none'
            if (kind == 'omnigent_exec' and record.has_admin_gui
                    and record.admin_password_carrier == 'app_managed'):
                continue
            if kind != 'none' and record.has_admin_gui:
                raise ValidationError(_(
                    'Template "%(name)s": "App Admin Password Reset" cannot be combined '
                    'with "Built-in Admin GUI".', name=record.name))

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
        """租戶送來的 helm values 只放行本 template ``helm_value_specs`` 宣告的鍵（白名單）。

        擋的是租戶改掉平台刻意控制的值（namespace、資源規格、securityContext、
        image……）。開通（``controllers/paas.py`` ``_create_service``：被拒的鍵靜默丟棄、
        只留 log）與儲存設定（``_update_service``：有被拒的鍵就整筆拒絕）都經過這裡；
        平台自己的寫入路徑（redeploy／reset／init／換 image／方案 overlay）不經過。

        比對規則（#1488 未改動）：values 的**頂層鍵**對 spec 的 ``key`` 字串做精確比對。
        前端送的是扁平 dot-path（``{"auth.openrouterApiKey": "..."}``），呼叫端之後才
        ``_unflatten_dotpath_keys``；巢狀形狀（``{"auth": {...}}``）或更深一層的
        dot-path（``auth.openrouterApiKey.x``）都不等於宣告的字串 ⇒ 拒絕。宣告鍵的值
        本身不檢查形狀。

        #1488 fail-closed：specs 沒有任何允許的鍵——欄位空白／null、``{}``、
        ``{"required": [], "optional": []}``、解析失敗或不是 dict——租戶送的 values
        **一律拒絕**（全部列入 rejected）。舊版這兩種情況都走「allow all
        （backward compatibility）」，於是 specs 空白的公開範本（7 個 mcp-*、pi-agent、
        ha-full）的租戶可經開通與儲存設定覆寫 chart 的 securityContext／image／
        resources／serviceAccount／tolerations 等。需要租戶參數的範本必須在 specs
        明列 key；允許的鍵與 #1408 redeploy 保留的「租戶鍵」同源（``_tenant_value_keys``）。

        Args:
            values (dict): User-provided Helm values

        Returns:
            tuple: (filtered_values dict, rejected_keys list)
        """
        if not values:
            return {}, []

        allowed_keys = self._tenant_value_keys()
        if not allowed_keys:
            rejected = list(values)
            _logger.warning(
                "[#1488] Template %s declares no tenant-settable helm value keys; "
                "rejecting all tenant helm values %s", self.name, rejected,
            )
            return {}, rejected

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

    def _tenant_value_keys(self):
        """租戶可設定的 helm value 鍵（helm_value_specs 的 required／optional ``key``）。

        #1408：Redeploy 以範本 image 覆蓋服務值時，這些鍵屬於租戶，不覆蓋。
        #1488：同一組鍵也是 ``_filter_allowed_helm_values`` 的白名單；空集合＝租戶
        什麼都不能設（fail-closed），不是「全部放行」。
        """
        specs = self._parse_helm_value_specs()
        keys = set()
        if not isinstance(specs, dict):
            return keys
        for section in ('required', 'optional'):
            for field in specs.get(section) or []:
                if isinstance(field, dict) and field.get('key'):
                    keys.add(field['key'])
                elif isinstance(field, str):
                    keys.add(field)
        return keys

    def _formatted_value_specs(self):
        """這個 template 帶 ``format`` 的 spec：``{key: format}``（未知 format 略過）。"""
        specs = self._parse_helm_value_specs()
        if not isinstance(specs, dict):
            return {}
        out = {}
        for section in ('required', 'optional'):
            fields_list = specs.get(section)
            if not isinstance(fields_list, list):
                continue
            for field in fields_list:
                if not isinstance(field, dict):
                    continue
                key, fmt = field.get('key'), field.get('format')
                if isinstance(key, str) and key and fmt in SPEC_FORMAT_PARSERS:
                    out[key] = fmt
        return out

    def _spec_format_message(self, code, params):
        """把 ``parse_*`` 的錯誤碼翻成租戶語言（``_()`` 需要這個 frame 的 ``self.env``）。"""
        if code == 'hosts_not_https':
            return _('Git token hosts only accept https; "%(entry)s" is not allowed.',
                     **params)
        if code == 'hosts_invalid':
            return _('Git token hosts: "%(entry)s" is not a valid host name '
                     '(example: github.com or git.example.com:3000).', **params)
        if code == 'hosts_whitespace':
            return _('Git token hosts: "%(entry)s" contains a space or line break; '
                     'separate hosts with commas (example: github.com, gitlab.com).',
                     **params)
        if code == 'hosts_single_label':
            return _('Git token hosts: "%(entry)s" is not a full host name; use a dotted '
                     'name such as github.com or git.example.com.', **params)
        if code == 'hosts_too_many':
            return _('Git token hosts: at most %(max)s hosts.', **params)
        if code == 'name_invalid':
            return _('Git author name must be one line of at most %(max)s characters, '
                     'without " \\ ; # < > or invisible control characters.', **params)
        if code == 'email_invalid':
            return _('Git author email is not a valid email address.')
        if code == 'domain_scheme':
            return _('Domain: enter the domain name only, without https:// (for example '
                     'abc-123.ngrok-free.app instead of "%(entry)s").', **params)
        if code == 'domain_path':
            return _('Domain: enter the domain name only, without a path, "/" or "@" '
                     '("%(entry)s" is not a domain name).', **params)
        if code == 'domain_port':
            return _('Domain: enter the domain name only, without a port number '
                     '("%(entry)s").', **params)
        if code == 'domain_single_label':
            return _('Domain: "%(entry)s" is not a full domain name; use a dotted name '
                     'such as abc-123.ngrok-free.app.', **params)
        if code == 'domain_too_long':
            return _('Domain: at most %(max)s characters.', **params)
        if code == 'domain_invalid':
            return _('Domain: "%(entry)s" is not a valid domain name. Use letters, digits '
                     'and hyphens separated by dots, for example abc-123.ngrok-free.app.',
                     **params)
        return _('Invalid setting value.')

    def _validate_spec_formats(self, flat_values):
        """存檔前（create／reconfigure）：帶 ``format`` 的鍵不合法就回錯誤訊息。

        Args:
            flat_values: 已過白名單的 dot-path 扁平 dict（controller 送進來的形態）

        Returns:
            str | None: 第一個錯誤的租戶語言訊息；全部合法回 None
        """
        if not flat_values:
            return None
        for key, fmt in self._formatted_value_specs().items():
            if key not in flat_values:
                continue
            _value, error = SPEC_FORMAT_PARSERS[fmt](flat_values[key])
            if error:
                return self._spec_format_message(*error)
        return None

    def _apply_spec_formats(self, values):
        """部署組裝：把 helm_values 裡帶 ``format`` 的鍵轉成 chart 要的形態（就地）。

        合法 → 正規化後的值（例 git.tokenHosts 逗號清單 → https URL list）；
        空 → 移除該鍵，chart 預設補位；不合法（繞過存檔驗證、例如管理員直接改了
        helm_values）→ 同樣移除並記 warning（不記值），寧可少一個設定也不讓一行字串
        把 chart render 打掛或寫壞 gitconfig。

        Args:
            values: 巢狀 helm values dict

        Returns:
            同一個 dict
        """
        if not isinstance(values, dict):
            return values
        for key, fmt in self._formatted_value_specs().items():
            parts = key.split('.')
            parent = values
            for part in parts[:-1]:
                parent = parent.get(part) if isinstance(parent, dict) else None
            if not isinstance(parent, dict) or parts[-1] not in parent:
                continue
            normalized, error = SPEC_FORMAT_PARSERS[fmt](parent[parts[-1]])
            if error:
                _logger.warning(
                    "Template %s: stored value for %s is invalid (%s); dropped from "
                    "the deploy values so the chart default applies",
                    self.slug, key, error[0],
                )
                del parent[parts[-1]]
            elif normalized is None:
                del parent[parts[-1]]
            else:
                parent[parts[-1]] = normalized
        return values

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
