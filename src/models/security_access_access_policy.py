from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# #536：sync-control 的逃生門必須是模組私有 sentinel，不是任何 JSON 表達得出
# 的值——`call_kw` 把呼叫端 context 整包換進 env，只判 truthy 的 key 租戶自己
# 就打得開。判定與開閘 helper 都住在 SA 那支（本子模型沒有自己的 sentinel）。
from .security_access import (
    _CTX_SKIP_ROUTE_SYNC,
    # #1071：`_anchor_target_id` 從本檔**下沉**到 `security_access.py`（route 的
    # 守衛住在那裡，而那一支不能 import 本 module——本 module 自己就 import 它，
    # 反向再拉一條是循環）。這一行是 re-export：rule module 的
    # `from .security_access_access_policy import _anchor_target_id`（#941）因此
    # 一個字都不用改。⛔ 不要在三個檔各留一份 normalize——那三行漂掉是無聲的。
    _anchor_target_id,
    _internal_policy_cf_write,
    _is_internal_policy_cf_write,
    _is_sync_control,
    _reject_derived_projection_writes,
    _reject_identity_anchor_writes,
    _strip_identity_anchors,
)


#: 錨點欄位的**唯一清單**（順序即訊息與 sparse-precedence 的探測順序）。
#:
#: #768：從二元擴成三元。刻意提到模組層而不是散在各處的字面 tuple——原本
#: `create()` 的 precedence 探測、`write()` 的 re-sync trigger、`unlink()` 的
#: 錨點抓取、`_access_sync_targets()` 各自維護一份，而漏改其中任一處的症狀都
#: **沒有輸出**（precedence 全撞在 10／Cloudflare 完全沒被推／CF 孤兒）。
_ANCHOR_FIELDS = ('route_id', 'cloud_service_id', 'smart_home_route_id')

#: 錯誤訊息用的人話標籤。與 `_ANCHOR_FIELDS` 同一份 key。
_ANCHOR_LABELS = {
    'route_id': 'an Application Route',
    'cloud_service_id': 'a Cloud Service',
    'smart_home_route_id': 'a Smart Home Route',
}

#: #798：**伺服器擁有的**外部物件 id，租戶一律不得寫（create 壓值、write 拒絕）。
#:
#: `cf_policy_id` 不是普通欄位：三腿的 `_build_existing_policy_ids()` 與帳本的
#: `cf_access_policy_reap._trusted_ids_for()` 都逐字從列上讀它，而那份清單是 operator
#: **唯一的刪除通道**（`existing_policy_ids`，delete-then-recreate）。#781／#792 為 #783
#: 立的判準明文把「安全」建立在「列上讀到的值是伺服器擁有的」——租戶寫得進來，那道判準
#: 就不成立（#798 已實測：`base.group_user` 走 `/web/dataset/call_kw` 寫得進去，值落地）。
#:
#: 清單化而不是在守衛旁邊寫字面值：#522 的成因正是逐欄位手列而漏掉 `tunnel_id`。
_POLICY_IDENTITY_ANCHOR_FIELDS = ('cf_policy_id',)

#: #849：**stored related 投影欄位 → 它的來源錨點欄位**。租戶一律不得直寫。
#:
#: 這兩欄是 `related=..., store=True`：值直接落在 policy 自己那根 column 上、來源列
#: 一動也不動，而 `security/ir_rules.xml` 的 policy member-read 與 owner-admin-manage
#: 兩條 rule 正是走這幾根 column 判歸屬（**不走** `route_id`）⇒ 直寫造成**歸屬分裂**
#: （真錨點在 X、ir.rule 判給 Y），已端到端量到受害者取得跨租戶 read。
#:
#: ⛔ **刻意不併進 `_ANCHOR_FIELDS`**：那份清單是**真錨點**，同時餵著 arc 檢查、
#: sparse precedence 探測、`_access_sync_targets()`、#848 的搬移授權與 #786 的 resync
#: 觸發集合。把投影欄位塞進去會讓那五處全部改變語意（最直接的：arc 會判定「同時
#: 掛了兩個錨點」而拒絕每一列合法 policy）。守線清單分開，理由見
#: `_reject_derived_projection_writes` 的 docstring。
_POLICY_PROJECTION_FIELDS = {
    'security_access_id': 'route_id',
    'smart_home_id': 'smart_home_route_id',
}


def _dedupe_anchors(anchors):
    """保序去重一串**混 model** 的錨點記錄。

    `_access_sync_targets()` 回的是 list 不是 recordset（三個錨點是三個不同
    model，`|` 用不了），所以聯集只能自己拿 ``(model, id)`` 當 key ——只用
    ``id`` 的話 `route#3` 會把 `cloud_service#3` 吃掉，而症狀是「其中一邊完全
    沒被推」，沒有例外也沒有 log（#745 在帳本那邊踩過同一顆）。

    **保留第一次出現**是刻意的：`write()` 把「失去了 policy 的錨點」排在前面
    餵進來，一個同時是舊錨點又是新錨點的錨點必須落在它作為**舊**錨點的位置。
    """
    seen = set()
    ordered = []
    for anchor in anchors:
        key = (anchor._name, anchor.id)
        if key not in seen:
            seen.add(key)
            ordered.append(anchor)
    return ordered


def _arc_violation(label, present):
    """回傳「恰好一個錨點」的違規訊息，或 ``None``（合法）。

    ``present``：``{錨點欄位: bool}``。單一文案來源：create 前置檢查與
    `@api.constrains` 兩個呼叫點共用，否則同一個違規在兩條路徑上會給出兩種說法。

    #768：訊息改成**動態列舉**而不是寫死「二選一」。寫死的話，第三個錨點上線
    後訊息會對使用者說謊（叫他在兩個裡面選一個，而他選的是第三個）。
    """
    filled = [f for f in _ANCHOR_FIELDS if present.get(f)]
    if len(filled) == 1:
        return None
    choices = ' / '.join(_ANCHOR_LABELS[f] for f in _ANCHOR_FIELDS)
    if filled:
        return (
            "Access policy %r is anchored on more than one of %s (got: %s). "
            "A policy belongs to exactly one Cloudflare Access application, "
            "so it must carry exactly one anchor."
            % (label, choices,
               ', '.join(_ANCHOR_LABELS[f] for f in filled)))
    return (
        "Access policy %r has no anchor. Set exactly one of %s — an "
        "unanchored policy can never be pushed to Cloudflare and is "
        "invisible to its own workspace." % (label, choices))


def _assert_anchor_move_authorised(records, vals):
    """契約：**把 policy 搬到一個新錨點上，要求對那個新錨點有 owner／admin。**

    #848。這是一道**獨立的**歸屬守衛，與 `write()` 裡 `if resync:` 區塊內那個
    `anchor.check_access('read')` 迴圈**不是同一件事**，也刻意不去改它：那個迴圈的
    契約是**排序**（「所有錨點都授權過，才准產生第一個 side effect」，見它自己的
    註解），歸屬把關只是它的**副作用**，改動它的人沒有義務保住這個性質。本函式存在
    的理由正是要終止「歸屬契約寄生在別人的排序契約上」。

    ## 缺口是 write 側專屬的

    框架在 create 與 write 兩側不對稱（`odoo/models.py`，判別事實是「哪個函式裡
    有、哪個沒有」而不是行號）：`BaseModel._create` 的**尾端**對**新建出來的那些
    列**再跑一次 `records.check_access('create')`；`BaseModel.write` 只有開頭那一次
    `self.check_access('write')`，評估的是**寫入前**的狀態，寫入後零複驗。
    ⇒ 「把既有 policy 搬到別人的錨點上」框架不擋。

    可利用性因此落在**兩個 workspace 的共同成員**、而不是陌生人：三個錨點 model 的
    member-read rule（`security/ir_rules.xml`）domain 都沒有 role 條件，任何成員都
    讀得到該 workspace 的錨點 ⇒ 「A 的 owner／admin ＋ B 的普通成員」把 A 的 policy
    寫到 B 的錨點上，兩道既有檢查全過（已實測：真 RPC 回 200、值落地）。搬完之後那
    列進了 B 的 domain，`perm_write` 要 owner／admin ⇒ 他自己也收不回來，是**單向
    注入**。

    ## 為什麼是 `check_access('write')` 而不是自己寫一套 role 比較

    三個錨點 model 對 `base.group_user` 的 ACL `perm_write` 都是 1，而該 group 上
    唯一 `perm_write=True` 的 ir.rule 都是各自的 `..._owner_admin_manage`（member
    那條全部 `perm_write=False`）⇒ `check_access('write')` 就**等於**「對該錨點的
    workspace 是 owner／admin」，而且沒有第二張會漂的表（`cloud_service_binding_
    guard.py` 檔頭：「不自己寫第二套 role 比較——兩套判準遲早會漂」）。

    收緊的方向不是新政策：**create 側今天就已經是這個語意**。policy model 上對
    `base.group_user` 唯一 `perm_create=True` 的 rule 也是 `..._owner_admin_manage`
    ⇒ 一列錨在別人 workspace 的新 policy 過不了 `_create` 尾端那次
    `check_access('create')`。write 側只要 `read` 才是那個例外，本函式是把 write
    對齊 create。

    例外**刻意不包裝**：`check_access` 自己拋的 `AccessError` 已經指名是哪個 model
    的哪個操作被拒，那正是呼叫端需要的資訊；再包一層等於多一個會與 ir.rule 漂開的
    訊息來源。

    ## su 豁免是刻意的，這裡與 #798 的結論相反

    `check_access()` 在 `env.su` 下自動 no-op，而 migration `18.0.1.0.37` 那條合法
    的錨點搬移正是以 superuser 跑（`api.Environment(cr, SUPERUSER_ID, {})`，框架對
    `uid == SUPERUSER_ID` 直接設 `su = True`）。

    這與 `_reject_identity_anchor_writes`／`cf_policy_id`（#798）用 sentinel、對
    superuser 也擋，**不是**疏漏而是不同的不變量：那一格是「值不可變」（**沒有**
    合法改值路徑，任何 su 豁免都是洞，見
    `docs/decisions/patterns/su-env-exempt-guard-false-green.md`），本函式是「值轉移
    受限」（**有**合法改值路徑，只是要 gated ⇒ 該文件「不適用」節指向的
    `scope-flip-write-guard-not-ir-rule` 那一側）。錨點本來就是可搬的——`write()`
    裡整套 `_anchor_move_plan()` / `_apply_anchor_move()` / 帳本 / 舊錨點排序就是
    #786／#783 為了「讓搬錨點正確」而蓋的。

    代價是本守衛的 deny 分支在 su env 下結構上不可達 ⇒ 守線測試一律走真 RPC 的
    **非 su** 租戶，並另有一格把 su 豁免本身斷言成契約
    （`test_policy_anchor_ownership_guard_848.py` 的 C2）。

    ## 比對的是「實際改值」不是「key 在不在 vals」

    UI 整批送欄位時 vals 會帶著與現值相同的錨點，那不是搬移；而且能寫這一列的人，
    框架開頭那次 `check_access('write')` 已經對它現在掛著的錨點要求過 owner／admin
    （policy 的 rule 與錨點的 rule 走的是同一條 workspace walk）。判準取自
    `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`。

    清空（寫 ``False``）不在本函式的守備範圍：沒有「新錨點」要授權，而「最終仍恰好
    一個錨點」是 arc 那三個觸點的事。

    ## ⛔ 本函式**擋不到 #849**

    #849 現行機制（留言 9445，端到端 RPC 量測）：寫 `policy.security_access_id` /
    `policy.smart_home_id` 直接落在 policy 自己那根 stored column 上，
    `policy.route_id`（本函式守的 `_ANCHOR_FIELDS`）完全沒動 ⇒ 本函式不 fire。
    兩張單是不同的機制、不同的修法落點。

    （曾誤判為 stored related 帶 `inverse` 委派、寫它們會寫到來源列——該說法已於
    留言 9433 撤回：這兩欄宣告 `related=` 未顯式覆寫 `readonly`，框架
    `_get_attrs` 早把 `readonly` 蓋成 `True`，`setup_related` 因此不裝
    `inverse`，值不會外溢到來源列。）
    """
    for field in _ANCHOR_FIELDS:
        if field not in vals:
            continue
        new_id = _anchor_target_id(vals[field])
        if not new_id:
            continue
        if all(rec[field].id == new_id for rec in records):
            continue
        records.env[records._fields[field].comodel_name].browse(
            new_id).check_access('write')


