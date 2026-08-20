"""#744（CS AC T2）：policy 錨點 exclusive arc、CS access 欄位、身分錨點、teardown。

本檔守的是 **model 層**的四件事，每一件的失敗都是靜默的：

1. **exclusive arc**：`route_id` XOR `cloud_service_id`。兩者皆空的列走不到任何
   workspace（ir.rule 兩腿皆假）⇒ 對租戶不可見、也永遠同步不到 Cloudflare，
   但它會安安靜靜地躺在表裡。arc 有兩層（SQL CHECK ＋ `@api.constrains`），
   **兩層各自都有測試**：只測 Python 那層的話，把 CHECK 拿掉不會有任何測試變紅，
   而 CHECK 才是擋得住裸 SQL／批次 migration 的那層。

2. **quota 跟著錨點走**：改之前 `_check_policy_quota` / `_check_emails_per_policy`
   直讀 `security_access_id`，CS 錨定時那個欄位恆空 ⇒ 整條 `continue`。症狀是
   「上限沒有生效」，而它與「方案本來就不限」**輸出完全相同**，沒有任何訊號分得
   出來。所以這裡不只測「超額會擋」，也測「兩個錨點的額度池互不相通」——後者才
   分得出「計數範圍對」與「計數範圍是全表」。

3. **身分錨點**（`access_app_id`）：對所有人（含 superuser）不可寫，只有模組私有
   sentinel 開得了門。測試刻意跑在 `TransactionCase`（＝su env）：守衛若寫成
   `not self.env.su` 這一整組會靜默恆綠（
   docs/decisions/patterns/su-env-exempt-guard-false-green.md）。

4. **teardown hook**：清不掉要 fail-loud 且**保留** `access_app_id`——那個 id 是
   唯一的重試錨點，清掉就變成永久 CF 孤兒且沒有任何路徑找得回來。

⚠ 陽性對照的紀律：每一條「被擋下來」的斷言旁邊都有一條「同一個操作在合法路徑下
會成功」。沒有它，`ValidationError` 可能來自 fixture 壞掉、欄位不存在、或一個
「什麼都擋」的壞 guard——那三種情況下所有負向斷言都會為了錯的理由變綠。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

# #536：`sa_skip_route_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.security_access import _skip_route_sync

from ..models.cloud_service import (
    _CS_ACCESS_IDENTITY_ANCHOR_FIELDS,
    _CS_ACCESS_IDENTITY_CTX_KEY,
    _INTERNAL_CS_ACCESS_IDENTITY_WRITE,
    _internal_cs_access_identity,
    _internal_provision,
)

_CS_MODEL = 'woow_paas_platform.cloud_service'
_POLICY_TABLE = 'woow_paas_platform_security_access_access_policy'
_ARC_CONSTRAINT = '%s_anchor_arc' % _POLICY_TABLE

#: 「JSON 表達得出來」的每一種偽造值。sentinel 的全部價值就是這幾個都打不開；
#: 少了這條迴圈，把守衛改回裸真值（`if ctx.get(KEY):`）不會有任何測試變紅。
_FORGED_CTX_VALUES = (True, 1, 'true', 'True', [1], {'x': 1}, 'internal')


class _CsAccessFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.Rule = self.env[
            'woow_paas_platform.security_access_access_policy_rule']
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Service = self.env[_CS_MODEL]
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'CS AC WS 744'})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].create({
                'name': 'CS AC tmpl 744',
                'slug': 'cs-ac-744',
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cs-ac',
                # ⚠ 必須釘版：未釘版的 template 會被 create() 的供給閘擋下，
                # fixture 會死在抵達受測步驟之前，而那個 error 長得跟「守衛擋住
                # 了」一模一樣。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #
    def _service(self, name='svc-744', subdomain='cs-ac-744', **extra):
        vals = {
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'subdomain': subdomain,
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _sa_route(self, prefix='ac744-route'):
        sa = self.SA.create(
            {'name': 'CS AC SA', 'workspace_id': self.workspace.id})
        route = self.Route.create(
            {'security_access_id': sa.id, 'subdomain_prefix': prefix})
        return sa, route

    def _plan(self, name, **quota):
        vals = {
            'name': name, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
        }
        vals.update(quota)
        return self.env['sh.subscription.plan'].create(vals)

    def _subscribe(self, record, plan):
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        record.subscription_id = sub.id
        return sub

    def _policy_vals(self, **extra):
        vals = {
            'name': 'p744', 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(extra)
        return vals


class TestPolicyAnchorExclusiveArc(_CsAccessFixture):
    """A 系列：錨點 exclusive arc（兩層）。"""

    def test_A1_route_anchor_still_works(self):
        """回歸對照：route 錨定的既有形態一字未改（含 stored related）。"""
        sa, route = self._sa_route()
        pol = self.Policy.create(self._policy_vals(route_id=route.id))
        self.assertEqual(pol.route_id, route)
        self.assertFalse(pol.cloud_service_id)
        self.assertEqual(pol.security_access_id, sa,
                         'route 錨定時 stored related 仍必須物化到 SA')

    def test_A2_cloud_service_anchor_accepted(self):
        """CS 錨定可建；`security_access_id` 空、one2many 反向掛得回去。"""
        svc = self._service()
        pol = self.Policy.create(
            self._policy_vals(cloud_service_id=svc.id))
        self.assertEqual(pol.cloud_service_id, svc)
        self.assertFalse(pol.route_id)
        self.assertFalse(
            pol.security_access_id,
            'CS 錨定的列不該有 SA——有值代表 related 從別處算出來了')
        self.assertIn(pol, svc.access_policy_ids)

    def test_A3_no_anchor_rejected(self):
        """兩者皆空 ⇒ 拒。改前是 `required=True` 擋，改後由 arc 擋。"""
        with self.assertRaises(ValidationError) as cm:
            self.Policy.create(self._policy_vals())
        self.assertIn('anchor', str(cm.exception).lower())

    def test_A4_both_anchors_rejected(self):
        """兩者皆填 ⇒ 拒。這一格是 arc 相對於「兩個 nullable M2O」的全部差別。"""
        svc = self._service()
        _sa, route = self._sa_route()
        with self.assertRaises(ValidationError):
            self.Policy.create(self._policy_vals(
                route_id=route.id, cloud_service_id=svc.id))

    def test_A5_arc_is_also_enforced_by_a_real_sql_check(self):
        """SQL CHECK 真的在 DB 上——不是「Python constrains 看起來有擋」而已。

        為什麼要獨立測這一格：`_add_sql_constraints` 加不上 constraint 時，
        Odoo 只留一行 `_schema.warning` 就繼續跑（`Registry.finalize_constraints`）
        ⇒ 升級「成功」、Python 那層照常擋、**所有行為測試全綠**，而擋不住裸 SQL
        與批次 migration 的那半層已經不在了。這條查的是 `pg_constraint` 本身，
        是唯一分得出這兩種世界的判準。
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
        self.assertIn('route_id', row[0])
        self.assertIn('cloud_service_id', row[0])

    def test_A6_sequence_is_sparse_per_anchor(self):
        """sparse precedence 以**錨點**為作用域，不是以 route 為作用域。

        改前的寫法在 CS 錨定時整段跳過 ⇒ 每一條都落在預設 10 上，之後在
        Cloudflare 撞成同一個 precedence。這裡同時對 route 錨定做對照，證明
        改動沒有把原本的行為換掉。
        """
        svc = self._service()
        p1 = self.Policy.create(self._policy_vals(
            name='cs-1', cloud_service_id=svc.id))
        p2 = self.Policy.create(self._policy_vals(
            name='cs-2', cloud_service_id=svc.id))
        self.assertEqual((p1.sequence, p2.sequence), (10, 20))

        _sa, route = self._sa_route()
        r1 = self.Policy.create(self._policy_vals(
            name='r-1', route_id=route.id))
        r2 = self.Policy.create(self._policy_vals(
            name='r-2', route_id=route.id))
        self.assertEqual((r1.sequence, r2.sequence), (10, 20))

    def test_A7_to_dict_carries_both_anchors(self):
        """to_dict 要送 `cloud_service_id`——只送 route_id 會讓前端把 CS 的
        policy 讀成「掛在 route 0」。"""
        svc = self._service()
        pol = self.Policy.create(self._policy_vals(cloud_service_id=svc.id))
        payload = pol.to_dict()
        self.assertEqual(payload['cloud_service_id'], svc.id)
        self.assertFalse(payload['route_id'])

    def test_A8_rule_submodel_mirrors_the_cs_anchor(self):
        """子模型的 `cloud_service_id` 是 stored related——ir.rule 走得到。

        沒有它，CS 錨定的 policy 底下 selector rule 的 domain 兩腿皆假，租戶
        連一條 include 都建不出來（fail-closed，但一樣是壞的）。這裡驗的是
        **物化**（store=True）而不只是屬性讀得到值：ir.rule 的 domain 走的是
        真欄位，非 stored 的 related 在 SQL 上不存在。
        """
        svc = self._service()
        pol = self.Policy.create(self._policy_vals(cloud_service_id=svc.id))
        rule = pol.rule_ids[0]
        self.assertEqual(rule.cloud_service_id, svc)
        self.assertFalse(rule.security_access_id)
        # 物化證明：直接用 domain search（走 SQL 欄位，不走 Python）
        self.assertIn(
            rule,
            self.Rule.search([('cloud_service_id', '=', svc.id)]),
            'cloud_service_id 必須是 stored——ir.rule 的 domain 只走真欄位')


