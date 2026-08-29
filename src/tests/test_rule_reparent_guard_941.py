"""#941：rule 子模型的真錨點 `policy_id` 在 model 層沒有歸屬守線（write 側）。

## 缺口的精確形狀——與 #848 同型，只是落在**另一個 model 的另一個欄位**

`security_access_access_policy_rule.write()` 對 `policy_id` 零 authz 把關：

* 欄位層：`required=True` 只禁 NULL、`ondelete='cascade'` 只管刪除，無 `domain=`。
* #929 的 `_reject_derived_projection_writes` 只逐 key 比對
  `_RULE_PROJECTION_FIELDS`，而 `policy_id` 是那張 map 的 **value（來源欄位）**、
  不是 key ⇒ `{'policy_id': ...}` 原封放行。
* ir.rule：框架 `BaseModel.write` **只有開頭**那一次 `check_access('write')`，
  評估的是**寫入前**的狀態（那時這列還掛在攻擊者自己的 policy 上 ⇒ 過），
  `super().write()` 落地後零複驗 ⇒ 搬進他人 workspace 之後沒有人再問一次。
  對照 `BaseModel._create` **尾端**對新建列再跑一次 `check_access('create')`
  ——create 有 post-check、write 沒有，這就是不對稱。

⇒ 「把既有 rule 搬到他人的 policy 底下」框架不擋，rule 側也沒有 #848 的對應物
（`_assert_anchor_move_authorised` 只接在 `policy.write()`、只跑 policy 的三個
真錨點 `_ANCHOR_FIELDS`）。

## 比 #848 多一格嚴重度：`policy_id` 就在 resync 觸發集合裡

`rule.write()` 的 resync 條件含 `'policy_id'`，而 `policies` 是**舊 ∪ 新**
⇒ 一次成功的 reparent 不只造成歸屬分裂，還會對**受害者的錨點**呼叫
`_sync_access_to_operator()`，把含注入 selector 的 full-state 推進受害者的
Cloudflare Access application（在受害者錨點的 liveness gate 通過時）。
A5 因此把「守衛擋下之後受害者的錨點一次都沒被 sync 呼叫」斷言成契約。

## 🔴 本檔最容易假綠的那一格，與五道對策

`rule.write()` 在 `super().write()` **之後**對「舊 ∪ 新 policy」連跑五個
validator，每一個都拋 `ValidationError`：`_check_at_least_one_include` /
`_check_emails_per_policy` / `_check_decision_selector_compat` /
`_check_allow_policy_no_bulk_grant` / `_check_access_seat_quota`
⇒ **只斷言「有拋例外」的攻擊格，在守衛不存在時一樣會綠。** 對策：

1. RED 輪先觀測到攻擊**成功**（`flush_all()` ＋裸 `SELECT` 讀到 `policy_id`
   真的落成 victim policy），**在守衛存在之前**。
2. C4「patch 掉守衛、攻擊必須翻回成功且值落地」——唯一能區分「我的守衛擋的」
   與「某條 constraint 擋的」的判別式。
3. 🔴 **斷言 exception class**：constraint 拋 `ValidationError`、#929 投影欄
   守衛拋 `UserError`、本守衛拋 `AccessError`，三類互斥。
   ⚠ 只斷言 class **不**斷言訊息字串（守衛跑在框架那次 `check_access('write')`
   之前，訊息由 `check_access` 自己產生、與 ir.rule 綁在一起）。
4. 每格攻擊前跑 `_tenant_write_positive_control`。
5. fixture 挑「五個 validator 全過」的組合，**驗法＝先在攻擊者自己的 workspace
   內做同一次搬家並看它成功**（那一格就是 C1）：
   - 搬的是 P_A 的**第二條** include rule ⇒ 來源 policy 仍有 include。
   - 兩邊 policy 都 `decision='allow'`、搬的是 `email_list` ⇒ 不撞
     `_check_decision_selector_compat` / `_check_allow_policy_no_bulk_grant`。
   - 錨點一律**不給訂閱** ⇒ `_anchor_plan()` 為空 ⇒ `_check_emails_per_policy`
     與 `_check_access_seat_quota` 都在 `if not plan: continue` 直接放行
     （席位那條在 #773 已把計數拆進 `_seat_usage_for_anchor()` /
     `_seat_quota_violation()`，但 `_check_access_seat_quota` 開頭的
     grandfather 分支不變 ⇒ 無訂閱仍然整條跳過）。

## 紀律

🔴 任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋
`else: self.fail(...)`，**不用 `assertRaises`**——Odoo 的 `BaseCase._assertRaises`
在進入 with 區塊前開一顆 flushing savepoint，例外穿出即 `ROLLBACK TO SAVEPOINT`
＋ `cr.clear()`，回捲邊界恰好是整個 with 區塊 ⇒ 區塊內寫的東西全部消失，回捲值
剛好等於期望值時假綠（`odoo-assertraises-savepoint-rollback.md`，#822 實撞）。

⚠ 本檔有**兩格刻意自己開 savepoint**（E1／B1b）：那兩條被測路徑會**先送出 SQL
再丟例外**（`_create` 是先 INSERT 後 post-check；清空 `policy_id` 是 flush 時撞
NOT NULL），不包 savepoint 會讓 transaction 進 aborted 狀態、後面所有查詢
`InFailedSqlTransaction`。代價是它們的 DB 狀態被回捲 ⇒ 這兩格**只主張 exception
class**，⛔ 不對它們做「值沒落地」那種回捲後恆真、鑑別力為零的否定式斷言；
DB 證據由同組不回捲的 RPC 那半提供。

判準用 traceback 的**函式名存在性**，⛔ 不用行號。

攻擊一律走真 RPC `/web/dataset/call_kw`，⛔ 不用 `with_user()` ＋ ORM：
`TransactionCase` 預設 su env（`env.su` 相關分支整條跳過），而
`odoo/api.py::call_kw` 把呼叫端 context 整包換進 env，in-process 呼叫模擬不到。

## RED 輪量到的東西（守衛尚未存在時，CI run 5846，`[941]` 指紋逐行）

本檔在 RED 輪**刻意不 import 也不 patch 任何尚未存在的守衛符號**——紅必須落在
「值落地了」那條斷言上，`AttributeError` 是另一種紅。量到的原始值：

```
[941][A1] RAW reparent RPC → http=200 error=None landed_policy_id=406   ← 落地
[941][A2] ... landed_policy_id=412        （victim policy 錨在 cloud service）
[941][A3] ... landed_policy_id=418        （victim policy 錨在 smart home route）
[941][A4] RAW x2many-push RPC → http=200 error=None landed_policy_id=421
[941][A5] ... landed_policy_id=426 / synced_route_ids=[1022, 1024]  ← 含受害者的 route
```

⇒ 這同時回答了 #941 留言 10714 掛著的那件事：**端到端可利用**，兩個 incidental
因素（sync-time read、per-payload 交互）在本 fixture 的形狀下都沒有擋住它。
B1／B1b 在同一趟就已經是 `AccessError` ⇒ create 側的框架 backstop 成立。

C3a／C3b／C4（守衛的 traceback 判準與注入對照）在守衛落地之後才加，本節之後的
`_GUARD` 就是那三格用的 patch 標的。

Refs #848 #929 #849
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
_RULE = 'woow_paas_platform.security_access_access_policy_rule'
_CS = 'woow_paas_platform.cloud_service'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'

#: C3b／C4 停用守衛用的 patch 標的。守衛是 **module-level function**、`write()` 以
#: 模組全域名稱呼叫它 ⇒ patch 這個名字就等於把守衛 body 拿掉，而且**同一個 process
#: 內的 HTTP server thread 也吃得到**（走真 RPC 的 C4 靠的正是這一點）。
_GUARD = ('odoo.addons.woow_paas_platform.models.'
          'security_access_access_policy_rule._assert_rule_reparent_authorised')


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 941' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _RuleReparent941Fixture(HttpCase):
    """兩個 workspace：A（Mallory 是 owner）與 B（Mallory 只是普通成員）。

    wsB 底下三種錨點形狀的 victim policy 各一個（route／cloud service／smart
    home route），因為 policy 的 `..._owner_admin_manage` ir.rule domain 是三腿
    OR ⇒ 守衛的 deny 不該隨 victim policy 的錨點形狀而異。

    三個「不是本檔要驗的門」各自關掉：
    * SA 一律**不給 `tunnel_id`** ⇒ `_access_sync_skip_reason()` 第一道就是
      「no live tunnel」，本檔任何 create／write 都不會真的打 operator。
    * 錨點一律**不給訂閱** ⇒ `_anchor_plan()` 回空 ⇒ email 上限與席位配額兩條
      constraint 直接 `continue`。
    * policy 一律**不帶 `cf_policy_id`** ⇒ `_apply_anchor_move()` 不會去碰
      admin-only 的 `cf_access_policy_reap`。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.Rule = self.env[_RULE]

        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory941')
        self.assertFalse(
            self.mallory.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user，否則 ir.rule 整個不生效（假綠）')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob941')

        # workspace A —— Mallory 是 owner
        self.ws_a = self._workspace('WS-A 941', self.mallory)
        # workspace B —— Bob 是 owner，Mallory 只是**普通成員**
        self.ws_b = self._workspace('WS-B 941', self.bob, {self.mallory: 'user'})

        self.sa_a, self.route_a = self._sa_route(self.ws_a, 'a')
        self.sa_a2, self.route_a2 = self._sa_route(self.ws_a, 'a2')
        self.sa_b, self.route_b = self._sa_route(self.ws_b, 'b')
        self.cs_b = self._cloud_service(self.ws_b, 'b')
        self.sh_route_b = self._sh_route(self.ws_b, 'b')

        # 攻擊者自己的兩個 policy（P_A 是來源，P_A2 是 C1 的合法目的地）
        self.pol_a = self._policy(self.route_a, 'a')
        self.pol_a2 = self._policy(self.route_a2, 'a2')
        # 三種錨點形狀的 victim policy
        self.pol_b_route = self._policy(self.route_b, 'b-route')
        self.pol_b_cs = self._policy(
            self.cs_b, 'b-cs', anchor_field='cloud_service_id')
        self.pol_b_sh = self._policy(
            self.sh_route_b, 'b-sh', anchor_field='smart_home_route_id')

        # 🔴 被搬的是 P_A 的**第二條** include rule：搬走唯一那條會撞
        # `_check_at_least_one_include`，那是另一道不變量、與本檔無關
        # （#849 的 L4 為了同一個理由也是搬第二條）。
        self.rule_a = self._extra_include_rule(self.pol_a, 'ra')
        self.assertEqual(
            len(self.pol_a.sudo().rule_ids.filtered(
                lambda r: r.group == 'include')), 2,
            '前提：P_A 必須有兩條 include rule，否則搬走一條會紅在'
            ' `_check_at_least_one_include`（那不是本檔要驗的門）')
        self.assertEqual(
            self._db(_RULE, self.rule_a.id, 'policy_id'), self.pol_a.id,
            '前提：R_A 一開始必須掛在 P_A 底下')

    # ------------------------------------------------------------------ #
    #  Fixture helpers                                                    #
    # ------------------------------------------------------------------ #
    def _workspace(self, name, owner, extra_members=None):
        """建 workspace；owner 只能由 `with_user()` 決定，不能事後補一列。

        `Workspace.create()` 會依 `owner_id` 自動 bootstrap 一列 owner 的
        `workspace_access`，而 `_check_owner_count` 限制一個 workspace 只能有
        一個 owner ⇒ 事後再補會撞「A workspace can only have one owner」。
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
            {'name': 'SA 941 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：SA 不得有訂閱，否則 email 上限／席位配額會變成我沒預期的門')
        return sa, self._route(sa, tag)

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r941%s-%s' % (tag, uuid.uuid4().hex[:6]),
        }).with_context(sa_skip_route_sync=False)

    def _cloud_service(self, workspace, tag):
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'T941 %s' % tag,
            'slug': 't941%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/t941',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        cs = _internal_provision(self.env[_CS].sudo()).create({
            'name': 'svc-941-%s' % tag,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
        })
        self.assertFalse(
            cs.sudo().subscription_id,
            '前提：Cloud Service 不得有訂閱（同 SA 的理由）')
        return cs

    def _sh_route(self, workspace, tag):
        sh = sh_model(self.env).create({
            'name': 'sh-941-%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'workspace_id': workspace.id,
            'state': 'active',
        }).with_env(self.env)
        self.assertFalse(
            sh.sudo().subscription_id,
            '前提：Smart Home 不得有訂閱（同 SA 的理由）')
        return _sm_skip_access_sync(self.env[_SH_ROUTE].sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})

    def _policy(self, anchor, tag, anchor_field='route_id'):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            anchor_field: anchor.id,
            'name': 'pol-941-%s' % tag,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'seed-%s@example.com' % tag})],
        })
        self.assertFalse(
            pol.cf_policy_id,
            '前提：policy 不得帶 cf_policy_id，否則 `_apply_anchor_move()` 會去碰'
            ' admin-only 的 cf_access_policy_reap，多出一道我沒預期的門')
        self.assertFalse(
            pol._anchor_plan(),
            '前提：`_anchor_plan()` 必須為空 ⇒ email 上限與席位配額直接 continue')
        return pol.with_context(sa_skip_route_sync=False)

    def _extra_include_rule(self, policy, tag):
        """在 `policy` 底下多加一條 include email_list rule（被搬的那一條）。"""
        return _skip_route_sync(self.Rule.sudo()).create({
            'policy_id': policy.id,
            'group': 'include',
            'rule_type': 'email_list',
            'rule_value': 'r941-%s@example.com' % tag,
        }).with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    #  RPC helpers                                                        #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。"""
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
        """直接讀 Postgres 的那一格——繞過 ir.rule，且先 `flush_all()`。

        用 ORM 讀會把「這一列已經被判給別人、我讀不到」誤讀成「寫入被丟棄」；
        不先 flush 則會把「還沒 flush」誤讀成「沒有寫入」。
        """
        self.env.flush_all()
        self.assertIn(field_name, self.env[model_name]._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.env[model_name]._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '記錄 %s#%s 不在 DB 裡' % (model_name, record_id))
        return row[0]

    def _tenant_write_positive_control(self, rule_id, tag):
        """同一條 RPC、同一列、租戶合法欄位——攻擊格前面的必備陽性對照。

        少了它，「攻擊被擋」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
        """
        value = 'pc-941-%s@example.com' % tag
        status, result = self._call_kw(
            _RULE, 'write', [[rule_id], {'rule_value': value}])
        self._assert_ok(
            status, result,
            '陽性對照：租戶對自己 workspace 內的 rule 有合法寫入權')
        self.assertEqual(
            self._db(_RULE, rule_id, 'rule_value'), value,
            '陽性對照：合法欄位必須真的落地，否則本測試零鑑別力')

    def _attempt_reparent(self, rule_id, vals, tag, model=_RULE, method='write'):
        """跑一次 reparent 的 RPC，把**量到的**原始輸出記進 log。

        HTTP 狀態碼與攻擊後 DB 讀到的 `policy_id` 都是量測值（不是字面量）
        ——本單可利用性的原始證據就是這兩個數。log 前綴 `[941]` 是
        `odoo-tests.yml` 的已知指紋，job log 會逐行留下來。
        """
        status, result = self._call_kw(model, method, [[rule_id], vals])
        landed = self._db(_RULE, rule_id, 'policy_id')
        _logger.info(
            '[941][%s] RAW reparent RPC → http=%s error=%r landed_policy_id=%r',
            tag, status,
            self._error_text(result) if 'error' in result else None, landed)
        return status, result, landed


