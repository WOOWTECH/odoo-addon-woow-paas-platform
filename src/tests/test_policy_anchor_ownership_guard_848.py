"""#848：access policy 的三個錨點 Many2one 在 model 層沒有歸屬守線（write 側）。

## 缺口的精確形狀——它是 **write 側專屬**的

框架在 create 與 write 兩側**不對稱**（宿主 checkout `odoo-18.0/odoo/models.py`，
判別事實是「哪個函式裡有、哪個沒有」而不是行號）：

* `BaseModel._create` 的**尾端**對**新建出來的那些列**再跑一次
  `records.check_access('create')` ⇒ 新列不在自己 domain 內就 raise。
* `BaseModel.write` **只有開頭**那一次 `self.check_access('write')`，評估的是
  **寫入前**的狀態，寫入後零複驗。

⇒ 「把既有 policy 搬到別人的錨點上」框架不擋。

## 可利用性落在「共同成員」不是「陌生人」

`security/ir_rules.xml` 三個錨點 model 的 member-read rule
（`security_access_route_rule_workspace_member` / `cloud_service_rule_member_read` /
`smart_home_route_rule_member_read`）**domain 都沒有 role 條件**，且
`perm_write=False`；帶 role 條件的是各自的 `..._owner_admin_manage`。於是：

| 步驟 | 判準 | 結果 |
| --- | --- | --- |
| Mallory 是 workspace **A** 的 owner／admin | 框架 `write()` 開頭對 A 的 policy 做的 `check_access('write')`（**寫入前**狀態） | 過 |
| Mallory 同時是 workspace **B** 的**普通成員** | `write()` 內 `if resync:` 區塊那個 `anchor.check_access('read')` 迴圈 | 過 |
| Mallory 對 A 的 policy 寫 `{'route_id': <B 的 route id>}` | 框架無寫入後 rule 複驗 | **落地** |

⇒ B 的唯讀成員可以往 B 受保護的錨點上注入一條 `allow` / `bypass` 的 policy，
而且搬完之後那列進了 B 的 domain、`perm_write` 要 owner／admin ⇒ **他自己也收不
回來，是單向注入**。

## 為什麼要一道**獨立**的守衛，而不是把那個 `read` 迴圈改成 `write`

那個迴圈的契約是**排序**不是歸屬——它自己的註解逐字寫「所有錨點都授權過，才准
產生第一個 side effect」，目的是不讓「第一個錨點先打 CF、第二個才 AccessError」
造成回捲不掉的外部變更。歸屬把關只是它的**副作用**，改動它的人沒有義務保住這個
性質。本檔存在的理由正是要終止「歸屬契約寄生在別人的排序契約上」。

## 收緊的方向與理由（issue-leader 裁定）

**新錨點也要 owner／admin。** 這不是新政策——**create 側今天就已經是這個語意**：
policy model 上對 `base.group_user` 只有兩條 rule，member 那條
`perm_create=False`／`perm_write=False`，能讓一列新 policy 通過 `_create` 尾端那次
`check_access('create')` 的只有 `..._owner_admin_manage`。write 側只要 `read` 才是
那個例外，本守衛是把 write 對齊 create。

## ⛔ 本守衛**擋不到 #849**

#849 現行機制（留言 9445，端到端 RPC 量測）：寫 `policy.security_access_id` /
`policy.smart_home_id` 直接落在 policy 自己那根 stored column 上，
`policy.route_id` 完全沒動 ⇒ 本守衛不 fire。兩張單是不同的機制、不同的修法
落點，別把本檔讀成順便關掉了 #849。

（曾誤判為 stored related 帶 `inverse` 委派、寫它們會寫到來源列——該說法已於
留言 9433 撤回：這兩欄宣告 `related=` 未顯式覆寫 `readonly`，框架
`_get_attrs` 早把 `readonly` 蓋成 `True`，`setup_related` 不裝 `inverse`，
值不會外溢到來源列。）

## 測試分層

* **A** — write 側可利用性（本單主格）。三個錨點各一格。每一條攻擊**前面**都先跑
  一次同一條 RPC、同一列、租戶合法欄位的寫入當陽性對照：少了它，「攻擊被擋」與
  「Mallory 對這一列根本寫不動」不可區分。
* **B** — create 側的**框架契約**。本單刻意不在 create 加守衛（框架已經擋得住），
  缺的是「我們依賴這個框架行為」這件事沒有任何東西記錄。B1 就是那份記錄。
* **C** — 陽性對照：合法搬移照常過、superuser 路徑刻意豁免、陌生人紅在新守衛、
  以及 member 讀得到但寫不動目標錨點（上表第二列的直接實證）。

🔴 **本檔任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋
`else: self.fail(...)`，不用 `assertRaises`**。Odoo 的 `BaseCase._assertRaises` 把
with 區塊包進 `env.cr.savepoint()`（`cr.clear()` ＋ `ROLLBACK TO SAVEPOINT`）⇒ 區塊
內的寫入被回捲，於是「守衛擋在寫入之前」與「寫入發生過又被回捲」在斷言輸出上不可
區分（本 repo 在 #822 踩過）。

⚠ 本檔在 RED 輪**刻意不 import 也不 patch 任何尚未存在的守衛符號**：RED 要看到的
紅必須落在「值落地了」那條斷言上，`AttributeError` 是另一種紅。C3b／C4（停用守衛
之後的兩格對照）在守衛落地之後才加。
"""

