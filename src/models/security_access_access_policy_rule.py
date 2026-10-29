import ipaddress
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services.access_state import (
    access_authorization_snapshot, access_mark_authorization_changes,
    access_record_ids, access_revoke_savepoint)
# #536：見 policy 子模型同一段註解——sync-control 的兩個 key 只認模組私有
# sentinel，租戶從 RPC payload 塞 `true` 打不開。
from .security_access import (
    _CTX_RULE_REPLACE,
    _CTX_SKIP_ROUTE_SYNC,
    _is_sync_control,
    _reject_derived_projection_writes,
)
# #941：純量 Many2one 的 write 值正規化，與 #848 共用同一份實作。⛔ 不複製：
# 那三行 normalize 邏輯漂掉是無聲的。import 安全——`models/__init__.py` 先載
# policy module 才載本 module，且**沒有任何模組 import 本 module**（只有
# `__init__`）⇒ 零循環風險。
from .security_access_access_policy import _anchor_target_id

#: #849：**stored related 投影欄位 → 它的來源錨點欄位**（rule 側三欄）。
#:
#: 與 policy 側同型：三欄都是 `related='policy_id.<同名>', store=True`，而
#: `security/ir_rules.xml` 的 rule member-read 與 owner-admin-manage 兩條 rule 的
#: domain 走的正是這三根 column（三腿 OR，**不走** `policy_id`）⇒ 直寫任一欄都能
#: 把一列 rule 的 ir.rule 歸屬推到別的 workspace，而真錨點 `policy_id` 不動。
#:
#: 三欄都列：policy 側的 `cloud_service_id` 是**真錨點**（在 `_ANCHOR_FIELDS` 裡、
#: 由 #848 守），rule 側的同名欄位卻是**投影**——同名不同性質，⛔ 兩張清單不可互抄。
_RULE_PROJECTION_FIELDS = {
    'security_access_id': 'policy_id',
    'cloud_service_id': 'policy_id',
    'smart_home_id': 'policy_id',
}

#: #941：rule 的**真錨點**——這一列到底屬於誰，只由這一根欄位決定。
#:
#: ⚠ 與上面那張 `_RULE_PROJECTION_FIELDS` 的關係要看清楚：`policy_id` 是那張 map
#: 的 **value（來源欄位）**、不是 key（被守欄位）⇒ #929 的守衛對它不 fire。
#: 兩道守衛守的是同一次攻擊的**兩條不同路**：#929 擋「不動真錨點、只改投影欄」，
#: 本守衛擋「真的改真錨點」。互補、零交集，⛔ 不可把 `policy_id` 塞進那張 map
#: （那會變成「值不可變」，打死租戶自己合法的同 workspace 搬移）。
_RULE_ANCHOR_FIELD = 'policy_id'


