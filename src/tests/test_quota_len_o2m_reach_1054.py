"""#1054：配額計數的第二／第三個家族——``len(one2many)`` 同樣吃 ``ir.rule``。

本檔是 **階段 1 的量測工具，不是修法**：src 一行都沒動。它把 #1054 body 提出的
三個問題各自寫成可執行的斷言，讓 CI 的紅／綠集合直接回答「可達／當前不可達／
結構上不可達」，而不是靠讀碼推論。

## 病灶（與 #896 同機制，不同承載體）

``One2many.read`` → ``comodel.search_fetch(domain, field_names)``
（``odoo/fields.py:4645``），而 ``search_fetch`` 的 docstring 逐字寫
「first determine a query that satisfies the domain **and access rules**」
（``odoo/models.py:1772``）⇒ **o2m 的長度讀出來就是 rule-filtered 的長度**。

三個家族：

* ``security_access.py::_check_route_quota``（model）：``len(sa.route_ids)``
  ——上一行才剛 ``sa.sudo().subscription_id``（讀方案有 sudo），下一行數池子沒有。
* ``controllers/security_access.py::_create_route``（BFF 前置）：同一個孿生體。
* ``security_access_access_policy.py::_check_emails_per_policy``：``pol.rule_ids``。

## 0 語意刻意不動

``sa_max_routes`` 的 ``0`` ＝「一條都不准」（與 ``sa_max_access_policies`` ／
``sa_max_emails_per_policy`` 的 ``0 ＝ unlimited`` 相反，見 #743）。本檔每個
fixture 都給**非零**額度，讓紅綠只可能來自「數池子的口徑」，不會摻進 0 語意。

## 為什麼 route 不能沿用 #896 的結論

#924／#896 判 policy 那條「當前不可達」，靠的是兩個**不屬於守衛本身**的外部
性質：stored related ``security_access_id`` 的 ``compute_sudo`` 重算會把
constraint 在 su env 再跑一次；以及 #848 的 ``_assert_anchor_move_authorised``
把跨錨點搬移擋在守衛之前。route 這邊**兩個都沒有**：

* ``security_access_route.security_access_id`` 是**普通 required Many2one**
  （``security_access.py:1667``），不是 stored related ⇒ 沒有 su 重算 backstop；
* route 側**沒有** ``_assert_anchor_move_authorised`` 的對應物
  （``grep -rn _assert_anchor_move_authorised src/`` 只命中 policy 與 rule）。

⇒ 所以要自己量。

## 讀本檔的紅綠時要知道的兩件框架事實

1. ``BaseModel._create``（``odoo/models.py:5291-5292``）的順序是
   ``records._validate_fields(...)`` **先**、``records.check_access('create')``
   **後** ⇒ 非成員的 create **會先跑到 constraint**，之後才被 ``ir.rule`` 擋掉。
   「create 被擋住了」不等於「守衛擋住了」——B 系列把這兩件事分開量。
2. ``BaseModel.write``（``odoo/models.py:4717``）只在**寫入前**對**舊值**做
   ``check_access('write')``，寫入後**不再**重驗 record rule ⇒ 把 m2o 錨點改寫
   到自己看不到的記錄上，rule 這一層攔不住；那條路上唯一的守衛就是
   ``@api.constrains``——而它正是本單說少算的那條。

## 快取：本檔為什麼每次都先 ``invalidate_all()``

``route_ids`` ／ ``rule_ids`` 的 cache key 不含 ``uid``
（``odoo/api.py:938 cache_key``，只有 ``depends_context`` 帶 ``uid`` 的欄位才分
使用者）⇒ fixture 以 su 讀過一次 o2m，換 env 後會**直接命中同一份 cache**，
少算根本不會發生，每一格都會假綠。真實寫入路徑上該 o2m 剛被 ``modified()``
洗掉、是冷的，所以 ``_cold_view()`` 先 ``invalidate_all()`` 是**重現真實狀態**，
不是為了製造 bug。相對地，守衛在 ``self`` 上讀的純量欄位（錨點 m2o）在真實
路徑上本來就在 cache 裡（剛被寫過），所以重新灌熱那幾欄——這與 #896
``_as_outsider()`` 的建構法同一套，理由也同一套。

## RED 紀律

本檔預期在**未修法的 develop 上有紅**——紅的那幾格就是量測結果本身
（見 `.scratch/1054-reachability-pi-leader3.md` 的預測表）。每一格紅的**形狀**
都寫在該格 docstring 裡；紅在別的形狀＝前提壞了，不是缺口被證實。
"""

