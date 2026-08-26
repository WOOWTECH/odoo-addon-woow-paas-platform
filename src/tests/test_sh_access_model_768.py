"""#768（SH AC）：SH route 子模型 ＋ policy 錨點從 exclusive arc 擴成「恰好一個」。

本檔守 model 層四件事，每一件漏掉的症狀都**沒有輸出**：

1. **arc 從二元變三元**。現行 CHECK 是 `(a IS NULL) <> (b IS NULL)`；把 `<>` 串成
   三元是**奇偶性**不是「恰好一個」——恰好一個非空會算出 FALSE（合法列被拒）、
   三個都空算出 TRUE（無錨點孤兒被放行），正好把這道 CHECK 存在的理由反過來。
   所以這裡不只測行為，還直接查 `pg_get_constraintdef` 斷言它是
   `num_nonnulls(...) = 1`：只測 Python 那層的話，把 CHECK 換成任何形狀都不會有
   測試變紅，而 CHECK 才是擋得住裸 SQL／批次 migration 的那層。

2. **quota 池以錨點為 scope，三個池互不相通**。`_check_policy_quota` 原本是
   `'cloud_service_id' if pol.cloud_service_id else 'security_access_id'` 的二元
   三元式；SH 錨定會落 `else` 分支 ⇒ 拿 SH 的 id 去比 `security_access_id`，**跨
   model 的 id 碰撞**後得到一個錯誤計數（可多可少）。「數錯」與「數對」在單一
   錨點的測試裡輸出相同，所以必須有「A 用掉額度不會吃到 B」那一格。

3. **route 子模型自己的不變式**：一台 SH 恰好一條 primary route、primary 的
   hostname 衍生自 `smart_home.subdomain`（不是複製欄位，複製就是第二個真相
   來源而兩者不一致時零訊號）、primary 不可刪也不可改綁定。

4. **錨點分派的單一裁決處**：`_anchor()` / `_access_sync_targets()` /
   sparse precedence / `to_dict()` 四處只要漏一處，症狀分別是「配額靜默失效」
   「Cloudflare 完全沒被推、與推成功不可區分」「precedence 全撞在 10」「前端把
   SH 的 policy 讀成掛在 route 0」。

⚠ 陽性對照紀律：每一條「被擋下來」旁邊都有一條「合法路徑會成功」。沒有它，
`ValidationError` 可能來自 fixture 壞掉、欄位不存在、或一個「什麼都擋」的壞
guard——那三種情況下所有負向斷言都會為了錯的理由變綠。
"""
from psycopg2 import IntegrityError

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from ..models.cloud_service import _internal_provision
from .smart_home_fixture import sh_model

_POLICY_TABLE = 'woow_paas_platform_security_access_access_policy'
_ARC_CONSTRAINT = '%s_anchor_arc' % _POLICY_TABLE
_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'