import json
import logging
import sys
import traceback
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.security_access import _skip_route_sync
from ..models.smart_home_route import _sm_skip_access_sync
from .smart_home_fixture import sh_model

_logger = logging.getLogger(__name__)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_CS = 'woow_paas_platform.cloud_service'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'

#: C3b／C4 停用守衛用的 patch 標的。守衛是 **module-level function**、
#: `write()` 以模組全域名稱呼叫它 ⇒ patch 這個名字就等於把守衛 body 拿掉，
#: 而且**同一個 process 內的 HTTP server thread 也吃得到**（RPC 那格靠這點）。
_GUARD = ('odoo.addons.woow_paas_platform.models.'
          'security_access_access_policy._assert_anchor_move_authorised')


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 848' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _AnchorOwnership848Fixture(HttpCase):
    """兩個 workspace：A（Mallory 是 owner）與 B（Mallory 只是普通成員）。

    B 底下三個錨點各一個（route／cloud service／smart home route），因為三條
    member-read rule 的 domain 全都沒有 role 條件 ⇒ 缺口三個錨點都成立。

    兩個 SA 都刻意**不給 `tunnel_id`**：`_access_sync_skip_reason()` 的第一道就是
    「the Security Access has no live tunnel」⇒ 本檔任何 create／write 都不會真的
    打 operator（否則測試會紅在網路上，那不是本檔要驗的分支）。
    錨點也刻意**不給訂閱**：`_anchor_plan()` 回空 ⇒ `_check_policy_quota` 直接
    continue，配額不會變成一道我沒預期到的門。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]

        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory848')
        self.assertFalse(
            self.mallory.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user（%s 撞了）' % self.mallory.login)
        self.bob, _ = _new_group_user(self.env, 'bob848')

        # workspace A —— Mallory 是 owner
        self.ws_a = self._workspace('WS-A 848', self.mallory)
        # workspace B —— Bob 是 owner，Mallory 只是**普通成員**
        self.ws_b = self._workspace(
            'WS-B 848', self.bob, {self.mallory: 'user'})

        self.sa_a, self.route_a = self._sa_route(self.ws_a, 'a')
        self.route_a2 = self._route(self.sa_a, 'a2')
        self.sa_b, self.route_b = self._sa_route(self.ws_b, 'b')
        self.cs_b = self._cloud_service(self.ws_b, 'b')
        self.sh_route_b = self._sh_route(self.ws_b, 'b')

        self.pol = self._policy(self.route_a, 'seeded')

    # ------------------------------------------------------------------ #
    #  Fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _workspace(self, name, owner, extra_members=None):
        """建一個 workspace，owner 是 `owner`，其餘成員照 `extra_members` 指派。

        ⚠ **owner 只能由 `with_user()` 決定，不能事後補一列 `role='owner'`**：
        `Workspace.create()` 會依 `owner_id`（`default=lambda self: self.env.user`）
        自動 bootstrap 一列 owner 的 `workspace_access`，而
        `workspace_access._check_owner_count` 限制一個 workspace 只能有一個
        owner ⇒ 事後再補會撞
        「A workspace can only have one owner」（RED 第一輪實撞）。
        """
        ws = self.Workspace.with_user(owner).create(
            {'name': '%s %s' % (name, uuid.uuid4().hex[:6])})
        self.assertEqual(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('role', '=', 'owner')]).user_id,
            owner,
            '前提：bootstrap 出來的 owner 必須是 %s' % owner.login)
        for user, role in (extra_members or {}).items():
            self.WorkspaceAccess.sudo().create({
                'workspace_id': ws.id, 'user_id': user.id, 'role': role})
        return ws.sudo()

    def _sa_route(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 848 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        return sa, self._route(sa, tag)

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r848%s-%s' % (tag, uuid.uuid4().hex[:6]),
        }).with_context(sa_skip_route_sync=False)

    def _cloud_service(self, workspace, tag):
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'T848 %s' % tag,
            'slug': 't848%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/t848',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        return _internal_provision(self.env[_CS].sudo()).create({
            'name': 'svc-848-%s' % tag,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
        })

    def _sh_route(self, workspace, tag):
        sh = sh_model(self.env).create({
            'name': 'sh-848-%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'workspace_id': workspace.id,
            'state': 'active',
        }).with_env(self.env)
        return _sm_skip_access_sync(self.env[_SH_ROUTE].sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})

    def _policy(self, route, name):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'route_id': route.id,
            'name': 'pol-848-%s' % name,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })
        self.assertFalse(
            pol.cf_policy_id,
            '前提：policy 不得帶 cf_policy_id，否則 `_apply_anchor_move()` 會去碰'
            ' admin-only 的 cf_access_policy_reap，多出一道我沒預期的門')
        return pol.with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    #  RPC helpers                                                        #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。

        `with_user()` ＋ `assertRaises` 比真 RPC 弱兩格：`TransactionCase` 預設是
        su env（任何 `env.su` 相關的分支在那裡整條跳過），而
        `odoo/api.py::call_kw` 把**呼叫端 context 整包**換進 env，in-process
        呼叫模擬不到。
        """
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open(
            '/web/dataset/call_kw', data=body,
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.status_code, resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, status, result, msg):
        self.assertEqual(status, 200, msg + '（HTTP %s）' % status)
        self.assertNotIn(
            'error', result, msg + '（回 %s）' % self._error_text(result))
        return result.get('result')

    def _db(self, model_name, record_id, field_name):
        """直接讀 Postgres 的那一格。

        `flush_all()` 先把尚未 flush 的寫入推下去——「沒有例外」與「沒有寫入」是
        兩個主張，不先 flush 會讓前者讀起來像後者。
        """
        self.env.flush_all()
        self.assertIn(field_name, self.env[model_name]._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.env[model_name]._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '記錄 %s#%s 不在 DB 裡' % (model_name, record_id))
        return row[0]

    def _tenant_write_positive_control(self, policy_id, tag):
        """同一條 RPC、同一列、租戶合法欄位——攻擊格前面的必備陽性對照。

        少了它，「攻擊被擋」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
        """
        new_name = 'renamed-by-tenant-848-%s' % tag
        status, result = self._call_kw(
            _POLICY, 'write', [[policy_id], {'name': new_name}])
        self._assert_ok(status, result,
                        '陽性對照：租戶對自己 workspace 內的 policy 有合法寫入權')
        self.assertEqual(
            self._db(_POLICY, policy_id, 'name'), new_name,
            '陽性對照：合法欄位必須真的落地，否則本測試零鑑別力')

    def _attempt_anchor_move(self, policy_id, vals, tag):
        """跑一次錨點搬移的 RPC，並把**量到的**原始輸出記進 log。

        HTTP 狀態碼與攻擊後 DB 讀到的錨點值都是量測值（不是字面量）——本單可
        利用性的原始證據就是這兩個數。
        """
        status, result = self._call_kw(_POLICY, 'write', [[policy_id], vals])
        landed = {f: self._db(_POLICY, policy_id, f)
                  for f in ('route_id', 'cloud_service_id',
                            'smart_home_route_id')}
        _logger.info(
            '[848][%s] RAW anchor-move RPC → http=%s error=%r landed=%r',
            tag, status, self._error_text(result) if 'error' in result else None,
            landed)
        return status, result, landed


