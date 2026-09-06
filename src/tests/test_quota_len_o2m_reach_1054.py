"""#1054：配額計數的第二／第三個家族——``len(one2many)`` 同樣吃 ``ir.rule``。

本檔是 **階段 1 的量測工具，不是修法**：src 一行都沒動。

## 病灶（與 #896 同機制，不同承載體）

``One2many.read`` → ``comodel.search_fetch(domain, field_names)``
（``odoo/fields.py:4645``），而 ``search_fetch`` 的 docstring 逐字寫
「first determine a query that satisfies the domain **and access rules**」
（``odoo/models.py:1772``）⇒ o2m 的長度讀出來是 rule-filtered 的長度。三個家族：

* ``security_access.py::_check_route_quota``：``len(sa.route_ids)``
  ——上一行才剛 ``sa.sudo().subscription_id``，下一行數池子沒有 sudo。
* ``controllers/security_access.py::_create_route``：同一個孿生體（BFF 前置）。
* ``security_access_access_policy.py::_check_emails_per_policy``：``pol.rule_ids``。

## 🔴 第一趟（run 2668）量到的東西改變了本檔的建構法

第一版把「非成員」當成少算的載體，五格全 ERROR，其中三格的形狀是
``AccessError`` 而不是預期的「少算後放行」。原因是 o2m 的取值路徑上**先**有一
道 read 檢查：``fields.py:1266 __get__`` → ``models.py:4100 _fetch_field`` →
``models.py:4124 fetch`` → ``check_access('read')`` ——檢查的是**父記錄**
（``sa`` ／ ``pol``），不是子列。所以：

* 讀不到父記錄的人（完全非成員）⇒ 走到守衛就 ``AccessError``，**fail-closed**，
  根本到不了「少算」那一步；
* 讀得到父記錄的人 ⇒ 他必然是錨點 workspace 的成員，而兩個 sub-model 的
  ``ir.rule`` 判歸屬走的就是同一份成員關係（D 系列釘住的 0-或-全）⇒ 他看得到
  **整池**，計數正確。

⇒ 「少算」要成立，必須存在「**寫得動父記錄、卻只看得到池中一部分子列**」的身
分。A6 ／ C5 就是直接量這件事的判別式（量的是**性質**，不是列舉今天的角色名）。

## 三種結論怎麼由紅綠讀出來

* **(a) 少算可達**：A6 ／ C5 紅（存在「能寫但只看得到一部分」的身分）。
* **(b) 結構上不可達**：A6 ／ C5 綠 ＋ D1 ／ D2 綠 ＋ A1 ／ C1 綠
  （能讀錨點的呼叫者計數正確、守衛照擋）。
* **(c) 非成員 fail-closed**：A2 ／ C2 綠（守衛那條路對非成員直接 ``AccessError``）。

## 兩個框架事實（B 系列的依據）

1. ``BaseModel._create``（``odoo/models.py:5291-5292``）先 ``_validate_fields``
   才 ``records.check_access('create')`` ⇒ 非成員的 create **會先跑到
   constraint**。「create 被擋住了」不等於「守衛擋住了」。
2. ``BaseModel.write``（``odoo/models.py:4717``）只在**寫入前**對**舊值**做
   ``check_access('write')``，寫入後**不再**重驗 record rule ⇒ 把 m2o 錨點改寫
   到自己看不到的記錄上，rule 這層攔不住。route 側又沒有 ``policy`` 的
   ``_assert_anchor_move_authorised``（#848）／``rule`` 的 #941 對應物
   ⇒ 那條路上唯一還會說話的就是 ``@api.constrains``。B2 ／ B2c 量的就是這個。

## ⚠ 兩個踩過的坑，改法寫在這裡免得下一個人重踩

* ``odoo.tests.common`` 覆寫的 ``assertRaises``（``common.py:477``）對
  ``exception`` 直接 ``issubclass(...)``，**不接受 tuple** ⇒
  ``assertRaises((A, B))`` 會 ``TypeError: issubclass() arg 1 must be a class``。
  要驗「兩種之一」請 ``assertRaises(Exception)`` ＋ ``assertIsInstance``。
* ``route_ids`` ／ ``rule_ids`` 的 cache key 不含 ``uid``
  （``odoo/api.py:938``：只有 ``depends_context`` 帶 ``uid`` 的欄位才分使用者）
  ⇒ fixture 以 su 讀過一次 o2m，換 env 後會直接命中同一份 cache，少算不會發生、
  每格假綠。所以換 env 前一律 ``invalidate_all()``——真實寫入路徑上該 o2m 剛被
  ``modified()`` 洗掉、本來就是冷的，這是**重現真實狀態**不是製造 bug。

## RED 紀律

本趟**預期恰一格紅**：``test_B2c_...``（見該格 docstring）。其餘 17 格預期綠。
紅在別的格、或紅在別的形狀＝前提壞了，不是缺口被證實。
"""