import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.security_access import _skip_route_sync

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_POLICY_RULE = 'woow_paas_platform.security_access_access_policy_rule'


def _new_group_user(env, name):
    """只有 ``base.group_user`` 的租戶帳號。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    return env['res.users'].sudo().create({
        'name': '%s 1054' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


def _plan_vals(name, **quota):
    vals = {
        'name': name, 'sh_duration': 1, 'sh_unit': 'month',
        'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
        # #521：席位預設 0 ＝「一席都沒有」，帶 email_list 的 fixture 會被
        # `_check_access_seat_quota` 擋掉。本檔測的不是席位那條，給足額度讓
        # 紅燈的原因只可能是被測的那一條（fixture 的 forward coupling）。
        'ac_max_access_seats': 99,
        # policy **數**那條（#896 的家族）在本檔一律不參與：0 ＝ unlimited
        # （#743）。只留 route 數與 per-policy email 數兩個變因。
        'sa_max_access_policies': 0,
    }
    vals.update(quota)
    return vals


class _LenO2mQuota1054Fixture(TransactionCase):
    """受害者 workspace（owner ＋ 一位 role='user' 成員）、攻擊者 workspace、
    以及一個與兩邊都無關的 outsider。

    ⚠ 建立順序刻意是「**先建 route／policy，再掛訂閱**」：反過來 fixture 自己
    就會撞上要測的那條配額，第二列根本建不出來（#896 同款陷阱）。掛訂閱前
    ``_anchor_plan()`` 回空 recordset ⇒ 守衛整條 continue。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.PolicyRule = self.env[_POLICY_RULE]

        self.victim_owner = self._group_user('victim1054')
        self.victim_member = self._group_user('member1054')
        self.attacker = self._group_user('attacker1054')
        self.outsider = self._group_user('outsider1054')

        self.ws_victim = self._workspace('WS-VICTIM 1054', self.victim_owner)
        self._add_member(self.ws_victim, self.victim_member, 'user')
        self.ws_attacker = self._workspace('WS-ATTACKER 1054', self.attacker)

        for stranger in (self.attacker, self.outsider):
            self.assertFalse(
                self.WorkspaceAccess.sudo().search_count([
                    ('workspace_id', '=', self.ws_victim.id),
                    ('user_id', '=', stranger.id)]),
                '前提：%s 不得是受害者 workspace 的成員，否則 ir.rule 會讓他'
                '看到整池，本檔每一格都會失去鑑別力' % stranger.login)

    # ------------------------------------------------------------------ #
    #  帳號 / workspace                                                    #
    # ------------------------------------------------------------------ #
    def _group_user(self, name):
        user = _new_group_user(self.env, name)
        self.assertFalse(
            user.has_group('base.group_system'),
            '前提：fixture 帳號只能有 base.group_user（%s 撞了）' % user.login)
        return user

    def _workspace(self, name, owner):
        """owner 只能由 ``with_user()`` 決定（``_check_owner_count`` 限一位）。"""
        ws = self.Workspace.with_user(owner).create(
            {'name': '%s %s' % (name, uuid.uuid4().hex[:6])})
        self.assertEqual(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('role', '=', 'owner')]).user_id,
            owner,
            '前提：bootstrap 出來的 owner 必須是 %s' % owner.login)
        return ws.sudo()

    def _add_member(self, workspace, user, role):
        return self.WorkspaceAccess.sudo().create({
            'workspace_id': workspace.id, 'user_id': user.id, 'role': role})

    # ------------------------------------------------------------------ #
    #  SA / route / policy                                                 #
    # ------------------------------------------------------------------ #
    def _sa(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 1054 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        return sa

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r1054%s-%s' % (tag, uuid.uuid4().hex[:6]),
        })

    def _policy(self, route, tag, emails):
        """一條 allow policy，帶兩條 include email_list rule（共 ``emails`` 個位址）。

        拆成兩條 rule 是刻意的：``_check_emails_per_policy`` 是「跨 rule 加總」，
        單一條 rule 的話「o2m 少算」與「rule_value 少算」分不開。
        """
        self.assertGreaterEqual(emails, 2, 'fixture：至少要能拆成兩條 rule')
        first = ['a%d@example.com' % i for i in range(emails - 1)]
        vals = {
            'name': 'pol-1054-%s' % tag,
            'route_id': route.id,
            'decision': 'allow',
            'rule_ids': [
                (0, 0, {'group': 'include', 'rule_type': 'email_list',
                        'rule_value': ','.join(first)}),
                (0, 0, {'group': 'include', 'rule_type': 'email_list',
                        'rule_value': 'z@example.com'}),
            ],
        }
        pol = _skip_route_sync(self.Policy.sudo()).create(vals)
        return pol.with_context(sa_skip_route_sync=False)

    def _plan(self, name, **quota):
        return self.env['sh.subscription.plan'].sudo().create(
            _plan_vals(name, **quota))

    def _subscribe(self, sa, plan):
        """``sh_partner_id`` 必須是錨點 workspace 的 owner（照 #896 fixture）。"""
        owner = sa.sudo().workspace_id.owner_id
        self.assertTrue(owner, '前提：錨點必須有 workspace owner')
        sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': owner.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.sudo().subscription_id = sub.id
        return sub

    # ------------------------------------------------------------------ #
    #  冷快取 + 換 env（本檔的核心建構法，理由見 module docstring）          #
    # ------------------------------------------------------------------ #
    def _cold_view(self, record, user, warm_fnames):
        """回傳 ``record`` 在 ``user`` env 下的視圖，且被數的那個 o2m 是**冷的**。

        1. ``flush_all()`` ＋ ``invalidate_all()``：清掉 fixture 以 su 讀進來的
           所有值（含 o2m），重現真實寫入路徑上該 o2m 剛被 ``modified()`` 洗掉
           的狀態。
        2. 只把守衛在 ``self`` 上會讀到的純量／m2o 欄位重新灌熱（以 fixture 的
           su env）——真實路徑上這幾欄剛被寫過、本來就在 cache 裡，從 cache 取值
           不觸發 ``ir.rule``。⛔ 這一步**絕不能**碰要被數的 o2m。
        3. 斷言換過去的 env 不是 su——su 之下 ``ir.rule`` 整個不套，本檔每一格
           都會恆綠（``docs/decisions/patterns/su-env-exempt-guard-false-green.md``）。
        """
        self.env.flush_all()
        self.env.invalidate_all()
        record.read(warm_fnames)
        view = record.with_user(user)
        self.assertFalse(
            view.env.su,
            '前提：%s 的 env 不得是 su——su 之下 ir.rule 不套，本格會恆綠'
            % user.login)
        return view

    def _assert_no_pending_recompute(self, model, fnames):
        """釘住 #896 的 flush 陷阱：待重算的 stored related 會在 su 再跑一次
        constraint，讓 RED 從未紅過。"""
        for fname in fnames:
            field = model._fields[fname]
            self.assertFalse(
                self.env.records_to_compute(field),
                '前提：%s.%s 不得還在 tocompute——su 重算會讓守衛在 su env 再'
                '跑一次，RED 因此不可能紅' % (model._name, fname))