class _ShAccessFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.Rule = self.env[
            'woow_paas_platform.security_access_access_policy_rule']
        self.SA = self.env['woow_paas_platform.security_access']
        self.SaRoute = self.env['woow_paas_platform.security_access_route']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.ShRoute = self.env[_ROUTE_MODEL]
        self.SmartHome = sh_model(self.env)
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SH AC WS 768'})
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #
    def _smart_home(self, name='sh-768', subdomain='paas-sm-768-aaaa', **extra):
        vals = {'name': name, 'workspace_id': self.workspace.id}
        vals.update(extra)
        # `.with_env(self.env)` 洗掉開閘 context：`with_context` 是 merge，
        # `.sudo()` / `.browse()` 都會保留它，不洗掉的話 fixture 自己把受測守衛
        # 關掉了（`test_smart_home_authz_524.py:153-168` 的同一條教訓）。
        sh = self.SmartHome.create(vals).with_env(self.env)
        if subdomain is not None:
            sh.sudo().write({'subdomain': subdomain})
        return sh

    def _sh_route(self, sh=None, **extra):
        sh = sh or self._smart_home()
        vals = {'smart_home_id': sh.id, 'is_primary': True}
        vals.update(extra)
        return self.ShRoute.create(vals)

    def _sa_route(self, prefix='sh768-route'):
        sa = self.SA.create(
            {'name': 'SH AC SA 768', 'workspace_id': self.workspace.id})
        route = self.SaRoute.create(
            {'security_access_id': sa.id, 'subdomain_prefix': prefix})
        return sa, route

    def _service(self, name='svc-768', subdomain='cs-768'):
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'SH AC tmpl 768',
            'slug': 'sh-ac-768',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/sh-ac',
            # ⚠ 必須釘版：未釘版的 template 會被 create() 的供給閘擋下，fixture
            # 會死在抵達受測步驟之前，而那個 error 長得跟「守衛擋住了」一樣。
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        return _internal_provision(self.Service).create({
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'subdomain': subdomain,
        })

    def _plan(self, name, **quota):
        vals = {
            'name': name, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            # #521：席位 enforcement 上線後 `ac_max_access_seats` 預設 0 ＝「一席
            # 都沒有」⇒ 任何帶 email_list 的 fixture 都會被
            # `_check_access_seat_quota` 擋住。本檔測的是 policy 數／每 policy
            # email 數那兩條 quota、不是席位，所以這裡給一個明顯夠用的席位額度，
            # 讓紅燈的原因只可能是被測的那一條。呼叫端仍可用 quota kwargs 覆寫。
            # ⚠ 這是 fixture 的 forward coupling，**不是**產品側豁免——「既有 0
            # 席方案上的資料怎麼辦」＝ #521 決策點 4，已拍板「一律擋（含修改舊
            # 資料）」（留言 9960）；產品側零豁免就是該裁決。
            'ac_max_access_seats': 99,
        }
        vals.update(quota)
        return self.env['sh.subscription.plan'].create(vals)

    def _subscribe(self, record, plan):
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        record.sudo().subscription_id = sub.id
        return sub

    def _policy_vals(self, **extra):
        vals = {
            'name': 'p768', 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(extra)
        return vals


class TestPolicyAnchorTernaryArc(_ShAccessFixture):
    """A 系列：錨點從 XOR-of-2 變成「恰好一個」（三腿）。"""

    def test_A1_sh_route_anchor_is_accepted(self):
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        self.assertTrue(pol.id, 'SH route 錨定的 policy 必須建得起來')
        self.assertEqual(pol.smart_home_route_id, route)
        self.assertFalse(
            pol.route_id, 'SA route 那條腿在 SH 錨定時必須是空的')
        self.assertFalse(
            pol.cloud_service_id, 'CS 那條腿在 SH 錨定時必須是空的')
        self.assertFalse(
            pol.security_access_id,
            'security_access_id 是 route_id 的 stored related，SH 錨定時恆空')

    def test_A2_existing_two_anchors_still_work(self):
        """陽性對照：擴成三腿不得讓既有兩腿失效。"""
        _sa, sa_route = self._sa_route()
        pol_route = self.Policy.create(
            self._policy_vals(route_id=sa_route.id))
        self.assertTrue(pol_route.id, 'SA route 錨定回歸')
        pol_cs = self.Policy.create(
            self._policy_vals(cloud_service_id=self._service().id))
        self.assertTrue(pol_cs.id, 'CS 錨定回歸')

    def test_A3_no_anchor_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.Policy.create(self._policy_vals())

    def test_A4_any_two_anchors_are_rejected(self):
        route = self._sh_route()
        _sa, sa_route = self._sa_route()
        svc = self._service()
        pairs = (
            {'smart_home_route_id': route.id, 'route_id': sa_route.id},
            {'smart_home_route_id': route.id, 'cloud_service_id': svc.id},
            {'route_id': sa_route.id, 'cloud_service_id': svc.id},
        )
        for pair in pairs:
            with self.assertRaises(ValidationError,
                                   msg='雙錨點 %r 必須被拒' % sorted(pair)):
                self.Policy.create(self._policy_vals(**pair))

    def test_A5_all_three_anchors_are_rejected(self):
        route = self._sh_route()
        _sa, sa_route = self._sa_route()
        with self.assertRaises(ValidationError):
            self.Policy.create(self._policy_vals(
                smart_home_route_id=route.id,
                route_id=sa_route.id,
                cloud_service_id=self._service().id))

    def test_A6_arc_is_a_real_sql_check_over_three_columns(self):
        """CHECK 必須是「恰好一個」而不是 `<>` 鏈。

        `_add_sql_constraints` 加不上時 Odoo 只留一行 `_schema.warning` 就繼續
        跑 ⇒ 升級「成功」、Python 那層照常擋、所有行為測試全綠。查
        `pg_get_constraintdef` 是唯一分得出這兩種世界的判準；而斷言它含
        `num_nonnulls` 是唯一分得出「三腿」與「奇偶性」的判準。
        """
        self.env.cr.execute(
            """
            SELECT pg_get_constraintdef(c.oid)
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = %s AND c.conname = %s AND c.contype = 'c'
            """,
            (_POLICY_TABLE, _ARC_CONSTRAINT),
        )
        row = self.env.cr.fetchone()
        self.assertTrue(
            row,
            'SQL CHECK `%s` 不存在 ⇒ arc 只剩 Python 那半層' % _ARC_CONSTRAINT)
        definition = row[0]
        self.assertIn(
            'num_nonnulls', definition,
            '三腿 arc 必須用 num_nonnulls(...) = 1；`<>` 串三元是奇偶性，'
            '會接受三個都空、拒絕恰好一個（定義：%r）' % definition)
        for column in ('route_id', 'cloud_service_id', 'smart_home_route_id'):
            self.assertIn(column, definition,
                          'CHECK 少了 %s 這一腿（定義：%r）' % (column, definition))

    def test_A7_sql_check_rejects_a_raw_three_anchor_row(self):
        """繞過 ORM 的裸 SQL 也要被擋——那正是 DB 那層存在的理由。"""
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        self.env.flush_all()
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    'UPDATE %s SET smart_home_route_id = NULL WHERE id = %%s'
                    % _POLICY_TABLE, (pol.id,))

    def test_A8_rule_submodel_mirrors_the_third_anchor_as_stored(self):
        """子模型必須物化 `smart_home_id`：ir.rule 的 domain 只走得到真欄位。

        用 `search` 而不是讀屬性——related 沒有 `store=True` 時屬性照樣讀得到
        值，只有 search 分得出「有沒有真的物化成欄位」。
        """
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        self.env.flush_all()
        found = self.Rule.search(
            [('smart_home_id', '=', route.smart_home_id.id)])
        self.assertTrue(
            found, '子模型的 smart_home_id 沒有物化成可 search 的欄位')
        self.assertEqual(found.policy_id, pol)

    def test_A9_to_dict_exposes_the_third_anchor(self):
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        data = pol.to_dict()
        self.assertEqual(
            data.get('smart_home_route_id'), route.id,
            'to_dict 少了第三個錨點 ⇒ 前端會把 SH 的 policy 讀成掛在 route 0')
        self.assertFalse(data.get('route_id'))
        self.assertFalse(data.get('cloud_service_id'))

    def test_A10_sparse_precedence_is_scoped_to_the_sh_anchor(self):
        """新 policy 落 max(sequence)+10，且 scope 是**自己那個錨點**。

        漏掉第三腿的症狀是每一條都落在預設 10 ⇒ 在 Cloudflare 上撞成同一個
        precedence，**沒有例外也沒有 log**。
        """
        route = self._sh_route()
        other = self._sh_route(sh=self._smart_home(
            name='sh-768-b', subdomain='paas-sm-768-bbbb'))
        first = self.Policy.create(
            self._policy_vals(name='p1', smart_home_route_id=route.id))
        second = self.Policy.create(
            self._policy_vals(name='p2', smart_home_route_id=route.id))
        self.assertEqual(first.sequence, 10)
        self.assertEqual(
            second.sequence, 20,
            'sparse precedence 沒有以 SH route 為 scope（落在 %s）'
            % second.sequence)
        # 另一個錨點自己重新起算，不受上面兩條影響
        elsewhere = self.Policy.create(
            self._policy_vals(name='p3', smart_home_route_id=other.id))
        self.assertEqual(
            elsewhere.sequence, 10,
            '別的錨點的 policy 不該吃到這個錨點的 sequence 水位')