class TestRuleReparentRpc941(_RuleReparent941Fixture):
    """A：wsB 的普通成員走真 RPC 把自己的 rule 搬進 wsB 的 policy 底下。"""

    def _assert_blocked(self, tag, victim_policy, status, result, landed):
        self.assertEqual(
            landed, self.pol_a.id,
            '#941：wsB 的普通成員不得把 wsA 的 rule 搬進 wsB 的 policy'
            '——實際落地 policy_id=%r（victim policy 是 %r，原值 %r）'
            % (landed, victim_policy.id, self.pol_a.id))
        self.assertIn(
            'error', result,
            '#941：守衛必須**大聲**拒絕（靜默丟棄會讓呼叫端以為搬成功了）；'
            '實際 HTTP %s' % status)
        text = self._error_text(result)
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        # 🔴 exception class 是本檔唯一有鑑別力的訊息判準：五個 validator 拋
        # ValidationError、#929 投影欄守衛拋 UserError、本守衛拋 AccessError。
        self.assertIn(
            'AccessError', text,
            '#941[%s]：拒絕的類型必須是 AccessError（授權不足）。若量到的是'
            ' ValidationError 代表擋它的是 rule.write() 之後那五個 validator'
            ' 之一，不是歸屬守衛 ⇒ 這一格零鑑別力。實際 %r' % (tag, text))

    def test_A1_common_member_cannot_move_rule_into_foreign_route_policy(self):
        """🔴 本單主格：把 R_A 搬進 wsB 的 route-anchored policy。

        危害不需要知道任何祕密：Mallory 是 wsB 的**普通成員**（`role='user'`），
        wsB 的 policy id 對她本來就是可讀的。搬完之後那條 include selector 就
        進了 wsB 受保護 route 的 Access application，而她自己再也收不回來
        （rule 的 `perm_write` 在 wsB 要 owner／admin）——單向注入。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'a1')

        status, result, landed = self._attempt_reparent(
            self.rule_a.id, {'policy_id': self.pol_b_route.id}, 'A1')

        self._assert_blocked('A1', self.pol_b_route, status, result, landed)

    def test_A2_common_member_cannot_move_rule_into_foreign_cs_policy(self):
        """victim policy 錨在 Cloud Service 上——policy 的 owner-admin ir.rule
        是三腿 OR，deny 不該隨錨點形狀而異。

        只驗 route 那格的話，一個「只認 route 腿」的守衛會綠。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'a2')

        status, result, landed = self._attempt_reparent(
            self.rule_a.id, {'policy_id': self.pol_b_cs.id}, 'A2')

        self._assert_blocked('A2', self.pol_b_cs, status, result, landed)

    def test_A3_common_member_cannot_move_rule_into_foreign_sh_policy(self):
        """第三腿：victim policy 錨在 Smart Home Route 上。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'a3')

        status, result, landed = self._attempt_reparent(
            self.rule_a.id, {'policy_id': self.pol_b_sh.id}, 'A3')

        self._assert_blocked('A3', self.pol_b_sh, status, result, landed)

    def test_A4_x2many_update_command_hits_the_same_guard(self):
        """x2many 側門（push）：`[(1, R_A, {'policy_id': victim})]`。

        One2many 的 UPDATE(1) 指令直接呼叫子 model 的**公開** `write()`，vals
        原封（ORM 不自動塞 inverse）⇒ 走的是同一道守衛。少了這一格，一個裝在
        controller 而不是 model 上的守衛會綠。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'a4')

        status, result = self._call_kw(
            _POLICY, 'write',
            [[self.pol_a.id],
             {'rule_ids': [(1, self.rule_a.id,
                            {'policy_id': self.pol_b_route.id})]}])
        landed = self._db(_RULE, self.rule_a.id, 'policy_id')
        _logger.info(
            '[941][A4] RAW x2many-push RPC → http=%s error=%r landed_policy_id=%r',
            status, self._error_text(result) if 'error' in result else None,
            landed)

        self._assert_blocked('A4', self.pol_b_route, status, result, landed)

    def test_A5_rejected_reparent_never_syncs_the_victim_anchor(self):
        """🔴 被拒的 reparent 不得對**受害者的錨點**產生任何 side effect。

        `policy_id` 就在 `rule.write()` 的 resync 觸發集合裡，而 `policies` 是
        **舊 ∪ 新** ⇒ 一次放行的 reparent 會把含注入 selector 的 full-state
        推進受害者的 Cloudflare Access application。這一格把「守衛擋下之後受害
        者的錨點一次都沒被呼叫」斷言成契約。

        ⚠ 否定性斷言必配陽性對照：探針（spy）自己壞掉時「一次都沒被呼叫」與
        「守衛有效」在輸出上不可區分。所以先跑一次**合法**寫入，證明同一個 spy
        在同一個 process 裡真的錄得到 route 的 sync 呼叫。
        """
        self.authenticate(self.mallory_login, self.mallory_login)

        RouteCls = type(self.env[_ROUTE])
        original = RouteCls._sync_access_to_operator
        seen = []

        def _spy(records, *args, **kwargs):
            seen.extend(records.ids)
            return original(records, *args, **kwargs)

        RouteCls._sync_access_to_operator = _spy
        try:
            # 陽性對照：合法寫 rule_value 會觸發 route_a 的 sync（liveness gate
            # 讓它在 SA 無 tunnel 時 no-op，但**呼叫本身**照樣發生 ⇒ spy 錄得到）
            self._tenant_write_positive_control(self.rule_a.id, 'a5')
            self.assertIn(
                self.route_a.id, seen,
                '探針前提：spy 必須錄得到合法寫入觸發的 sync 呼叫，否則下面那句'
                '「victim 沒被 sync」零鑑別力（實際錄到 %r）' % (seen,))
            seen.clear()

            status, result, landed = self._attempt_reparent(
                self.rule_a.id, {'policy_id': self.pol_b_route.id}, 'A5')
        finally:
            RouteCls._sync_access_to_operator = original

        _logger.info('[941][A5] RAW synced_route_ids=%r', seen)
        self._assert_blocked('A5', self.pol_b_route, status, result, landed)
        self.assertNotIn(
            self.route_b.id, seen,
            '#941：被拒的 reparent 不得對受害者的錨點呼叫'
            ' `_sync_access_to_operator()`——那一次呼叫會把注入的 selector 推進'
            ' 受害者的 Cloudflare Access application（實際錄到 %r）' % (seen,))


