"""#896：``_check_policy_quota`` 的「數池子」查詢沒有 ``sudo()``。

## 缺口的精確形狀

``_check_policy_quota`` 是 ``@api.constrains``，跑在**寫入者自己的 env** 上。
它用 ``self.search_count([(anchor_field, '=', anchor.id)])`` 數整個錨點池，
而 ``search_count`` 會套 ``ir.rule`` ⇒ 呼叫者看不到的列不算進去 ⇒ **少算**。
少算的症狀是「配額安靜地變寬」，與「方案的政策數上限很大」在輸出上**完全不可
區分**——沒有任何訊號分得出來，這正是不能放著不修的那一格。

## 少算是 **0-或-全**，不是「少一點」

policy 的 ``ir.rule`` 三腿（``security/ir_rules.xml`` 的
``security_access_access_policy_rule_workspace_member`` ／
``..._owner_admin_manage``）判歸屬走的**就是** ``_ANCHOR_POOL_FIELD`` 那三根
column（``security_access_id`` ／ ``cloud_service_id`` ／ ``smart_home_id``）。
池的 domain 是 ``[(anchor_field, '=', anchor.id)]`` ⇒ 池裡每一列的可見度判準
**完全相同**：

* 呼叫者是該錨點 workspace 的成員 ⇒ 看得到**整池**，計數正確；
* 呼叫者不是 ⇒ 看得到 **0 筆**，計數恆為 0 ⇒ 配額**恆放行**。

⇒ 這是本檔測試怎麼建構的依據：要造出少算，寫入者必須是該 workspace 的
**非成員**（`B` 系列把這個 0-或-全的性質本身也測起來，免得日後 rule 改成
「部分可見」而沒人發現判準前提變了）。

## 為什麼今天端到端打不進來（⇒ 為什麼測試是**直接呼叫**守衛）

#924 在真沙盒量過（留言 11897）：非成員裸 ``search_count`` 得 0、``sudo()`` 得 2,
落差**確實存在**；但端到端跑不到，靠的是兩個**不屬於這個守衛**的外部性質：

1. stored related ``security_access_id`` 的 ``compute_sudo``（Odoo
   ``fields.py`` ``attrs['compute_sudo'] = attrs.get('compute_sudo',
   attrs.get('related_sudo', True))``）：重算在 ``records.sudo()`` 上跑，
   而重算又會回頭 ``_validate_fields`` ⇒ 大多數寫入路徑的這條 constraint
   其實跑在 su env，裸 ``search_count`` 剛好看得到全部。
2. #848 的 ``_assert_anchor_move_authorised``：跨錨點搬移在守衛層就
   ``AccessError``，走不到 constraint。

⇒ 🔴 **測試刻意不走那兩條路徑**：走 stored-related 重算那條，**修前也會擋**，
測試會「修前綠」＝零鑑別力；走 CS→CS write 那條，修前會紅在 #848 的
``AccessError`` 上，紅的原因不是本單要驗的那一格。本檔因此**直接以寫入者的
env 呼叫守衛**，把「計數口徑不得受呼叫者可見度影響」這個性質單獨隔離出來測。

## 陽性對照

``A2`` 在同一個 fixture 上重現 #924 的那組讀數（非成員 0 vs sudo 2）。少了它，
``A1`` 的紅／綠可能來自 fixture 壞掉、workspace 建錯、或攻擊者其實看得到整池
——那三種情況下 ``A1`` 都會為了錯的理由變色。``A3`` 則證明守衛不是「一律
raise」（合規的池在同一個 env 下必須放行）。

## ⚠ ``TransactionCase`` 預設是 su env

``env.su`` 為真時 ``self.sudo()`` 是 no-op、``search_count`` 也不套 rule ⇒
本檔任何一格若跑在 su env 上都會**恆綠**（
``docs/decisions/patterns/su-env-exempt-guard-false-green.md``）。所以每一格
在斷言之前都先 ``assertFalse(env.su)``。

## BFF 前置為什麼沒有對應的行為測試

``controllers/security_access.py::_create_policy`` 的前置計數同一天一起加了
``sudo()``（口徑對齊），但它**沒有 RED 可達路徑**：那條端點的呼叫者一律先過
``_gated_sa()``（該 SA 所屬 workspace 的成員），而上面「0-或-全」那段說明了
成員必然看得到整池 ⇒ 前置計數對合法呼叫者永遠不會少算。硬寫一格端到端測試
只會修前修後都綠＝零鑑別力。那一改是純 defense-in-depth，證據寫在 PR 描述裡。
"""

