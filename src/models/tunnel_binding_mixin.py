"""一條 tunnel 只給一台 Home Assistant 接（#1766；元件端 issue #31）。

## 問題

Smart Home 與 Security Access 的 tunnel 權杖是「誰拿到誰就能接」的憑證，而
`/api/smarthome/homes/<id>/tunnel-token` 與
`/api/security-access/accesses/<id>/tunnel-token` 原本是**無狀態**的：同一個
workspace 的 owner／admin 在幾台 HA 上各跑一次設定流程，就會拿到**同一份**權杖，
每台都起一個 cloudflared。

Cloudflare 對「一條 tunnel、多個 connector」的語意是**分流**（那是給零停機換版
用的），不是拒絕——於是對外網址會輪流落到其中一台機器。使用者的症狀是「遠端登入
時好時壞」：請求打到沒有他帳號的那台。

平台端完全看不到：`action_refresh_status` 只讀 `connections[0]`，兩台接著的時候
一樣顯示 `connected`、一個 connector。

2026-10-17 正式站實況（home id=1 `woowtech-ha`）：同一條 tunnel 上 8 條連線（兩台
各 4 條），對照只有一台的 `elmohome` 是 4 條。

## 守線

1. **發權杖時記綁定**（本檔）：第一台帶 `instance_uid` 來要權杖的 HA 會被記成綁定
   對象；之後其他 uid 來要同一筆資源，端點回 409 `already_bound`。同一個 uid 重複
   要是冪等的（HA 每次重啟、reload 都會再要一次）。
2. **偵測已經接上的第二台**（`_tunnel_binding_observe_connections`）：數 distinct
   `client_id`（一個 cloudflared 行程的 4 條連線共用一個），>1 就把
   `binding_conflict` 立起來，讓後台與 /woow 看得到「有兩台接著」。

## 刻意的設計選擇

- **沒帶 uid 的舊元件照發**（`legacy`）。硬要求 uid 會讓所有既有安裝在下次重啟時
  集體斷線——那是用一次全面故障換一個尚未發生的風險。legacy 的殘留風險由守線 2
  兜。但 `legacy → claimed` 是**單向升級**：一旦有自報身分的實例claim 了，之後不帶
  uid 的請求一律拒絕（否則舊版元件變成繞過綁定的後門）。
- **guard 無條件擋所有人（含 superuser）**，解綁走明確的 `action_release_binding()`
  ——不是 `env.su` 豁免。理由見 `su-env-exempt-guard-false-green`：TransactionCase
  是 su env，用 su 當豁免條件會讓測試假綠，sudo'd controller 也會靜默繞過。
- 綁定欄位**不是** identity anchor：解綁是正常營運動作（換機器、RMA、重裝），所以
  不走 `_IDENTITY_ANCHOR_FIELDS` 的不可變 guard。
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# 一個 cloudflared 行程預設開 4 條 edge 連線；數機器要用 distinct client_id，
# 這個常數只用在「舊 operator 沒有 client_id」的退路（見 observe 的 docstring）。
_CONNS_PER_CONNECTOR = 4


class TunnelBindingConflict(UserError):
    """另一台 HA 實例已經綁定這筆資源。

    端點把它翻成 409 `already_bound`（不是 403／500）：這不是權限不足、也不是伺服
    器壞了，而是「資源狀態與請求衝突」，而且呼叫端有明確的修法（解綁或改綁別的）。
    """

    def __init__(self, message, *, bound_to="", bound_at=""):
        """Keep the holder's label/time so the endpoint can hand them to the user."""
        super().__init__(message)
        self.bound_to = bound_to
        self.bound_at = bound_at