class TestRouteQuotaPoolVisibility1054(_LenO2mQuota1054Fixture):
    """A 系列（家族一，model）：``_check_route_quota`` 的計數口徑。"""

    def _victim_sa_at_quota(self, quota=1, n_routes=2):
        sa = self._sa(self.ws_victim, 'a')
        routes = self.Route.browse()
        for i in range(n_routes):
            routes |= self._route(sa, 'a%d' % i)
        # 先建 route → 再掛訂閱（順序理由見 fixture docstring）。
        self.env.flush_all()
        self._subscribe(sa, self._plan('R1054', sa_max_routes=quota))
        self.env.flush_all()
        self.assertEqual(
            len(sa.sudo().route_ids), n_routes,
            '前提：受害者 SA 必須真的有 %d 條 route' % n_routes)
        return sa, routes

    def test_A1_outsider_env_route_quota_counts_the_whole_pool(self):
        """🔴 本單主格（家族一）：非成員 env 呼叫守衛，超額必須擋下來。

        現況（未修法）：``len(sa.route_ids)`` 在非成員 env 下是 0，
        ``0 > 1`` 為假 ⇒ **靜默放行** ⇒ 本格紅在
        ``ValidationError not raised``。紅在別的形狀＝建構前提壞了。
        """
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        view = self._cold_view(routes[0], self.outsider, ['security_access_id'])
        with self.assertRaises(ValidationError) as cm:
            view._check_route_quota()
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但訊息不是配額那條——確認紅的是本格要驗的分支')

    def test_A2_positive_control_outsider_sees_none_of_the_route_pool(self):
        """陽性對照：重現 #924 的讀數形狀（非成員 0 vs sudo N）。

        沒有這一格，A1 的紅／綠分不出「守衛口徑不對」與「fixture 根本沒造出
        可見度落差」。⚠ 刻意用 ``search_count`` 而不是讀 ``sa.route_ids``——
        後者會把整池灌進 transaction cache，A1 隨即假綠。
        """
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        domain = [('security_access_id', '=', sa.id)]
        as_outsider = self.Route.with_user(self.outsider)
        self.assertFalse(as_outsider.env.su, '前提：對照組必須跑在非 su env')
        self.assertEqual(
            as_outsider.search_count(domain), 0,
            'ir.rule 應該讓非成員完全看不到這個池；看得到就表示 rule 改過了，'
            'A1 的建構前提已經不成立')
        self.assertEqual(
            self.Route.sudo().search_count(domain), len(routes),
            'sudo 的真值必須是 fixture 建的筆數')

    def test_A3_compliant_pool_still_passes_under_the_same_env(self):
        """反面對照：合規的池在**同一個** env 下必須放行。

        少了它，一個「什麼都 raise」的壞守衛也會讓 A1 變綠。
        """
        sa, routes = self._victim_sa_at_quota(quota=5, n_routes=2)
        view = self._cold_view(routes[0], self.outsider, ['security_access_id'])
        view._check_route_quota()  # 不得 raise

    def test_A4_su_env_baseline_still_raises(self):
        """迴歸：su env（既有絕大多數路徑）行為不變——超額仍擋。"""
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        self.assertTrue(routes.env.su, '前提：fixture 的 env 應該是 su')
        with self.assertRaises(ValidationError):
            routes[0]._check_route_quota()