class TestPolicyQuotaPoolsAreDisjoint(_ShAccessFixture):
    """B 系列：三個錨點的配額池互不相通。"""

    def _quota_plan(self, name):
        return self._plan(name, sa_max_access_policies=1)

    def test_B1_sh_anchor_quota_follows_its_own_subscription(self):
        sh = self._smart_home()
        route = self._sh_route(sh=sh)
        self._subscribe(sh, self._quota_plan('sh-quota-768'))
        self.Policy.create(
            self._policy_vals(name='q1', smart_home_route_id=route.id))
        with self.assertRaises(ValidationError,
                               msg='SH 錨點的第二條 policy 應該超額被擋'):
            self.Policy.create(
                self._policy_vals(name='q2', smart_home_route_id=route.id))

    def test_B2_pools_are_disjoint_across_the_three_anchors(self):
        """這一格才分得出「計數範圍正確」與「數了全表」。"""
        sh = self._smart_home()
        sh_route = self._sh_route(sh=sh)
        self._subscribe(sh, self._quota_plan('sh-pool-768'))

        sa, sa_route = self._sa_route(prefix='sh768-pool')
        self._subscribe(sa, self._quota_plan('sa-pool-768'))

        svc = self._service(name='svc-pool-768', subdomain='cs-pool-768')
        self._subscribe(svc, self._quota_plan('cs-pool-768'))

        # 每個錨點各用掉自己那一格額度
        self.Policy.create(
            self._policy_vals(name='sh1', smart_home_route_id=sh_route.id))
        self.Policy.create(
            self._policy_vals(name='sa1', route_id=sa_route.id))
        self.Policy.create(
            self._policy_vals(name='cs1', cloud_service_id=svc.id))

        # 三邊都該剛好用完、且互不吃到對方
        for label, anchor_vals in (
                ('SH', {'smart_home_route_id': sh_route.id}),
                ('SA', {'route_id': sa_route.id}),
                ('CS', {'cloud_service_id': svc.id})):
            with self.assertRaises(
                    ValidationError,
                    msg='%s 錨點的第二條 policy 應該超額被擋' % label):
                self.Policy.create(
                    self._policy_vals(name='%s2' % label, **anchor_vals))

    def test_B3_zero_quota_still_means_unlimited_on_the_sh_anchor(self):
        """#743 的 `0 = unlimited` 慣例對第三個錨點也要成立。

        SH 的 40 筆 AC 變體 `sa_max_access_policies` 全走 default 0；這條若沒有
        跟著擴，租戶連第一條 policy 都建不出來。
        """
        sh = self._smart_home()
        route = self._sh_route(sh=sh)
        self._subscribe(sh, self._plan('sh-zero-768', sa_max_access_policies=0))
        for i in range(3):
            self.Policy.create(self._policy_vals(
                name='z%d' % i, smart_home_route_id=route.id))
        self.assertEqual(
            self.Policy.search_count(
                [('smart_home_route_id', '=', route.id)]), 3)

    def test_B4_email_quota_follows_the_sh_anchor_plan(self):
        sh = self._smart_home()
        route = self._sh_route(sh=sh)
        self._subscribe(
            sh, self._plan('sh-mail-768', sa_max_emails_per_policy=1))
        # 陽性對照：一個 email 通得過
        self.Policy.create(self._policy_vals(
            name='m1', smart_home_route_id=route.id))
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'name': 'm2',
                'smart_home_route_id': route.id,
                'decision': 'allow',
                'rule_ids': [(0, 0, {
                    'group': 'include', 'rule_type': 'email_list',
                    'rule_value': 'a@example.com,b@example.com'})],
            })

    def test_B5_bulk_grant_guard_still_applies_on_the_sh_anchor(self):
        """#352 的整批放行守衛換錨點後必須照擋。"""
        route = self._sh_route()
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'name': 'bulk', 'smart_home_route_id': route.id,
                'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'everyone'})],
            })


