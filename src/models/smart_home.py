import logging
import uuid

from odoo import fields, models
from odoo.exceptions import UserError

from ..services.naming import make_smarthome_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)


class SmartHome(models.Model):
    _name = 'woow_paas_platform.smart_home'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Smart Home Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all smart homes.',
        ),
    ]

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined name for this smart home',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this smart home belongs to',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        required=True,
    )

    # Store original service_url for resume after suspend
    original_service_url = fields.Char(
        help='Original service URL before suspension, used for resume',
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if provisioning failed',
    )

    # Tunnel info
    tunnel_id = fields.Char(string='Tunnel ID')
    tunnel_token = fields.Char(
        string='Tunnel Token',
        groups='base.group_system',
    )
    tunnel_name = fields.Char(string='Tunnel Name')
    connector_id = fields.Char(string='Connector ID')
    connector_type = fields.Char(string='Connector Type')
    tunnel_route = fields.Char(string='Tunnel Route URL')
    tunnel_status = fields.Selection(
        selection=[
            ('connected', 'Connected'),
            ('disconnected', 'Disconnected'),
            ('error', 'Error'),
        ],
        string='Tunnel Status',
        default='disconnected',
    )
    tunnel_uptime = fields.Char(string='Uptime')

    # Config
    ha_port = fields.Integer(
        string='HA Port',
        default=8123,
        help='Home Assistant service port',
    )
    subdomain = fields.Char(
        string='Subdomain',
        help='Assigned subdomain for this smart home tunnel',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _naming_ws_hash(self):
        """Workspace 的 **stored** namespace 身分碼，Cloudflare 命名的 hash 種子。

        #677 之前這裡回的是 `workspace.slug`，而 slug 由租戶提供的 `name` 逐字決定、
        namespace 是它的 `md5[:8]`（32-bit 截斷）⇒ 租戶可以讓自己的子網域落在別人
        的 ws_hash 段上。現在回 stored `ws_hash`（伺服器產生、DB `UNIQUE`）。

        原本那個「`slug` 取不到就退回 `name.lower()`」的 fallback 一併移除：
        `workspace_id` 是 `required=True`，該分支在空 recordset 上本來就會炸
        （`False.lower()`）；而讓它靜默退回 name 推導，正是這個洞的形狀。ws_hash
        缺值時交給 `naming.py::_require_ws_hash` fail-loud（fail-closed，不會產出
        `paas-sm-False-...`）。
        """
        return self.workspace_id.ws_hash

    def _generate_subdomain(self):
        """Generate the public subdomain (= user-facing hostname base).

        Deliberately env-token-FREE: the subdomain is the external URL and is
        immutable, so prod/stg are told apart by per-env ``paas_domain``, not by
        mutating the hostname (DP2). The env token rides the internal tunnel
        name instead — see ``_generate_tunnel_name``.
        """
        return make_smarthome_subdomain(self._naming_ws_hash(), self.reference_id, self.name)

    def _generate_tunnel_name(self):
        """Generate the Cloudflare *tunnel display name*, carrying the deploy-env token.

        A SmartHome owns a dedicated Cloudflare tunnel on the prod/stg-shared
        account; with no env token in its name — and, unlike CloudService, no
        shared-tunnel CNAME ``content`` clue — it cannot self-identify its
        environment (the orphan-attribution gap from cf-ns-orphan-no-account-gc).
        We stamp the env onto the *internal* tunnel name (e.g. ``paas-stg-sm-…``)
        while the public subdomain stays clean, mirroring SecurityAccess where
        the token rides the internal ``canonical_name`` rather than the route's
        user-facing ``subdomain_prefix``. The operator keeps the tunnel name and
        the DNS hostname independent (DNS is derived from the hostname, never the
        name — see operator tunnels.py), so the user-facing URL is unchanged.
        Teardown reads the stored ``tunnel_id``, never a recomputed name, so the
        token is reverse-lookup-safe (external-resource-env-token-naming 守則 3/4).
        """
        env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_smarthome_subdomain(
            self._naming_ws_hash(), self.reference_id, self.name, env=env)

    def _get_paas_domain(self):
        """Get PaaS domain from system settings."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    def action_provision(self):
        """Provision the smart home by creating a dedicated Cloudflare Tunnel.

        **Idempotency guard（#484）**：這個 method 原本是**單段**的——``create_tunnel``
        一次做完全部，所以「``error`` ⇒ 遠端沒有資源」恆成立，retry 無腦重跑也安全，
        不需要 guard（``two-stage-remote-provision-idempotency`` 那份 pattern doc 正是
        拿本 method 當「單段免 guard」的對照組）。

        本次改動把它變成**雙結果**：operator 的 ``dns_failed=True`` 代表 tunnel 已經在
        Cloudflare 誕生、但 DNS record 沒建成，於是 ``error`` 從此可能與一條**活著且在
        計費**的 tunnel 共存。那份 pattern doc 的原話是「單段 method 的『免 guard 安全』
        是它的單段性換來的，不是天生的；一旦加第二段，這份安全就得用 guard 重新買回
        來」——所以下面的 stored-id guard 是這個修法的必要組成，不是附帶重構。

        **與 SecurityAccess 的 guard 有一處刻意不同**：SA 的 guard 分支可以只重跑第二段
        （declarative route sync）並收斂回 ``active``；SmartHome 沒有這個選項——operator
        只有 ``create_tunnel`` 與 routes sync 兩處會建 DNS record，前者會建**新** tunnel，
        後者需要 SmartHome 尚未建模的 route payload。既然平台無法修好 DNS，就不能假裝
        可以：guard 分支 fail-loud，把「已經有一條 tunnel、重跑會多漏一條」講清楚，並指出
        兩條安全出路（人工補 DNS，或刪掉重建——``action_delete`` 會連 tunnel 一起拆）。

        順帶：``/api/smart-homes`` 的 ``action='provision'`` **沒有 state 閘**（只驗
        OWNER/ADMIN ＋ 訂閱），所以對一個已經 ``active`` 的 Smart Home 再打一次
        provision，修前同樣會建第二條 tunnel 並蓋掉 ``tunnel_id``。同一道 guard 一併關掉。
        """
        self.ensure_one()

        # stored-id guard —— 必須在任何 write 之前（含 state='provisioning'），
        # 否則失敗的重試會把記錄推離 error 卻什麼也沒做成。
        if self.tunnel_id:
            raise UserError(
                "This Smart Home already has a Cloudflare tunnel (%s). "
                "Re-provisioning would create a second, separately billed tunnel "
                "and leave this one orphaned with no record pointing at it. "
                "If its DNS record is missing, ask an administrator to create it "
                "for %s; otherwise delete this Smart Home (which tears the tunnel "
                "down) and create it again."
                % (self.tunnel_id,
                   self.tunnel_route or self.subdomain or 'the configured hostname')
            )

        client = self._get_operator_client()

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            # Generate subdomain
            subdomain = self._generate_subdomain()
            domain = self._get_paas_domain()
            hostname = f"{subdomain}.{domain}"

            # Create tunnel via operator. The tunnel *name* carries the deploy-env
            # token (internal traceability on the shared CF account); the
            # *hostname* stays env-clean so the user-facing URL is unchanged.
            result = client.create_tunnel(
                name=self._generate_tunnel_name(),
                hostname=hostname,
                service_url=f"http://localhost:{self.ha_port}",
            )

            # tunnel 已經誕生且在計費——不論下面判成功或失敗，這些欄位都要落地，
            # 否則就是一條沒有任何 DB row 指向的永久孤兒（F-ENV-4 守線）。
            tunnel_vals = {
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'tunnel_route': f"https://{hostname}",
                'subdomain': subdomain,
            }

            if result.get('dns_failed'):
                # operator 把 CF 的 DNS 例外就地吞成 HTTP 200 ＋ dns_failed=True
                # （api/tunnels.py）——平台側 client 只在 HTTP >= 400 才 raise，所以
                # 這條路無例外可接，只能讀這個欄位。hostname 不會解析，服務實際上
                # 不可達，**不得**宣告 active。
                #
                # 注意 operator 註解假設「rely entirely on a later re-sync to
                # self-heal」——那個假設對 SmartHome 不成立：src/data/ 底下沒有任何
                # smart-home cron，沒有東西會回來補這筆 DNS。所以這裡必須留下
                # 人看得懂、修得動的訊息，而不是一句籠統的失敗。
                self.write(dict(
                    tunnel_vals,
                    state='error',
                    error_message=(
                        "Tunnel created but its DNS record could not be created, "
                        "so %s will not resolve. The tunnel (%s) is kept so it is "
                        "not orphaned — an administrator must create the DNS "
                        "record, or delete and re-create this Smart Home."
                        % (hostname, result.get('tunnel_id'))
                    ),
                ))
                _logger.error(
                    "[SH] provision left without DNS sh=%s tunnel=%s hostname=%s "
                    "— record kept in 'error' with the tunnel anchored",
                    self.id, result.get('tunnel_id'), hostname,
                )
                return

            self.write(dict(
                tunnel_vals,
                state='active',
                deployed_at=fields.Datetime.now(),
            ))

            _logger.info(
                "Smart Home '%s' provisioned successfully. Tunnel ID: %s",
                self.name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("Smart Home provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("Smart Home provision error: %s", e.message)

    def action_delete(self):
        """Delete the smart home and its Cloudflare Tunnel.

        Teardown + fail-loud orphan guard are delegated to unlink(): if the
        tunnel cannot be deleted, the record is kept and a UserError surfaces,
        so a billed tunnel is never left orphaned in Cloudflare with no DB row
        to drive a retry (F-ENV-4).
        """
        self.ensure_one()
        if self.tunnel_id:
            self.write({'state': 'deleting'})
        self.unlink()

    def _cleanup_tunnel(self):
        """Best-effort idempotent Cloudflare tunnel teardown.

        Clears tunnel_id on success and KEEPS it on failure, so unlink() / a
        retry can detect a still-live tunnel and avoid orphaning it (symmetric
        with SecurityAccess._cleanup_cloudflare).
        """
        self.ensure_one()
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            client.delete_tunnel(self.tunnel_id)
            _logger.info(
                "[SH] tunnel deleted sh=%s tunnel=%s", self.id, self.tunnel_id,
            )
            self.tunnel_id = False
        except Exception as e:
            _logger.warning(
                "[SH] tunnel cleanup failed sh=%s: %s — kept for retry",
                self.id, e,
            )

    def unlink(self):
        """Direct-unlink guard: the Cloudflare tunnel must never outlive the
        record.

        Without this, a transient delete_tunnel failure would drop the DB row
        while a billed, token-still-valid tunnel lives on in Cloudflare with no
        DB reference left to drive a retry — a permanent orphan
        (cf-ns-orphan-no-account-gc: SmartHome's unconditional unlink was the
        source of 3 such orphan tunnels). Mirrors SecurityAccess.unlink: attempt
        cleanup, then fail-loud if the tunnel is still live so both the DB row
        and the CF tunnel survive for the user / cron to retry.
        """
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        # 捕獲在 fail-loud 之前、結算在 super().unlink() 之後：tunnel fail-loud
        # raise 時不結算，全交易 rollback。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
            if record.tunnel_id:
                record._cleanup_tunnel()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        res = super().unlink()
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Smart Home deleted')
        return res

    def action_refresh_status(self):
        """Refresh tunnel status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return

        client = self._get_operator_client()

        try:
            result = client.get_tunnel_status(self.tunnel_id)

            # Parse connections
            connections = result.get('connections', [])
            update_vals = {
                'tunnel_status': 'connected' if connections else 'disconnected',
            }

            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']

            self.write(update_vals)

        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': f"Status refresh failed: {e.message}",
            })
            _logger.warning(
                "Failed to refresh tunnel status for Smart Home '%s': %s",
                self.name, e.message,
            )

    def _get_mcp_integration(self):
        """Return the MCP integration string granted by the active plan, or ''.

        Mirrors _get_subscription_info's ``self.sudo().subscription_id`` read:
        subscription_id is ACL-restricted, so a non-sudo caller (portal user)
        would hit AccessError without the sudo. Derivation follows the SA
        pattern (sub.sh_subscription_plan_id.<field>).
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        plan = sub.sh_subscription_plan_id if sub else False
        return (plan.mcp_integration or '') if plan else ''

    def to_dict(self):
        """Serialize smart home data for API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_route': self.tunnel_route or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'ha_port': self.ha_port,
            'subdomain': self.subdomain or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            'subscription': self._get_subscription_info(),
            'mcp': self._get_mcp_integration(),
        }

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """Update tunnel ingress to return 503."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_suspend sh=%s name=%s state=%s sub=%s",
            self.id, self.name, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return

        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url="http_status:503",
                )
                _logger.info(
                    "[SH] _do_suspend tunnel 503 sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_suspend tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        self.write({
            'original_service_url': f"http://localhost:{self.ha_port}",
            'pre_suspend_state': self.state if self.state in ('active', 'error') else 'active',
            'state': 'suspended',
        })
        _logger.info("[SH] _do_suspend done sh=%s new_state=suspended", self.id)

    def _do_resume(self):
        """Restore tunnel ingress to original service URL."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_resume sh=%s name=%s state=%s pre_suspend=%s",
            self.id, self.name, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return

        service_url = self.original_service_url or f"http://localhost:{self.ha_port}"
        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url=service_url,
                )
                _logger.info(
                    "[SH] _do_resume tunnel restored sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_resume tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        restore_state = self.pre_suspend_state or 'active'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })
        _logger.info(
            "[SH] _do_resume done sh=%s new_state=%s",
            self.id, restore_state,
        )

    def _do_cleanup(self):
        """Delete tunnel permanently (cron path).

        Mirrors SecurityAccess._do_cleanup fail-loud contract: the cron calls
        _do_cleanup (not unlink), so this path is NOT covered by the unlink
        guard and must not silently swallow a delete_tunnel failure then write
        'deleting' (a false success signal — the subscription is already
        cancelled so the cron never revisits, leaving a billed orphan tunnel).
        _cleanup_tunnel keeps tunnel_id on failure; if it is still set, land in
        'error' so the next cron pass retries (F-ENV-4 M1).
        """
        self.ensure_one()
        _logger.info(
            "[SH] _do_cleanup sh=%s name=%s tunnel=%s",
            self.id, self.name, self.tunnel_id,
        )
        self._cleanup_tunnel()
        if self.tunnel_id:
            self.write({
                'state': 'error',
                'error_message': (
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. Will retry on the next cron pass."
                ),
            })
            _logger.warning(
                "[SH] _do_cleanup incomplete sh=%s tunnel=%s (state=error)",
                self.id, self.tunnel_id,
            )
            return
        self.write({'state': 'deleting'})