class TestRouteQuotaEndToEndReach1054(_LenO2mQuota1054Fixture):
    """B 系列（家族一）：端到端可達性——真的走 ORM 寫入，不直接呼叫守衛。

    這一組回答 #1054 的第 2 問：「這條 constraint 有沒有哪條寫入路徑跑在非 su
    env 上、而且少算真的造成後果？」——create 與 write 兩條路的答案不同，所以
    分開量。
    """

    def _victim_sa_at_quota(self, quota=1, n_routes=1):
        sa = self._sa(self.ws_victim, 'b')
        routes = self.Route.browse()
        for i in range(n_routes):
            routes |= self._route(sa, 'b%d' % i)
        self.env.flush_all()
        self._subscribe(sa, self._plan('R1054B', sa_max_routes=quota))
        self.env.flush_all()
        return sa, routes

    def test_B1_outsider_create_never_persists_a_route(self):
        """不變量：非成員對受害者 SA 建 route，永遠不得留下任何一列。

        兩種擋法（``ValidationError`` ＝配額守衛、``AccessError`` ＝
        ``ir.rule`` create 腿）都算通過——本格只釘「不得成功」，所以它在階段 2
        修法前後都必須是綠的。誰擋的由 B1b 量。
        """
        sa, _routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        before = self.Route.sudo().search_count(
            [('security_access_id', '=', sa.id)])
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises((ValidationError, AccessError)):
            self.Route.with_user(self.outsider).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'b1x-%s' % uuid.uuid4().hex[:6],
            })
        self.assertEqual(
            self.Route.sudo().search_count(
                [('security_access_id', '=', sa.id)]),
            before, '被擋的 create 不得留下任何 route')

    def test_B1b_today_the_create_blocker_is_ir_rule_not_the_quota_guard(self):
        """量測：create 路徑上**今天是誰擋的**。

        ``BaseModel._create``（``odoo/models.py:5291-5292``）先
        ``_validate_fields`` 才 ``check_access('create')`` ⇒ 守衛**有**跑到，
        只是少算後放行，真正擋下來的是 ``ir.rule`` 的 create 腿 ⇒ 現況預期
        ``AccessError``。

        ⚠ 本格是**故意會在階段 2 翻紅的量測格**：修法後守衛會先 raise
        ``ValidationError``，屆時應把期待改成 ``ValidationError``（而不是把
        斷言放寬成兩者皆可——那會讓「誰擋的」這個資訊消失）。
        """
        sa, _routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.Route.with_user(self.outsider).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'b1y-%s' % uuid.uuid4().hex[:6],
            })

    def test_B2_foreign_owner_can_move_a_route_into_a_full_victim_sa(self):
        """🔴 端到端可達性主格：把自己的 route 改錨到受害者已額滿的 SA 上。

        路徑上每一層為什麼都攔不住（三層都在 develop 上可讀）：

        * ``check_access('write')`` 只在**寫入前**對**舊值**跑
          （``odoo/models.py:4717``）——那時 route 還在攻擊者自己的 SA 底下，
          他是 owner ⇒ 通過；寫入後 Odoo **不再**重驗 record rule。
        * route 側**沒有** ``_assert_anchor_move_authorised``
          （policy 有 #848、rule 有 #941，route 這層是空的）。
        * ``route.write()`` 自己的兩道守衛只管
          ``_ROUTE_IDENTITY_ANCHOR_FIELDS``（dns/app id）與 ``subdomain_prefix``
          不可變，``security_access_id`` 兩者都不在。

        ⇒ 那條路上**唯一**的守衛就是 ``_check_route_quota``，而它在攻擊者 env
        下數到 0 ⇒ 放行。本格預期紅在
        ``(ValidationError, AccessError) not raised``。B3 是它的判別對照。
        """
        sa_victim, _victim_routes = self._victim_sa_at_quota(
            quota=1, n_routes=1)
        sa_attacker = self._sa(self.ws_attacker, 'b2')
        mine = self._route(sa_attacker, 'b2mine')
        view = self._cold_view(mine, self.attacker, ['security_access_id'])
        with self.assertRaises((ValidationError, AccessError)):
            view.write({'security_access_id': sa_victim.id})

    def test_B3_the_same_move_is_caught_under_su(self):
        """判別對照：**同一次搬移**在 su env 下必須被配額擋住。

        它把 B2 的紅歸因鎖死在「可見度」上：若 B2 紅、B3 綠 ⇒ 搬移本身不是
        合法操作，唯一的差別就是數池子的 env；若兩格都紅 ⇒ 是別的東西壞了
        （fixture 沒把受害者 SA 填到額滿），不是本單的缺口。
        """
        sa_victim, _victim_routes = self._victim_sa_at_quota(
            quota=1, n_routes=1)
        sa_attacker = self._sa(self.ws_attacker, 'b3')
        mine = self._route(sa_attacker, 'b3mine')
        self.env.flush_all()
        self.assertTrue(mine.env.su, '前提：對照組必須跑在 su env')
        with self.assertRaises(ValidationError) as cm:
            mine.write({'security_access_id': sa_victim.id})
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但不是配額那條——B2 的歸因就不成立')