class TestSmartHomeRouteModel(_ShAccessFixture):
    """C 系列：route 子模型自己的不變式。"""

    def test_C1_primary_hostname_is_derived_not_copied(self):
        sh = self._smart_home(subdomain='paas-sm-768-cccc')
        route = self._sh_route(sh=sh)
        self.assertEqual(route.hostname, 'paas-sm-768-cccc.woowtech.io')
        # 衍生而非複製：改 SH 的 subdomain，route 的 hostname 必須跟著動。
        # 複製欄位在兩者不一致時**沒有任何訊號**。
        sh.sudo().write({'subdomain': 'paas-sm-768-dddd'})
        route.invalidate_recordset(['hostname'])
        self.assertEqual(route.hostname, 'paas-sm-768-dddd.woowtech.io')

    def test_C2_hostname_is_empty_before_provisioning(self):
        """未 provision（無 subdomain）時沒有可掛 Access 的錨點。"""
        sh = self._smart_home(subdomain=None)
        route = self._sh_route(sh=sh)
        self.assertFalse(route.hostname)

    def test_C3_at_most_one_primary_route_per_smart_home(self):
        """ORM 路徑：Python constraint 給出講得出「錯在哪」的訊息。"""
        sh = self._smart_home()
        self._sh_route(sh=sh)
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self._sh_route(sh=sh)
                self.env.flush_all()

    def test_C3b_single_primary_is_backed_by_a_real_partial_unique_index(self):
        """真正的守線在 DB：Python constraint 擋不住併發也擋不住裸 SQL。

        只測 ORM 那層的話，把 index 拿掉不會有任何測試變紅。
        """
        self.env.cr.execute(
            """
            SELECT indexdef FROM pg_indexes
             WHERE tablename = 'woow_paas_platform_smart_home_route'
               AND indexname = %s
            """,
            ('woow_paas_platform_smart_home_route_one_primary',),
        )
        row = self.env.cr.fetchone()
        self.assertTrue(
            row, 'primary route 的 partial unique index 不存在 ⇒ 唯一性只剩 '
                 'Python 那半層（擋不住併發與裸 SQL）')
        self.assertIn('UNIQUE', row[0].upper())
        self.assertIn('is_primary', row[0])

    def test_C3c_raw_sql_cannot_create_a_second_primary(self):
        """繞過 ORM 的裸 INSERT 也要被擋——那正是 DB 那層存在的理由。"""
        sh = self._smart_home()
        self._sh_route(sh=sh)
        self.env.flush_all()
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute(
                    """
                    INSERT INTO woow_paas_platform_smart_home_route
                        (smart_home_id, is_primary, is_protected,
                         session_duration, login_google, login_email_otp,
                         create_uid, create_date, write_uid, write_date)
                    VALUES (%s, TRUE, FALSE, '24h', FALSE, FALSE,
                            1, now(), 1, now())
                    """, (sh.id,))

    def test_C4_primary_route_cannot_be_unlinked(self):
        route = self._sh_route()
        with self.assertRaises(UserError):
            route.unlink()
        self.assertTrue(route.exists(), 'primary route 必須被保住')

    def test_C5_primary_route_cannot_be_rebound_to_another_smart_home(self):
        route = self._sh_route()
        other = self._smart_home(name='sh-768-e', subdomain='paas-sm-768-eeee')
        with self.assertRaises(UserError):
            route.write({'smart_home_id': other.id})

    def test_C6_route_to_dict_shape_matches_the_shared_card_contract(self):
        """`AccessAppCard` 的 config 形狀＝SA route.to_dict 與 CS
        `_access_to_dict()` 的交集，三個 surface 逐字相同。"""
        route = self._sh_route()
        data = route.to_dict()
        for key in ('id', 'hostname', 'is_protected', 'session_duration',
                    'login_google', 'login_email_otp',
                    'access_unresolved_idp_types', 'access_policies'):
            self.assertIn(key, data, 'route.to_dict 少了 %s' % key)
        self.assertNotIn(
            'access_app_id', data,
            'access_app_id 是伺服器擁有的 CF 物件 id，不得出現在租戶 payload')

    def test_C7_deleting_the_smart_home_removes_its_routes(self):
        sh = self._smart_home()
        route = self._sh_route(sh=sh)
        route_id = route.id
        sh.sudo().unlink()
        self.assertFalse(
            self.ShRoute.browse(route_id).exists(),
            '刪掉 SH 之後 route 不得殘留（policy 會跟著變成孤兒）')