class TestPolicyQuotaFollowsAnchor(_CsAccessFixture):
    """B 系列：quota 的計數範圍與方案來源都跟著錨點走。"""

    def test_B1_cs_anchor_hits_its_own_policy_quota(self):
        """CS 錨定的 policy 吃**它自己**訂閱方案的政策數上限。

        改前：`security_access_id` 恆空 ⇒ 整條跳過，上限對 CS 靜默失效。
        """
        svc = self._service()
        self._subscribe(svc, self._plan('CS Q', sa_max_access_policies=1))
        # 陽性對照：第一條必須建得起來（否則下面的 raise 分不出是「額度生效」
        # 還是「CS 錨定根本建不了 policy」）
        first = self.Policy.create(self._policy_vals(
            name='q1', cloud_service_id=svc.id))
        self.assertTrue(first.exists())
        with self.assertRaises(ValidationError) as cm:
            self.Policy.create(self._policy_vals(
                name='q2', cloud_service_id=svc.id))
        self.assertIn('quota', str(cm.exception).lower())

    def test_B2_quota_pools_are_per_anchor_not_global(self):
        """兩個錨點的額度池互不相通。

        這一格才分得出「計數範圍正確」與「計數範圍是全表」——只測 B1 的話，
        把 `search_count` 的 domain 拿掉（＝數全表）同樣會綠。
        """
        svc = self._service()
        sa, route = self._sa_route()
        self._subscribe(svc, self._plan('CS Q2', sa_max_access_policies=1))
        self._subscribe(sa, self._plan('SA Q2', sa_max_access_policies=1))
        self.Policy.create(self._policy_vals(
            name='cs-only', cloud_service_id=svc.id))
        # SA 那邊的額度沒有被 CS 那一條吃掉
        sa_pol = self.Policy.create(self._policy_vals(
            name='sa-only', route_id=route.id))
        self.assertTrue(sa_pol.exists())
        # 反過來也一樣：SA 額滿不影響 CS 已滿的判定（各自撞自己的上限）
        with self.assertRaises(ValidationError):
            self.Policy.create(self._policy_vals(
                name='cs-second', cloud_service_id=svc.id))

    def test_B3_email_quota_applies_under_cs_anchor(self):
        """`sa_max_emails_per_policy` 在 CS 錨定下同樣生效（同一個靜默失效）。"""
        svc = self._service()
        self._subscribe(svc, self._plan(
            'CS EQ', sa_max_access_policies=10, sa_max_emails_per_policy=2))
        ok = self.Policy.create(self._policy_vals(
            name='eq-ok', cloud_service_id=svc.id,
            rule_ids=[(0, 0, {'group': 'include', 'rule_type': 'email_list',
                              'rule_value': 'a@x.com, b@x.com'})]))
        self.assertTrue(ok.exists())
        with self.assertRaises(ValidationError):
            self.Policy.create(self._policy_vals(
                name='eq-bad', cloud_service_id=svc.id,
                rule_ids=[(0, 0, {'group': 'include',
                                  'rule_type': 'email_list',
                                  'rule_value': 'a@x.com, b@x.com, c@x.com'})]))

    def test_B4_bulk_grant_guard_still_fires_under_cs_anchor(self):
        """#352 的整批放行守衛不讀錨點，換錨點後必須照擋（回歸鎖）。

        它擋的是「一條規則放進不特定多數人、卻計 0 人」——受害的是**平台上其他
        租戶**的 Cloudflare 席位，與這條 policy 掛在哪個錨點無關。
        """
        svc = self._service()
        with self.assertRaises(ValidationError) as cm:
            self.Policy.create(self._policy_vals(
                name='bulk', cloud_service_id=svc.id,
                rule_ids=[(0, 0, {'group': 'include',
                                  'rule_type': 'email_domain',
                                  'rule_value': 'acme.com'})]))
        self.assertIn('Include', str(cm.exception))
        # 陽性對照：同一個錨點下，email_list 的寫法建得起來
        self.assertTrue(self.Policy.create(self._policy_vals(
            name='bulk-ok', cloud_service_id=svc.id)).exists())