class TestRuleReparentCreateBackstop941(_RuleReparent941Fixture):
    """B：create 側的**框架契約**——本單刻意不在 create 加守衛。"""

    def test_B1_framework_create_backstop_blocks_foreign_policy(self):
        """🔴 這條紅了代表框架行為變了，或 create 側是本單範圍外的第二個缺口。

        `BaseModel._create` 的尾端對**新建出來的列**再跑一次
        `check_access('create')`，而 rule model 上對 `base.group_user` 唯一
        `perm_create=True` 的 ir.rule 是 `..._owner_admin_manage`
        ⇒ 一列 `policy_id` 指向他人 policy 的新 rule 過不了那一關。

        缺的從來不是守衛，是**契約**——在本檔之前沒有任何東西記錄「我們依賴
        這個框架行為」。⛔ 在這一格綠之前，守衛的 docstring 不得宣稱
        「create 側已被框架覆蓋」。
        """
        self.authenticate(self.mallory_login, self.mallory_login)

        # 陽性對照：同一條 RPC、自己 workspace 的 policy 必須建得起來
        ok_status, ok_result = self._call_kw(_RULE, 'create', [{
            'policy_id': self.pol_a.id,
            'group': 'include',
            'rule_type': 'email_list',
            'rule_value': 'b1-own-941@example.com',
        }])
        own_id = self._assert_ok(
            ok_status, ok_result,
            '陽性對照：租戶對自己 workspace 的 policy 必須建得出 rule')
        self.assertEqual(
            self._db(_RULE, own_id, 'policy_id'), self.pol_a.id,
            '陽性對照：合法 create 必須真的落地，否則下面那格零鑑別力')

        status, result = self._call_kw(_RULE, 'create', [{
            'policy_id': self.pol_b_route.id,
            'group': 'include',
            'rule_type': 'email_list',
            'rule_value': 'b1-foreign-941@example.com',
        }])
        _logger.info(
            '[941][B1] RAW foreign-create RPC → http=%s error=%r',
            status, self._error_text(result) if 'error' in result else None)
        self.assertIn(
            'error', result,
            '框架契約：帶他人 policy 的 create 必須被 `_create` 尾端那次'
            ' `check_access(\'create\')` 擋下（實際 HTTP %s）' % status)
        self.assertIn(
            'AccessError', self._error_text(result),
            '框架契約：擋下它的必須是授權層（AccessError），不是某條 validation'
            '（實際 %r）' % self._error_text(result))
        self.env.flush_all()
        self.assertFalse(
            self.Rule.sudo().search(
                [('rule_value', '=', 'b1-foreign-941@example.com')]),
            '框架契約：被拒的 create 不得留下任何列')

    def test_B1b_with_user_create_shape_is_also_rejected(self):
        """B1 的 `with_user()` 形狀（工單字面要求），量的是 exception class。

        ⚠ 包在 `env.cr.savepoint()` 裡是刻意的：`_create` 是**先 INSERT 再做
        post-check**，不回捲會讓 transaction 進 aborted 狀態、後面所有查詢
        `InFailedSqlTransaction`。也因為回捲，這一格**只**主張 exception class；
        「沒留下任何列」那條 DB 證據由 B1 的 RPC 那半提供（回捲後的否定式斷言
        恆真、鑑別力為零）。

        `except Exception` 是刻意的：要量的正是 class 本身，先接住再斷言，
        才分得出「授權層擋的」與「某條 validation 擋的」。
        """
        raised = None
        try:
            with self.env.cr.savepoint():
                self.Rule.with_user(self.mallory).create({
                    'policy_id': self.pol_b_route.id,
                    'group': 'include',
                    'rule_type': 'email_list',
                    'rule_value': 'b1b-foreign-941@example.com',
                })
        except Exception as exc:  # noqa: BLE001 —— class 本身就是待量測的量
            raised = exc
        _logger.info('[941][B1b] RAW with_user create → raised=%s: %s',
                     type(raised).__name__ if raised else None, raised)
        if raised is None:
            self.fail(
                '框架契約：`with_user(mallory).create({policy_id: victim})` 必須'
                '被拒——沒被拒代表 create 側是本單範圍外的第二個缺口')
        self.assertEqual(
            type(raised).__name__, 'AccessError',
            '框架契約：拒絕必須來自授權層，實際 %s: %s'
            % (type(raised).__name__, raised))