class TunnelBindingMixin(models.AbstractModel):
    """Smart Home 與 Security Access 共用的「一台機器一條 tunnel」綁定。

    ⚠ 兩邊共用同一份實作不是為了省程式碼，是因為它們是**鏡像端點**：任一側收緊而
    另一側沒跟上，攻擊面只是搬家（平台 `two-layer-acl-lockstep` pattern）。要加
    欄位或改判斷請改這裡，不要在個別 model 複製。
    """

    _name = 'woow_paas_platform.tunnel.binding.mixin'
    _description = 'One Home Assistant instance per tunnel'

    bound_instance_uid = fields.Char(
        string='Bound HA Instance',
        copy=False,
        help='Home Assistant instance id that holds this tunnel. Empty = not '
             'claimed yet.',
    )
    bound_instance_label = fields.Char(
        string='Bound HA Name',
        copy=False,
        help='Human-readable name of that installation, as it reported itself.',
    )
    bound_at = fields.Datetime(string='Bound At', copy=False)
    binding_state = fields.Selection(
        [
            ('unbound', 'Not bound'),
            ('claimed', 'Bound to one installation'),
            ('legacy', 'Bound to an installation that did not identify itself'),
        ],
        string='Binding',
        default='unbound',
        required=True,
        copy=False,
    )
    connector_count = fields.Integer(
        string='cloudflared Instances',
        default=0,
        copy=False,
        help='How many cloudflared processes are attached to this tunnel. More '
             'than one means two machines share it and the public address '
             'reaches whichever answers first.',
    )
    binding_conflict = fields.Boolean(
        string='Two Machines Attached',
        default=False,
        copy=False,
    )

    # ---- claim / release ----------------------------------------------------
    def _tunnel_binding_claim(self, instance_uid, instance_label=''):
        """Record that `instance_uid` holds this tunnel, or refuse.

        Returns the binding_state that is now in force. Raises
        TunnelBindingConflict when somebody else holds it.

        ⚠ 這個 method **會寫 DB**，而呼叫端（tunnel-token 端點）在同一個
        transaction 裡接著把權杖回給對方。順序是刻意的：claim 失敗就不該有權杖
        出門；claim 成功但 HTTP 回應在路上丟掉也沒關係——同一個 uid 再問一次是
        冪等的。
        """
        self.ensure_one()
        # ⚠ 這些區域變數**不能叫 `uid`**。Odoo 的 `_()` 在找「用哪個語言」時會翻
        # 呼叫端的 frame locals（`tools/translate.py::_get_uid`：先看 `uid`，再看
        # `user`，最後才 `self.env.uid`），所以一個名字叫 `uid` 的字串會被當成
        # 使用者 id 餵進 `res.users.browse()` ⇒ `invalid input syntax for type
        # integer`。2026-10-17 第一版就是這樣讓三個測試掛掉的。
        claim_uid = (instance_uid or '').strip()
        label = (instance_label or '').strip()
        state = self.binding_state
        holder_uid = self.bound_instance_uid or ''

        if claim_uid and holder_uid and claim_uid == holder_uid:
            # 同一台又問一次（重啟／reload／reauth）。順手更新 label：使用者在 HA
            # 改了位置名稱時後台要跟著變，但不動 bound_at——那是「什麼時候綁的」。
            if label and label != self.bound_instance_label:
                self.write({'bound_instance_label': label})
            return state

        if claim_uid and state == 'claimed' and holder_uid and claim_uid != holder_uid:
            raise TunnelBindingConflict(
                _("This is already connected to another Home Assistant "
                  "installation (%(label)s). Two installations must not share "
                  "one tunnel: the public address reaches whichever answers "
                  "first. Release the binding here first, or connect the other "
                  "installation to a different one.",
                  label=self.bound_instance_label or holder_uid),
                bound_to=self.bound_instance_label or holder_uid,
                bound_at=fields.Datetime.to_string(self.bound_at) if self.bound_at else '',
            )

        if not claim_uid and state == 'claimed':
            # 自報身分的實例已經claim 了，現在來了一個不說自己是誰的請求。放行就
            # 等於「降版元件＝繞過綁定」。
            raise TunnelBindingConflict(
                _("This is already connected to a Home Assistant installation "
                  "(%(label)s). This request did not say which installation it "
                  "comes from, so it cannot be told apart from a second machine. "
                  "Update the WOOW PaaS integration on that machine, or release "
                  "the binding here first.",
                  label=self.bound_instance_label or holder_uid),
                bound_to=self.bound_instance_label or holder_uid,
                bound_at=fields.Datetime.to_string(self.bound_at) if self.bound_at else '',
            )

        vals = {
            'bound_instance_uid': claim_uid,
            'bound_instance_label': label,
            'bound_at': fields.Datetime.now(),
            'binding_state': 'claimed' if claim_uid else 'legacy',
        }
        if state == 'legacy' and claim_uid:
            _logger.info(
                "[tunnel-binding] %s id=%s legacy -> claimed by %s (%s)",
                self._name, self.id, claim_uid, label or '-')
        elif state == 'unbound':
            _logger.info(
                "[tunnel-binding] %s id=%s claimed by %s (%s)",
                self._name, self.id, claim_uid or 'legacy caller', label or '-')
        self.write(vals)
        return vals['binding_state']

    def action_release_binding(self):
        """Forget which installation holds this tunnel (換機器、重裝、RMA）。

        解綁本身**不會**把對方的 cloudflared 踢掉——權杖已經在那台機器上，平台沒有
        辦法收回已交出的憑證。真的要斷開對方，要改權杖（刪掉重建這筆資源）。這個
        動作只是「下一台來要權杖的機器可以接手」。UI 文案必須說清楚，否則使用者會
        以為按了就安全了。
        """
        for record in self:
            if record.binding_state == 'unbound':
                continue
            _logger.info(
                "[tunnel-binding] %s id=%s released (was %s / %s) by user=%s",
                record._name, record.id, record.binding_state,
                record.bound_instance_label or record.bound_instance_uid or '-',
                record.env.user.login)
            record.write({
                'bound_instance_uid': False,
                'bound_instance_label': False,
                'bound_at': False,
                'binding_state': 'unbound',
            })
        return True

    # ---- observation --------------------------------------------------------
    @api.model
    def _tunnel_binding_count_connectors(self, connections):
        """How many cloudflared processes the tunnel's connections represent.

        ⚠ `connector_id` 是**每條連線**一個 id，不是每個行程——cloudflared 預設開 4
        條。數機器只能用 `client_id`（同一行程的連線共用）。舊版 operator 不回
        client_id（欄位是 2026-10-17 才加的），那時退回「連線數 / 4」近似。
        """
        conns = [c for c in (connections or []) if isinstance(c, dict)]
        if not conns:
            return 0
        clients = {c.get('client_id') for c in conns if c.get('client_id')}
        if clients:
            return len(clients)
        return -(-len(conns) // _CONNS_PER_CONNECTOR)  # ceil，近似值

    @api.model
    def _tunnel_binding_vals_from_connections(self, connections):
        """`{'connector_count': n, 'binding_conflict': bool}`，純函式。

        併進各 model 的 `_tunnel_vals_from_status()`：那份判讀同時餵「寫回 DB」與
        「唯讀疊在詳情 API 上」兩條路，放這裡兩條路才會同時看得到第二台機器。
        """
        count = self._tunnel_binding_count_connectors(connections)
        return {'connector_count': count, 'binding_conflict': count > 1}

    def _tunnel_binding_log_conflict(self, vals):
        """Log the moment a tunnel gains a second cloudflared (flip only, not every poll)."""
        self.ensure_one()
        if vals.get('binding_conflict') and not self.binding_conflict:
            _logger.warning(
                "[tunnel-binding] %s id=%s has %s cloudflared instances attached "
                "to tunnel %s — the public address reaches whichever answers "
                "first (bound to %s)",
                self._name, self.id, vals.get('connector_count'),
                getattr(self, 'tunnel_id', '') or '-',
                self.bound_instance_label or self.bound_instance_uid or 'nobody')