import json
import uuid

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.security_access import _skip_route_sync
from ..models.smart_home_route import _sm_skip_access_sync
from .smart_home_fixture import sh_model

_POLICY = 'woow_paas_platform.security_access_access_policy'
_CS = 'woow_paas_platform.cloud_service'
_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'


def _new_group_user(env, name):
    """只有 ``base.group_user`` 的租戶帳號（password == login，可 authenticate）。"""
    login = '%s_%s' % (name, uuid.uuid4().hex[:8])
    return env['res.users'].sudo().create({
        'name': '%s 896' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


def _plan_vals(name, **quota):
    vals = {
        'name': name, 'sh_duration': 1, 'sh_unit': 'month',
        'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
        # #521：`ac_max_access_seats` 預設 0 ＝「一席都沒有」⇒ 任何帶
        # email_list 的 fixture 都會被 `_check_access_seat_quota` 擋住。本檔
        # 測的是 policy **數**那條，給一個明顯夠用的席位額度，讓紅燈的原因只
        # 可能是被測的那一條。（fixture 的 forward coupling，不是產品側豁免
        # ——#521 留言 9960 §4 產品側零豁免。）
        'ac_max_access_seats': 99,
    }
    vals.update(quota)
    return vals


class _PolicyQuotaSudo896Fixture(TransactionCase):
    """受害者 workspace（帶錨點與方案）＋ 一個完全不相干的租戶帳號。

    ⚠ 順序是刻意的：**先把 policy 建好、再掛訂閱**。反過來的話 fixture 自己
    就會撞上要測的那條配額（superuser 建第二條時 ``count=2 > quota=1``），
    第二條根本建不出來。先建後掛＝``_anchor_plan()`` 在 create 當下回空
    recordset ⇒ 整條 ``continue``（既有的 grandfathered 語意）。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Policy = self.env[_POLICY]
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]

        self.victim_owner = self._group_user('victim896')
        self.outsider = self._group_user('outsider896')
        self.ws_victim = self._workspace('WS-VICTIM 896', self.victim_owner)

        self.assertFalse(
            self.WorkspaceAccess.sudo().search_count([
                ('workspace_id', '=', self.ws_victim.id),
                ('user_id', '=', self.outsider.id)]),
            '前提：outsider 不得是受害者 workspace 的成員，否則 ir.rule 會讓'
            ' 他看到整池，本檔每一格都會失去鑑別力')

    # ------------------------------------------------------------------ #
    #  Fixture helpers（形狀照抄 test_policy_anchor_ownership_guard_848.py）
    # ------------------------------------------------------------------ #
    def _group_user(self, name):
        user = _new_group_user(self.env, name)
        self.assertFalse(
            user.has_group('base.group_system'),
            '前提：fixture 帳號只能有 base.group_user（%s 撞了）' % user.login)
        return user

    def _workspace(self, name, owner):
        """owner 只能由 ``with_user()`` 決定（``_check_owner_count`` 限制一位）。"""
        ws = self.Workspace.with_user(owner).create(
            {'name': '%s %s' % (name, uuid.uuid4().hex[:6])})
        self.assertEqual(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('role', '=', 'owner')]).user_id,
            owner,
            '前提：bootstrap 出來的 owner 必須是 %s' % owner.login)
        return ws.sudo()

    def _cloud_service(self, workspace, tag):
        template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'T896 %s' % tag,
                'slug': 't896%s-%s' % (tag, uuid.uuid4().hex[:6]),
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/t896',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        return _internal_provision(self.env[_CS].sudo()).create({
            'name': 'svc-896-%s' % tag,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
        })

    def _sa_route(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 896 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        route = _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r896%s-%s' % (tag, uuid.uuid4().hex[:6]),
        })
        return sa, route.with_context(sa_skip_route_sync=False)

    def _sh_route(self, workspace, tag):
        """#1053：B3 用的 SH 錨點。

        ⚠ 兩件事刻意照 `test_sh_access_endpoints_768.py` 的 fixture，不要「簡化」：

        * `sh_model()` 開建立閘（#524 起 `SmartHome.create()` 有一道無 su 豁免的
          閘），建完 `with_env(self.env)` 把 sentinel **洗掉**——不洗的話 fixture
          自己把受測守衛一起關掉了。
        * ⛔ **不帶 `tunnel_id`**：有 live tunnel 時 route 的
          `_sync_access_to_operator()` liveness gate 會放行，fixture 會真的去打
          operator。
        """
        sh = sh_model(self.env).sudo().create({
            'name': 'SH 896 %s' % tag,
            'workspace_id': workspace.id,
        }).with_env(self.env)
        sh.sudo().write({'subdomain': 'sh896%s-%s' % (tag, uuid.uuid4().hex[:8])})
        self.assertFalse(
            sh.sudo().tunnel_id,
            '前提：SH 不得有 live tunnel，否則 sync 會真的打 operator')
        route = _sm_skip_access_sync(self.env[_SH_ROUTE].sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})
        return sh.sudo(), route.with_context(sm_skip_access_sync=False)

    def _plan(self, name, **quota):
        return self.env['sh.subscription.plan'].sudo().create(
            _plan_vals(name, **quota))

    def _subscribe(self, record, plan):
        """⚠ ``sh_partner_id`` 必須是**錨點 workspace 的 owner**。

        ``cloud_service._assert_subscription_binding()``（``cloud_service.py``）
        在綁定當下要求 ``sub.sh_partner_id == workspace_id.owner_id.partner_id``
        且 ``state == 'in_progress'``；照抄 744 fixture 的
        ``self.env.user.partner_id`` 在本檔會撞——那邊的 workspace owner 剛好
        就是 ``env.user``，本檔的 owner 是另建的租戶帳號。
        """
        owner = record.sudo().workspace_id.owner_id
        self.assertTrue(owner, '前提：錨點必須有 workspace owner')
        sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': owner.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        record.sudo().subscription_id = sub.id
        return sub

    def _settle_projections(self):
        """把 stored related 投影欄位的待重算清空，並斷言真的清空了。

        🔴 沒有這一步，本檔的 RED 會**從未紅過**：``security_access_id`` ／
        ``smart_home_id`` 是 stored related（``compute_sudo`` 預設 True，
        ``odoo/fields.py`` ``attrs['compute_sudo'] = attrs.get('compute_sudo',
        attrs.get('related_sudo', True))``），而 ``_check_policy_quota`` 的
        ``@api.constrains`` 清單**含這兩欄** ⇒ 只要它們還在 ``tocompute``，
        後面任何一次 flush（``search_count`` 自己就會 flush）都會在
        ``records.sudo()`` 上重算並回頭 ``_validate_fields`` ⇒ **修法前那條
        constraint 也會 raise**，assertRaises 於是修前就綠。

        擺 fixture 的時候先 flush（那時方案還沒掛上、``_anchor_plan()`` 回空
        ⇒ 守衛整條 continue，重算是安全的），再掛訂閱；測試裡再驗一次沒有殘留。
        """
        self.env.flush_all()
        for fname in ('security_access_id', 'smart_home_id'):
            field = self.Policy._fields[fname]
            self.assertFalse(
                self.env.records_to_compute(field),
                '前提：%s 不得還在 tocompute——su 重算會讓守衛在 su env 再跑'
                '一次，修前也會擋，RED 因此不可能紅' % fname)

    def _policy(self, name, **anchor):
        vals = {
            'name': 'pol-896-%s' % name,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(anchor)
        pol = _skip_route_sync(self.Policy.sudo()).create(vals)
        return pol.with_context(sa_skip_route_sync=False)

    def _as_outsider(self, pol):
        """把 ``pol`` 換成 outsider 的 env，並預熱錨點欄位的 transaction cache。

        ``Environment.cache`` 是 **per-transaction 共用**的：fixture 以
        superuser 讀進來的欄位值，換 env 之後直接命中 cache、不會再回 DB、
        因此不會觸發 ``ir.rule`` 讀取檢查。這**不是**繞過 ACL，而是在重現
        框架的真實狀態——真正的寫入路徑上，constraint 跑的時候被驗證的那幾列
        剛剛被 ``write()`` 寫過，值本來就在 cache 裡。

        ⚠ 相對地 ``search_count`` **一定**回 DB ⇒ 一定套 ``ir.rule``，
        這個不對稱正是本單缺口的來源。
        """
        # 呼叫守衛前先把投影欄位的待重算清乾淨（理由見 `_settle_projections`）。
        self._settle_projections()
        pol.read(['cloud_service_id', 'security_access_id', 'smart_home_id',
                  'name'])
        view = pol.with_user(self.outsider)
        self.assertFalse(
            view.env.su,
            '前提：outsider env 不得是 su——su 之下 self.sudo() 是 no-op、'
            'search_count 也不套 ir.rule，本格會恆綠'
            '（docs/decisions/patterns/su-env-exempt-guard-false-green.md）')
        try:
            view.mapped('cloud_service_id')
            view.mapped('security_access_id')
            view.mapped('smart_home_id')
        except AccessError as exc:
            self.fail(
                '前提破了：outsider env 讀不到錨點欄位（%s）。本格要驗的是'
                '「數池子少算」，不是 ACL 讀取；若 Odoo 改了 transaction cache'
                ' 的共用語意，這個建構法要換掉，不要把斷言放寬。' % exc)
        return view


class TestPolicyQuotaPoolIgnoresCallerVisibility896(_PolicyQuotaSudo896Fixture):
    """A 系列：配額的計數口徑不得受呼叫者可見度影響。"""

    def _victim_pool(self, quota=1, n_policies=2):
        cs = self._cloud_service(self.ws_victim, 'a')
        pols = self.Policy.browse()
        for i in range(n_policies):
            pols |= self._policy('cs%d' % i, cloud_service_id=cs.id)
        # 先建 → 先把投影欄位重算掉（此時方案未掛、守衛 continue）→ 再掛訂閱。
        # 順序錯了會撞 fixture 自己要測的那條配額，或讓 RED 從未紅過。
        self._settle_projections()
        self._subscribe(cs, self._plan('CS Q896', sa_max_access_policies=quota))
        self._settle_projections()
        self.assertEqual(
            pols.sudo().mapped('cloud_service_id'), cs.sudo(),
            '前提：這些 policy 必須真的錨在同一個 CS 上（池才成立）')
        return cs, pols

    def test_A1_outsider_env_still_hits_the_policy_quota(self):
        """🔴 本單主格：非成員 env 呼叫守衛，超額仍必須擋下來。

        修前：``self.search_count`` 在 outsider env 下得 0（``ir.rule`` 濾光），
        ``0 > 1`` 為假 ⇒ **靜默放行**，這一格紅在「沒有 raise」。
        修後：``self.sudo().search_count`` 得 2，``2 > 1`` ⇒ raise。
        """
        cs, pols = self._victim_pool(quota=1, n_policies=2)
        view = self._as_outsider(pols[0])
        with self.assertRaises(ValidationError) as cm:
            view._check_policy_quota()
        self.assertIn(
            'quota', str(cm.exception).lower(),
            '擋下來了，但訊息不是配額那條——確認紅的是本格要驗的分支')

    def test_A2_positive_control_outsider_sees_none_of_the_pool(self):
        """陽性對照：重現 #924 的讀數（非成員 0 vs sudo N）。

        沒有這一格，A1 的紅／綠分不出「守衛口徑對了」與「fixture 根本沒造出
        可見度落差」。
        """
        cs, pols = self._victim_pool(quota=1, n_policies=2)
        domain = [('cloud_service_id', '=', cs.id)]
        as_outsider = self.Policy.with_user(self.outsider)
        self.assertFalse(as_outsider.env.su, '前提：對照組必須跑在非 su env')
        self.assertEqual(
            as_outsider.search_count(domain), 0,
            'ir.rule 應該讓非成員完全看不到這個池；看得到就表示 rule 改過了，'
            'A1 的建構前提已經不成立')
        self.assertEqual(
            self.Policy.sudo().search_count(domain), len(pols),
            'sudo 的真值必須是 fixture 建的筆數')

    def test_A3_compliant_pool_still_passes_under_the_same_env(self):
        """反面對照：合規的池在**同一個** env 下必須放行。

        少了它，一個「什麼都 raise」的壞守衛也會讓 A1 變綠。
        """
        cs, pols = self._victim_pool(quota=2, n_policies=2)
        view = self._as_outsider(pols[0])
        view._check_policy_quota()  # 不得 raise

    def test_A4_su_env_baseline_unchanged(self):
        """迴歸：su env（既有的絕大多數路徑）行為不變。

        ``self.sudo()`` 在 su env 是 no-op ⇒ 這一格修前修後都綠，它的用途是
        釘住「本次改動沒有動到既有路徑」。
        """
        cs, pols = self._victim_pool(quota=1, n_policies=2)
        self.assertTrue(pols.env.su, '前提：fixture 的 env 應該是 su')
        with self.assertRaises(ValidationError):
            pols[0]._check_policy_quota()


class TestPolicyPoolVisibilityIsAllOrNothing896(_PolicyQuotaSudo896Fixture):
    """B 系列：池的可見度是「0 或全」——這是 BFF 前置沒有 RED 路徑的依據。

    ``ir.rule`` 判歸屬走的就是 ``_ANCHOR_POOL_FIELD`` 那幾根 column，與池的
    domain **同一根**。所以合法呼叫者（該 workspace 的成員）必然看得到整池，
    ``controllers/security_access.py::_create_policy`` 的前置計數對他永遠不會
    少算。這三格把這個前提釘住（三根 column 各一格，#1053 補上 SH 那根）：日後
    有人把 rule 改成「部分可見」，這裡會紅，
    而不是讓 BFF 那條悄悄變成可利用。
    """

    def test_B1_member_sees_the_whole_sa_pool(self):
        sa, route = self._sa_route(self.ws_victim, 'b1')
        pols = self.Policy.browse()
        for i in range(3):
            pols |= self._policy('sa%d' % i, route_id=route.id)
        domain = [('security_access_id', '=', sa.id)]
        as_owner = self.Policy.with_user(self.victim_owner)
        self.assertFalse(as_owner.env.su, '前提：必須跑在非 su env')
        self.assertEqual(
            as_owner.search_count(domain), 3,
            'workspace 成員必須看得到整池——這正是 BFF 前置不會少算的理由')
        self.assertEqual(
            self.Policy.with_user(self.outsider).search_count(domain), 0,
            '非成員必須是 0（0-或-全，沒有中間值）')

    def test_B2_member_sees_the_whole_cs_pool(self):
        cs = self._cloud_service(self.ws_victim, 'b2')
        pols = self.Policy.browse()
        for i in range(3):
            pols |= self._policy('cs%d' % i, cloud_service_id=cs.id)
        domain = [('cloud_service_id', '=', cs.id)]
        as_owner = self.Policy.with_user(self.victim_owner)
        self.assertFalse(as_owner.env.su, '前提：必須跑在非 su env')
        self.assertEqual(as_owner.search_count(domain), 3)
        self.assertEqual(
            self.Policy.with_user(self.outsider).search_count(domain), 0)

    def test_B3_member_sees_the_whole_sh_pool(self):
        """#1053：第三根 column（`smart_home_id`）也必須是 0-或-全。

        🔴 少了這一格，B1／B2 只釘住 `security_access_id` 與 `cloud_service_id`
        兩根。日後有人收窄 `ir_rules.xml` 那條 read rule 的 **SH 那一腿**，B1／B2
        照樣綠，而 SH 錨點的 BFF 前置計數（`controllers/smart_home.py::
        _create_sh_access_policy`，#1053 加的 `sudo()`）就會**無聲**地從
        defense-in-depth 變成真的在補一個可利用的少算。

        ⚠ domain 用 `smart_home_id`（`_ANCHOR_POOL_FIELD` 登記的池欄位、也是
        model 端 `_check_policy_quota` 數的同一根），不是 `smart_home_route_id`
        ——後者只說「這條 policy 掛在哪條 route」，不是池。
        """
        sh, route = self._sh_route(self.ws_victim, 'b3')
        for i in range(3):
            self._policy('sh%d' % i, smart_home_route_id=route.id)
        domain = [('smart_home_id', '=', sh.id)]
        as_owner = self.Policy.with_user(self.victim_owner)
        self.assertFalse(as_owner.env.su, '前提：必須跑在非 su env')
        self.assertEqual(
            as_owner.search_count(domain), 3,
            'workspace 成員必須看得到整池——這正是 SH 的 BFF 前置不會少算的理由')
        self.assertEqual(
            self.Policy.with_user(self.outsider).search_count(domain), 0,
            '非成員必須是 0（0-或-全，沒有中間值）')


@tagged('post_install', '-at_install')
class TestPolicyQuotaBffSudoCrossUser896(HttpCase):
    """C 系列：BFF 前置那顆新 ``sudo()`` 的 cross-user negative。

    ``repo-lint`` 的 ``controller-sudo`` gate 要求「controller 每多一個
    ``sudo()``，就要有一格 HttpCase cross-user negative test」——理由是
    controller 層的 sudo 繞過 ``ir.rule``，是 B1-class data leak 的主要來源
    （``docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md``）。
    本組就是那一格，守三件事：

    * ``C1`` — sudo **只放寬可見度，不放寬 domain**：別的 SA 的池不得被算進來
      （否則租戶會因為「別人建了很多 policy」而被擋，且那是跨租戶資訊外洩）。
    * ``C2`` — 自己的池仍然照擋（sudo 之後計數仍然正確，不是恆 0 也不是恆大）。
    * ``C3`` — **非成員照樣在 gate 就被擋**：新 sudo 不得讓未授權呼叫者拿到
      任何關於別人 SA 的配額狀態（連「額滿了」這種訊息都不行）。
    """

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.alice = _new_group_user(self.env, 'alice896_%s' % suffix)
        self.bob = _new_group_user(self.env, 'bob896_%s' % suffix)
        self.mallory = _new_group_user(self.env, 'mallory896_%s' % suffix)
        self.Policy = self.env[_POLICY].sudo()

        self.ws_a, self.sa_a, self.route_a = self._workspace_sa(
            self.alice, 'A', suffix)
        self.ws_b, self.sa_b, self.route_b = self._workspace_sa(
            self.bob, 'B', suffix)

    # ------------------------------------------------------------------ #
    #  Fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _workspace_sa(self, owner, tag, suffix):
        ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'WS-%s 896 %s' % (tag, suffix), 'owner_id': owner.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', ws.id),
                              ('user_id', '=', owner.id)]):
            Access.create({'workspace_id': ws.id, 'user_id': owner.id,
                           'role': 'owner'})
        sa = self.env[_SA].sudo().create(
            {'name': 'SA 896 %s' % tag, 'workspace_id': ws.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        route = self.env[_ROUTE].sudo().create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'ep896%s-%s' % (tag.lower(), suffix)})
        return ws, sa, route

    def _subscribe(self, record, tag, quota):
        # `sh_partner_id` 取錨點 workspace 的 owner：`cloud_service` 那側的
        # `_assert_subscription_binding()` 逐字要求兩者相等，SA 這側今天沒有
        # 同款守衛，但沒有理由讓兩邊的 fixture 長得不一樣。
        owner = record.sudo().workspace_id.owner_id
        plan = self.env['sh.subscription.plan'].sudo().create(
            _plan_vals('EP896 %s' % tag, sa_max_access_policies=quota))
        sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': owner.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        record.sudo().subscription_id = sub.id
        self.env.flush_all()
        return sub

    def _seed_policy(self, route, name):
        return self.Policy.create({
            'route_id': route.id, 'name': name, 'decision': 'allow',
            'rule_type': 'email_list', 'rule_value': 'seed@example.com'})

    def _rpc(self, url, params=None):
        r = self.url_open(url, data=json.dumps({'jsonrpc': '2.0',
                          'method': 'call', 'params': params or {}}),
                          headers={'Content-Type': 'application/json'})
        body = r.json()
        # 存原始 body：結構化拒絕（{success: False}）與端點爆掉（JSON-RPC
        # error）必須分得出來，否則 `assertFalse(res.get('success'))` 會在
        # 端點 raise 的時候假綠（`{}.get('success')` 是 None）。
        self._last_body = body
        return body.get('result') or {}

    def _create_policy_rpc(self, ws, sa, name):
        return self._rpc(
            '/api/workspaces/%s/security-accesses/%s/access-policies'
            % (ws.id, sa.id),
            {'action': 'create', 'name': name, 'decision': 'allow',
             'rule_type': 'email_list', 'rule_value': 'new@example.com'})

    def _assert_structured_denial(self, res):
        self.assertNotIn(
            'error', self._last_body,
            '端點爆掉（JSON-RPC error）而不是回結構化拒絕：%s'
            % self._last_body.get('error'))
        self.assertFalse(res.get('success'))
        self.assertIn('error', res)

    def _pool_count(self, sa):
        return self.Policy.search_count([('security_access_id', '=', sa.id)])

    # ------------------------------------------------------------------ #
    #  C 系列                                                             #
    # ------------------------------------------------------------------ #
    def test_C1_foreign_pool_is_not_counted_against_this_sa(self):
        """sudo 只放寬可見度，**不放寬 domain**：別的 SA 的池不算進來。

        SA-B（另一個 workspace）塞滿 5 條 policy；SA-A 的方案上限 2、目前 1 條
        ⇒ alice 必須還能建第 2 條。這一格擋的是「把 sudo 加上去時順手把 domain
        放寬／拿掉」——那會同時造成誤擋與跨租戶資訊外洩。
        """
        for i in range(5):
            self._seed_policy(self.route_b, 'b-noise-%d' % i)
        self._seed_policy(self.route_a, 'a-first')
        self._subscribe(self.sa_a, 'A', quota=2)
        self.assertEqual(self._pool_count(self.sa_b), 5, '前提：外池要夠大')
        self.assertEqual(self._pool_count(self.sa_a), 1, '前提：自己的池 1 條')

        self.authenticate(self.alice.login, self.alice.login)
        res = self._create_policy_rpc(self.ws_a, self.sa_a, 'a-second')
        self.assertTrue(
            res.get('success'),
            '別的 SA 的 5 條不得吃掉 SA-A 的額度（回應：%s）' % res)
        self.assertEqual(self._pool_count(self.sa_a), 2)
        self.assertEqual(
            self._pool_count(self.sa_b), 5, '外池不得被這次請求動到')

    def test_C2_own_pool_still_hits_the_quota(self):
        """自己的池額滿仍照擋——sudo 之後計數仍然正確。"""
        self._seed_policy(self.route_a, 'a-first')
        self._subscribe(self.sa_a, 'A', quota=1)
        before = self._pool_count(self.sa_a)

        self.authenticate(self.alice.login, self.alice.login)
        res = self._create_policy_rpc(self.ws_a, self.sa_a, 'a-overflow')
        self._assert_structured_denial(res)
        self.assertIn('quota', res['error'].lower(),
                      '擋下來了，但不是配額那條：%s' % res['error'])
        self.assertEqual(self._pool_count(self.sa_a), before,
                         '被擋的請求不得留下任何 policy')

    def test_C3_non_member_denied_at_the_gate_and_learns_nothing(self):
        """🔴 gate 要求的 cross-user negative：非成員照樣被擋、且拿不到配額狀態。

        SA-A 已額滿。mallory（完全非成員）打同一個端點：
        必須停在 workspace／role gate（結構化拒絕），⛔ 錯誤訊息**不得**是配額
        那條——那會等於用新 sudo 把「別人 SA 的池滿了沒」漏給未授權呼叫者。
        """
        self._seed_policy(self.route_a, 'a-first')
        self._subscribe(self.sa_a, 'A', quota=1)
        before = self._pool_count(self.sa_a)

        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._create_policy_rpc(self.ws_a, self.sa_a, 'injected')
        self._assert_structured_denial(res)
        self.assertNotIn(
            'quota', res['error'].lower(),
            '非成員不得從錯誤訊息得知 SA-A 的配額狀態（%s）' % res['error'])
        self.assertEqual(self._pool_count(self.sa_a), before,
                         '被拒的請求不得建出任何 policy')