class TestRuleReparentPositiveControls941(_RuleReparent941Fixture):
    """C：守衛不得擋掉合法路徑，也不得取代既有防線。"""

    def test_C1_owner_can_move_rule_within_own_workspace(self):
        """合法搬移照常過：Mallory 對 P_A 與 P_A2 都是 owner。

        這一格紅了代表守衛把正常操作一起擋掉了——那是本單最大的回歸面，也是
        「一律封死 `policy_id`」那條替代案被否決的理由（#849 的 L4 證明同
        workspace 內的 reparent 今天是活的合法路徑）。

        它同時是 fixture 的驗法：這一次搬家五個 validator 全過 ⇒ 攻擊格若被
        擋，擋它的不可能是那五個之一。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'c1')

        status, result, landed = self._attempt_reparent(
            self.rule_a.id, {'policy_id': self.pol_a2.id}, 'C1')

        self._assert_ok(status, result,
                        '同一個 workspace 內的 rule 搬移必須照常成功')
        self.assertEqual(
            landed, self.pol_a2.id,
            '合法搬移的值必須真的落地（實際 %r）' % (landed,))

    def test_C2_superuser_reparent_is_deliberately_exempt(self):
        """su 豁免是**刻意的**，這裡把它寫成被斷言的契約。

        判準用 `check_access('write')` 打在新 policy 上，而 `check_access()`
        在 `env.su` 下自動 no-op。migration `18.0.1.0.39` 的 `Rule.create`
        正是以 superuser 跑；而本欄位的不變量是「值轉移**受限**」不是「值
        不可變」（對照 #798 的 `cf_policy_id`：那一格沒有合法改值路徑，任何
        su 豁免都是洞）。

        su 豁免的代價是守衛的 deny 分支在 su env 下結構上不可達——本檔因此把
        攻擊格全部放在真 RPC 的非 su 租戶身上，並用這一格把豁免本身釘住。
        """
        _skip_route_sync(self.Rule.sudo().browse(self.rule_a.id)).write(
            {'policy_id': self.pol_b_route.id})

        self.assertEqual(
            self._db(_RULE, self.rule_a.id, 'policy_id'), self.pol_b_route.id,
            'su 路徑的 reparent 必須照常落地——擋掉它等於擋掉 migration 與所有'
            ' sudo 維運路徑')

    def test_C6_pull_direction_is_blocked_by_the_framework_pre_check(self):
        """pull 方向（把**別人的** rule 拉進自己的 policy）三種門一起釘住。

        這一側今天靠框架**寫入前**那次 `check_access('write')` 在守：目標列
        R_B 現在掛在 wsB，Mallory 對它只有 member-read（`perm_write=False`）
        ⇒ 三種寫法都在 `super().write()` 之前就被擋。沒有測試＝回歸無訊號。

        （這也是 7.4 的另一半：pull 若可行，攻擊者只要接著 unlink 自己的
        policy，cascade 就會連帶刪掉受害者的 rule。）
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        rule_b = self.pol_b_route.sudo().rule_ids[0]

        doors = (
            ('direct-write', _RULE, 'write',
             [[rule_b.id], {'policy_id': self.pol_a.id}]),
            ('x2many-link', _POLICY, 'write',
             [[self.pol_a.id], {'rule_ids': [(4, rule_b.id)]}]),
            ('x2many-set', _POLICY, 'write',
             [[self.pol_a.id], {'rule_ids': [(6, 0, [rule_b.id])]}]),
        )
        for tag, model, method, args in doors:
            status, result = self._call_kw(model, method, args)
            landed = self._db(_RULE, rule_b.id, 'policy_id')
            _logger.info(
                '[941][C6:%s] RAW pull RPC → http=%s error=%r landed_policy_id=%r',
                tag, status,
                self._error_text(result) if 'error' in result else None, landed)
            self.assertIn(
                'error', result,
                '#941[C6:%s]：把他人的 rule 拉進自己的 policy 必須被拒'
                '（實際 HTTP %s）' % (tag, status))
            self.assertEqual(
                landed, self.pol_b_route.id,
                '#941[C6:%s]：被拒之後 R_B 必須仍掛在 wsB 的 policy 底下'
                '（實際 %r）' % (tag, landed))

    def test_C7_policy_unlink_cascade_never_reaches_a_foreign_rule(self):
        """`policy.unlink()` 的 cascade 沒有本守衛該管的面（§7.4）。

        cascade 只**消滅**自己名下的列，不可能把一列 rule 送進他人 policy；
        而要刪掉 victim policy 本身需要對它有 `perm_unlink`（wsB 的 owner／
        admin）⇒ 兩個方向都不是 reparent。這一格把兩件事都量下來。
        """
        # (a) 刪自己的 policy ⇒ 只帶走自己的 rule
        doomed = self._policy(self.route_a, 'c7')
        doomed_rule_ids = doomed.sudo().rule_ids.ids
        self.assertTrue(doomed_rule_ids, '前提：待刪 policy 必須有 rule')
        victim_rule_ids = self.pol_b_route.sudo().rule_ids.ids
        _skip_route_sync(doomed.sudo()).unlink()
        self.env.flush_all()
        self.assertFalse(
            self.Rule.sudo().browse(doomed_rule_ids).exists(),
            'cascade：policy 被刪之後它自己的 rule 必須跟著消失')
        self.assertEqual(
            self.Rule.sudo().browse(victim_rule_ids).exists().ids,
            victim_rule_ids,
            'cascade：刪自己的 policy 不得動到他人 policy 的 rule')

        # (b) 刪他人的 policy ⇒ 被授權層擋下，rule 全數存活
        self.authenticate(self.mallory_login, self.mallory_login)
        status, result = self._call_kw(
            _POLICY, 'unlink', [[self.pol_b_route.id]])
        _logger.info(
            '[941][C7] RAW foreign-unlink RPC → http=%s error=%r',
            status, self._error_text(result) if 'error' in result else None)
        self.assertIn(
            'error', result,
            '普通成員不得刪掉 wsB 的 policy（實際 HTTP %s）' % status)
        self.env.flush_all()
        self.assertEqual(
            self.Rule.sudo().browse(victim_rule_ids).exists().ids,
            victim_rule_ids,
            '被拒的 unlink 不得帶走任何一列 victim rule')