class TestCsAccessIdentityAnchor(_CsAccessFixture):
    """C 系列：`access_app_id` 身分錨點。**全部跑在 su env**（TransactionCase）。

    su env 是刻意的：守衛若寫成 `not self.env.su` 豁免，這一整組會靜默恆綠，
    而那正是它該證明有效的地方。
    """

    def setUp(self):
        super().setUp()
        self.svc = self._service()
        self.assertTrue(
            self.env.su, '前提：本組必須跑在 su env，否則守不到 superuser 那格')

    def test_C1_anchor_list_names_real_fields(self):
        """清單裡的每一個名字都要對應到真欄位。

        打錯字／改名會讓 `field in vals` 與底下每一條迴圈**靜默變成 no-op**，
        整組測試照樣綠。
        """
        self.assertTrue(_CS_ACCESS_IDENTITY_ANCHOR_FIELDS)
        for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
            self.assertIn(field, self.Service._fields,
                          '錨點清單裡的 %r 不是 cloud_service 的欄位' % field)

    def test_C2_create_injection_is_neutralised_under_su(self):
        """create 帶錨點 ⇒ 值不落地（su env 下也一樣）。"""
        for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
            svc = self._service(
                name='inj-%s' % field, subdomain='inj-%s' % field.lower(),
                **{field: 'victim-app-id'})
            self.assertFalse(
                svc[field],
                '%s 在 create 被注入後落地了' % field)

    def test_C3_create_injection_via_context_default_is_neutralised(self):
        """`default_<欄位>` 走 context 也要被壓掉。

        🔴 這一格是 pop 與「壓成 False」的分野：`default_*` 是在
        `super().create()` 內部的 `_add_missing_default_values` 才展開的，只
        `vals.pop()` 的守衛對它完全無效，而 `ir.default` 對租戶是可寫的、
        `call_kw` 又直接把呼叫端 context 換進 env（#519／#705 同形狀）。
        """
        for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
            svc = _internal_provision(self.Service.with_context(
                **{'default_%s' % field: 'victim-app-id'})).create({
                    'name': 'ctxdef-%s' % field,
                    'workspace_id': self.workspace.id,
                    'template_id': self.template.id,
                    'subdomain': 'ctxdef-%s' % field.lower(),
                })
            self.assertFalse(
                svc[field],
                '%s 經 default_ context 注入後落地了' % field)

    def test_C4_write_is_rejected_under_su(self):
        """write 搬錨點 ⇒ UserError（su env 下也一樣）。"""
        for field in _CS_ACCESS_IDENTITY_ANCHOR_FIELDS:
            with self.assertRaises(UserError):
                self.svc.write({field: 'victim-app-id'})

    def test_C5_forged_context_does_not_open_the_hatch(self):
        """JSON 表達得出的每一種值都打不開逃生口。

        sentinel 的全部價值在這裡：少了這條迴圈，把判準改回裸真值不會有任何
        測試變紅——而裸真值那版在 `/web/dataset/call_kw` 上是可偽造的（#522
        在 18.0 實測過，偽造值真的 commit 進 Postgres）。
        """
        for value in _FORGED_CTX_VALUES:
            forged = self.svc.with_context(
                **{_CS_ACCESS_IDENTITY_CTX_KEY: value})
            with self.assertRaises(
                    UserError,
                    msg='偽造值 %r 打開了 access_app_id 的逃生口' % (value,)):
                forged.write({'access_app_id': 'victim-app-id'})

    def test_C6_real_sentinel_still_writes(self):
        """陽性對照：真 sentinel 寫得進去。

        沒有它，上面每一條負向斷言都可能只是因為「這個欄位對誰都寫不動」——
        一個什麼都擋的壞守衛會讓它們全部為了錯的理由變綠。
        """
        _internal_cs_access_identity(self.svc).write(
            {'access_app_id': 'app-real-123'})
        self.assertEqual(self.svc.access_app_id, 'app-real-123')
        # 同一顆 sentinel、同一把 key（避免有人另外造一顆同名物件）
        self.assertIs(
            _internal_cs_access_identity(self.svc).env.context[
                _CS_ACCESS_IDENTITY_CTX_KEY],
            _INTERNAL_CS_ACCESS_IDENTITY_WRITE)

    def test_C7_ordinary_access_fields_stay_writable(self):
        """陽性對照 2：一般 access 設定欄位不受守衛影響。

        守衛若誤擋整個 write，租戶連開關保護都做不到，而 C4/C5 依然全綠。

        #745 之後這個 write 會**真的**去打 operator（`is_protected` 進了
        `_CS_ACCESS_SYNC_FIELDS`），所以要 mock 掉出站呼叫。刻意**不**用
        `cs_skip_access_sync` 繞過：那會讓這條陽性對照少走一段真實路徑，而
        它的全部價值就是「一般欄位的 write 從頭到尾走得完」。
        """
        client = MagicMock()
        client.sync_access_app.return_value = {
            'app_id': 'app-c7', 'policy_ids': [], 'allowed_idps': [],
            'unresolved_idp_types': [], 'orphaned_policy_ids': []}
        with patch('odoo.addons.woow_paas_platform.services.paas_operator.'
                   'get_paas_operator_client', return_value=client):
            self.svc.write({
                'is_protected': True, 'login_google': True,
                'session_duration': '8h'})
        self.assertTrue(self.svc.is_protected)
        self.assertTrue(self.svc.login_google)
        self.assertEqual(self.svc.session_duration, '8h')