class TestEmailsPerPolicyQuotaReach1054(_LenO2mQuota1054Fixture):
    """C 系列（家族三）：``_check_emails_per_policy`` 走 ``pol.rule_ids``。"""

    def _victim_policy_over_quota(self, quota=2, emails=3):
        sa = self._sa(self.ws_victim, 'c')
        route = self._route(sa, 'c0')
        pol = self._policy(route, 'c0', emails=emails)
        # 先建 policy＋rules → 再掛訂閱（否則 fixture 自己就撞配額）。
        self.env.flush_all()
        self._subscribe(
            sa, self._plan('E1054', sa_max_emails_per_policy=quota,
                           sa_max_routes=5))
        self.env.flush_all()
        self._assert_no_pending_recompute(
            self.Policy, ('security_access_id', 'smart_home_id'))
        self.assertEqual(
            len(pol.sudo().rule_ids), 2,
            '前提：policy 必須真的有兩條 rule（跨 rule 加總才有意義）')
        return sa, route, pol

    def test_C1_outsider_env_email_quota_counts_the_whole_rule_set(self):
        """🔴 本單主格（家族三）：非成員 env 呼叫守衛，超額必須擋下來。

        現況：``pol.rule_ids`` 在非成員 env 下是空 recordset ⇒ ``count`` 恆 0
        ⇒ 放行 ⇒ 本格紅在 ``ValidationError not raised``。
        """
        sa, route, pol = self._victim_policy_over_quota(quota=2, emails=3)
        view = self._cold_view(
            pol, self.outsider,
            ['security_access_id', 'cloud_service_id', 'smart_home_id', 'name'])
        with self.assertRaises(ValidationError) as cm:
            view._check_emails_per_policy()
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但訊息不是配額那條——確認紅的是本格要驗的分支')

    def test_C2_positive_control_outsider_sees_none_of_the_rule_set(self):
        """陽性對照：非成員對 rule 池的裸讀數必須是 0、sudo 必須是 2。"""
        sa, route, pol = self._victim_policy_over_quota(quota=2, emails=3)
        domain = [('policy_id', '=', pol.id)]
        as_outsider = self.PolicyRule.with_user(self.outsider)
        self.assertFalse(as_outsider.env.su, '前提：對照組必須跑在非 su env')
        self.assertEqual(
            as_outsider.search_count(domain), 0,
            'ir.rule 應該讓非成員完全看不到這組 rule；看得到就表示 rule 改過了')
        self.assertEqual(self.PolicyRule.sudo().search_count(domain), 2)

    def test_C3_su_env_baseline_still_raises(self):
        """迴歸：su env 下超額仍擋（3 個位址 > 上限 2）。"""
        sa, route, pol = self._victim_policy_over_quota(quota=2, emails=3)
        self.assertTrue(pol.env.su, '前提：fixture 的 env 應該是 su')
        with self.assertRaises(ValidationError):
            pol._check_emails_per_policy()

    def test_C4_outsider_cannot_write_rule_ids_on_a_foreign_policy(self):
        """量測：家族三的 write 路徑今天被誰擋。

        ``_check_emails_per_policy`` 的 trigger 是 ``rule_ids``——只有「寫
        **policy** 的 rule_ids」會觸發它（直接建／改子列不會回頭跑 parent 的
        constraint）。而寫 policy 需要 policy 的 write 權 ⇒ ``ir.rule`` 的
        owner/admin 腿先擋 ⇒ 預期 ``AccessError``，且不得留下任何 rule。
        """
        sa, route, pol = self._victim_policy_over_quota(quota=2, emails=3)
        before = self.PolicyRule.sudo().search_count(
            [('policy_id', '=', pol.id)])
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            pol.with_user(self.outsider).write({
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'x@example.com'})]})
        self.assertEqual(
            self.PolicyRule.sudo().search_count([('policy_id', '=', pol.id)]),
            before, '被擋的 write 不得留下任何 rule')