class TestRuleReparentEdgeCases941(_RuleReparent941Fixture):
    """§7 的兩格待查證：清空語意，與被拒之後 session 還活著。"""

    def test_E1_clearing_policy_id_is_rejected_by_required_not_by_the_guard(self):
        """§7.2：`policy_id` 是 `required=True`，守衛的「清空不守」分支是否可達。

        守衛照 #848 同形保留 `if not new_id: return`（沒有「新 policy」要授權）。
        這一格量的是：清空實際上被**哪一層**擋下——只要它被擋，守衛不守清空就
        不是破口。

        ⚠ 包在 `env.cr.savepoint()` 裡是刻意的：清空會在 flush 時撞 NOT NULL，
        那是 SQL 之後才丟的例外，不回捲會毒掉 cursor。因此本格**只**主張
        「有被擋」與 exception class，⛔ 不主張回捲後的 DB 值。
        """
        raised = None
        try:
            with self.env.cr.savepoint():
                self.Rule.with_user(self.mallory).browse(
                    self.rule_a.id).write({'policy_id': False})
        except Exception as exc:  # noqa: BLE001 —— class 本身就是待量測的量
            raised = exc
        _logger.info('[941][E1] RAW clear policy_id → raised=%s: %s',
                     type(raised).__name__ if raised else None, raised)
        if raised is None:
            self.fail(
                '§7.2：`policy_id` 宣告 required=True，清空必須被擋'
                '（沒被擋代表 required 沒有生效，那是另一個缺口）')

    def test_E2_denied_reparent_leaves_the_session_usable(self):
        """§7.3：被拒之後同一個 session 還能繼續讀寫。

        `controllers/security_access.py::_update_policy` 把 `write()` 包在
        `try/except Exception` 裡並回 `{"success": False}`（HTTP 200）
        ⇒ 一個會毒掉 cursor 的拒絕會被吞成「安靜的失敗」，而呼叫端拿到的是
        200。守衛拋的 `AccessError` 發生在 `super().write()` **之前**、不碰
        DB ⇒ 不該毒。這一格把它量下來。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'e2-pre')

        status, result, landed = self._attempt_reparent(
            self.rule_a.id, {'policy_id': self.pol_b_route.id}, 'E2')
        self.assertIn(
            'error', result,
            '前提：這一格要量的是「被拒之後還活著」，攻擊必須先被拒'
            '（實際 HTTP %s、落地 %r）' % (status, landed))

        # 拒絕之後：同一個 session 還讀得到、也還寫得動
        read_status, read_result = self._call_kw(
            _RULE, 'read', [[self.rule_a.id], ['policy_id', 'rule_value']])
        rows = self._assert_ok(
            read_status, read_result, '被拒之後同一個 session 必須還讀得到')
        self.assertEqual(
            rows[0]['policy_id'][0], self.pol_a.id,
            '被拒之後讀回來的 policy_id 必須仍是原值（實際 %r）'
            % (rows[0]['policy_id'],))
        self._tenant_write_positive_control(self.rule_a.id, 'e2-post')


class TestRuleReparentRegression941(_RuleReparent941Fixture):
    """L：守衛不得誤擋今天活著的兩條合法路徑。"""

    def test_L1_controller_rule_set_replace_still_works(self):
        """controller 的 `(5,0,0) ＋ (0,0,{})` 整組 replace 必須照常成功。

        那是今天唯一的合法 rule 編輯路徑（三支 controller 一律這個寫法），而
        它從不產生任何 `policy_id` 寫入 ⇒ 守衛不該 fire。這一格用真 HTTP 打
        `access-policies` 端點證明它。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        url = ('/api/workspaces/%d/security-accesses/%d/access-policies'
               % (self.ws_a.id, self.sa_a.id))
        resp = self.url_open(url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'action': 'update', 'policy_id': self.pol_a.id,
                       'include': [{'rule_type': 'ip_range',
                                    'rule_value': '10.0.0.0/8'}],
                       'exclude': [{'rule_type': 'email_domain',
                                    'rule_value': 'evil.example'}]},
        }), headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = resp.json()
        res = body.get('result') or {}
        _logger.info('[941][L1] RAW controller replace → http=%s error=%r res=%r',
                     resp.status_code, body.get('error'), res.get('error'))
        self.assertNotIn(
            'error', body,
            'L1：controller 端點不得因為本守衛而 crash（JSON-RPC error=%r）'
            % (body.get('error'),))
        self.assertTrue(
            res.get('success'),
            'L1：整組 rule replace 必須照常成功（實際 %r）' % (res,))
        self.env.flush_all()
        self.assertEqual(
            res['data']['include'],
            [{'rule_type': 'ip_range', 'rule_value': '10.0.0.0/8'}],
            'L1：新的 include 必須真的落地（實際 %r）' % (res['data'],))

    def test_L2_framework_related_recompute_is_not_falsely_blocked(self):
        """框架自己的 related 重算不得被誤擋——而且要驗**新值真的算出來**。

        ⚠ 只斷言「不 raise」會**恆綠**：守衛只看 `vals` 有沒有 `policy_id`，
        而 stored related 的重算走低階 `_write()`、不經公開 `write()`
        ⇒ 必須照 #849 的 `_assert_projection` 同時斷言新值。
        """
        before = self._db(_RULE, self.rule_a.id, 'security_access_id')
        self.assertEqual(
            before, self.sa_a.id,
            '前提：重算前 rule 的投影欄位必須 = SA_A（實際 %r）' % (before,))

        self.Policy.with_user(self.mallory).browse(self.pol_a.id).write(
            {'route_id': self.route_a2.id})

        after = self._db(_RULE, self.rule_a.id, 'security_access_id')
        _logger.info('[941][L2] RAW projection after anchor move → %r', after)
        self.assertEqual(
            after, self.sa_a2.id,
            'L2：policy 換錨點之後 rule 的 `security_access_id` 必須重算成 %r'
            '——若仍等於舊值 %r 代表重算被吃掉了（守衛沒 raise、但值也沒更新，'
            '症狀是安靜的）' % (self.sa_a2.id, before))