def _assert_rule_reparent_authorised(records, vals):
    """契約：**把一列 rule 搬到新的 policy 底下，要求對那個新 policy 有 owner／admin。**

    #941，對稱 #848 的 `_assert_anchor_move_authorised`（policy 側）。四個判準逐一
    對齊：守單一真錨點、對**新**目標做 `check_access('write')`、比對的是「值真的
    改變」不是「key 在 vals 裡」、su 自動豁免。

    ## 缺口是 write 側專屬的（與 #848 同一個框架不對稱）

    `BaseModel.write` **只有開頭**那一次 `self.check_access('write')`，評估的是
    **寫入前**的狀態；`super().write()` 落地後零複驗。對照 `BaseModel._create`
    的**尾端**對新建列再跑一次 `records.check_access('create')`（判別事實是「哪個
    函式裡有、哪個沒有」，不是行號）。

    套到攻擊形狀 `R.write({'policy_id': P_victim})`：寫入前 R 還掛在攻擊者自己的
    policy 上 ⇒ 它的三個 stored related 投影欄指向攻擊者的 workspace ⇒ pre-write
    檢查 PASS；落地後投影欄重算成受害者的 workspace，但 ir.rule 不再評估 ⇒ 放行。

    可利用性因此落在**兩個 workspace 的共同成員**而不是陌生人：rule 的
    member-read rule（`security/ir_rules.xml` 的
    `security_access_access_policy_rule_rule_workspace_member`）domain 沒有 role
    條件，`perm_write=False`；帶 role 條件的是
    `..._rule_owner_admin_manage`。搬完之後那列進了受害者的 domain、`perm_write`
    要 owner／admin ⇒ **攻擊者自己也收不回來，是單向注入**。

    🔴 已端到端實測（#941 RED，CI run 5846）：真租戶 HTTP RPC 回 **200**、
    `policy_id` 真的落成受害者的 policy，route／cloud service／smart home route
    三種錨點形狀的受害 policy 全部成立，x2many 的 `(1, id, vals)` 側門同樣落地。

    ## 比 #848 多一格嚴重度：`policy_id` 在 resync 的觸發集合裡

    本方法所在的 `write()` 尾端，`policy_id` 是 resync 的觸發 key 之一，而
    `policies` 是**舊 ∪ 新** ⇒ 一次放行的 reparent 會對**受害者的錨點**呼叫
    `_sync_access_to_operator()`，把含注入 selector 的 full-state 推進受害者的
    Cloudflare Access application。RED 那趟量到的 `synced_route_ids` 確實含受害者
    的 route（守線測試 A5 把「被拒之後受害者的錨點一次都沒被呼叫」釘成契約）。

    ## 為什麼是 `check_access('write')` 而不是自己寫一套 role 比較

    policy model 對 `base.group_user` 的 ACL `perm_write` 是 1，而該 group 上唯一
    `perm_write=True` 的 ir.rule 是 `..._owner_admin_manage`（member 那條
    `perm_write=False`）⇒ `check_access('write')` 就**等於**「對該 policy 的
    workspace 是 owner／admin」，而且沒有第二張會漂的表
    （`cloud_service_binding_guard.py` 檔頭：「不自己寫第二套 role 比較」）。
    `base.group_system` 由 `..._rule_admin_full` 全開 ⇒ 平台 admin 照常放行。

    例外**刻意不包裝**：`check_access` 自己拋的 `AccessError` 已經指名是哪個
    model 的哪個操作被拒，再包一層等於多一個會與 ir.rule 漂開的訊息來源。
    ⇒ 守線測試斷言的是 exception **class**（本守衛 `AccessError`／#929 的投影欄
    守衛 `UserError`／`write()` 之後那五個 validator `ValidationError`，三類互斥），
    ⛔ 不斷言訊息字串。

    ## create 側不加守衛：靠框架 post-check，而且**已經實測過**

    `_create` 尾端那次 `check_access('create')` 會擋下「`policy_id` 指向他人
    policy」的新列——rule model 上對 `base.group_user` 唯一 `perm_create=True`
    的 ir.rule 同樣是 `..._owner_admin_manage`。這句話**不是推論**：守線測試 B1／
    B1b 以真 RPC 與 `with_user()` 兩種形狀各量一次，RED 那趟（守衛尚未存在）就
    已經回 `AccessError` 且沒有留下任何列。

    ⚠ 殘留條件：post-check 走 `filtered_domain`（純 Python in-memory），本 repo
    的 ir.rule domain 目前沒有 `child_of`／`parent_of`（`grep -c` = 0）；哪天有人
    把它們加進這兩條 domain，post-check 會落回真 `search()`，flush 時序的問題重開。

    ## su 豁免是刻意的，這裡與 #798 的結論相反

    `check_access()` 在 `env.su` 下自動 no-op，而 migration `18.0.1.0.39` 那條
    合法的 rule 建立正是以 superuser 跑。這與 `cf_policy_id`（#798）用 sentinel、
    對 superuser 也擋**不是**疏漏而是不同的不變量：那一格是「值不可變」（**沒有**
    合法改值路徑，任何 su 豁免都是洞，見
    `docs/decisions/patterns/su-env-exempt-guard-false-green.md`），本函式是「值
    轉移**受限**」（**有**合法改值路徑，只是要 gated）——`#849` 的
    `test_L4_rule_moved_to_another_policy_recomputes_three_fields` 就以真租戶身分
    在同一個 workspace 內搬 rule 並斷言三個投影欄重算，那是活的合法路徑。
    ⇒ 一律封死會打紅既有測試、也會拿掉租戶自己的合法能力。

    代價是本守衛的 deny 分支在 su env 下結構上不可達 ⇒ 守線測試一律走真 RPC 的
    **非 su** 租戶，並另有一格（C2）把 su 豁免本身斷言成契約。

    ## 清空（寫 ``False``）不在守備範圍——而且它被 NOT NULL 擋著

    沒有「新 policy」要授權 ⇒ 早退。這一格不是破口：`policy_id` 宣告
    `required=True`，清空會撞 Postgres 的 NOT NULL 約束。

    #973 F1：這句話現在**被斷言鎖著**了（守線測試 E1）——它斷言的是**性質**而不是
    driver 的 class 名：(a) 不是 `AccessError`／`UserError`／`ValidationError`
    任一（＝不是我們自己那三層擋的，證明守衛確實早退），(b) 是
    `psycopg2.IntegrityError`，(c) `pgcode` 等於 `psycopg2.errorcodes` 的
    `NOT_NULL_VIOLATION`（SQLSTATE 是 ISO 標準碼，class 名會隨 driver 版本漂），
    (d) 訊息指名 `policy_id` 那一根 column。在 #973 之前這句只是散文，E1 的 body
    只有「有沒有拋例外」——拋任何例外都綠。

    ⚠ `_anchor_target_id` 最後一行是 `int(value)`，看起來會被 `NewId` 炸——**不會**：
    `odoo/api.py::NewId.__bool__` 回 `False`，所以第一道 `if not value: return
    False` 就短路；new record 的 recordset 走 `value.id`（也是 `NewId`）之後同樣被
    本函式的 `if not new_id: return` 接住。⇒ 可原樣重用該 helper。

    ## ⛔ 兩個**本單刻意沒有修**的面（另開單，⛔ 不要在這裡順手動）

    1. **policy 與 rule 的 `owner_admin_manage` domain 逐字相同，卻沒有 lockstep
       gate。** 實測兩條 `domain_force` 字串完全相等（各 363 字元，只有 `model_id`
       不同），而 `repo-lint.py` 對它們零檢查。本守衛「`target_policy.check_access
       ('write')` 字面上就是 post-move 的 rule 可寫性判準」這句等價性，靠的正是那
       份相等；改一邊沒改另一邊，等價性會**無聲**斷掉。
    2. **rule model 沒宣告 `_allow_sudo_commands`**（框架預設 `True`，全 `src/`
       零命中）⇒ 未來任何 `policy.sudo().write({'rule_ids': 客戶端送的 commands})`
       會**同時**關掉框架 pre-check 與本守衛，而且無聲。現況已查證非 sudo
       （controller 的 `_update_policy` 用 `request.env[...]` ＋ `_rule_replace`）。

    ## ⚠ 存量資料的殘留面（未查證，需 stg／prod 唯讀授權）

    若 DB 裡存在「投影欄與真錨點不一致」的 split 列（#849 修法之前可能造成），
    本守衛授權的是**目標 policy 宣稱的 workspace**（ir.rule 走投影欄），而不是它
    真錨點所在的 workspace。本單⛔ **未**查 prod／stg（授權尚未下來），只在此記明。
    """
    if _RULE_ANCHOR_FIELD not in vals:
        return
    new_id = _anchor_target_id(vals[_RULE_ANCHOR_FIELD])
    if not new_id:
        return
    # 「值真的改變」才驗：UI 整批送欄位時 vals 會帶著與現值相同的 policy_id，那不是
    # 搬移；而能寫這一列的人，框架開頭那次 `check_access('write')` 已經對它現在掛著
    # 的 policy 要求過 owner／admin（rule 與 policy 走同一條 workspace walk）。
    # 判準取自 `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`。
    #
    # #973 F4(a)：這條早退由 `test_E3_unchanged_policy_id_short_circuits_before_
    # check_access` 守著。⚠ 它的判準**不是**「拿掉會被拒」——拿掉之後功能不會壞
    # （能寫這一列的人對它現在掛著的 policy 必定有 owner／admin，兩個 model 的
    # `..._owner_admin_manage` domain 逐字相同）⇒ 那樣的測試恆綠。判準是「那次
    # `check_access` 有沒有真的發生」：測試在 policy model 上掛 spy，值沒改變時
    # 必須錄到零次 write 檢查，配一格「值真的改變時錄得到」的陽性對照。
    if all(rec[_RULE_ANCHOR_FIELD].id == new_id for rec in records):
        return
    # comodel 名字從**欄位宣告**取，不寫死字串：欄位改指向別的 model 時這裡自動跟上
    # （與 #848 的 `records._fields[field].comodel_name` 同一個寫法）。
    records.env[
        records._fields[_RULE_ANCHOR_FIELD].comodel_name
    ].browse(new_id).check_access('write')