class TestQuotaPoolVisibilityIsAllOrNothing1054(_LenO2mQuota1054Fixture):
    """D 系列：池的可見度是「0 或全」——這是判「BFF 前置有沒有 RED 路徑」的依據。

    兩個 sub-model 的 ``ir.rule`` 判歸屬走的都是**錨點 workspace 的成員關係**，
    與 o2m 的 domain 同一根 ⇒ 看得到一列就看得到整池。而 BFF
    （``controllers/security_access.py::_create_route``）的呼叫者一律先過
    ``_get_workspace_or_error()``（要有該 workspace 的 ``workspace_access``
    列），所以合法呼叫者必然是成員 ⇒ 前置計數對他不會少算。

    這兩格把這個前提釘住：日後有人把 read 腿改窄（或給非成員開 create 腿），
    這裡會紅，而不是讓 BFF 那條悄悄變成可利用。
    """

    def test_D1_route_pool_is_all_or_nothing(self):
        sa = self._sa(self.ws_victim, 'd1')
        routes = self.Route.browse()
        for i in range(3):
            routes |= self._route(sa, 'd1%d' % i)
        self.env.flush_all()
        domain = [('security_access_id', '=', sa.id)]
        for user, expected in ((self.victim_owner, 3),
                               (self.victim_member, 3),
                               (self.outsider, 0)):
            view = self.Route.with_user(user)
            self.assertFalse(view.env.su, '前提：必須跑在非 su env')
            self.assertEqual(
                view.search_count(domain), expected,
                '%s 應該看到 %d 條——0-或-全的前提破了，BFF 前置的'
                '「不會少算」論證就不成立' % (user.login, expected))

    def test_D2_policy_rule_pool_is_all_or_nothing(self):
        sa = self._sa(self.ws_victim, 'd2')
        route = self._route(sa, 'd2')
        pol = self._policy(route, 'd2', emails=3)
        self.env.flush_all()
        domain = [('policy_id', '=', pol.id)]
        for user, expected in ((self.victim_owner, 2),
                               (self.victim_member, 2),
                               (self.outsider, 0)):
            view = self.PolicyRule.with_user(user)
            self.assertFalse(view.env.su, '前提：必須跑在非 su env')
            self.assertEqual(
                view.search_count(domain), expected,
                '%s 應該看到 %d 條 rule' % (user.login, expected))