class TestAnchorDispatchSingleSource(_ShAccessFixture):
    """D 系列：錨點分派的單一裁決處。"""

    def test_D1_anchor_returns_the_smart_home_for_sh_routed_policies(self):
        sh = self._smart_home()
        route = self._sh_route(sh=sh)
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        self.assertEqual(
            pol._anchor(), sh,
            '_anchor() 回空 ⇒ _anchor_plan() 回空 ⇒ 配額與 email 上限全部 '
            'continue，而那與「方案不限」輸出完全相同')

    def test_D2_sync_targets_include_the_sh_route(self):
        route = self._sh_route()
        pol = self.Policy.create(
            self._policy_vals(smart_home_route_id=route.id))
        targets = pol._access_sync_targets()
        self.assertIn(
            route, targets,
            '_access_sync_targets 漏掉 SH route ⇒ Cloudflare 完全不會被推，'
            '而這與「推了而且成功」在輸出上不可區分')

    def test_D3_quota_pool_field_is_declared_per_anchor_model(self):
        """配額池欄位改用**查表**而非三元式。

        三元式的錯誤是結構性的：第四個錨點還會再錯一次，而錯法是拿 A model 的
        id 去比 B model 的欄位（跨 model id 碰撞），計數可多可少。
        """
        mapping = self.Policy._ANCHOR_POOL_FIELD
        self.assertEqual(
            mapping.get('woow_paas_platform.smart_home'), 'smart_home_id')
        self.assertEqual(
            mapping.get('woow_paas_platform.cloud_service'),
            'cloud_service_id')
        self.assertEqual(
            mapping.get('woow_paas_platform.security_access'),
            'security_access_id')
        for model_name, field_name in mapping.items():
            self.assertIn(
                field_name, self.Policy._fields,
                '%s 對映到不存在的欄位 %s（打錯字會讓整組查表靜默失效）'
                % (model_name, field_name))