# Shared with the policy格式驗證：bare domain（不可帶 @），如 acme.com。
_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)
# ISO 3166-1 Alpha-2 國碼，如 TW / US（大小寫不拘，存前 upper）。
_COUNTRY_RE = re.compile(r'^[A-Za-z]{2}$')


class SecurityAccessAccessPolicyRule(models.Model):
    """One selector rule inside an Access policy（P-rule 子模型）.

    P-rule（2026-06）把「1 policy = 1 flat rule」拆成「1 policy → N rules」，
    對齊 Cloudflare Access 原生 policy 結構：每條 rule 屬於 include / require /
    exclude 三組之一（OR / AND / NOT 語意），policy 是 rule 的容器並承載
    decision + precedence（sequence）。

    rule 不直接呼叫 operator——父 policy 的 route 以宣告式 full-state 推送
    （鏡像 policy 子模型）。route_id 經 policy_id 兩跳 stored related 物化
    （policy_id.security_access_id），讓 ir.rule workspace walk 與 SA-level
    email 配額 count 可 traverse。

    #744：父 policy 的錨點變成 exclusive arc 之後，本子模型**必須對等鏡射**
    第二個錨點（`cloud_service_id`），否則 CS 錨定的 policy 底下這張表的
    ir.rule domain 兩腿皆假 ⇒ 租戶連自己那條 selector rule 都建不出來、讀不到。
    這是 [[unbound-project-acl-dual-leg]] 明列的「sub-model 必須對等改」那一條
    （那邊的教訓是漏改 = leak；本案方向相反是 fail-closed，但同樣是壞的）。

    #768：同一條規矩再套一次——第三個錨點（`smart_home_id`）也必須對等鏡射，
    否則 SH 錨定的 policy 底下這張表的 ir.rule domain 三腿皆假，租戶連自己那條
    selector rule 都建不出來，而錯誤訊息指向的是**子表**，沒人會聯想到 parent
    換了錨點。
    """

    _name = 'woow_paas_platform.security_access_access_policy_rule'
    _description = 'Security Access — Access Policy Rule'
    _order = 'sequence, id'

    policy_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_access_policy',
        string='Access Policy', required=True, ondelete='cascade', index=True,
        help='The policy this rule belongs to. The policy carries the '
             'allow/deny/bypass decision; this rule is one selector within '
             'its include/require/exclude group.',
    )
    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        related='policy_id.security_access_id', store=True, index=True,
        help='Derived from policy_id (two hops); kept stored so the ir.rule '
             'workspace walk and the SA-level email quota can traverse it. '
             'Empty when the parent policy is anchored on a cloud service.',
    )
    cloud_service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        string='Cloud Service',
        related='policy_id.cloud_service_id', store=True, index=True,
        help="#744: the parent policy's other anchor; likewise store=True so "
             "the ir.rule can traverse it.",
    )
    smart_home_id = fields.Many2one(
        comodel_name='woow_paas_platform.smart_home',
        string='Smart Home',
        related='policy_id.smart_home_id', store=True, index=True,
        help="#768: the parent policy's third anchor; likewise store=True so "
             "the ir.rule can traverse it.",
    )
    group = fields.Selection(
        selection=[
            ('include', 'Include'),
            ('require', 'Require'),
            ('exclude', 'Exclude'),
        ],
        string='Group', default='include', required=True,
        help='Include = OR (match any), Require = AND (match all), '
             'Exclude = NOT (match none). A policy must have at least one '
             'Include rule.',
    )
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
            ('everyone', 'Everyone'),
            ('country', 'Country'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
    rule_value = fields.Char(
        string='Rule Value',
        help='Email domain (no @) / comma-separated emails / CIDR / '
             'comma-separated ISO-2 country codes, per rule_type. '
             'Empty for "everyone".',
    )
    sequence = fields.Integer(string='Display Order', default=10)

    @api.constrains('rule_type', 'rule_value', 'group')
    def _check_rule_value(self):
        for rule in self:
            val = (rule.rule_value or '').strip()
            if rule.rule_type == 'everyone':
                # everyone 是「所有人」selector，不帶值；有值多半是誤填。
                if val:
                    raise ValidationError(
                        _("The 'Everyone' rule takes no value — got %r.")
                        % rule.rule_value)
                continue
            if rule.rule_type == 'email_domain':
                if val.startswith('@') or not _DOMAIN_RE.match(val):
                    raise ValidationError(
                        _("Email domain must be a bare domain (no @), e.g. "
                          "acme.com — got %r") % rule.rule_value)
            elif rule.rule_type == 'email_list':
                emails = [e.strip() for e in val.split(',') if e.strip()]
                if not emails or any('@' not in e for e in emails):
                    raise ValidationError(
                        _("Email list must be comma-separated email addresses."))
            elif rule.rule_type == 'ip_range':
                try:
                    ipaddress.ip_network(val, strict=False)
                except ValueError:
                    raise ValidationError(
                        _("IP range must be a valid CIDR, e.g. 203.0.113.0/24 — "
                          "got %r") % rule.rule_value)
            elif rule.rule_type == 'country':
                codes = [c.strip() for c in val.split(',') if c.strip()]
                if not codes or any(not _COUNTRY_RE.match(c) for c in codes):
                    raise ValidationError(
                        _("Country must be comma-separated ISO 3166-1 alpha-2 "
                          "codes, e.g. TW,US — got %r") % rule.rule_value)

    def _suppress_route_sync(self):
        """Whether a per-rule operator sync should be skipped for this op.

        Both hatches open ONLY for the module's own sentinel (#536) — open them
        with `_skip_route_sync()` / `_rule_replace()` from models/security_access,
        never by putting `True` in the context: `call_kw` hands the caller's
        context straight to the ORM, so a truthy-valued key is forgeable from
        any tenant's RPC payload.

        sa_skip_route_sync: migration / provision bulk row moves converge CF via
        their own explicit re-sync, not per-row here.

        sa_rule_replace: a policy-driven whole-rule-set replace (controller
        _update_policy writes (5,0,0)+creates). Odoo runs the (5,0,0) unlink
        FULLY — including this per-rule sync — BEFORE the (0,0,) creates, so a
        per-rule sync in that window pushes the policy in its transient
        empty-include state and the operator rejects "≥1 include" (422), aborting
        the whole edit. The policy's own write() issues the final full-state sync
        after the replace settles, so per-rule syncs here are wrong AND redundant.
        """
        return (_is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                or _is_sync_control(self.env, _CTX_RULE_REPLACE))

    # Atomicity + sync: mirror the policy sub-model. A rule create/write/unlink
    # re-syncs its policy's ANCHOR — the route, or (#745) the cloud_service —
    # which owns the declarative full-state push to the operator; the dispatch
    # itself lives in policy._access_sync_targets(). Wrapped in a savepoint so a
    # sync raise rolls the rule row back. The liveness gate lives inside the
    # anchor's own _sync_access_to_operator (no-ops without a live tunnel /
    # unprotected route / a CS with no subdomain), so we always call it and let
    # the anchor decide. _suppress_route_sync() skips the per-rule call
    # for migration/provision bulk moves (sa_skip_route_sync) and for a
    # policy-driven rule-set replace (sa_rule_replace), where the policy's own
    # write() owns the final full-state sync.
    #
    # Note: creating a policy WITH rule_ids fires BOTH the nested rule sync and
    # the policy's own post-create sync; the duplicate is benign — the route
    # push is an idempotent full-state declaration and the liveness gate inside
    # route._sync keeps a pending SA from calling the operator at all.

    @api.model_create_multi
    def create(self, vals_list):
        policy_ids = access_record_ids(vals.get('policy_id') for vals in vals_list)
        pre_policies = self.env[
            'woow_paas_platform.security_access_access_policy'].browse(
                policy_ids).exists()
        pre_auth = access_authorization_snapshot(
            pre_policies._access_sync_targets())
        reached = []
        with access_revoke_savepoint(self.env, reached):
            rules = super().create(vals_list)
            policies = rules.mapped('policy_id')
            # Adding rules can only push email count OVER the cap or attach an
            # email selector to a bypass policy — re-check those (partial-state
            # safe: both only raise on a real violation, never on an undercount).
            # The "≥1 include" check is NOT re-run here: a fresh policy create
            # may add a non-include rule first, and the policy-level constraint
            # already covers the final state.
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            # #352：新增一條 include 規則就可能把 allow 政策變成整批放行。
            # policy 上的 @api.constrains 不會因為「單獨建 rule」而觸發，所以
            # 這裡與上面兩條同樣要顯式再跑一次——否則只擋得到走 policy.rule_ids
            # 的寫法，直接對 rule 表寫入的路徑（含 API）整條漏掉。
            policies._check_allow_policy_no_bulk_grant()
            # #521：新增一條 email_list 規則會把「跨該訂閱去重後的位址數」推高
            # ⇒ 席位配額與上面三條同樣要在 rule 層顯式再跑一次。少了這一行，
            # 「先建一個合法 policy、再單獨塞 rule」就整條繞過席位計數（#352
            # 當年踩的是同一個面）。partial-state 安全：它只在真的超額時 raise。
            policies._check_access_seat_quota()
            if not self._suppress_route_sync():
                # #745：走 policy 的錨點分派（route 或 cloud_service），不是
                # `mapped('policy_id.route_id')`——CS 錨定時那個 mapped 是空集，
                # 症狀是「規則改了但 Cloudflare 不知道」且完全無聲。
                targets = policies._access_sync_targets()
                reached.extend(access_mark_authorization_changes(
                    self.env, targets, pre_auth))
                for anchor in targets:
                    anchor._sync_access_to_operator()
        return rules

    def write(self, vals):
        # #849：投影欄位的直寫守衛，**必須是本方法的第一件事**——拒絕要發生在
        # `policies` 擷取與 `super().write()` 之前。與 policy 側同一個 helper。
        _reject_derived_projection_writes(vals, _RULE_PROJECTION_FIELDS)
        # #941：真錨點的歸屬守衛。順序有意義——它必須排在 `super().write()` 與
        # 任何 side effect（`policies` 擷取、savepoint、operator sync）之前，與
        # #848 在 `policy.write()` 的位置同構。同時送 `policy_id` ＋ 投影欄的
        # payload 由上面那道先拒（`UserError`），不漏。
        # #973 F4(b)：上面那句原本是 [推論]（設計時自己標的），現在由
        # `test_E4_mixed_payload_is_rejected_by_the_projection_guard_first` 量著
        # ——判別式是 exception class：兩道守衛順序若對調，混合 payload 會改拋
        # `AccessError` 而不是 `UserError`。
        _assert_rule_reparent_authorised(self, vals)
        policies = self.mapped('policy_id')
        pre_auth = access_authorization_snapshot(policies._access_sync_targets())
        reached = []
        with access_revoke_savepoint(self.env, reached):
            result = super().write(vals)
            policies = (policies | self.mapped('policy_id'))
            policies._check_at_least_one_include()
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            # #352／#521 P-J：把既有規則改成 `_BULK_GRANT_RULE_TYPES` 裡的任一型
            # （email_domain / everyone / ip_range / country）、或把它從 exclude
            # 搬進 include，都是同一個繞過（見 policy 上的 constraint）。⛔ 這裡
            # 刻意不複述型別清單當判準——判準只有 policy 上那一份常數。
            policies._check_allow_policy_no_bulk_grant()
            # #521：改 rule_value（加位址）或把 rule 搬到另一個 policy 都會動到
            # 席位計數。`policies` 上面已經把**舊**與**新** policy 都聯集進來，
            # 所以搬家的兩側各自重驗自己的池。
            policies._check_access_seat_quota()
            if (not self._suppress_route_sync()
                    and any(k in vals for k in (
                        'group', 'rule_type', 'rule_value', 'sequence',
                        'policy_id'))):
                targets = policies._access_sync_targets()
                reached.extend(access_mark_authorization_changes(
                    self.env, targets, pre_auth))
                for anchor in targets:
                    anchor._sync_access_to_operator()
        return result

    def unlink(self):
        policies = self.mapped('policy_id')
        anchors = policies._access_sync_targets()
        pre_auth = access_authorization_snapshot(anchors)
        reached = []
        with access_revoke_savepoint(self.env, reached):
            result = super().unlink()
            # Removing a rule can drop the last Include — re-validate surviving
            # policies (a policy deleted by cascade no longer exists → skipped).
            # sa_rule_replace: a whole-rule-set replace (controller _update_policy
            # does (5,0,0)+creates) transiently empties the policy before the new
            # rules land; suppress the per-rule check during that window — the
            # policy-level @api.constrains validates the final state.
            if not _is_sync_control(self.env, _CTX_RULE_REPLACE):
                policies.exists()._check_at_least_one_include()
            if not self._suppress_route_sync():
                targets = [anchor for anchor in anchors if anchor.exists()]
                reached.extend(access_mark_authorization_changes(
                    self.env, targets, pre_auth))
                for anchor in targets:
                    anchor._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'group': self.group,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence,
        }