class TestRuleReparentInjectionControls941(_RuleReparent941Fixture):
    """🔴 C3a／C3b／C4：把守衛停用，證明綠是**守衛造成的**。

    這一組是本檔的判別式所在。少了它，A 系列的綠與「A 系列根本沒打到守衛」
    （fixture 走偏、RPC 沒到、id 打錯、或某條 constraint 順手擋掉了）在輸出上
    完全不可區分——那正是本 repo 反覆踩到的假綠形狀。
    """

    def _stranger_workspace(self, tag):
        """一個與 wsA／wsB 都無關的第三方（Carol），連 wsB 的成員都不是。"""
        carol, _ = _new_group_user(self.env, 'carol941%s' % tag)
        ws_c = self._workspace('WS-C 941 %s' % tag, carol)
        _, route_c = self._sa_route(ws_c, 'c%s' % tag)
        pol_c = self._policy(route_c, 'c%s' % tag)
        rule_c = self._extra_include_rule(pol_c, 'rc%s' % tag)
        self.assertFalse(
            self.WorkspaceAccess.sudo().search(
                [('workspace_id', '=', self.ws_b.id),
                 ('user_id', '=', carol.id)]),
            '前提：Carol 不得是 wsB 的成員，否則這一格測的是 A1 不是陌生人')
        return carol, pol_c, rule_c

    def test_C3a_stranger_reparent_is_rejected_by_the_new_guard(self):
        """陌生人（完全非成員）搬 rule：守衛在的時候，紅**必須來自新守衛**。

        判準用 traceback 裡的**函式名存在性**，⛔ 不用行號——行號會被任何一次
        docstring 增修整段推移，而那種失效是靜默的。
        """
        carol, pol_c, rule_c = self._stranger_workspace('a')

        try:
            self.Rule.with_user(carol).browse(rule_c.id).write(
                {'policy_id': self.pol_b_route.id})
        except AccessError:
            frames = traceback.extract_tb(sys.exc_info()[2])
        else:
            self.fail('陌生人搬 rule 必須被拒（守衛或既有防線任一擋下都算）')

        names = [f.name for f in frames]
        _logger.info('[941][C3a] RAW stranger frames=%r', names)
        self.assertEqual(
            self._db(_RULE, rule_c.id, 'policy_id'), pol_c.id,
            '被拒之後 DB 那一格必須仍是舊 policy')
        self.assertIn(
            '_assert_rule_reparent_authorised', names,
            '#941：守衛在的時候，陌生人這一格必須紅在**新守衛**上'
            '——實際 traceback 的 frame 依序為 %r' % (names,))

    def test_C3b_with_the_guard_disabled_the_stranger_is_not_stopped_by_it(self):
        """把新守衛停用之後，陌生人這條路上**還剩什麼**——誠實記錄，不預設答案。

        ⚠ rule 側**沒有** policy 側那個 `check_access('read')` 排序迴圈，所以
        這一格的形狀由實測決定，不能從 #848 的 C3b 外推。兩種結局都合法，但
        **不論哪一種，擋下它的都不得是新守衛**（那正是「patch 真的生效了」的
        證明）；哪一種發生則寫進 log 與本 docstring 的量測欄。

        量測（GREEN 輪 CI）：兩個分支的原始輸出見 job log 的 `[941][C3b]` 行。
        """
        carol, pol_c, rule_c = self._stranger_workspace('b')

        raised = None
        frames = []
        with patch(_GUARD):
            try:
                self.Rule.with_user(carol).browse(rule_c.id).write(
                    {'policy_id': self.pol_b_route.id})
            except Exception as exc:  # noqa: BLE001 —— 要量的正是「還剩什麼」
                raised = exc
                frames = traceback.extract_tb(sys.exc_info()[2])

        names = [f.name for f in frames]
        landed = self._db(_RULE, rule_c.id, 'policy_id')
        _logger.info(
            '[941][C3b] RAW guard-disabled stranger → raised=%s landed=%r '
            'frames=%r',
            type(raised).__name__ if raised else None, landed, names)

        self.assertNotIn(
            '_assert_rule_reparent_authorised', names,
            '前提：本格必須是在守衛**停用**的狀態下量到的；traceback 裡出現守衛'
            '代表 patch 沒生效，這一格與 C3a 就變成同一件事（實際 frame %r）'
            % (names,))
        if raised is None:
            # 誠實記錄：守衛之外，rule 側對陌生人沒有第二道防線。
            self.assertEqual(
                landed, self.pol_b_route.id,
                'C3b：守衛停用後沒有拋例外，那值就必須真的落地——「沒拋也沒落地」'
                '代表有東西靜默丟棄了寫入，那是另一個問題（實際 %r）' % (landed,))
        else:
            self.assertEqual(
                landed, pol_c.id,
                'C3b：守衛停用後被別的東西擋下（%s），那 DB 就必須原封不動'
                '（實際 %r）' % (type(raised).__name__, landed))

    def test_C4_injection_control_disabling_the_guard_lets_the_attack_land(self):
        """🔴 注入對照：把守衛停用，A1 的攻擊必須**翻回成功且值落地**。

        這是「A1 的綠是守衛造成的」唯一的判別式，也是 #941 端到端可利用性的
        直接證據（真 HTTP session ＋ 非 su 租戶 ＋ 守衛停用）。RED 輪已經量到
        同一個結論（`landed_policy_id=406`）；本格把它變成**每次 CI 都重跑**的
        對照，而不是一次性的觀測。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.rule_a.id, 'c4')

        with patch(_GUARD):
            status, result, landed = self._attempt_reparent(
                self.rule_a.id, {'policy_id': self.pol_b_route.id}, 'C4')

        self._assert_ok(
            status, result,
            '注入對照：守衛停用後這條 RPC 必須成功（它是 A1 的同一條）')
        self.assertEqual(
            landed, self.pol_b_route.id,
            '注入對照：守衛停用後攻擊必須落地——沒落地代表 A1 的綠不是守衛造成的'
            '（實際落地 %r）' % (landed,))