class TestPolicyAnchorMoveRpc848(_AnchorOwnership848Fixture):
    """A：共同成員走真 RPC 把 policy 搬到自己不是 owner／admin 的錨點上。"""

    def test_A1_common_member_cannot_move_policy_onto_foreign_route(self):
        """🔴 本單主格，也是 #848 留言 9222 自陳「結構推論、未實跑」的那一格。

        危害不需要知道任何祕密：Mallory 是 wsB 的**普通成員**（`role='user'`），
        wsB 的 route id 對她本來就是可讀的。搬完之後那條 `allow` policy 就掛在
        wsB 受保護的 route 上，而她自己再也收不回來（`perm_write` 要 owner／
        admin）——單向注入。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.pol.id, 'a1')

        status, result, landed = self._attempt_anchor_move(
            self.pol.id, {'route_id': self.route_b.id}, 'A1')

        self.assertEqual(
            landed['route_id'], self.route_a.id,
            '#848：wsB 的普通成員不得把 wsA 的 policy 搬到 wsB 的 route 上'
            '——實際落地 route_id=%r（wsB 的 route 是 %r），原值 %r'
            % (landed['route_id'], self.route_b.id, self.route_a.id))
        self.assertIn(
            'error', result,
            '#848：守衛必須**大聲**拒絕（靜默丟棄會讓呼叫端以為搬成功了）；'
            '實際 HTTP %s' % status)
        self.assertIn(
            'AccessError', self._error_text(result),
            '#848：拒絕的類型必須是 AccessError（授權不足），實際 %r'
            % self._error_text(result))

    def test_A2_common_member_cannot_move_policy_onto_foreign_cloud_service(self):
        """同一個缺口的第二個錨點——`cloud_service_rule_member_read` 的 domain
        同樣沒有 role 條件。

        錨點是 **exclusive arc**（恰好一個），所以跨 model 搬要同時清掉舊的那一
        個；只驗 route 那格的話，一個「只認 `route_id`」的守衛會綠。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.pol.id, 'a2')

        status, result, landed = self._attempt_anchor_move(
            self.pol.id,
            {'route_id': False, 'cloud_service_id': self.cs_b.id}, 'A2')

        self.assertFalse(
            landed['cloud_service_id'],
            '#848：普通成員不得把 policy 搬到 wsB 的 Cloud Service 上'
            '——實際落地 cloud_service_id=%r' % (landed['cloud_service_id'],))
        self.assertEqual(
            landed['route_id'], self.route_a.id,
            '#848：被拒之後舊錨點必須原封不動（實際 %r）' % (landed['route_id'],))
        self.assertIn('error', result,
                      '#848：守衛必須大聲拒絕（實際 HTTP %s）' % status)

    def test_A3_common_member_cannot_move_policy_onto_foreign_smart_home_route(self):
        """第三個錨點——`smart_home_route_rule_member_read` 同型。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.pol.id, 'a3')

        status, result, landed = self._attempt_anchor_move(
            self.pol.id,
            {'route_id': False, 'smart_home_route_id': self.sh_route_b.id},
            'A3')

        self.assertFalse(
            landed['smart_home_route_id'],
            '#848：普通成員不得把 policy 搬到 wsB 的 Smart Home Route 上'
            '——實際落地 smart_home_route_id=%r'
            % (landed['smart_home_route_id'],))
        self.assertEqual(
            landed['route_id'], self.route_a.id,
            '#848：被拒之後舊錨點必須原封不動（實際 %r）' % (landed['route_id'],))
        self.assertIn('error', result,
                      '#848：守衛必須大聲拒絕（實際 HTTP %s）' % status)


class TestPolicyAnchorCreateBackstop848(_AnchorOwnership848Fixture):
    """B：create 側**刻意不加守衛**——這一格是把框架行為當契約釘住。"""

    def test_B1_framework_create_backstop_blocks_foreign_anchor(self):
        """🔴 **這條紅了代表框架行為變了，不是我們的碼壞了。**

        本單不在 `create()` 加歸屬守衛，因為框架已經擋得住：`BaseModel._create`
        的尾端對**新建出來的列**再跑一次 `check_access('create')`，而 policy
        model 上對 `base.group_user` 唯一 `perm_create=True` 的 rule 是
        `security_access_access_policy_rule_owner_admin_manage`（member 那條是
        `perm_create=False`）⇒ 新列的錨點在 wsB 就過不了。

        缺的從來不是守衛，是**契約**——在本檔之前沒有任何東西記錄「我們依賴這個
        框架行為」。升 Odoo 版本而這一格紅了，要改的是這個依賴，不是去修守衛。

        順帶：它也是 §「收緊成對稱」的依據——create 側今天就已經要求對錨點有
        owner／admin，write 側只要 `read` 才是那個例外。
        """
        self.authenticate(self.mallory_login, self.mallory_login)

        # 陽性對照：同一條 RPC、自己 workspace 的錨點必須建得起來。
        ok_status, ok_result = self._call_kw(_POLICY, 'create', [{
            'route_id': self.route_a.id,
            'name': 'b1-own-848',
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'b@example.com'})],
        }])
        own_id = self._assert_ok(
            ok_status, ok_result,
            '陽性對照：租戶對自己 workspace 的錨點必須建得出 policy')
        self.assertEqual(
            self._db(_POLICY, own_id, 'route_id'), self.route_a.id,
            '陽性對照：合法 create 必須真的落地，否則下面那格零鑑別力')

        status, result = self._call_kw(_POLICY, 'create', [{
            'route_id': self.route_b.id,
            'name': 'b1-foreign-848',
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'c@example.com'})],
        }])
        self.assertIn(
            'error', result,
            '框架契約：帶他人錨點的 create 必須被 `_create` 尾端那次'
            ' `check_access(\'create\')` 擋下（實際 HTTP %s）' % status)
        self.env.flush_all()
        self.assertFalse(
            self.Policy.sudo().search(
                [('name', '=', 'b1-foreign-848')]),
            '框架契約：被拒的 create 不得留下任何列')


class TestPolicyAnchorMovePositiveControls848(_AnchorOwnership848Fixture):
    """C：守衛不得擋掉合法路徑，也不得取代既有防線。"""

    def test_C1_owner_admin_can_move_policy_within_own_workspace(self):
        """合法搬錨點照常過：Mallory 對 wsA 的兩條 route 都是 owner。

        這一格紅了代表守衛把正常操作一起擋掉了——那是本單最大的回歸面。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.pol.id, 'c1')

        status, result, landed = self._attempt_anchor_move(
            self.pol.id, {'route_id': self.route_a2.id}, 'C1')

        self._assert_ok(status, result,
                        '同一個 workspace 內的錨點搬移必須照常成功')
        self.assertEqual(
            landed['route_id'], self.route_a2.id,
            '合法搬移的值必須真的落地（實際 %r）' % (landed['route_id'],))

    def test_C2_superuser_anchor_move_is_deliberately_exempt(self):
        """su 豁免是**刻意的**，這裡把它寫成被斷言的契約。

        判準用 `check_access('write')` 打在新錨點上，而 `check_access()` 在
        `env.su` 下自動 no-op（`BaseModel.check_access` 的 `if not self.env.su`）；
        migration `18.0.1.0.37` 那條合法的錨點搬移正是以 superuser 跑
        （`api.Environment(cr, SUPERUSER_ID, {})`，而 `Environment.__new__` 對
        `uid == SUPERUSER_ID` 直接設 `su = True`）。

        ⚠ 這與 #798 的 `cf_policy_id` 守衛**結論相反**，而那不是疏漏：那一格的
        不變量是「值不可變」（**沒有**合法改值路徑，任何 su 豁免都是洞），本格的
        不變量是「值轉移受限」（**有**合法改值路徑，只是要 gated）。

        su 豁免的代價是守衛的 deny 分支在 su env 下結構上不可達——本檔因此把
        攻擊格全部放在真 RPC 的非 su 租戶身上，並用這一格把豁免本身釘住。
        """
        pol = self._policy(self.route_a, 'c2')
        # migration 37 的形狀：superuser ＋ `sa_skip_route_sync`。
        _skip_route_sync(pol).write({'route_id': self.route_b.id})

        self.assertEqual(
            self._db(_POLICY, pol.id, 'route_id'), self.route_b.id,
            'su 路徑的錨點搬移必須照常落地——擋掉它等於擋掉 migration 37')

    def test_C3a_stranger_move_is_rejected_by_the_new_guard(self):
        """陌生人（完全非成員）搬錨點：守衛在的時候，紅**必須來自新守衛**。

        ⚠ 工單原本要求這一格「紅在既有的排序迴圈上」。那個要求照字面不可能成立：
        新守衛跑在 `super().write()` **之前**，而陌生人對目標錨點的
        `check_access('write')` 同樣不過 ⇒ 陌生人一定先撞到新守衛，那個排序迴圈
        在這個案例裡不可達。issue-leader 已裁定改成本格 ＋ C3b 兩格（C3b 在守衛
        落地後才加）：本格誠實記錄「紅在新守衛」，C3b 把守衛停用後證明既有防線
        仍在原地做它原本的事。

        判準用 traceback 裡的**函式名存在性**，不用行號——#854 剛把這個檔的行號
        整段推移過，行號判準會靜默失效。
        """
        carol, _ = _new_group_user(self.env, 'carol848')
        ws_c = self._workspace('WS-C 848', carol)
        _, route_c = self._sa_route(ws_c, 'c')
        pol = self._policy(route_c, 'c3a')

        # 前提：Carol 對 wsB 完全沒有成員資格。
        self.assertFalse(
            self.WorkspaceAccess.sudo().search(
                [('workspace_id', '=', self.ws_b.id),
                 ('user_id', '=', carol.id)]),
            '前提：Carol 不得是 wsB 的成員，否則這一格測的是 A1 不是陌生人')

        try:
            pol.with_user(carol).write({'route_id': self.route_b.id})
        except AccessError:
            frames = traceback.extract_tb(sys.exc_info()[2])
        else:
            self.fail('陌生人搬錨點必須被拒（既有防線與新守衛任一擋下都算）')

        self.assertEqual(
            self._db(_POLICY, pol.id, 'route_id'), route_c.id,
            '被拒之後 DB 那一格必須仍是舊錨點')
        names = [f.name for f in frames]
        self.assertIn(
            '_assert_anchor_move_authorised', names,
            '#848：守衛在的時候，陌生人這一格必須紅在**新守衛**上'
            '——實際 traceback 的 frame 依序為 %r' % (names,))

    def test_C3b_existing_ordering_loop_still_catches_the_stranger(self):
        """把新守衛停用之後，陌生人**必須仍然紅在既有的那個排序迴圈上**。

        這一格才是真正在證「既有防線沒有被我改壞、也沒有被取代掉」。C3a 證的是
        「守衛在的時候誰先攔下」——那只說明順序。issue-leader 已裁定用這兩格取代
        工單原本那條（原條要求陌生人紅在既有迴圈上，但守衛跑在 `super().write()`
        之前、陌生人對目標錨點的 `check_access('write')` 同樣不過 ⇒ 那個迴圈在該
        案例裡結構上不可達，原條照字面不可能成立）。

        判準用 frame 的**原始碼內容**（`check_access('read')`）而不是行號：#854 剛把
        這個檔的行號整段推移過，行號判準會靜默失效。
        """
        carol, _ = _new_group_user(self.env, 'carol848b')
        ws_c = self._workspace('WS-C 848b', carol)
        _, route_c = self._sa_route(ws_c, 'cb')
        pol = self._policy(route_c, 'c3b')

        with patch(_GUARD):
            try:
                pol.with_user(carol).write({'route_id': self.route_b.id})
            except AccessError:
                frames = traceback.extract_tb(sys.exc_info()[2])
            else:
                self.fail(
                    '新守衛停用之後，既有的排序迴圈仍必須擋下陌生人——這一格若通過'
                    '代表本單把既有防線改壞或取代掉了')

        self.assertEqual(
            self._db(_POLICY, pol.id, 'route_id'), route_c.id,
            '被拒之後 DB 那一格必須仍是舊錨點')
        names = [f.name for f in frames]
        self.assertNotIn(
            '_assert_anchor_move_authorised', names,
            '前提：本格必須是在守衛**停用**的狀態下量到的（實際 frame %r）'
            % (names,))
        self.assertTrue(
            any(f.name == 'write' and "check_access('read')" in (f.line or '')
                for f in frames),
            '#848：擋下陌生人的必須是 `write()` 裡那個 `check_access(\'read\')` '
            '迴圈——實際 frame 為 %r'
            % ([(f.name, f.line) for f in frames],))

    def test_C4_injection_control_disabling_the_guard_lets_the_attack_land(self):
        """🔴 注入對照：把守衛停用，A1 的攻擊必須**翻回成功**。

        少了這一格，A1 的綠與「A1 根本沒打到守衛」（fixture 走偏、RPC 沒到、錨點
        id 打錯）在輸出上不可區分——那正是本 repo 反覆踩到的假綠形狀。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.pol.id, 'c4')

        with patch(_GUARD):
            status, result, landed = self._attempt_anchor_move(
                self.pol.id, {'route_id': self.route_b.id}, 'C4')

        self._assert_ok(
            status, result,
            '注入對照：守衛停用後這條 RPC 必須成功（它是 A1 的同一條）')
        self.assertEqual(
            landed['route_id'], self.route_b.id,
            '注入對照：守衛停用後攻擊必須落地——沒落地代表 A1 的綠不是守衛造成的'
            '（實際落地 %r）' % (landed['route_id'],))


class TestPolicyAnchorRuleAsymmetry848(_AnchorOwnership848Fixture):
    """C5：留言 9222 那張表第二列的直接實證——member 讀得到、但寫不動。"""

    def test_C5_member_can_read_but_not_write_the_foreign_anchor(self):
        """`read` 過、`write` 不過，正是可利用性落在的那個差。

        它同時說明為什麼 RED 的落點是乾淨的：既有那個排序迴圈用的是 `read`，在
        A1 的案例裡它是**通過**的 ⇒ A1 的紅只可能來自新守衛。
        """
        route_b_as_mallory = self.route_b.with_user(self.mallory)
        # 讀得到：member-read rule 的 domain 沒有 role 條件。
        route_b_as_mallory.check_access('read')

        try:
            route_b_as_mallory.check_access('write')
        except AccessError:
            pass
        else:
            self.fail(
                'wsB 的普通成員不該對 wsB 的 route 有 write——這一格若通過，'
                '本單用 `check_access(\'write\')` 當判準的前提就不成立')