import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase

from ..models.security_access import _skip_route_sync

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_POLICY_RULE = 'woow_paas_platform.security_access_access_policy_rule'

#: 探針用的角色矩陣：(屬性名, workspace 角色或 None＝完全非成員)。
#: ⛔ 這份清單是**探針的輸入**，不是判準——判準寫在 A6 ／ C5 的斷言裡
#: （「能寫 ⇒ 看得到整池」），新增角色只要加進這裡就會被一起量到。
_PROBE_IDENTITIES = (
    ('victim_owner', 'owner'),
    ('victim_admin', 'admin'),
    ('victim_member', 'user'),
    ('victim_guest', 'guest'),
    ('outsider', None),
)


class _ProbeRollback(Exception):
    """只用來把探針的 savepoint 捲回去，不代表失敗。"""


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
        # `_check_access_seat_quota` 擋掉。本檔測的不是席位那條。
        'ac_max_access_seats': 99,
        # policy **數**那條（#896 的家族）在本檔一律不參與：0 ＝ unlimited（#743）。
        'sa_max_access_policies': 0,
    }
    vals.update(quota)
    return vals


class _LenO2mQuota1054Fixture(TransactionCase):
    """受害者 workspace（owner／admin／user／guest 四種成員）＋攻擊者 workspace
    ＋完全非成員 outsider。

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

        self.victim_owner = self._group_user('victimowner1054')
        self.victim_admin = self._group_user('victimadmin1054')
        self.victim_member = self._group_user('victimuser1054')
        self.victim_guest = self._group_user('victimguest1054')
        self.attacker = self._group_user('attacker1054')
        self.outsider = self._group_user('outsider1054')

        self.ws_victim = self._workspace('WS-VICTIM 1054', self.victim_owner)
        self._add_member(self.ws_victim, self.victim_admin, 'admin')
        self._add_member(self.ws_victim, self.victim_member, 'user')
        self._add_member(self.ws_victim, self.victim_guest, 'guest')
        self.ws_attacker = self._workspace('WS-ATTACKER 1054', self.attacker)

        for stranger in (self.attacker, self.outsider):
            self.assertFalse(
                self.WorkspaceAccess.sudo().search_count([
                    ('workspace_id', '=', self.ws_victim.id),
                    ('user_id', '=', stranger.id)]),
                '前提：%s 不得是受害者 workspace 的成員，否則 fail-closed 那幾格'
                '會失去鑑別力' % stranger.login)

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

    def _identity(self, attr):
        return getattr(self, attr)

    # ------------------------------------------------------------------ #
    #  SA / route / policy                                                 #
    # ------------------------------------------------------------------ #
    def _sa(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 1054 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.assertEqual(
            sa.state, 'pending',
            "前提：SA 起始 state 必須是 'pending'——`_recompute_route_state` 只在"
            " active/active_no_route 才寫 sa.state，探針要靠這個保持零副作用")
        return sa

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r1054%s-%s' % (tag, uuid.uuid4().hex[:6]),
        })

    def _policy(self, route, tag, emails):
        """一條 allow policy，帶兩條 include email_list rule（共 ``emails`` 個位址）。

        拆兩條是刻意的：``_check_emails_per_policy`` 是**跨 rule 加總**，只有一條
        rule 的話「o2m 少算」與「rule_value 少算」分不開。
        """
        self.assertGreaterEqual(emails, 2, 'fixture：至少要能拆成兩條 rule')
        first = ['a%d@example.com' % i for i in range(emails - 1)]
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'name': 'pol-1054-%s' % tag,
            'route_id': route.id,
            'decision': 'allow',
            'rule_ids': [
                (0, 0, {'group': 'include', 'rule_type': 'email_list',
                        'rule_value': ','.join(first)}),
                (0, 0, {'group': 'include', 'rule_type': 'email_list',
                        'rule_value': 'z@example.com'}),
            ],
        })
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
    #  換 env（冷快取）                                                     #
    # ------------------------------------------------------------------ #
    def _cold_view(self, record, user, warm_fnames):
        """回傳 ``record`` 在 ``user`` env 下的視圖，且要被數的 o2m 是**冷的**。

        步驟與理由見 module docstring「兩個踩過的坑」第二點。灌熱的只有守衛在
        ``self`` 上讀的純量／m2o 欄位（真實路徑上剛被寫過、本來就在 cache）；
        ⛔ 絕不碰要被數的 o2m。
        """
        self.env.flush_all()
        self.env.invalidate_all()
        record.read(warm_fnames)
        view = record.with_user(user)
        self.assertFalse(
            view.env.su,
            '前提：%s 的 env 不得是 su——su 之下 ir.rule 不套，本格會恆綠'
            '（docs/decisions/patterns/su-env-exempt-guard-false-green.md）'
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

    # ------------------------------------------------------------------ #
    #  探針（一律在 savepoint 內做、做完捲回並清 cache）                     #
    # ------------------------------------------------------------------ #
    def _probe(self, thunk):
        """跑 ``thunk``：回 True＝做得到、False＝被 ``AccessError`` 擋。

        ⚠ 只吞 ``AccessError``。``ValidationError``（例如撞到配額）會照樣往上拋
        ——探針只准回答「授權上做不做得到」，把別的失敗吞掉就會把
        「被配額擋住」誤報成「沒有權限」。所以呼叫端一律拿**沒有訂閱**的錨點來
        探（``_anchor_plan()`` 回空 ⇒ 配額守衛整條 continue）。
        """
        allowed = False
        try:
            with self.env.cr.savepoint():
                thunk()
                allowed = True
                raise _ProbeRollback()
        except _ProbeRollback:
            pass
        except AccessError:
            allowed = False
        finally:
            # savepoint 只捲回 DB；ORM cache 仍留著剛建出來的列（含 o2m 的
            # tuple），不清掉會污染後面的可見度讀數。
            self.env.invalidate_all()
        return allowed

    def _can_create_route(self, user, sa):
        Route = _skip_route_sync(self.Route.with_user(user))
        return self._probe(lambda: Route.create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'p1054-%s' % uuid.uuid4().hex[:8],
        }))

    def _can_write_rule_ids(self, user, pol):
        view = _skip_route_sync(self.Policy.with_user(user)).browse(pol.id)
        return self._probe(lambda: view.write({
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'probe@example.com'})]}))


class TestRouteQuotaPoolVisibility1054(_LenO2mQuota1054Fixture):
    """A 系列（家族一，model ``_check_route_quota``）。"""

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

    def test_A1_member_env_counts_the_whole_pool_and_still_blocks(self):
        """讀得到錨點的呼叫者（workspace 成員）計數必須正確、超額必須擋下來。

        這是結論 (b) 的主格：``ir.rule`` 讓成員看得到整池 ⇒ ``len(sa.route_ids)``
        ＝2 ＞ 上限 1 ⇒ ``ValidationError``。若本格紅在
        ``ValidationError not raised``，代表成員也會少算 ⇒ 結論翻成 (a)、缺口
        端到端成立。
        """
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        view = self._cold_view(
            routes[0], self.victim_member, ['security_access_id'])
        with self.assertRaises(ValidationError) as cm:
            view._check_route_quota()
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但訊息不是配額那條——確認綠的是本格要驗的分支')

    def test_A2_outsider_env_is_fail_closed_with_access_error(self):
        """結論 (c)：非成員走到守衛就 ``AccessError``，到不了「少算」那一步。

        形狀（run 2668 實測）：``len(sa.route_ids)`` →
        ``models.py:4124 fetch`` → ``check_access('read')`` 對**父記錄**
        ``woow_paas_platform.security_access`` 拒絕。⇒ 這條路對非成員是
        fail-closed，不是 fail-open。
        """
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        view = self._cold_view(
            routes[0], self.outsider, ['security_access_id'])
        with self.assertRaises(AccessError):
            view._check_route_quota()

    def test_A3_compliant_pool_still_passes_under_the_member_env(self):
        """反面對照：合規的池在**同一個** env 下必須放行。

        少了它，一個「什麼都 raise」的壞守衛也會讓 A1 變綠。
        """
        sa, routes = self._victim_sa_at_quota(quota=5, n_routes=2)
        view = self._cold_view(
            routes[0], self.victim_member, ['security_access_id'])
        view._check_route_quota()  # 不得 raise

    def test_A4_su_env_baseline_still_raises(self):
        """迴歸：su env（既有絕大多數路徑）行為不變——超額仍擋。"""
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        self.assertTrue(routes.env.su, '前提：fixture 的 env 應該是 su')
        with self.assertRaises(ValidationError):
            routes[0]._check_route_quota()

    def test_A5_positive_control_the_visibility_gap_exists_at_search_level(self):
        """陽性對照：#924 那組讀數在 search 層**確實存在**（非成員 0 vs sudo N）。

        它把 A2 的綠與「fixture 根本沒造出可見度落差」分開：落差是真的，只是
        守衛那條路先撞上父記錄的 read 檢查。⚠ 刻意用 ``search_count``——讀
        ``sa.route_ids`` 會把整池灌進 transaction cache，後面每一格都假綠。
        """
        sa, routes = self._victim_sa_at_quota(quota=1, n_routes=2)
        domain = [('security_access_id', '=', sa.id)]
        as_outsider = self.Route.with_user(self.outsider)
        as_member = self.Route.with_user(self.victim_member)
        self.assertFalse(as_outsider.env.su, '前提：對照組必須跑在非 su env')
        self.assertEqual(
            as_outsider.search_count(domain), 0,
            'ir.rule 應該讓非成員完全看不到這個池')
        self.assertEqual(
            as_member.search_count(domain), len(routes),
            '成員必須看得到整池——0-或-全的前提破了，(b) 的論證就不成立')
        self.assertEqual(
            self.Route.sudo().search_count(domain), len(routes),
            'sudo 的真值必須是 fixture 建的筆數')

    def test_A6_every_writer_of_a_route_sees_the_whole_route_pool(self):
        """🔴 核心判別式：**能建 route 的身分，必然看得到整池**。

        「少算」要成立，必須存在「寫得動、卻只看得到一部分」的身分。本格對
        ``_PROBE_IDENTITIES`` 逐一量 (能不能 create, 看得見幾條)，斷言
        ``能 create ⇒ 看見數 == sudo 真值``。

        * 綠 ⇒ 那種身分不存在 ⇒ 家族一**結構上不可達**（結論 (b)）。
        * 紅 ⇒ 找到了 ⇒ 結論翻成 (a)，缺口真的可利用。

        探針跑在**沒有訂閱**的 SA 上（``_anchor_plan()`` 回空 ⇒ 配額守衛整條
        continue），所以探針的失敗只可能是授權，不會是配額。
        並帶陽性對照：至少要有一個身分 create 得成，否則這條蘊含式恆真＝零鑑別力。
        """
        sa = self._sa(self.ws_victim, 'a6')
        for i in range(2):
            self._route(sa, 'a6%d' % i)
        self.env.flush_all()
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：探針用的 SA 不得有訂閱，否則探針會把配額誤報成沒有權限')
        domain = [('security_access_id', '=', sa.id)]
        total = self.Route.sudo().search_count(domain)
        self.assertEqual(total, 2, '前提：池裡要有 2 條 route')

        writers = []
        for attr, role in _PROBE_IDENTITIES:
            user = self._identity(attr)
            self.env.invalidate_all()
            can_create = self._can_create_route(user, sa)
            visible = self.Route.with_user(user).search_count(domain)
            if can_create:
                writers.append(attr)
                self.assertEqual(
                    visible, total,
                    '%s（role=%s）建得出 route 卻只看得到 %d/%d 條 ⇒ '
                    '「能寫但部分可見」的身分存在，len(o2m) 的少算端到端可達'
                    % (attr, role, visible, total))

        self.assertTrue(
            writers,
            '陽性對照破了：沒有任何身分建得出 route，這條蘊含式恆真＝零鑑別力'
            '（探針或 fixture 壞了，不是「缺口不存在」）')


class TestRouteQuotaEndToEndReach1054(_LenO2mQuota1054Fixture):
    """B 系列（家族一）：端到端——真的走 ORM 寫入，不直接呼叫守衛。"""

    def _victim_sa(self, quota=None, n_routes=1, tag='b'):
        """``quota=None`` ⇒ 不掛訂閱（配額守衛整條 continue）。"""
        sa = self._sa(self.ws_victim, tag)
        routes = self.Route.browse()
        for i in range(n_routes):
            routes |= self._route(sa, '%s%d' % (tag, i))
        self.env.flush_all()
        if quota is not None:
            self._subscribe(sa, self._plan('R1054%s' % tag,
                                           sa_max_routes=quota))
            self.env.flush_all()
        return sa, routes

    def test_B1_outsider_create_never_persists_a_route(self):
        """不變量：非成員對受害者 SA 建 route，永遠不得留下任何一列。

        兩種擋法都算通過（守衛的 ``ValidationError`` ／ ``ir.rule`` 或守衛自身
        read 檢查的 ``AccessError``）——本格只釘「不得成功」，所以階段 2 修法
        前後都必須綠。⚠ ``odoo.tests.common`` 的 ``assertRaises`` 不吃 tuple
        （run 2668 撞過 ``TypeError``），所以拆成 ``Exception`` ＋
        ``assertIsInstance``。
        """
        sa, _routes = self._victim_sa(quota=1, n_routes=2)
        before = self.Route.sudo().search_count(
            [('security_access_id', '=', sa.id)])
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises(Exception) as cm:
            self.Route.with_user(self.outsider).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'b1x-%s' % uuid.uuid4().hex[:6],
            })
        self.assertIsInstance(cm.exception, (ValidationError, AccessError))
        self.assertEqual(
            self.Route.sudo().search_count(
                [('security_access_id', '=', sa.id)]),
            before, '被擋的 create 不得留下任何 route')

    def test_B1b_ir_rule_create_leg_blocks_even_when_the_guard_is_a_no_op(self):
        """量測：把守衛整條關掉，``ir.rule`` 的 create 腿**自己**擋不擋得住。

        受害者 SA **沒有訂閱** ⇒ ``_check_route_quota`` 在讀 ``route_ids``
        之前就 ``continue``（``sub`` 為空）⇒ 這一格的 ``AccessError`` 只可能
        來自 ``records.check_access('create')``。

        它與 A2 合起來說明 create 這條路有**兩層**互相獨立的擋法（守衛自身的
        read 檢查、以及 create 腿），所以 create 方向不是「靠守衛」才安全。
        ⛔ 刻意不靠比對錯誤訊息字串來分辨是誰擋的（訊息會隨語系／版本漂）。
        """
        sa, _routes = self._victim_sa(quota=None, n_routes=2, tag='b1b')
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：本格要的是「守衛 no-op」，SA 不得有訂閱')
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.Route.with_user(self.outsider).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'b1y-%s' % uuid.uuid4().hex[:6],
            })

    def test_B2_move_into_a_subscribed_victim_sa_is_blocked(self):
        """把自己的 route 改錨到受害者**有訂閱**的 SA 上——必須被擋。

        write 路徑上 ``check_access('write')`` 只驗舊值（那時 route 還在攻擊者
        自己的 SA 底下 ⇒ 通過），寫入後不再重驗 record rule，route 側也沒有
        ``_assert_anchor_move_authorised`` ⇒ 唯一還會說話的就是
        ``_check_route_quota``。預期它以 ``AccessError``（讀不到受害者 SA）
        擋下來；``ValidationError``（配額）也算擋住。
        """
        sa_victim, _v = self._victim_sa(quota=1, n_routes=1, tag='b2')
        sa_attacker = self._sa(self.ws_attacker, 'b2atk')
        mine = self._route(sa_attacker, 'b2mine')
        view = self._cold_view(mine, self.attacker, ['security_access_id'])
        with self.assertRaises(Exception) as cm:
            view.write({'security_access_id': sa_victim.id})
        self.assertIsInstance(cm.exception, (ValidationError, AccessError))
        self.assertEqual(
            self.Route.sudo().browse(mine.id).security_access_id,
            sa_attacker.sudo(),
            '被擋的搬移不得留下任何錨點變更')

    def test_B2c_move_into_an_unsubscribed_victim_sa_is_blocked(self):
        """🔴 **本趟唯一預期紅的一格**——而且它量到的不是配額，是 anchor move。

        同一次搬移，只把受害者 SA 換成**沒有訂閱**的：
        ``_check_route_quota`` 讀 ``sub`` 為空 ⇒ **在碰到 ``route_ids`` 之前就
        ``continue``** ⇒ B2 那道 ``AccessError`` 不會發生，而 write 路徑上又沒有
        任何其他授權層（理由見 B2 docstring）⇒ 攻擊者的 route 直接落進受害者
        的 SA。

        預期紅的形狀：``AssertionError: Exception not raised``。

        ⚠ **這不是 #1054 的配額缺口**——沒有訂閱的 SA 本來就不受配額管
        （grandfathered）。它是 route 側缺少 ``_assert_anchor_move_authorised``
        （policy 有 #848、rule 有 #941）的跨租戶 anchor-move 缺口，範圍在
        #1054 之外，建議另開單。放在這裡是因為量測本身順手撞到，而且它同時
        說明了「B2 的綠是靠守衛的 read 檢查，不是靠授權層」——守衛一旦提前
        ``continue``，那條路就完全沒有守線。
        """
        sa_victim, _v = self._victim_sa(quota=None, n_routes=1, tag='b2c')
        self.assertFalse(
            sa_victim.sudo().subscription_id,
            '前提：受害者 SA 不得有訂閱，否則量到的是 B2 那條路')
        sa_attacker = self._sa(self.ws_attacker, 'b2catk')
        mine = self._route(sa_attacker, 'b2cmine')
        view = self._cold_view(mine, self.attacker, ['security_access_id'])
        with self.assertRaises(Exception) as cm:
            view.write({'security_access_id': sa_victim.id})
        self.assertIsInstance(cm.exception, (ValidationError, AccessError))

    def test_B3_the_same_move_is_caught_under_su(self):
        """判別對照：**同一次搬移**在 su env 下被配額擋住。

        它把 B2 的綠歸因鎖死：su 下守衛數得到真值（受害者 1 條＋搬進來 1 條
        ＞ 上限 1）⇒ ``ValidationError``。若本格紅，代表 fixture 沒把受害者 SA
        填到額滿，B2 ／ B2c 的解讀都要作廢。
        """
        sa_victim, _v = self._victim_sa(quota=1, n_routes=1, tag='b3')
        sa_attacker = self._sa(self.ws_attacker, 'b3atk')
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

    def _victim_policy(self, quota=None, emails=3):
        """``quota=None`` ⇒ 不掛訂閱（守衛整條 continue，供探針用）。"""
        sa = self._sa(self.ws_victim, 'c')
        route = self._route(sa, 'c0')
        pol = self._policy(route, 'c0', emails=emails)
        # 先建 policy＋rules → 再掛訂閱（否則 fixture 自己就撞配額）。
        self.env.flush_all()
        if quota is not None:
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

    _WARM = ['security_access_id', 'cloud_service_id', 'smart_home_id', 'name']

    def test_C1_member_env_counts_the_whole_rule_set_and_still_blocks(self):
        """讀得到 policy 的成員，email 加總必須正確、超額必須擋下來（結論 (b)）。"""
        sa, route, pol = self._victim_policy(quota=2, emails=3)
        view = self._cold_view(pol, self.victim_member, self._WARM)
        with self.assertRaises(ValidationError) as cm:
            view._check_emails_per_policy()
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但訊息不是配額那條——確認綠的是本格要驗的分支')

    def test_C2_outsider_env_is_fail_closed_with_access_error(self):
        """結論 (c)：非成員讀 ``pol.rule_ids`` 時對**父記錄** policy 的
        ``check_access('read')`` 就被拒（run 2668 實測形狀）。"""
        sa, route, pol = self._victim_policy(quota=2, emails=3)
        view = self._cold_view(pol, self.outsider, self._WARM)
        with self.assertRaises(AccessError):
            view._check_emails_per_policy()

    def test_C3_su_env_baseline_still_raises(self):
        """迴歸：su env 下超額仍擋（3 個位址 > 上限 2）。"""
        sa, route, pol = self._victim_policy(quota=2, emails=3)
        self.assertTrue(pol.env.su, '前提：fixture 的 env 應該是 su')
        with self.assertRaises(ValidationError):
            pol._check_emails_per_policy()

    def test_C4_positive_control_the_visibility_gap_exists_at_search_level(self):
        """陽性對照：非成員對 rule 池的裸讀數 0、成員與 sudo 皆為 2。"""
        sa, route, pol = self._victim_policy(quota=2, emails=3)
        domain = [('policy_id', '=', pol.id)]
        as_outsider = self.PolicyRule.with_user(self.outsider)
        as_member = self.PolicyRule.with_user(self.victim_member)
        self.assertFalse(as_outsider.env.su, '前提：對照組必須跑在非 su env')
        self.assertEqual(as_outsider.search_count(domain), 0)
        self.assertEqual(as_member.search_count(domain), 2)
        self.assertEqual(self.PolicyRule.sudo().search_count(domain), 2)

    def test_C5_every_writer_of_rule_ids_sees_the_whole_rule_set(self):
        """🔴 家族三的核心判別式：**能寫 ``policy.rule_ids`` 的身分必然看得到整組**。

        ``_check_emails_per_policy`` 的 trigger 只有 ``rule_ids``——只有「寫
        policy 的 rule_ids」會觸發它（直接建／改子列不會回頭跑 parent 的
        constraint）。所以「能寫但部分可見」的身分若不存在，家族三就是結構上
        不可達。探針同樣跑在**沒有訂閱**的錨點上。
        """
        sa, route, pol = self._victim_policy(quota=None, emails=3)
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：探針用的錨點不得有訂閱，否則探針會把配額誤報成沒有權限')
        domain = [('policy_id', '=', pol.id)]
        total = self.PolicyRule.sudo().search_count(domain)
        self.assertEqual(total, 2, '前提：這條 policy 要有 2 條 rule')

        writers = []
        for attr, role in _PROBE_IDENTITIES:
            user = self._identity(attr)
            self.env.invalidate_all()
            can_write = self._can_write_rule_ids(user, pol)
            visible = self.PolicyRule.with_user(user).search_count(domain)
            if can_write:
                writers.append(attr)
                self.assertEqual(
                    visible, total,
                    '%s（role=%s）寫得動 rule_ids 卻只看得到 %d/%d 條 ⇒ '
                    '「能寫但部分可見」的身分存在，家族三的少算端到端可達'
                    % (attr, role, visible, total))

        self.assertTrue(
            writers,
            '陽性對照破了：沒有任何身分寫得動 rule_ids，蘊含式恆真＝零鑑別力')


class TestQuotaPoolVisibilityIsAllOrNothing1054(_LenO2mQuota1054Fixture):
    """D 系列：池的可見度是「0 或全」——(b) 與「BFF 前置不會少算」共用的前提。

    兩個 sub-model 的 ``ir.rule`` 判歸屬走的都是**錨點 workspace 的成員關係**，
    與 o2m 的 domain 同一根 ⇒ 看得到一列就看得到整池。而 BFF
    （``controllers/security_access.py::_create_route``）的呼叫者一律先過
    ``_get_workspace_or_error()``（要有該 workspace 的 ``workspace_access``
    列），所以合法呼叫者必然是成員 ⇒ 前置計數對他不會少算。
    """

    def test_D1_route_pool_is_all_or_nothing(self):
        sa = self._sa(self.ws_victim, 'd1')
        for i in range(3):
            self._route(sa, 'd1%d' % i)
        self.env.flush_all()
        domain = [('security_access_id', '=', sa.id)]
        for attr, role in _PROBE_IDENTITIES:
            user = self._identity(attr)
            expected = 0 if role is None else 3
            view = self.Route.with_user(user)
            self.assertFalse(view.env.su, '前提：必須跑在非 su env')
            self.assertEqual(
                view.search_count(domain), expected,
                '%s（role=%s）應該看到 %d 條——0-或-全的前提破了，(b) 與 BFF'
                '前置的論證都不成立' % (attr, role, expected))

    def test_D2_policy_rule_pool_is_all_or_nothing(self):
        sa = self._sa(self.ws_victim, 'd2')
        route = self._route(sa, 'd2')
        pol = self._policy(route, 'd2', emails=3)
        self.env.flush_all()
        domain = [('policy_id', '=', pol.id)]
        for attr, role in _PROBE_IDENTITIES:
            user = self._identity(attr)
            expected = 0 if role is None else 2
            view = self.PolicyRule.with_user(user)
            self.assertFalse(view.env.su, '前提：必須跑在非 su env')
            self.assertEqual(
                view.search_count(domain), expected,
                '%s（role=%s）應該看到 %d 條 rule' % (attr, role, expected))