class SecurityAccessAccessPolicy(models.Model):
    """One reusable Cloudflare Access policy on a ROUTE or on a CLOUD SERVICE.

    Mirrors the route sub-model: rows are the desired-state, the parent anchor
    pushes the full set declaratively to the operator (no per-row CF call).
    cf_policy_id stores the Cloudflare reusable-policy id after sync.

    P2.5: the real binding is ``route_id`` — each Application Route is its own
    Cloudflare Access application with its own policies. ``security_access_id``
    is kept as a store=True related on ``route_id.security_access_id`` so the
    existing ir.rule walk and the SA-level policy quota keep working
    (the related store materialises a real column the ir.rule domain / quota
    count can traverse), but it is no longer the field a caller binds.

    #744（CS Access Control T2）：錨點擴成 **exclusive arc** ——
    ``route_id`` XOR ``cloud_service_id``。cloud service 買了 AC 席位後可以在
    自己的 subdomain 上開 Cloudflare Access 保護，而**刻意不建隱形 SA**：
    SA route 的 DNS 機制會把 CNAME 改指 SA 專屬 tunnel，CS 流量走的是平台共用
    tunnel，掛進去即斷線（設計拍板 2，見
    ``docs/plans/2026-Q3/2026-08-15-cs-access-control-design.md``）。因此
    policy 這個 model 原地擴充、不開平行 model——守衛（#352 整批放行、quota、
    ≥1 include）長兩份就會漂移（拍板 9）。

    arc 是 **exclusive** 而不是「兩個都可空的 nullable M2O」，這點由三個
    觸點一起保證（create 前置 ``_assert_anchor_arc_vals``、write 側
    ``@api.constrains``、以及**兩個方向都蓋得到**的 SQL CHECK ``anchor_arc``；
    分成三處的理由見各自的 docstring——簡言之 create 的 INSERT 早於 Python
    constraint，而 CHECK 的訊息不可讀）：
    ir.rule 的 workspace walk 是**逐錨點**寫的（route 走
    ``security_access_id.workspace_id``、CS 走 ``cloud_service_id.workspace_id``），
    一列若兩個錨點皆空就走不到任何 workspace ⇒ 對租戶不可見（fail-closed，
    不是外洩），但同時也**永遠同步不到 Cloudflare**——所以要在寫入當下就擋，
    不要留一個只能靠人工發現的孤兒形狀。

    P-rule (2026-06): a policy is now a CONTAINER of selector rules
    (``rule_ids``) split into include (OR) / require (AND) / exclude (NOT)
    groups, mirroring Cloudflare's native policy shape. The policy itself
    carries the decision (allow / deny / bypass) and the evaluation order
    (``sequence`` → CF precedence); the flat ``rule_type`` / ``rule_value``
    fields moved to the ``security_access_access_policy_rule`` sub-model.
    """

    _name = 'woow_paas_platform.security_access_access_policy'
    _description = 'Security Access — Access Policy'
    _order = 'sequence, id'

    #: #744：錨點的 exclusive arc。DB 層是**唯一**對所有寫入路徑（含裸 SQL、含
    #: superuser、含繞過 `_validate_fields` 的批次 migration）都生效的那一層；
    #: 兩個 Python 觸點（`_assert_anchor_arc_vals` / `_check_anchor_exclusive_arc`）
    #: 負責的是「訊息看得懂」，不是「擋得住」。
    #:
    #: ⚠ constraint key 刻意取短：Odoo 的實際名字是 `{table}_{key}`，本 model
    #: 的 table 名 48 字元，超過 63 就會被 `sql.make_identifier` 換成雜湊名
    #: （`_add_sql_constraints`），migration 的存在性驗證要跟著改查雜湊名。
    #: `anchor_arc` ⇒ 59 字元，維持可讀且可直接查。
    #: 🔴 #768：三腿一定要用 `num_nonnulls(...) = 1`，**不可以**把 `<>` 串成三元。
    #: `<>` 是奇偶性不是「恰好一個」：恰好一個非空 ⇒ `(F<>T)<>T` = FALSE（合法列
    #: 被拒）；三個都空 ⇒ `(T<>T)<>T` = TRUE（無錨點孤兒被放行）——正好接受這道
    #: CHECK 存在的理由所要拒絕的那一格。
    #:
    #: ⚠ 改這個定義要同步 `migrations/18.0.1.0.150/post-migrate.py` 的
    #: `_CHECK_DEF`（那支的補建路徑會逐字用它），且該支的存在性驗證查的是
    #: `pg_get_constraintdef` 而**不是**只查 `conname`——同名但舊定義的
    #: constraint 會通過只查名字的驗證。
    _sql_constraints = [
        (
            'anchor_arc',
            'CHECK(num_nonnulls(route_id, cloud_service_id, '
            'smart_home_route_id) = 1)',
            'An Access policy must be anchored on exactly one of '
            'Application Route / Cloud Service / Smart Home Route.',
        ),
    ]

    route_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_route',
        string='Application Route', ondelete='cascade',
        index=True,
        help='The route this policy protects. Each protected route is its own '
             'Cloudflare Access application with its own policy set. '
             'Exactly one of route_id / cloud_service_id is set (#744).',
    )
    cloud_service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        string='Cloud Service', ondelete='cascade',
        index=True,
        help='The cloud service this policy protects (#744). Its public '
             'subdomain is its own Cloudflare Access application. '
             'Exactly one of route_id / cloud_service_id is set.',
    )
    smart_home_route_id = fields.Many2one(
        comodel_name='woow_paas_platform.smart_home_route',
        string='Smart Home Route', ondelete='cascade',
        index=True,
        help='The Smart Home route this policy protects (#768). Each route is '
             'its own Cloudflare Access application. Exactly one of '
             'route_id / cloud_service_id / smart_home_route_id is set.',
    )
    smart_home_id = fields.Many2one(
        comodel_name='woow_paas_platform.smart_home',
        string='Smart Home',
        related='smart_home_route_id.smart_home_id', store=True, index=True,
        help='Derived from smart_home_route_id; kept stored so the ir.rule '
             'workspace walk and the per-Smart-Home policy quota can traverse '
             'it (ir.rule domains only walk REAL columns). The real binding is '
             'smart_home_route_id. Empty on the other two anchors (#768).',
    )
    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        related='route_id.security_access_id', store=True, index=True,
        help='Derived from route_id; kept stored so the ir.rule workspace walk '
             'and the SA-level policy quota can traverse it. The real binding '
             'is route_id. Empty on a cloud-service-anchored policy — that '
             'half walks cloud_service_id.workspace_id instead (#744).',
    )
    name = fields.Char(string='Name', required=True)
    decision = fields.Selection(
        selection=[
            ('allow', 'Allow'),
            ('deny', 'Block'),
            ('bypass', 'Bypass'),
        ],
        string='Decision', default='allow', required=True,
        help='Allow grants access; Block denies; Bypass skips authentication '
             '(no identity selectors allowed). Evaluated by precedence top to '
             'bottom, first match wins; Cloudflare evaluates Bypass / Service '
             'Auth policies first.',
    )
    rule_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy_rule',
        inverse_name='policy_id', string='Rules', copy=True,
        help='Selector rules grouped into include (OR) / require (AND) / '
             'exclude (NOT). At least one Include rule is required. '
             'copy=True so the P2.5 migration fan-out (policy.copy onto each '
             'protected route) carries the rules — a copied policy with no '
             'rules would trip the at-least-one-include constraint.',
    )
    sequence = fields.Integer(string='Evaluation Order', default=10)
    cf_policy_id = fields.Char(string='CF Policy ID', copy=False)

    # ------------------------------------------------------------------ #
    #  #744：錨點 exclusive arc ＋ 「這一列屬於哪個訂閱」的單一裁決處       #
    # ------------------------------------------------------------------ #

    @api.model
    def _assert_anchor_arc_vals(self, vals):
        """create 側的 arc 檢查——必須在 ``super().create()`` 的 INSERT 之前。

        為什麼不能只靠底下的 ``@api.constrains``：Odoo 的 ``_create()`` 先送
        INSERT、**之後**才跑 Python constraint（``odoo/models.py`` 的
        ``_create``：``records._validate_fields(...)`` 在 INSERT 之後），所以
        create 方向永遠是 PostgreSQL 的 CHECK 先炸——拿到的是
        ``CheckViolation: ..._anchor_arc``，說不出錯在哪，而且交易當場 abort。
        write 方向相反（``write()`` 把值排進 ``towrite`` 延後 flush，
        ``_validate_fields`` 先跑），那一側交給 constrains 就夠。

        傳進來的 vals 必須是**框架解析後**的（``_add_missing_default_values``）：
        ``default_route_id`` 之類是在 ``super().create()`` 內部才展開的，只看
        raw vals 會讓「用 context default 塞錨點」整道跳過（#705 F1 同形狀）。
        """
        message = _arc_violation(
            vals.get('name') or '<new>',
            {f: bool(vals.get(f)) for f in _ANCHOR_FIELDS})
        if message:
            raise ValidationError(message)

    @api.constrains(*_ANCHOR_FIELDS)
    def _check_anchor_exclusive_arc(self):
        """恰好一個錨點 —— 全空與多於一個都拒（write 側）。

        與 ``_sql_constraints`` 的 ``anchor_arc`` 是**同一條不變式的兩層**，
        不是二選一：SQL CHECK 是唯一擋得住裸 SQL／批次 migration／
        ``_validate_fields`` 跑不到的路徑的那層，本方法負責在 write 方向給出
        講得出「錯在哪」的訊息（PG 的 CHECK 違反只回 constraint 名字）。
        """
        for pol in self:
            message = _arc_violation(
                pol.name or pol.id,
                {f: bool(pol[f]) for f in _ANCHOR_FIELDS})
            if message:
                raise ValidationError(message)

    #: 錨點**記錄的 model 名** → 這一列在 policy 表上對應的「配額池」欄位。
    #:
    #: 🔴 #768：刻意做成查表而不是三元式。原本是
    #: ``'cloud_service_id' if pol.cloud_service_id else 'security_access_id'``
    #: ——SH 錨定會落 ``else`` 分支，於是拿 SH 的 id 去比 ``security_access_id``，
    #: 那是**跨 model 的 id 碰撞**（兩張表的 id 各自從 1 開始），結果不是「不
    #: 擋」而是一個錯誤的計數（可多算也可少算）。三元式的錯是結構性的：第四個
    #: 錨點還會再錯一次。查表則讓「哪個 model 用哪個池欄位」變成可被測試逐項
    #: 斷言的宣告（見 `test_sh_access_model_768.py` D3）。
    _ANCHOR_POOL_FIELD = {
        'woow_paas_platform.cloud_service': 'cloud_service_id',
        'woow_paas_platform.security_access': 'security_access_id',
        'woow_paas_platform.smart_home': 'smart_home_id',
    }

    def _anchor(self):
        """這一列的錨點記錄：cloud_service（#744）／route 的 SA／SH（#768）。

        **唯一**的「這條 policy 歸誰管」裁決處。quota 與 email 上限都讀它，
        免得兩處各自 walk 一次錨點、日後長出第四個錨點時漏改其中一處。
        arc 保證至多一邊有值，所以 ``or`` 鏈不會遮蔽任何一邊。
        """
        self.ensure_one()
        return (self.cloud_service_id or self.security_access_id
                or self.smart_home_id)

    def _anchor_plan(self):
        """錨點訂閱的方案；沒有訂閱／沒有方案 ⇒ 空 recordset（grandfathered）。

        ``sudo()``：典型租戶對 ``sh.subscription.subscription`` 沒有讀權，
        constraint 走的是租戶自己的 env（見 ``subscription_lifecycle_mixin``
        的同款註解）。
        """
        self.ensure_one()
        anchor = self._anchor()
        Plan = self.env['sh.subscription.plan']
        if not anchor:
            return Plan.browse()
        sub = anchor.sudo().subscription_id
        if not sub:
            return Plan.browse()
        return sub.sh_subscription_plan_id

    @api.constrains('rule_ids')
    def _check_at_least_one_include(self):
        # Cloudflare hard rule: every policy must contain at least one Include
        # rule (a policy with only require/exclude matches no one). Triggered on
        # policy create/write touching rule_ids; rule create/write/unlink call
        # this explicitly for standalone rule edits (a direct rule.unlink does
        # not write policy.rule_ids, so the constraint would not fire on its
        # own).
        for pol in self:
            if not pol.rule_ids.filtered(lambda r: r.group == 'include'):
                raise ValidationError(
                    _("Access policy %r must contain at least one Include rule.")
                    % (pol.name or pol.id))

    @api.constrains('decision', 'rule_ids')
    def _check_decision_selector_compat(self):
        # Bypass policies skip identity verification, so Cloudflare rejects
        # identity-based selectors (email / email domain) on them. Fail fast in
        # Odoo rather than letting the operator push an invalid policy to CF
        # (defense in depth: the operator validates this too).
        for pol in self:
            if pol.decision == 'bypass':
                if pol.rule_ids.filtered(
                        lambda r: r.rule_type in ('email_domain', 'email_list')):
                    raise ValidationError(
                        _("Bypass policies cannot use identity-based selectors "
                          "(email domain / email list). Use IP range, country, "
                          "or everyone instead."))

    #: #352（email_domain／everyone）＋ #521 P-J（ip_range／country）：讓
    #: 「不特定多數人」通過認證的 include 選擇器。
    #:
    #: AC 方案按人數計費，而人數是「數 email_list 規則裡的位址」算出來的
    #: ——這四種選擇器計 0 人卻放行無限多人。剩下的 ``email_list`` 是唯一
    #: 「說得出是誰」的 include 選擇器，也就是 allow 政策唯一可用的那一種。
    #:
    #: 🔴 這份清單在前端有一份鏡像：``src/static/src/paas/components/modal/
    #: AddPolicyModal.js`` 的 module-level ``BULK_GRANT_RULE_TYPES``（builder 用它
    #: 決定 allow+include 下拉要不要提供該選擇器、以及切到 allow 時要重寫哪些既有
    #: 列）。兩份漂開的症狀是「UI 讓租戶選得到、送出必被拒」，租戶端零線索可循，
    #: 開發側則是 repo-lint 綠／odoo-tests 綠（前端無測試）⇒ 由 repo-lint 第 37 條
    #: ``bulk-grant-ruler-sync`` 機械對帳（#543 的根因正是兩份清單分離）。
    _BULK_GRANT_RULE_TYPES = ('email_domain', 'everyone', 'ip_range', 'country')

    @api.constrains('decision', 'rule_ids')
    def _check_allow_policy_no_bulk_grant(self):
        """allow 型政策的 include 規則不得整批放行（#352）。

        ## 為什麼要擋

        Cloudflare Access 的席位是 **per-ACCOUNT** 的全平台共用池，而且是**使用
        者實際認證登入時**才消耗一席。AC 方案按人數計費，計數方式是數
        ``email_list`` 規則裡的位址數——``email_domain``（整個網域放行）、
        ``everyone``（所有人放行）、``ip_range``（整段 IP，極端如 ``0.0.0.0/0``）
        與 ``country``（整個國家）**一律計 0 人**。兩者相乘的後果不是「少收一點
        錢」，而是：租戶用一條規則放進不特定多數人，這些人登入時照樣吃席位，池
        子用完之後下一個人登入當場被擋——**受害的是平台上其他租戶，而他們零
        workaround**。

        ⚠ 席位池的**精確席數**是 Cloudflare 方案的外部常數（會隨方案變動），刻意
        不寫在本檔也不寫進租戶可見訊息（#890，Eugene 2026-09-05 裁「拿掉數字」）
        ——精確值與其時效見 ``docs/reference/gotchas/cf-zero-trust-seat-semantics.md``。

        ## 為什麼只擋 include

        include 是 OR：命中任一條就放行，所以整批放行的入口在這裡。
        ``require``（AND）與 ``exclude``（NOT）只會**縮小**放行範圍——
        「allow 但排除 evil.com 這個網域」是正當且常見的寫法，擋它沒有道理。

        ## 為什麼 bypass 豁免（#521 P-J＝Eugene 2026-09-05 裁決 (B)「只擋 allow」）

        bypass 政策**不驗證身分**，使用者不經 Cloudflare 認證 ⇒ **不消耗席位**，
        因此不是繞過席數計費的路徑；bypass + IP 是既有的運維工具（stg 現有
        ``odoo-stg-ip-bypass`` 等規則就是這個形狀），且已在席位盤點口徑裡看得見
        （#521 留言 9960 §2 盤點納入 bypass，**盤點口徑不因本守衛改動**）。

        🔴 而且擴到 bypass 會讓 bypass 政策**一種寫法都不剩**：
        ``_check_decision_selector_compat`` 規定 bypass 不得用 identity-based
        選擇器（email_domain / email_list），若本守衛再把 ip_range / country /
        everyone 也擋掉，五型全禁而 ``_check_at_least_one_include`` 又要求每個
        policy 至少一條 include ⇒ bypass policy 建不出來。⇒ (B) 在結構上是**必需**
        而非偏好。

        機制上這道豁免**不是**靠列舉，而是靠下面 ``decision != 'allow'`` 那行早退
        ——它在 rule_type 判定**之前**，所以 bypass 的規則結構上到不了
        ``_BULK_GRANT_RULE_TYPES``。⛔ 動那行等於同時撤掉本段的全部論證。

        ``deny`` 同樣不受限：整批**封鎖**一個網域不會放任何人進來。

        ## 本檢查的邊界

        擋掉繞過寫法 **≠ 席數計費成立**：本守衛只保證「allow 政策的 include 群組
        說得出放行的是誰」，它與 ``ac_max_access_seats`` 的席數上限之間仍然沒有
        連線（那條線在 ``_check_access_seat_quota``，另一把尺）。

        ⚠ **本守衛與席位盤點是刻意分離的兩把尺，⛔ 不要「對齊」**：
        ``_listed_seat_addresses`` 只計 ``rule_type == 'email_list'``、不分
        decision、不分 group（#521 留言 9960 §2）。把 ip_range / country 加進本
        清單**不會**連動席位計數；反過來把它們加進 ``_listed_seat_addresses``
        會同時推翻 9960 §4（cap=0 的租戶仍可用非 email_list 型規則）。

        四條繞過路徑（#352 收 email_domain／everyone，#521 P-J 收 ip_range／
        country）到此收齊；allow 政策的 include 群組現在只剩 ``email_list``
        可用——這是刻意的產品收窄，不是副作用。

        ## migration bypass：``cr.sa_skip_bulk_grant_check``（#543）

        本守衛 2026-08-02 才加入（#352），而把 flat policy 轉成 include rule 的
        ``migrations/18.0.1.0.39/post-migrate.py`` 早在 2026-06-13 就存在。任何
        仍停在 ``< 18.0.1.0.39`` 且帶 allow × (email_domain|everyone) flat policy
        的 DB（**主要是從舊備份還原**，不是日常升版）升版時，migration 的
        ``Rule.create`` 會撞上這條守衛而**整個升版中斷**。

        擋住升版並不會讓那筆 bulk-grant 資料消失——它在 pre-P-rule 時代就已經
        寫在 policy 的 flat 欄位裡了。migration 只是**換形狀**，不改語意；真正
        該擋的是「使用者現在新寫一條」，那條路徑不帶本 flag，照擋。

        因此 ``.39`` migration 以本 flag 讓歷史資料 grandfather 通過，並在
        migration 結束後**逐筆 WARNING 列出**這些政策（用本方法自己判定，不另
        抄一份 rule_type 清單），讓它們可見而非被靜默漂白。

        flag 放在 **transaction CURSOR** 而非 context。⚠ 這裡**不**照抄
        ``sa_skip_policy_quota`` 的理由：那條的 context-stripped 論證是針對
        ``_check_policy_quota``（trigger 含 ``security_access_id``，會被 stored
        related 的延遲重算在 context-stripped env 裡再觸發一次）；本守衛的
        trigger 是 ``decision`` / ``rule_ids``，不在那條重算路徑上。**實測
        context flag 在 .39 的路徑上一樣有效**（#543 的鑑別實驗：把本判斷改成
        ``self.env.context.get(...)`` 並在 ``Rule.with_context`` 帶上，A–E 五組
        結果與 cursor 版完全相同）。

        仍選 cursor 的理由是**涵蓋面**而非該路徑必要：本方法有三個呼叫端
        （``Rule.create`` / ``Rule.write`` 的顯式呼叫、以及自身的
        ``@api.constrains``），context flag 必須逐一掛對；cursor 是整個
        transaction 共用，migration 只要在同一個 ``finally`` 裡與
        ``sa_skip_policy_quota`` 一起清掉即可，日後 migration 若長出 write
        路徑也不會靜默漏掉 bypass。
        """
        if getattr(self.env.cr, 'sa_skip_bulk_grant_check', False):
            return
        for pol in self:
            if pol.decision != 'allow':
                continue
            offenders = pol.rule_ids.filtered(
                lambda r: (r.group == 'include'
                           and r.rule_type in self._BULK_GRANT_RULE_TYPES))
            if not offenders:
                continue
            raise ValidationError(
                _("Allow policy %r cannot use %s as an Include rule.\n\n"
                  "Why: seat-based Access plans count people by the addresses "
                  "listed in 'Email list' rules, so a whole-domain, everyone, "
                  "IP-range or country rule admits an unlimited number of "
                  "people while counting as zero. Every one of them still "
                  "draws on the Cloudflare Access seat pool shared by the whole "
                  "platform, and once that pool is exhausted the next login is "
                  "refused — for every tenant, not just this one.\n"
                  "What to use instead: list the individual addresses in an "
                  "'Email list' Include rule. To narrow (not widen) access you "
                  "can still use these selectors in Require / Exclude rules, and "
                  "Block policies are unaffected. Bypass policies are exempt "
                  "because they skip authentication and therefore consume no "
                  "seat.")
                % (pol.name or pol.id,
                   ' / '.join(sorted(set(offenders.mapped('rule_type'))))))

    @api.constrains('rule_ids')
    def _check_emails_per_policy(self):
        # Per-policy email cap (plan.sa_max_emails_per_policy, seeded 20). Counts
        # every address across the policy's email_list rules. 0 = unlimited; no
        # subscription / no plan = grandfathered (unlimited), matching
        # _check_policy_quota. Honours the cursor-level sa_skip_policy_quota flag
        # so the migration fan-out (and the stored-related recompute that fires
        # this constraint in a context-stripped env) can bypass it.
        #
        # Two different plan fields, one shared convention for 0 (#743): this
        # constraint's sa_max_emails_per_policy and _check_policy_quota's
        # sa_max_access_policies both read 0 as "unlimited". They did NOT until
        # #743 — this one already exempted 0 while _check_policy_quota treated
        # it as "not a single policy allowed", which is why a plan carrying
        # sa_max_access_policies = 0 could not create its first policy (#521).
        # Change one convention, change the other, and keep both in step with
        # the controller pre-check in
        # controllers/security_access.py::_create_policy.
        # NOT a module-wide rule: a third field, sa_max_routes, deliberately
        # keeps 0 = none allowed (see its help text and _check_route_quota).
        if getattr(self.env.cr, 'sa_skip_policy_quota', False):
            return
        for pol in self:
            # #744：錨點走 _anchor_plan()，CS 錨定的 policy 因此吃**它自己**
            # 訂閱方案的上限。原本直讀 security_access_id：CS 錨定時那個欄位
            # 恆空 ⇒ 整條 continue，上限對 CS 靜默失效（症狀與「方案不限」
            # 完全相同，沒有任何訊號分得出來）。
            plan = pol._anchor_plan()
            if not plan:
                continue
            quota = plan.sa_max_emails_per_policy
            if not quota:  # 0 = unlimited
                continue
            count = 0
            # #1054：`pol.rule_ids` 是本檔第三個 `len(o2m)` 家族——同樣走
            # `search_fetch`、同樣吃 ir.rule、同樣跑在寫入者 env 上。之所以
            # 今天不會少算：本 constraint 的 trigger 只有 `rule_ids`，只有
            # 「寫 policy 的 rule_ids」會觸發它（直接建／改子列不會回頭跑
            # parent 的 constraint），而寫 policy 需要 policy 的 write 權 ⇒ 寫
            # 得動的人必然看得到整組 rule。讀不到 policy 的人在這一行直接
            # AccessError（fail-closed）。2026-09-06 量測（run 2674）判**結構
            # 上不可達**，Eugene 裁 (2) 不加 sudo；守線＝
            # `src/tests/test_quota_len_o2m_reach_1054.py`。
            for rule in pol.rule_ids.filtered(
                    lambda r: r.rule_type == 'email_list'):
                count += len([e for e in (rule.rule_value or '').split(',')
                              if e.strip()])
            if count > quota:
                raise ValidationError(
                    _("Email quota exceeded: plan '%s' allows %d email(s) per "
                      "policy.") % (plan.name, quota))

    @api.constrains('security_access_id', 'smart_home_id', *_ANCHOR_FIELDS)
    def _check_policy_quota(self):
        # P2.5: the quota is an SA-LEVEL total (plan.sa_max_access_policies is
        # a per-SA allowance, not per-route), so count every policy across all
        # of the SA's routes via the stored related security_access_id. Policies
        # now bind to routes, but the budget is still pooled per Security Access.
        #
        # #744: the same shape, one anchor over — a CS-anchored policy pools
        # per CLOUD SERVICE (that is the entity carrying the subscription), so
        # the count is scoped by whichever anchor this row actually has. The
        # anchor also decides WHICH subscription's plan supplies the number;
        # both come from _anchor()/_anchor_plan() so the scope of the count and
        # the source of the quota can never drift apart.
        #
        # sa_skip_policy_quota: the P2.5 migration fan-out copies the OLD single
        # shared app's legacy policy set onto EACH protected route (behaviour-
        # equivalent re-home, not a user adding capacity). The quota was a per-SA
        # total designed for the old single-app model where the set was counted
        # once; fanning the same set across M routes multiplies the count under
        # the same budget, so an SA with a real subscription near its quota would
        # trip this constraint and abort the migration. The migration runs the
        # re-home + fan-out under this key so the constraint is bypassed for that
        # one-shot data move; normal user-driven create/write keep enforcing it.
        # sa_skip_policy_quota lives on the transaction CURSOR, not the context:
        # this constraint also fires from the deferred recompute of the stored
        # related security_access_id at flush time, and that recompute re-browses
        # the records in a context-STRIPPED env, so a context-only flag is
        # silently dropped on those passes (the fan-out copy would still abort).
        # The cursor is shared by every env in the transaction, so a flag set on
        # it survives the recompute env reconstruction. Set + cleared by the
        # migration around its one-shot data move.
        #
        # quota 0 = unlimited (#743), same convention _check_emails_per_policy
        # above uses for its own field — see the note there for why the two
        # fields used to disagree. The CS access variant is specced to carry
        # sa_max_access_policies = 0 meaning "no cap on policy count" (#521
        # design 拍板 4, not yet built); before #743 that would have rejected
        # its very first policy. The BFF pre-check in
        # controllers/security_access.py::_create_policy carries the same
        # exemption and runs FIRST — a fix here alone is unreachable from the UI.
        # sa_max_routes is deliberately NOT part of this: 0 there still means
        # none allowed.
        if getattr(self.env.cr, 'sa_skip_policy_quota', False):
            return
        for pol in self:
            plan = pol._anchor_plan()
            if not plan:
                continue
            quota = plan.sa_max_access_policies
            if not quota:  # 0 = unlimited, same as _check_emails_per_policy
                continue
            anchor = pol._anchor()
            # #768：查表而非三元式，理由見 `_ANCHOR_POOL_FIELD`。查不到就 raise
            # ——新增第四個錨點卻忘了登記時，靜默 continue 的症狀是「配額對該錨
            # 點完全失效」，而那與「方案不限」輸出完全相同。
            anchor_field = self._ANCHOR_POOL_FIELD.get(anchor._name)
            if not anchor_field:
                raise ValidationError(
                    _("Access policy %r is anchored on %s, which has no entry in "
                      "_ANCHOR_POOL_FIELD — the plan quota cannot be scoped. "
                      "Register the anchor model before shipping it.")
                    % (pol.name or pol.id, anchor._name))
            # #896：`sudo()` 是尺的一部分，不是可省的最佳化——與同檔
            # `_seat_usage_for_anchor()` 同一個理由。配額是**平台成本**，不是
            # 「這位呼叫者看得到多少」：`ir.rule` 只讓呼叫者看到池中一部分
            # policy 時，裸 `search_count` 會**少算**，而少算的症狀是「配額安靜
            # 地變寬」——與「方案的政策數上限很大」在輸出上完全不可區分。
            # #924 已在真沙盒量到這個落差（非成員裸 `search_count` 0 vs
            # `sudo()` 2）。當時端到端不可達，靠的是兩個**不屬於這個守衛**的
            # 外部性質：stored related `security_access_id` 的 `compute_sudo`
            # 重算讓大多數路徑跑在 su env、以及 #848 的 `AccessError` 前置擋掉
            # 跨錨點搬移；任一被改動這裡就露出來，而且露出來時無訊號。
            # （池仍以錨點 id 收斂 ⇒ sudo 只放寬可見度，不會把別的租戶算進來。）
            count = self.sudo().search_count([(anchor_field, '=', anchor.id)])
            if count > quota:
                raise ValidationError(
                    _("Access policy quota exceeded: plan '%s' allows %d "
                      "polic(ies).") % (plan.name, quota))

    @api.constrains('rule_ids', 'security_access_id', 'smart_home_id',
                    *_ANCHOR_FIELDS)
    def _check_access_seat_quota(self):
        """#521 第 1 層：``plan.ac_max_access_seats`` 的 enforcement。

        判準＝**1-A 清單基準**（Eugene 2026-08-25 拍板；權威措辭在
        ``docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md:270``）：
        「distinct email 位址數 ≤ 內含＋已購」，計數範圍是**跨該訂閱全部
        route 去重**（同檔 ``:258``）。超額行為＝擋寫入（2026-08-15 已拍，
        不做自動計費）。

        ## 「內含＋已購」為什麼就是 plan 上那一個欄位

        內含 M=0；已購走 ``{base}_ac{n}_{cycle}`` 隱藏席數變體 plan
        （``sh_subscription_plan_visibility.py::_attach_variants``）。一筆訂閱
        恰好掛一個 plan ⇒ 該 plan 的 ``ac_max_access_seats`` 就是這筆訂閱的
        席位總額，不需要再去別處加總。

        ## 🔴 0 ＝ 一席都沒有，**不是** unlimited

        **這個語意是被拍板過的，不是從實作推導出來的。** Eugene 2026-08-26
        逐字裁決「**維持 0 席，要用就加購**」——問法是「基本方案（沒加購席位的）
        要不要送 1 席？接上檢查後這些客戶一個 email 都不能放」，答案是不送。
        裁決載體＝**#521 留言 id 9960 §4**（同批關閉「未驗前提 4」）。
        ⇒ ⛔ **不要把 base 方案改成內含席位**，那不是「現況可以另外決定」的一格，
        是已經被問過而且決定了的一格；要翻案得回去重新提問拍板。

        🔴 **它與相鄰 ``sa_max_access_policies`` 的「0 ＝ 不限」語意相反，而且
        兩邊都是刻意的**——那一邊也拍過：**#521 留言 id 7397**（2026-08-15）
        逐字「``_check_policy_quota`` 改 0=unlimited（與同檔 email 檢查語意對齊，
        順治兩段 0 語意相反的缺陷）」，實作票＝#743。
        ⇒ 兩個 0 並存**不是**還沒統一的技術債，⛔ **不要順手把其中一邊「對齊」
        成另一邊**——那會同時推翻兩個不同日期的拍板。

        欄位自己的三個載體與上述裁決一致（以下三個載體仍然正確，不要改）：
        ``sh_subscription_plan_ac.py`` 的 help 逐字「上限＝0＋已購 AC 席數」
        「0＝未加購任何席位」；pricing ``:270`` 的「內含 M=0」；前端能力 gate
        ``ServiceDetailPage.js:229`` 是 ``> 0`` 才顯示 AC 頁籤（base 方案根本
        不提供這個功能）。
        ``_check_emails_per_policy`` 的註解已寫明 0 的語意**不是** module 級
        慣例（``sa_max_routes`` 的 0 同樣是「一條都不准」）⇒ 逐欄位認定。
        ⚠ 改這一格之前先看 ``TestAcSeatBasePlanBlastRadius``：seed 的 base 方案
        全部是 0，所以這個語意直接決定既有租戶的可寫性。

        ## 計數範圍走錨點池，與 ``_check_policy_quota`` 同一把尺

        ⚠ #773 起，「取池 → 去重計數」與「≤ 就放行」這兩段**實作已移到**
        ``_seat_usage_for_anchor()`` / ``_seat_quota_violation()``，因為四條換方案路徑的
        降級 gate 必須用**同一把尺**（見那兩個方法的 docstring）。以下兩段講的是那把尺
        的性質，實作在那裡、不在本方法裡。

        判準的字面是「跨該訂閱全部 route」，而 policy 綁的是 route、池欄位是
        stored related ``security_access_id`` ⇒ 用 ``_ANCHOR_POOL_FIELD`` 取池
        欄位，等價於「該 SA 的全部 route」。CS／SH 錨定同理（各自 pool 在自己
        的錨點上，而錨點就是帶訂閱的那個實體）。**不自己走一次錨點**：
        ``_anchor()`` / ``_anchor_plan()`` 是唯一裁決處，計數範圍與配額來源
        因此不可能漂開。

        ## 為什麼池的 search 要 ``sudo()``

        席位是**平台成本**，不是這位使用者看得到多少。租戶側若因 ir.rule 只看
        得到池中一部分 policy，未 sudo 的計數會**少算**，而少算的症狀是「配額
        安靜地變寬」——與「方案席位很多」在輸出上完全不可區分。
        （池仍以錨點 id 收斂 ⇒ sudo 只放寬可見度，不會把別的租戶算進來。）

        ⚠ **這一段曾經寫著「``_check_policy_quota`` 沒有 sudo 是既有形狀，不在
        本次範圍內改」——#896 已經把那一邊也補上 sudo（2026-09-05）**，兩條
        quota 的計數口徑因此一致。⛔ 別再把任何一邊改回裸 ``search_count``：
        少算的症狀無訊號，#924 已在真沙盒量到落差（非成員裸 ``search_count`` 0
        vs ``sudo()`` 2）。
        ⚠ 這裡講的**只是「數池子」那一次查詢**；讀 record 內容一律維持呼叫者的
        env，sudo 不得外溢（``_listed_seat_addresses()`` 讀的是 sudo 過的 pool，
        因為它就是那把尺的一部分）。

        ## migration bypass 沿用 ``cr.sa_skip_policy_quota``

        既有 DB 的 plan 一律 ``ac_max_access_seats = 0``，而
        ``18.0.1.0.37`` / ``.39`` / ``.150`` 三支 migration 都會在資料搬移過程
        中建立／重算帶 email_list 的 policy。不沿用同一顆 cursor flag 的話，
        任何帶 email_list policy 的既有 DB **升版當場中斷**。沿用它零成本地
        把三支都涵蓋到，且不必回頭改那三個檔。
        ⚠ 這**不是** grandfather：flag 只活在 migration 那顆 transaction 裡，
        清掉之後租戶的下一次編輯照擋。**（這一段講的是 flag 的作用域，與決策點
        4 拍不拍板無關，不要因為下面那格已拍就把它刪掉。）**

        「既有超額資料怎麼辦」＝ #521 決策點 4，**Eugene 2026-08-26 已拍板：
        一律擋（含修改舊資料）**；4A（grandfather 豁免名單）與 4C（只擋新增）
        皆未採。⇒ 本方法不做任何豁免**就是那個裁決本身**，不是等拍板期間的
        暫時保守做法。裁決載體＝#521 留言 id **9960**（量測報告＝留言 9959）。
        """
        if getattr(self.env.cr, 'sa_skip_policy_quota', False):
            return
        for pol in self:
            plan = pol._anchor_plan()
            if not plan:
                # 無訂閱／無方案 ⇒ grandfathered，與兩條姊妹 quota 同款。
                continue
            violation = self._seat_quota_violation(pol._anchor(), plan,
                                                   _blame=pol)
            if violation:
                used, seats = violation
                raise ValidationError(
                    "Access seat quota exceeded: plan '%s' allows %d Access "
                    "seat(s), but this subscription would list %d distinct "
                    "email address(es) across all of its routes. Seats are "
                    "counted per subscription with addresses de-duplicated "
                    "across every policy and route, so listing the same "
                    "address twice still costs one seat. Remove addresses, or "
                    "add Access seats to the subscription."
                    % (plan.name, seats, used))

    @api.model
    def _seat_usage_for_anchor(self, anchor, _blame=None):
        """該錨點池已列出的席位位址集合（去重後）。

        #773：這是**降級前置合規 gate 與寫入守衛共用的同一把尺**。
        `_check_access_seat_quota` 與四條換方案路徑的 gate 都只能經由這裡取用量，
        ⛔ **不得另寫計數**——兩把尺一旦口徑漂開，就會出現「gate 放行、但接下來
        的寫入被守衛擋」的死結（反向亦然：gate 擋住一個守衛其實允許的換方案）。

        ## 為什麼是「錨點池」而不是「這一批 policy」

        判準是 1-A 的「跨該訂閱全部 route 去重」（#521 留言 9960），
        所以計數範圍必須是**整個錨點底下的所有 policy**，不是被驗證的那幾筆。
        錨點→池欄位的對應是 `_ANCHOR_POOL_FIELD` 這張查表（#768：刻意不用三元式）。

        ## 為什麼 `sudo()` 是尺的一部分、不是可省的最佳化

        席位是**平台成本**，不是「這位使用者看得到多少」。租戶側若因 ir.rule 只看得到
        池中一部分 policy，未 sudo 的計數會**少算**，而少算的症狀（配額安靜地變寬）
        與「這個方案席位很多」在輸出上完全不可區分。
        （池仍以錨點 id 收斂 ⇒ sudo 只放寬可見度，不會把別的租戶算進來。）

        :param anchor: cloud_service／security_access／smart_home 之一（`_anchor()` 的回傳）
        :param _blame: 只用於湊 fail-loud 訊息的來源 policy，可省略
        :return: 去重後的位址 ``set``（呼叫端自己 ``len()``）
        """
        anchor_field = self._ANCHOR_POOL_FIELD.get(anchor._name)
        if not anchor_field:
            raise ValidationError(
                "Access policy %r is anchored on %s, which has no entry in "
                "_ANCHOR_POOL_FIELD — the Access seat quota cannot be "
                "scoped. Register the anchor model before shipping it."
                % ((_blame.name or _blame.id) if _blame else anchor.id,
                   anchor._name))
        pool = self.sudo().search([(anchor_field, '=', anchor.id)])
        return pool._listed_seat_addresses()

    @api.model
    def _seat_quota_violation(self, anchor, plan, _blame=None):
        """合規回 ``None``；超額回 ``(used, seats)``。

        #773：**「≤ 就放行」這個比較子全 repo 只有這一份**。守衛與四條換方案路徑的
        gate 都經過它，所以不可能出現一邊寫 `>`、另一邊寫 `>=` 的漂移。

        🔴 **`plan` 是呼叫端給的，本方法刻意不自己走 `_anchor_plan()`**——
        降級 gate 問的是「**目標** plan 放不放得下現有用量」，而換方案當下錨點掛的
        還是**舊** plan；若這裡自己去讀錨點的 plan，gate 會拿舊 cap 比對、永遠放行。

        🔴 **`ac_max_access_seats == 0 ＝ 一席都沒有，不是 unlimited**
        （Eugene 2026-08-26 逐字裁決「維持 0 席，要用就加購」，載體＝#521 留言 9960 §4）。
        ⛔ 不要在這裡補 `if not seats: continue` ——相鄰的 `sa_max_access_policies`
        與 `sa_max_emails_per_policy` 的 0 **是** unlimited，兩種語意都各自拍過板、
        刻意相反；順手「對齊」會讓整條 fail-open，而且剛好在最需要擋的那格
        （base 方案的席數全部是 0）。
        """
        used = len(self._seat_usage_for_anchor(anchor, _blame=_blame))
        seats = plan.ac_max_access_seats
        return None if used <= seats else (used, seats)

    @api.model
    def _seat_gate_denial(self, anchor, plan):
        """降級前置合規 gate（#773）：合規回 ``None``，超額回**已組好的拒絕內容**。

        Eugene 2026-08-28 裁決（#773 留言 **10716**）逐字：降級要「由用戶手動處理，
        不由系統全刪除，所以系統要能偵測是否合規格」。本方法就是那個**偵測**；
        🔴 它**只回報、絕不刪除任何存取權**——收斂動作一律由租戶自己完成
        （刪 policy／rule 走既有 ``unlink()`` → CF 同步）。

        ## 為什麼不需要先判「這次是升級還是降級」

        判準寫成「現有用量 ≤ 目標 plan 的席數才放行」之後，升級（席數變大）
        天然通過，⇒ 不必定義「什麼算降級」，也就少一個會被繞過的分類邊界。
        （與 issue-leader 2026-08-28 對齊。）

        ## 為什麼文案講「刪除」而不是「編輯」

        rule 側的 ``write()`` 會重跑守衛（``security_access_access_policy_rule.py:247``）
        而守衛比的是**絕對值**，所以處於超額狀態時，**把清單改短但仍超額照樣被擋**
        （行為釘在 ``test_pre_existing_policy_is_blocked_on_edit``）。租戶真正可用的
        收斂通道只有兩條：unlink 整條 rule／整個 policy，或一次改到合規。
        ⛔ 文案若寫「請編輯位址」會把人導進一個他做不到的操作。

        ## 文案的射程：只講 email 位址數，⛔ 不講「存取權已移除」

        尺只計 ``rule_type == 'email_list'``（見 ``_listed_seat_addresses``），
        其餘四型（``email_domain``／``ip_range``／``everyone``／``country``）一律算 0 席。
        租戶把 email 白名單刪光會看到 0 席，但一條 ``email_domain`` 仍在放行整個網域
        ⇒ 訊息只能承諾席位數，不能敘述成「存取權都拆掉了」。

        ⚠ #521 P-J 之後那四型在 **allow 政策的 include 群組**已被
        ``_check_allow_policy_no_bulk_grant`` 擋掉，但本段敘述仍然完整成立——
        它們在 ``deny`` / ``bypass`` 政策、以及 ``require`` / ``exclude`` 群組
        裡都還寫得出來，而本尺**不分 decision、不分 group**。

        :return: ``None``，或 ``{'error': str, 'current_seats': int, 'target_seats': int}``
        """
        violation = self._seat_quota_violation(anchor, plan)
        if not violation:
            return None
        used, seats = violation
        if seats:
            message = _(
                "This plan includes %(seats)s Access seat(s), but this service "
                "currently lists %(used)s email address(es). Delete access "
                "rules or policies until at most %(seats)s address(es) remain, "
                "then change the plan."
            ) % {'seats': seats, 'used': used}
        else:
            # 0 席是最常見的目標態（base 方案的席數全部是 0，見 #521 留言 9960 §4），
            # 值得一句讀得順的文案，不要落到「includes 0 seat(s)」。
            message = _(
                "This plan includes no Access seats, but this service currently "
                "lists %(used)s email address(es). Delete every email allow-list "
                "rule before changing to this plan."
            ) % {'used': used}
        return {'error': message,
                'current_seats': used,
                'target_seats': seats}

    def _listed_seat_addresses(self):
        """這批 policy 的 ``email_list`` 位址集合（正規化後去重）。

        🔴 **不分 decision、不分 group**——這是照 1-A 措辭逐字實作的結果，
        **但該口徑本身未經拍板**。pricing ``:270`` 逐字只有「distinct email
        位址數 ≤ 內含＋已購」、欄位 help 只寫「``email_list`` 去重位址數」，
        兩處都沒有 group 限定；既有陽性範本 ``_check_emails_per_policy`` 與
        #352 同樣只 filter ``rule_type``、不看 ``group``。
        ⚠ 可見的後果：租戶在 ``exclude`` 群組列出「他明確要擋掉的人」，一樣
        佔他的席位。語意可疑，但措辭沒排除它 ⇒ 加不加 group 過濾是產品語意、
        屬拍板範圍，這裡刻意不自己加。行為釘在
        ``test_addresses_in_exclude_and_require_groups_are_counted_too``
        （鑑別力已用 mutation 驗過），要改必須連同 pricing §4.3 一起改。

        🔴 正規化＝``strip()`` ＋ ``lower()``——**這一格未經拍板，而且它是本次
        接線裡唯一連措辭都沒有的判讀**（group 那格至少有 1-A 的字面可循）。
        1-A 只說 ``distinct``，沒說什麼叫 distinct；欄位 help 只說「去重位址數」。
        ⇒ 真正的判別式是 **Cloudflare 自己怎麼折疊 email 大小寫**，屬 P-K 家族
        的外部未驗事實。

        ⚠ 兩種選擇的失效方向**相反**，而且本實作站在 fail-**open** 那一側：

        - CF 折疊大小寫 ⇒ 本實作準確；大小寫敏感版**多算**（租戶提早被擋、
          平台不吃虧）。
        - CF **不**折疊大小寫 ⇒ 本實作**少算**：CF 吃兩席、我們只記一席，差額由
          全平台共用池吸收。這時大小寫敏感版才是準確的。

        選 ``lower()`` 的理由只是「email 比對一般不分大小寫」這個常識性預期，
        **而那正是還沒驗的那件事**。要翻面只需拿掉 ``.lower()``。

        ⚠ 另外注意相鄰兩個計數器對「一筆」的定義**不同**：本方法去重，
        ``_check_emails_per_policy`` 的 ``count += len(...)`` 完全不去重、也不
        處理大小寫。去重本身有載體（1-A 逐字 ``distinct``），大小寫沒有。
        變更前請連同 ``test_two_spellings_of_one_address_cost_one_seat``
        與 ``test_dedupe_is_case_insensitive_but_a_new_address_still_blocks``
        一起看。
        """
        addresses = set()
        for rule in self.mapped('rule_ids').filtered(
                lambda r: r.rule_type == 'email_list'):
            for raw in (rule.rule_value or '').split(','):
                address = raw.strip().lower()
                if address:
                    addresses.add(address)
        return addresses

    # Atomicity scope: mirror the route sub-model's savepoint so a
    # create/write/unlink whose Access re-sync raises rolls the DB row back and
    # re-raises, rather than leaving a committed policy row the operator never
    # received. The guarantee is Odoo-side ONLY — sync_access_app is a
    # declarative full-state push with no compensating action, so a
    # mid-reconcile operator failure can still leave Cloudflare partially
    # mutated; that residual CF↔DB divergence converges via the next idempotent
    # full-state re-sync, not via this savepoint (same scope the route model
    # documents). A stale policy row alone is not served publicly, so the
    # window is benign relative to routes, but keeping the savepoint matters
    # for parity: a BFF that catches the sync error and reports success would
    # otherwise leave the row committed while CF was never updated.

    # P2.5: a policy now belongs to ONE route, so a mutation re-syncs that
    # route's own Access app (not the whole SA). The liveness gate lives inside
    # route._sync_access_to_operator (it no-ops without a live tunnel / when the
    # route is not protected), so create/write/unlink just call the route sync
    # and let the route decide. This keeps the gate single-sourced on the route.
    #
    # #745：三個 override 的 re-sync 一律走 `_access_sync_targets()`（錨點分派），
    # 不再寫死 `mapped('route_id')`。理由見該方法的 docstring。

    def _access_sync_targets(self):
        """這批 policy 該重推的錨點記錄（route 與 cloud_service 混合清單）。

        單一裁決處。改成分派而不是在三個 override 各加一個
        `mapped('cloud_service_id')` 分支，是因為漏改其中一處的症狀是**什麼都
        不會發生**：沒有例外、沒有 log，DB 有一條 policy 而 Cloudflare 上沒有
        對應的規則——與「推了而且成功」在輸出上不可區分。

        回 list 而非 recordset：三個錨點是不同 model，union 不起來。三邊都有
        `_sync_access_to_operator()`（各自帶自己的 liveness gate），呼叫端不必
        知道拿到的是哪一種。arc 保證每一列恰好落在其中一邊，所以不會有同一次
        變更被兩個錨點各推一次的情形。

        #768：迴圈跑 `_ANCHOR_FIELDS` 而不是手寫三個 `mapped()`——手寫的版本每
        新增一個錨點就多一個「漏加就完全靜默」的位置。
        """
        targets = []
        for field in _ANCHOR_FIELDS:
            targets += list(self.mapped(field))
        return targets

    def _access_anchor(self):
        """這一列的那個錨點記錄（arc 保證恰好一個），沒有錨點時回空 recordset。

        單一裁決處：`unlink()` 的捕獲迴圈與 `write()` 的搬家偵測問的是同一件事
        （「這一列現在掛在誰身上」）。各寫一份的話，第四個錨點上線時會漏改其中
        一處，而漏改的症狀是**什麼都不會發生**——與 `_access_sync_targets()` 的
        docstring 講的是同一格。
        """
        self.ensure_one()
        return next(
            (self[f] for f in _ANCHOR_FIELDS if self[f]),
            self.env['woow_paas_platform.security_access_route'].browse())

    def _anchor_move_plan(self, pre_anchor_state):
        """算出「這一批 policy 換錨點」的後果，**不做任何寫入**。

        回傳 ``(失去了 policy 的舊錨點 list, {舊錨點 key: [要回收的 cf id]},
        要清掉 cf id 的 policy recordset)``。

        ``pre_anchor_state``：``{policy.id: ((舊錨點 model, id) 或 None,
        舊 cf_policy_id)}``，由 `write()` 在 ``super().write()`` **之前**擷取。

        **刻意純計算**：`write()` 要先拿它排出推送順序、把**所有**錨點授權過一
        遍，才准產生第一個 side effect（落帳、清列、推 operator 都算）。計算與
        寫入混在一起的話，授權失敗前就已經有東西寫出去了。實際寫入在
        `_apply_anchor_move()`。

        「失去了 policy 的舊錨點」刻意**不從 `_access_sync_targets()` 推導**：
        那個方法回的是 `mapped()` 的結果，而 Odoo 明文寫著 `mapped()` 的
        recordset 順序 arbitrary（`odoo/models.py` 的 docstring），實際順序由
        policy 的 `_order` 決定，跟「誰失去了 policy」無關。想表達的是語意
        （失去者要先被告知），就得直接算語意，不能借用集合順序。

        ## 為什麼搬家要落帳＋清列（兩件事共用同一個判斷）

        ### 為什麼落帳（只重推舊錨點不夠）

        那顆 cf id 已經跟著列搬到新錨點去了，而舊錨點的
        `_build_existing_policy_ids()` 是從它底下**尚存**的列推導出來的
        ⇒ 舊錨點那一輪送出去的 `existing_policy_ids` 裡沒有它。那份清單是
        operator 唯一的刪除通道，所以少了這一步，重推只是推了一份「沒有它」的
        期望狀態：舊 app 不再引用它（好事），但那顆帳號級的 reusable policy
        從此沒有人刪得掉。

        ### 為什麼順手把列上的 id 清掉

        `cf_policy_id` 的語意是「這一列的錨點上一次成功 sync 時為它建出來的那顆
        CF policy」。列搬走之後這句話是假的——那顆 policy 還掛在**舊** app 上。
        留著的話新錨點的 `_build_existing_policy_ids()` 會把它當成自己的東西送
        去刪，而此時舊 app 可能還引用著它。那次刪除會發生什麼，**repo 內沒有
        證據**：operator 的 detach-before-delete 註解講的是「本 app 自己那顆」，
        `delete_reusable_policy` 只有 (200,202,404) vs 其他的二分，沒有任何
        in-use／409 分支。兩種可能都不好——被拒絕就變成記在**新**錨點帳上的
        假孤兒（每輪重送、每輪再記）；真的被刪掉就是把**舊 app 正在用的** policy
        無聲拆掉。清掉之後這一整格不存在，也就不必賭 CF 的實際行為。

        清的範圍嚴格等於落帳的範圍（同一個判斷、同一個迴圈），所以不存在
        「清掉了卻沒記到」的中間狀態。

        ### 為什麼不走 context

        一次性的 context 通道用完即丟，而舊錨點這一次重推可能被它自己的
        liveness gate 整個擋掉（停權中的 SA 就是）——擋掉之後那顆 id 就沒有任何
        記錄指向它。這正是 #528 的根因，帳本就是為了那一格存在的。
        （本節原本指名的 `sa_unlinked_cf_policy_ids` 已於 #783 整條移除，理由見
        `cf_access_policy_reap` 模組 docstring 的設計決定 1。）

        ### 為什麼**不看** `_CTX_SKIP_ROUTE_SYNC`

        sentinel 抑制的是「這一次要不要對 operator 說話」，不是「這件事有沒有
        發生」。批次搬列（migration）之後那顆 CF policy 一樣掛在舊 app 上，帳落
        下去才等得到那些流程自己發的 re-sync；跟著跳過就又退回一次性機制。
        `unlink()` 的落帳同樣不看 sentinel。

        ⚠ 記進去的一律是 **pre-write 從列上讀到的** `cf_policy_id`，不是 vals
        也不是 context 的值（帳本模組 docstring 的設計決定 1）。
        """
        lost = []
        seen_lost = set()
        moved_by_anchor = {}
        booked = self.browse()
        for pol in self:
            old_key, old_cf_id = pre_anchor_state.get(pol.id) or (None, None)
            if not old_key:
                continue
            anchor = pol._access_anchor()
            new_key = (anchor._name, anchor.id) if anchor else None
            if new_key == old_key:
                continue
            if old_key not in seen_lost:
                seen_lost.add(old_key)
                lost.append(self.env[old_key[0]].browse(old_key[1]))
            # 沒有 cf id 的列一樣算「舊錨點失去了一條 policy」（它的期望狀態變
            # 了，必須重推），只是沒有東西要回收。
            if old_cf_id:
                moved_by_anchor.setdefault(old_key, []).append(old_cf_id)
                booked |= pol
        return lost, moved_by_anchor, booked

    def _apply_anchor_move(self, moved_by_anchor, booked):
        """執行 `_anchor_move_plan()` 算出來的落帳與清列。

        呼叫時機：**所有錨點都授權過之後、推 operator 之前**。落帳要在推之前，
        推出去的那一輪才帶得到這顆 id（與 `unlink()` 同一個位置、同一個理由）；
        而推失敗時整段連同 `super().write()` 一起被 savepoint 回捲——那是對的，
        列沒搬走就不欠這筆帳。
        """
        Reap = self.env['woow_paas_platform.cf_access_policy_reap']
        for (anchor_model, anchor_id), cf_ids in moved_by_anchor.items():
            Reap._record_ids(anchor_model, anchor_id, cf_ids)
        if booked:
            # #798：這一行走的是 policy 自己的 `write()`，守衛上線後會撞到本 model
            # 那道 reject ⇒ 必須開閘。它是**伺服器端**的清值（搬家語意，見上方
            # docstring），不是呼叫端塞進來的值。
            _internal_policy_cf_write(booked).write({'cf_policy_id': False})

    @api.model_create_multi
    def create(self, vals_list):
        # #798：create 側的一半。`super().create()` 不繞經 `write()`，只守 write 會
        # 留下 create-time squat——一列全新的 policy 出生時就帶著別人的 CF policy id，
        # 之後這個錨點的每一次 sync 都會把那顆 id 送進 operator 的刪除清單。
        # **壓成伺服器端值而不是 pop**：`super().create()` 內部的
        # `_add_missing_default_values()` 會從三個載體補回「vals 裡缺席」的欄位，其中
        # `default_<field>` context 與 `ir.default` 的列**都由租戶決定**（兩者 #798 都
        # 有測試）。完整論證見 `security_access.py::_strip_identity_anchors`。
        _strip_identity_anchors(
            self, vals_list, _POLICY_IDENTITY_ANCHOR_FIELDS,
            is_open=_is_internal_policy_cf_write)
        for vals in vals_list:
            # TRANSITIONAL back-compat shim (P-rule rollout): accept the legacy
            # flat rule_type/rule_value and expand them into a single include
            # rule. Keeps the in-flight controller + existing fixtures working
            # across the Phase 1→3 transition without a 30-site churn. New
            # callers pass rule_ids (include/require/exclude) directly. REMOVE
            # once the controller (Phase 3) and tests use rule_ids natively —
            # tracked in docs/tracking/followups/follow-ups.md.
            rt = vals.pop('rule_type', None)
            rv = vals.pop('rule_value', None)
            if rt is not None and not vals.get('rule_ids'):
                vals['rule_ids'] = [(0, 0, {
                    'group': 'include', 'rule_type': rt,
                    'rule_value': rv or False})]
            # Sparse precedence: a new policy lands at max(sequence)+10 within
            # its ANCHOR (not the flat default 10), so policies never all
            # collide on one precedence — the reorder UI then renumbers in 10s.
            # An explicit sequence in vals is respected.
            #
            # #744：per-anchor，不是 per-route。CF precedence 的作用域就是
            # 「一個 Access application」＝一個錨點，所以 CS 錨定的 policy 要
            # 跟自己那台 CS 的既有 policy 比，不能沿用 route 那條（CS 錨定時
            # route_id 恆空 ⇒ 舊寫法整段跳過，每一條都落在 sequence=10 上，
            # 之後在 CF 撞成同一個 precedence）。
            #
            # ⚠ 這裡讀 raw vals 是**刻意**的，與 `_assert_anchor_arc_vals` 讀
            # 解析後 vals 的理由不衝突：sequence 是體驗（precedence 不要都撞在
            # 10），錨點是安全（不能自帶別人的錨點）。用 default context 塞
            # route_id 的呼叫端會在下一行的 arc 檢查被判定，這裡不必重算一次。
            anchor_field = next(
                (f for f in _ANCHOR_FIELDS if vals.get(f)), None)
            if not vals.get('sequence') and anchor_field:
                last = self.search(
                    [(anchor_field, '=', vals[anchor_field])],
                    order='sequence desc, id desc', limit=1)
                vals['sequence'] = (last.sequence + 10) if last else 10
            # #744：arc 的 create 側。必須在 `super().create()` 之前——INSERT
            # 一旦送出去，擋下來的就是 PG 的 CHECK（訊息不可讀且交易 abort）。
            self._assert_anchor_arc_vals(self._add_missing_default_values(vals))
        with self.env.cr.savepoint():
            policies = super().create(vals_list)
            # Guarantee the ≥1-include invariant even when rule_ids was absent
            # from vals (an empty policy is invalid). Fires after the nested
            # rule_ids creates so the final state is what is validated.
            policies._check_at_least_one_include()
            # sa_skip_route_sync: migration / provision-time bulk row moves set
            # this so the row re-home does NOT call the operator (CF converges
            # via the explicit re-sync those flows issue, not per-row here).
            if not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC):
                for anchor in policies._access_sync_targets():
                    anchor._sync_access_to_operator()
        return policies

    def write(self, vals):
        # #849：投影欄位的直寫守衛。排在最前面與 #798 同一個理由——拒絕要發生在任何
        # side effect 之前；而它排在 #798 **之上**是因為這一格連「這個 key 可以出現在
        # vals 裡」都不成立（`security_access_id` / `smart_home_id` 的值由框架從真錨點
        # 算出來，呼叫端沒有任何合法理由送它）。⛔ 沒有 su 豁免、沒有 sentinel 逃生門，
        # 理由見 helper 的 docstring。
        _reject_derived_projection_writes(vals, _POLICY_PROJECTION_FIELDS)
        # #798：write 側的一半，**必須是本方法的第一件事**——拒絕要發生在任何 side
        # effect（含下面的 pre-write 擷取與 `super().write()`）之前。對**所有人**生效，
        # 含 superuser：`env.su` 豁免會在每個 TransactionCase 與每條 sudo() 路徑上
        # 靜默停用守衛（見 `_INTERNAL_IDENTITY_WRITE` 的註解）。
        _reject_identity_anchor_writes(
            self.env, vals, _POLICY_IDENTITY_ANCHOR_FIELDS,
            is_open=_is_internal_policy_cf_write)
        # #848：錨點搬移的**歸屬**守衛。位置在任何狀態變更之前——新錨點的授權要在
        # `super().write()` 之前成立，不要跟著下面那個排序迴圈靠 savepoint 回捲。
        # 契約、與那個迴圈的分工、以及 su 豁免為何刻意與 #798 相反，全在函式的
        # docstring 裡。
        _assert_anchor_move_authorised(self, vals)
        # #786：錨點欄位出現在 vals 裡時，「該重推誰」**必須在**
        # `super().write()` **之前也算一份**。之後才算的那一份只看得到新錨點，
        # 於是舊錨點一次都不會被重推——而它的 Cloudflare Access application 仍然
        # 引用著剛搬走的那顆 reusable policy（**授權發散**，不只是成本），那顆
        # cf id 也再進不了舊錨點的 `existing_policy_ids`（operator 唯一的刪除
        # 通道，它是從舊錨點底下**尚存**的列推導出來的）。
        #
        # ⚠ 錨點欄位**本來就在**下面的觸發條件裡，所以這不是「忘了觸發」而是
        # 「觸發了但對象取錯」——症狀因此更難發現：換錨點的 write 看起來確實打了
        # operator，只是打的是新錨點。
        #
        # sibling `..._rule.write()`（`security_access_access_policy_rule.py`）的
        # pre/post 聯集是同一個形狀；差別有二：那邊同 model 可以用 `|`（這邊三個
        # 錨點是不同 model，得自己用 (model, id) 去重），而且那邊沒有「誰失去了
        # 子列」這層語意（這邊有，見下面的排序）。
        anchor_write = any(k in vals for k in _ANCHOR_FIELDS)
        pre_anchor_state = {}
        if anchor_write:
            for pol in self:
                anchor = pol._access_anchor()
                pre_anchor_state[pol.id] = (
                    (anchor._name, anchor.id) if anchor else None,
                    pol.cf_policy_id)
        with self.env.cr.savepoint():
            result = super().write(vals)
            lost_anchors, moved_by_anchor, to_clear = (
                self._anchor_move_plan(pre_anchor_state)
                if anchor_write else ([], {}, self.browse()))
            resync = (not _is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                      and any(k in vals for k in (
                          'name', 'decision', 'sequence', 'rule_ids',
                      ) + _ANCHOR_FIELDS))
            targets = []
            if resync:
                # 失去 policy 的錨點排在前面。這個順序**直接由語意算出來**
                # （`_anchor_move_plan()` 回的就是「誰失去了」），刻意不從
                # `_access_sync_targets()` 的順序推導：那是 `mapped()` 的結果，
                # Odoo 明文寫它的順序 arbitrary，實際由 policy 的 `_order` 決定
                # ——與「誰失去了 policy」無關。單筆 vals 只可能有一個新錨點，
                # 但**舊**錨點可以有很多個（一批分屬 N 條 route 的 policy 一次
                # 收攏到同一條），而且新錨點自己也可能同時是某一列的舊錨點。
                targets = _dedupe_anchors(
                    lost_anchors + self._access_sync_targets())
                # 🔴 **所有錨點都授權過，才准產生第一個 side effect。**
                #
                # 錨點欄位在 ORM 層沒有歸屬檢查（arc 是 arity 不是 ownership，
                # `controllers/smart_home.py` 的白名單註解逐字寫過這件事），而
                # Odoo 只在 `write()` 開頭對 **self** 做一次
                # `check_access('write')`，對 Many2one 的**目標**不做任何檢查
                # ⇒ 裸 ORM／RPC 寫得進別人 workspace 的錨點 id。
                #
                # 修這張單之前那條路徑是 fail-closed 的：唯一那一次推送就是新
                # 錨點，讀不到它就 AccessError、savepoint 整段回捲、CF 一次都
                # 沒被碰。改成推兩個之後，若不先授權就開推，第一個（自己的舊
                # 錨點）會先把一輪 delete-then-recreate 打上 CF，然後才因為第
                # 二個錨點 AccessError 而整段回捲——**外部變更回捲不掉**，於是
                # 租戶手上多了一根可以穩定製造 CF 孤兒的槓桿。
                #
                # ⚠ 別把「今天好像也擋得住」當成不必要：那是**隱性**的
                # （帶著 cf id 搬家時，下面 `_apply_anchor_move()` 的清列 write
                # 會先撞到 ir.rule），而沒有 cf id 的列連那個保護都沒有。本迴圈
                # 是唯一一處明文寫著「授權先於不可回捲的外部變更」的地方。
                #
                # 用 `read` 是刻意的：那就是推送路徑本來就要做的事
                # （`_sync_access_to_operator()` 一開頭就讀錨點欄位），不是新加
                # 一道更嚴的門。
                for anchor in targets:
                    anchor.check_access('read')
            if anchor_write:
                self._apply_anchor_move(moved_by_anchor, to_clear)
            for anchor in targets:
                anchor._sync_access_to_operator()
        return result

    def unlink(self):
        affected = self._access_sync_targets()
        # Capture each doomed row's cf_policy_id BEFORE super().unlink() wipes
        # it. The operator deletes reusable policies only via existing_policy_ids
        # (delete-old-then-create), which _build_access_payload derives from the
        # SURVIVING rows. Once the row is gone its cf id never enters that list,
        # so the operator never deletes the policy → permanent CF orphan, one
        # leaked per unlink. The captured ids are booked per-ANCHOR on
        # `cf_access_policy_reap` so the operator reaps them.
        #
        # #528：一次性的通道不夠——那一次 resync 被 gate 擋掉的話 id 就永久消失，
        # 所以帳要落進 DB（見下方 savepoint 內）。
        #
        # #783：帳本是**唯一**的通道了。原本這裡另外用
        # `with_context(sa_unlinked_cf_policy_ids=...)` 把同一批 id 帶給那一次
        # re-sync，但 `_pending_for()` 讀的是「錨點當下的 env.context」，而
        # `/web/dataset/call_kw` 把呼叫端 context 整包換進 env ⇒ 那條腿等於讓租戶
        # 指定 operator 去刪任何一顆它知道 id 的**帳號級** CF reusable policy。
        # 拿掉它不減功能：下面的 `_record_ids()` 對每一個錨點**無條件**執行、而
        # context 只注入給 `exists()` 的子集 ⇒ 帳本本來就是它的嚴格超集。
        #
        # #745：key 帶上 model 名——route 與 cloud_service 的 id 各自從 1 開始，
        # 只用 id 當 key 會把 route#3 的孤兒 id 餵給 cloud_service#3。
        doomed_by_anchor = {}
        for pol in self:
            # #786：「這一列掛在誰身上」改讀 `_access_anchor()` 的單一裁決處，
            # 與 `write()` 的搬家偵測共用同一份判斷。
            anchor = pol._access_anchor()
            if pol.cf_policy_id and anchor:
                doomed_by_anchor.setdefault(
                    (anchor._name, anchor.id), []).append(pol.cf_policy_id)
        with self.env.cr.savepoint():
            result = super().unlink()
            # #528：落帳在**推之前**，而且刻意不看 `anchor.exists()`。
            #
            # 這一次 `_sync_access_to_operator()` 可能被錨點的 liveness gate 整個
            # 擋掉（SA／SH 的停權狀態就是）——擋掉之後 DB 列已刪，那顆 CF policy
            # 若沒有任何記錄指向它，operator 唯一的刪除通道
            # （`existing_policy_ids`）就再也帶不到它。帳落進 DB，壽命才不綁在
            # 「這一次 sync 有沒有跑」上。（#528 之前那條 context 通道只活一次
            # 呼叫，正是這一格被打穿的原因；它已於 #783 移除。）
            #
            # ⚠ 餵進去的一律是**列上讀到的** `cf_policy_id`。這張表決定的是
            # **帳號級** CF 物件要不要被刪，所以它的輸入只能是伺服器端從 DB 列讀
            # 到的值——#783 之所以把 context 那條腿整個拿掉，是同一條理由。
            #
            # sync 拋例外時這段連同 `super().unlink()` 一起被 savepoint 回捲——
            # 那是對的：列活著，cf id 就還在列上，不需要帳。
            Reap = self.env['woow_paas_platform.cf_access_policy_reap']
            for (anchor_model, anchor_id), doomed in doomed_by_anchor.items():
                Reap._record_ids(anchor_model, anchor_id, doomed)
            for anchor in affected:
                if anchor.exists():
                    # doomed id 由上面剛落的帳供給（`_build_existing_policy_ids`
                    # → `_pending_for` → `_pending_ids`）。這裡刻意**不帶任何
                    # context**：#783。
                    anchor._sync_access_to_operator()
        return result

    def _rules_by_group(self, group):
        self.ensure_one()
        # Normalize the value: Odoo reads an UNSET Char (e.g. a valueless
        # 'everyone' selector) back as Python False, which serialises to JSON
        # `false` and is rejected by the operator's Optional[str] rule_value
        # (a non-string) → the whole Access sync 422s. Coerce falsy → None so a
        # valueless selector travels as JSON null the operator accepts.
        return [
            {'rule_type': r.rule_type, 'rule_value': r.rule_value or None}
            for r in self.rule_ids.sorted(key=lambda x: (x.sequence, x.id))
            if r.group == group
        ]

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'name': self.name, 'decision': self.decision,
            'sequence': self.sequence, 'route_id': self.route_id.id,
            # #744／#768：三個錨點都送。前端判「這條 policy 掛在哪」不必回頭
            # 猜——`route_id` 在 CS／SH 錨定時是 False，只讀它會把那些 policy
            # 讀成「掛在 route 0」。
            'cloud_service_id': self.cloud_service_id.id,
            'smart_home_route_id': self.smart_home_route_id.id,
            'include': self._rules_by_group('include'),
            'require': self._rules_by_group('require'),
            'exclude': self._rules_by_group('exclude'),
        }
