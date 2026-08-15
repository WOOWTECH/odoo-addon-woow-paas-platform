from odoo import api, fields, models
from odoo.exceptions import ValidationError


def _arc_violation(label, has_route, has_cs):
    """回傳 exclusive arc 的違規訊息，或 ``None``（合法）。

    單一文案來源：create 前置檢查與 `@api.constrains` 兩個呼叫點共用，否則
    同一個違規在兩條路徑上會給出兩種說法。
    """
    if has_route and has_cs:
        return (
            "Access policy %r is anchored on BOTH an Application Route and a "
            "Cloud Service. A policy belongs to exactly one Cloudflare Access "
            "application, so it must carry exactly one anchor." % label)
    if not has_route and not has_cs:
        return (
            "Access policy %r has no anchor. Set either an Application Route "
            "or a Cloud Service — an unanchored policy can never be pushed to "
            "Cloudflare and is invisible to its own workspace." % label)
    return None


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
    _sql_constraints = [
        (
            'anchor_arc',
            'CHECK((route_id IS NULL) <> (cloud_service_id IS NULL))',
            'An Access policy must be anchored on exactly one of '
            'Application Route / Cloud Service.',
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
            bool(vals.get('route_id')), bool(vals.get('cloud_service_id')))
        if message:
            raise ValidationError(message)

    @api.constrains('route_id', 'cloud_service_id')
    def _check_anchor_exclusive_arc(self):
        """route_id XOR cloud_service_id —— 兩者皆空與兩者皆填都拒（write 側）。

        與 ``_sql_constraints`` 的 ``anchor_arc`` 是**同一條不變式的兩層**，
        不是二選一：SQL CHECK 是唯一擋得住裸 SQL／批次 migration／
        ``_validate_fields`` 跑不到的路徑的那層，本方法負責在 write 方向給出
        講得出「錯在哪」的訊息（PG 的 CHECK 違反只回 constraint 名字）。
        """
        for pol in self:
            message = _arc_violation(
                pol.name or pol.id, bool(pol.route_id),
                bool(pol.cloud_service_id))
            if message:
                raise ValidationError(message)

    def _anchor(self):
        """這一列的錨點記錄：cloud_service（#744）或 route 的 SA。

        **唯一**的「這條 policy 歸誰管」裁決處。quota 與 email 上限都讀它，
        免得兩處各自 walk 一次錨點、日後長出第三個錨點時漏改其中一處。
        arc 保證至多一邊有值，所以 ``or`` 不會遮蔽另一邊。
        """
        self.ensure_one()
        return self.cloud_service_id or self.security_access_id

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
                    "Access policy %r must contain at least one Include rule."
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
                        "Bypass policies cannot use identity-based selectors "
                        "(email domain / email list). Use IP range, country, "
                        "or everyone instead.")

    #: #352：讓「不特定多數人」通過認證的 include 選擇器。
    #:
    #: AC 方案按人數計費，而人數是「數 email_list 規則裡的位址」算出來的
    #: ——這兩種選擇器計 0 人卻放行無限多人。
    _BULK_GRANT_RULE_TYPES = ('email_domain', 'everyone')

    @api.constrains('decision', 'rule_ids')
    def _check_allow_policy_no_bulk_grant(self):
        """allow 型政策的 include 規則不得整批放行（#352）。

        ## 為什麼要擋

        Cloudflare Access 的席位是 **per-ACCOUNT** 的全平台共用池（50 席），而且
        是**使用者實際認證登入時**才消耗一席。AC 方案按人數計費，計數方式是數
        ``email_list`` 規則裡的位址數——``email_domain``（整個網域放行）與
        ``everyone``（所有人放行）**計 0 人**。兩者相乘的後果不是「少收一點
        錢」，而是：租戶用一條規則放進不特定多數人，這些人登入時照樣吃席位，
        第 51 人登入當場被擋——**受害的是平台上其他租戶，而他們零 workaround**。

        ## 為什麼只擋 include

        include 是 OR：命中任一條就放行，所以整批放行的入口在這裡。
        ``require``（AND）與 ``exclude``（NOT）只會**縮小**放行範圍——
        「allow 但排除 evil.com 這個網域」是正當且常見的寫法，擋它沒有道理。

        ## 為什麼 bypass 豁免

        bypass 政策**不驗證身分**，使用者不經 Cloudflare 認證 ⇒ **不消耗席位**，
        因此不是繞過席數計費的路徑。而且無差別禁用 ``everyone`` 會與
        ``_check_decision_selector_compat`` 打架：那條規定 bypass 不得用
        identity-based 選擇器（email_domain / email_list），只剩 ip_range /
        country / everyone 三種寫法，再拿掉 everyone 就只剩兩種。

        ``deny`` 同樣不受限：整批**封鎖**一個網域不會放任何人進來。

        ## 本檢查的邊界

        擋掉繞過寫法 **≠ 席數計費成立**。``ac_max_access_seats`` 至今沒有任何
        程式碼在讀，補完本檢查後席位數與實際放行人數之間仍然沒有連線（#521）。
        另外 allow + include + ``ip_range``（如 ``0.0.0.0/0``）與 ``country``
        同樣不計人頭卻會佔席位，是第三、四條繞過路徑，**不在本次範圍**。

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
                "Allow policy %r cannot use %s as an Include rule.\n\n"
                "Why: seat-based Access plans count people by the addresses "
                "listed in 'Email list' rules, so a whole-domain / everyone "
                "rule admits an unlimited number of people while counting as "
                "zero. Every one of them still consumes one of the 50 "
                "Cloudflare Access seats shared by the whole platform, so the "
                "51st login is refused — for every tenant, not just this one.\n"
                "What to use instead: list the individual addresses in an "
                "'Email list' Include rule. To narrow (not widen) access you "
                "can still use these selectors in Require / Exclude rules, and "
                "Block policies are unaffected. Bypass policies are exempt "
                "because they skip authentication and therefore consume no "
                "seat."
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
            for rule in pol.rule_ids.filtered(
                    lambda r: r.rule_type == 'email_list'):
                count += len([e for e in (rule.rule_value or '').split(',')
                              if e.strip()])
            if count > quota:
                raise ValidationError(
                    "Email quota exceeded: plan '%s' allows %d email(s) per "
                    "policy." % (plan.name, quota))

    @api.constrains('security_access_id', 'route_id', 'cloud_service_id')
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
            anchor_field = ('cloud_service_id'
                            if pol.cloud_service_id else 'security_access_id')
            count = self.search_count([(anchor_field, '=', anchor.id)])
            if count > quota:
                raise ValidationError(
                    "Access policy quota exceeded: plan '%s' allows %d "
                    "polic(ies)." % (plan.name, quota))

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

        回 list 而非 recordset：兩個錨點是不同 model，union 不起來。兩邊都有
        `_sync_access_to_operator()`（各自帶自己的 liveness gate），呼叫端不必
        知道拿到的是哪一種。exclusive arc 保證每一列恰好落在其中一邊，所以不會
        有同一次變更被兩個錨點各推一次的情形。
        """
        return list(self.mapped('route_id')) + list(
            self.mapped('cloud_service_id'))

    @api.model_create_multi
    def create(self, vals_list):
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
                (f for f in ('route_id', 'cloud_service_id') if vals.get(f)),
                None)
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
            if not self.env.context.get('sa_skip_route_sync'):
                for anchor in policies._access_sync_targets():
                    anchor._sync_access_to_operator()
        return policies

    def write(self, vals):
        with self.env.cr.savepoint():
            result = super().write(vals)
            if (not self.env.context.get('sa_skip_route_sync')
                    and any(k in vals for k in (
                        'name', 'decision', 'sequence', 'route_id',
                        'cloud_service_id', 'rule_ids'))):
                for anchor in self._access_sync_targets():
                    anchor._sync_access_to_operator()
        return result

    def unlink(self):
        affected = self._access_sync_targets()
        # Capture each doomed row's cf_policy_id BEFORE super().unlink() wipes
        # it. The operator deletes reusable policies only via existing_policy_ids
        # (delete-old-then-create), which _build_access_payload derives from the
        # SURVIVING rows. Once the row is gone its cf id never enters that list,
        # so the operator never deletes the policy → permanent CF orphan, one
        # leaked per unlink. Inject the captured ids per-ANCHOR on the resync so
        # the operator reaps them.
        #
        # #745：key 帶上 model 名——route 與 cloud_service 的 id 各自從 1 開始，
        # 只用 id 當 key 會把 route#3 的孤兒 id 餵給 cloud_service#3。
        doomed_by_anchor = {}
        for pol in self:
            anchor = pol.route_id or pol.cloud_service_id
            if pol.cf_policy_id and anchor:
                doomed_by_anchor.setdefault(
                    (anchor._name, anchor.id), []).append(pol.cf_policy_id)
        with self.env.cr.savepoint():
            result = super().unlink()
            for anchor in affected:
                if anchor.exists():
                    doomed = doomed_by_anchor.get(
                        (anchor._name, anchor.id)) or []
                    anchor.with_context(
                        sa_unlinked_cf_policy_ids=doomed
                    )._sync_access_to_operator()
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
            # #744：兩個錨點都送。前端（T4）判「這條 policy 掛在哪」不必回頭
            # 猜——`route_id` 在 CS 錨定時是 False，只讀它會把 CS 的 policy 讀成
            # 「掛在 route 0」。
            'cloud_service_id': self.cloud_service_id.id,
            'include': self._rules_by_group('include'),
            'require': self._rules_by_group('require'),
            'exclude': self._rules_by_group('exclude'),
        }