class TestCsAccessTeardown(_CsAccessFixture):
    """D 系列：刪除路徑的 Access teardown（seam 用 mock）。

    🔴 **所有 patch 都必須帶 `autospec=True`**，而且理由不是風格：Odoo 的
    `unlink()` 會跑 `self._ondelete_methods`，那份清單是用
    `getmembers(cls, lambda f: callable(f) and hasattr(f, '_ondelete'))`
    **現場掃出來**的（`odoo/models.py :: BaseModel._ondelete_methods`）。裸
    `MagicMock` 對任何屬性都回真 ⇒ 被掃成 `@api.ondelete` 方法，`unlink()`
    會**主動呼叫它一次**（實測：`Calls: [call._ondelete.__bool__(),
    call(cloud_service(19,))]`）。更糟的是那份清單會 `cls._ondelete_methods =
    methods` 快取在 class 上，一旦被污染就跨測試殘留。`autospec=True` 讓
    `hasattr(mock, '_ondelete')` 回 False，整個問題不存在。
    """

    _SEAM = ('odoo.addons.woow_paas_platform.models.cloud_service.'
             'CloudService._push_access_teardown')

    def setUp(self):
        super().setUp()
        self.svc = self._service(subdomain='td744')

    def _with_access_app(self, service, app_id='app-td-1'):
        """把服務推到「有 Access app」的狀態（只有 sentinel 路徑寫得動）。

        `cs_skip_access_sync`（#745）：這是**佈景設定**不是受測動作。T3 之後
        寫 `is_protected` 會觸發真的 operator push，沒有這個抑制的話 fixture
        會死在「PaaS Operator is not configured」上——而那個 error 跟本組要測
        的 teardown 行為完全無關。與 SA fixture 用 `sa_skip_route_sync` 同款。
        """
        _internal_cs_access_identity(service).with_context(
            cs_skip_access_sync=True).write({
                'is_protected': True, 'access_app_id': app_id})
        return service

    def test_D1_unlink_without_access_app_does_not_call_the_seam(self):
        """沒有 app id、沒有 cf policy id ⇒ 根本不該打 operator。

        這是現況的實際形狀（T3 之前沒有任何路徑會寫 access_app_id），也是
        `_push_access_teardown` 今天丟 NotImplementedError 卻不會炸的原因。
        """
        with patch(self._SEAM, autospec=True) as seam:
            self.svc.unlink()
        seam.assert_not_called()
        self.assertFalse(self.svc.exists())

    def test_D2_unlink_tears_down_and_passes_the_captured_ids(self):
        """有 app id ⇒ 拆除照打，且帶著刪除前才拿得到的 id。"""
        svc = self._with_access_app(self._service(
            name='td2', subdomain='td744-2'))
        # `sa_skip_route_sync`（#745）：這台服務現在是 is_protected + 有 app id
        # 的狀態，所以建 policy 會觸發真的 operator push。這裡是佈景不是受測動作。
        pol = _skip_route_sync(self.Policy).create(
            self._policy_vals(cloud_service_id=svc.id))
        pol.cf_policy_id = 'cf-pol-1'
        with patch(self._SEAM, autospec=True, return_value=None) as seam:
            svc.unlink()
        seam.assert_called_once()
        kwargs = seam.call_args.kwargs
        self.assertEqual(kwargs['app_id'], 'app-td-1')
        self.assertEqual(kwargs['policy_ids'], ['cf-pol-1'],
                         'cf_policy_id 必須在 super().unlink() 之前抓走——'
                         '子列是 ondelete=cascade，刪完就再也拿不到')
        # hostname 而不是 URL：Cloudflare Access app 的錨是主機名，payload 裡
        # 沒有 scheme。斷言刻意不比對整串（domain 來自 config parameter，寫死
        # 只會綁死環境），比對的是「是 host 不是 URL」＋「是這一台的 subdomain」
        # ——這兩點才是傳錯值時會壞掉的地方。
        self.assertFalse(kwargs['hostname'].startswith('http'),
                         '送出去的必須是 hostname，不是 _public_base_url()')
        self.assertTrue(kwargs['hostname'].startswith('td744-2.'))
        self.assertFalse(svc.exists())

    @mute_logger('odoo.addons.woow_paas_platform.models.cloud_service')
    def test_D3_unlink_fails_loud_and_keeps_the_retry_anchor(self):
        """拆不掉 ⇒ raise、記錄留著、`access_app_id` 留著。

        保留 id 是重點：清掉的話那個 Access app 變成永久 CF 孤兒，而且沒有任何
        路徑找得回來（刪掉的列再也進不了任何重試集合）。
        """
        svc = self._with_access_app(self._service(
            name='td3', subdomain='td744-3'), app_id='app-td-3')
        with patch(self._SEAM, autospec=True, side_effect=RuntimeError('operator down')):
            with self.assertRaises(UserError) as cm:
                svc.unlink()
        self.assertIn('Access', str(cm.exception))
        self.assertTrue(svc.exists(), '拆除失敗時記錄必須留著供重試')
        self.assertEqual(svc.access_app_id, 'app-td-3',
                         '拆除失敗時 app id 必須留著——它是唯一的重試錨點')

    @mute_logger('odoo.addons.woow_paas_platform.models.cloud_service')
    def test_D4_do_cleanup_lands_in_error_when_teardown_fails(self):
        """lifecycle 清理路徑：拆不掉 ⇒ 落 'error'，**不可**寫 'deleting'。

        'deleting' 是一個成功訊號；在 CF 還留著一個 Access app 的情況下寫下去，
        維運看到的「已清乾淨」與「清失敗」長得一模一樣（SA 的 `_do_cleanup`
        踩過同一條）。
        """
        svc = self._with_access_app(self._service(
            name='td4', subdomain='td744-4'), app_id='app-td-4')
        with patch(self._SEAM, autospec=True, side_effect=RuntimeError('operator down')):
            svc._do_cleanup()
        self.assertEqual(svc.state, 'error')
        self.assertIn('Access', svc.error_message or '')
        self.assertEqual(svc.access_app_id, 'app-td-4')

    def test_D5_do_cleanup_clears_the_anchor_on_a_clean_teardown(self):
        """陽性對照：拆乾淨 ⇒ 清掉 app id 與 cf policy id，狀態進 'deleting'。"""
        svc = self._with_access_app(self._service(
            name='td5', subdomain='td744-5'), app_id='app-td-5')
        # 同 D2：佈景設定，抑制 T3 之後才存在的 per-row operator push。
        pol = _skip_route_sync(self.Policy).create(
            self._policy_vals(cloud_service_id=svc.id))
        pol.cf_policy_id = 'cf-pol-5'
        with patch(self._SEAM, autospec=True, return_value=None):
            svc._do_cleanup()
        self.assertEqual(svc.state, 'deleting')
        self.assertFalse(svc.access_app_id)
        self.assertFalse(pol.cf_policy_id)

    def test_D6_suspend_does_not_touch_the_access_app(self):
        """停權**不動** Access（設計拍板：停機由 helm 層處理）。

        拆掉再復權要重建並重新拿 id，中間任何一步失敗都會讓一台復權的服務變成
        無保護對外曝光——方向是 fail-open。
        """
        svc = self._with_access_app(self._service(
            name='td6', subdomain='td744-6'), app_id='app-td-6')
        with patch(self._SEAM, autospec=True) as seam:
            svc._do_suspend()
        seam.assert_not_called()
        self.assertEqual(svc.access_app_id, 'app-td-6')
        self.assertTrue(svc.is_protected)
