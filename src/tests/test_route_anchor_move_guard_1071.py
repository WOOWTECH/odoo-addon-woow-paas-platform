"""#1071：route 子模型的真錨點 `security_access_id` 在 model 層沒有歸屬守線（write 側）。

## 缺口的精確形狀——與 #848／#941 同型，只是落在**第三個** model 的欄位上

`SecurityAccessRoute.write()` 對 `security_access_id` 零 authz 把關：

* 欄位層：`required=True` 只禁 NULL、`ondelete='cascade'` 只管刪除，無 `domain=`。
* `_reject_identity_anchor_writes` 只逐欄比對 `_ROUTE_IDENTITY_ANCHOR_FIELDS`
  （`dns_record_id` / `access_app_id`），**不含**真錨點 ⇒ 原封放行。
* ir.rule：框架 `BaseModel.write` **只有開頭**那一次 `check_access('write')`，
  評估的是**寫入前**的狀態（那時這列還掛在攻擊者自己的 SA 上 ⇒ 過），
  `super().write()` 落地後零複驗。對照 `BaseModel._create` **尾端**對新建列再跑
  一次 `check_access('create')`——create 有 post-check、write 沒有。

## 🔴 「今天擋得住」是配額守衛的副作用，不是授權層（run 2674 實測）

`_check_route_quota` 為了數 `len(sa.route_ids)` 去讀 o2m，順手觸發對**父記錄**的
`check_access('read')` ⇒ 讀不到受害者 SA 的人在那裡撞 `AccessError`。但守衛開頭
就有 `if not sub or not sub.sh_subscription_plan_id: continue` ——**受害者 SA 沒有
訂閱時整條提前 continue，連 `route_ids` 都碰不到**。

量到的原始值（CI run **2674**，job 8018，branch `test/1054-reach`@`0506f937`，
2026-09-06 03:52Z，3934 條中**唯一**一條紅）：

```
FAIL: TestRouteQuotaEndToEndReach1054.test_B2c_move_into_an_unsubscribed_victim_sa_is_blocked
  File ".../tests/test_quota_len_o2m_reach_1054.py", line 563, in test_B2c_...
    with self.assertRaises(Exception) as cm:
AssertionError: Exception not raised
```

同 class 的 `test_B2_move_into_a_subscribed_victim_sa_is_blocked` 在同一趟**綠**。
⇒ 兩格合起來把歸因釘死：有訂閱那格靠的是配額守衛的 read 副作用（incidental、會漂），
無訂閱那條路上零守線。B2c 那一格已隨本單移進本檔當判別式（`Reach1071` class），
B2 一併移進來並**收緊**成「必須紅在新守衛上」——⛔ 不用「給受害 SA 加訂閱」讓 B2c 綠。

## 可利用性落在共同成員，不是陌生人

route 的 member-read rule（`security/ir_rules.xml` 的
`security_access_route_rule_workspace_member`）domain 沒有 role 條件、
`perm_write=False`，任何成員都讀得到該 workspace 的 SA id ⇒ 「A 的 owner／admin
＋ B 的普通成員」把 A 的 route 寫進 B 的 SA。搬完之後那列進了 B 的 domain、
`perm_write` 要 owner／admin ⇒ **攻擊者自己也收不回來，是單向注入**。

## 與 #941 的一處不同：錨點**不在** resync 觸發集合裡

`rule.write()` 的 `policy_id` 就在觸發集合中；本檔的 `security_access_id` 兩個集合
（`('service_url', 'http_host_header')` 與 `('is_protected', 'login_google',
'login_email_otp', 'session_duration')`）**都不在** ⇒ 單獨改錨點不觸發 operator。

⚠ 但混合 payload 會：`for route in self: route._sync_access_to_operator()` 是在
`super().write()` **之後**跑的，而該方法內部 `sa = self.security_access_id` 取到的
是**新**（受害者）SA ⇒ 一次放行的搬移＋一個 `session_duration` 就把注入的 route 的
Access 狀態推向受害者那一側。A3 把「被拒之後那次 sync 一次都沒發生」釘成契約。

## 🔴 本檔最容易假綠的那一格，與五道對策

route 的 `write()` 之後還有 `@api.constrains` 會拋 `ValidationError`，而配額守衛
讀 o2m 的 read 副作用會拋 `AccessError`——**與本守衛同一個 class** ⇒ 只斷言「有拋
AccessError」的攻擊格，在守衛不存在時（有訂閱的受害者）一樣會綠。對策：

1. 攻擊格的受害者 SA 一律**無訂閱**（配額守衛整條 continue）⇒ 副作用不會發生。
2. B2（有訂閱）那一格改用 **traceback 的函式名存在性**當判準，⛔ 不用 exception
   class。⚠ 那一格守衛失效時**不是** #1054 B2 量到的 `AccessError`：本檔的
   Mallory 是 wsB 的成員（#1054 的 attacker 不是）⇒ 她讀得到受害者 SA、也看得到
   整池 route ⇒ 撞的是配額本身的 `ValidationError`。它的 `except` 因此收兩類，
   讓紅落在判準那句而不是變成未捕捉例外的 ERROR。
3. C4「patch 掉守衛、攻擊必須翻回成功且值落地」——它是「A1 的綠是守衛造成的」的
   判別式。⚠ **C4 對「呼叫點被整行拿掉」這種 mutation 零鑑別力**（`patch()` 換的
   是 module 屬性，呼叫點不在就不會被觸發 ⇒ C4 照樣綠）⇒ ⛔ 不可拿 C4 的綠單獨
   當「守衛還在」的證據，要配 A1／A2／A3／B2c／B2／C3a 一起讀。
4. 每格攻擊前跑 `_tenant_write_positive_control`。
5. 值落地與否一律讀**裸 SQL**（`_db()`），不用 ORM——ORM 讀會把「這一列已經被判給
   別人、我讀不到」誤讀成「寫入被丟棄」。

## 紀律（沿用 #941 檔頭）

🔴 任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋
`else: self.fail(...)`，**不用 `assertRaises`**——Odoo 的 `BaseCase._assertRaises`
在進入 with 區塊前開一顆 flushing savepoint，例外穿出即 `ROLLBACK TO SAVEPOINT`，
回捲邊界恰好是整個 with 區塊 ⇒ 區塊內寫的東西全部消失，回捲值剛好等於期望值時假綠
（`odoo-assertraises-savepoint-rollback.md`，#822 實撞）。

⚠ 本檔有**兩格刻意自己開 savepoint**（E1／B1b）：那兩條被測路徑會**先送出 SQL
再丟例外**，不包 savepoint 會讓 transaction 進 aborted 狀態。代價是它們的 DB 狀態
被回捲 ⇒ 這兩格**只主張 exception 的性質**，⛔ 不對它們做回捲後恆真的否定式斷言。

判準用 traceback 的**函式名存在性**，⛔ 不用行號。

攻擊一律走真 RPC `/web/dataset/call_kw`，⛔ 不用 `with_user()` ＋ ORM：
`TransactionCase` 預設 su env（`env.su` 相關分支整條跳過），而
`odoo/api.py::call_kw` 把呼叫端 context 整包換進 env，in-process 呼叫模擬不到。
**三個例外**（C3a／B1b／Reach1071 那一組）各自在自己的 docstring 說明理由。

⚠ o2m 的 cache key 不含 `uid`（`odoo/api.py`：只有 `depends_context` 帶 `uid` 的
欄位才分使用者）⇒ 換 env 前一律 `invalidate_all()`，否則 su 讀過一次的 `route_ids`
會被非 su 的呼叫直接命中同一份 cache（#1054 踩過，那會讓每格假綠）。

Refs #1054 #848 #941 #849
"""

import json
import logging
import sys
import traceback
import uuid
from unittest.mock import patch

from psycopg2 import IntegrityError
from psycopg2.errorcodes import NOT_NULL_VIOLATION

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, tagged

from ..models.security_access import _skip_route_sync

_logger = logging.getLogger(__name__)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_RULE = 'woow_paas_platform.security_access_access_policy_rule'

#: C3b／C4 停用守衛用的 patch 標的。守衛是 **module-level function**、`write()` 以
#: 模組全域名稱呼叫它 ⇒ patch 這個名字就等於把守衛 body 拿掉，而且**同一個 process
#: 內的 HTTP server thread 也吃得到**（走真 RPC 的 C4 靠的正是這一點）。
_GUARD = ('odoo.addons.woow_paas_platform.models.'
          'security_access._assert_route_anchor_move_authorised')


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 1071' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


def _plan_vals(name, **quota):
    """訂閱方案（只有 B2 那一格用得到；其餘 SA 一律不掛訂閱）。

    ⚠ 照 #1054 的 fixture：席位與 policy 數兩條配額必須讓開，否則本檔量到的會是
    那兩道而不是 route 配額。
    """
    vals = {
        'name': name, 'sh_duration': 1, 'sh_unit': 'month',
        'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
        'ac_max_access_seats': 99,
        'sa_max_access_policies': 0,
    }
    vals.update(quota)
    return vals


@tagged('post_install', '-at_install')
class _RouteAnchorMove1071Fixture(HttpCase):
    """兩個 workspace：A（Mallory 是 owner）與 B（Mallory 只是普通成員）。

    三個「不是本檔要驗的門」各自關掉：

    * SA 一律**不給 `tunnel_id`** ⇒ route 的 `_access_sync_skip_reason()` 第一道
      就是「no live tunnel」，本檔任何 create／write 都不會真的打 operator。
    * SA 一律**不給訂閱** ⇒ `_check_route_quota` 開頭 `continue` ⇒ 配額守衛的
      read 副作用（今天唯一擋得住有訂閱受害者的東西）不參與，攻擊格量到的是
      **授權層**本身。B2 那一格是唯一的例外，它自己掛訂閱。
    * route 建立時走 `_skip_route_sync` ⇒ create 腿不打 operator。回傳時
      `.with_context(sa_skip_route_sync=False)` 只是**不讓這個 fixture 物件把 skip
      sentinel 帶進後續任何直接對它做的 ORM write**（照 #941 `_route()` 的慣例）——
      ⛔ 它**不是** A3 的前提：A3 走真 RPC，server 端從 session 重建 env，Python
      物件身上的 context 根本傳不過去（覆核時實測過這條因果不成立）。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.Rule = self.env[_RULE]

        self.mallory, self.mallory_login = _new_group_user(
            self.env, 'mallory1071')
        self.assertFalse(
            self.mallory.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user，否則 ir.rule 整個不生效（假綠）')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob1071')

        # workspace A —— Mallory 是 owner
        self.ws_a = self._workspace('WS-A 1071', self.mallory)
        # workspace B —— Bob 是 owner，Mallory 只是**普通成員**
        self.ws_b = self._workspace('WS-B 1071', self.bob, {self.mallory: 'user'})

        self.sa_a = self._sa(self.ws_a, 'a')
        self.sa_a2 = self._sa(self.ws_a, 'a2')      # C1 的合法目的地
        self.sa_b = self._sa(self.ws_b, 'b')        # 受害者（無訂閱）

        self.route_a = self._route(self.sa_a, 'a')
        self.route_b = self._route(self.sa_b, 'b')  # C6 pull direction 用

        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'security_access_id'),
            self.sa_a.id,
            '前提：R_A 一開始必須掛在 SA_A 底下')
        self.assertTrue(
            self.WorkspaceAccess.sudo().search_count([
                ('workspace_id', '=', self.ws_b.id),
                ('user_id', '=', self.mallory.id),
                ('role', '=', 'user')]),
            '前提：Mallory 必須是 wsB 的**普通成員**——她若不是成員，攻擊格量到的'
            '就是「陌生人被 ir.rule 擋」那條路，不是本單的共同成員注入面')

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

    def _sa(self, workspace, tag):
        sa = self.SA.sudo().create(
            {'name': 'SA 1071 %s' % tag, 'workspace_id': workspace.id})
        self.assertFalse(
            sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.assertFalse(
            sa.sudo().subscription_id,
            '前提：SA 不得有訂閱——配額守衛的 read 副作用會蓋過授權層，'
            '攻擊格就分不出擋它的是誰（#1054 的 B2 vs B2c 正是這件事）')
        return sa

    def _route(self, sa, tag):
        return _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r1071%s-%s' % (tag, uuid.uuid4().hex[:6]),
        }).with_context(sa_skip_route_sync=False)

    def _policy(self, route, tag):
        """route 底下一條 allow policy（帶一條 include rule），供 L1 驗投影欄重算。"""
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'route_id': route.id,
            'name': 'pol-1071-%s' % tag,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'seed-%s@example.com' % tag})],
        })
        self.assertFalse(
            pol.cf_policy_id,
            '前提：policy 不得帶 cf_policy_id，否則搬移會去碰 admin-only 的'
            ' `cf_access_policy_reap`，多出一道我沒預期的門')
        return pol.with_context(sa_skip_route_sync=False)

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

    def _tenant_write_positive_control(self, route_id, tag):
        """同一條 RPC、同一列、租戶合法欄位——攻擊格前面的必備陽性對照。

        少了它，「攻擊被擋」與「這個帳號對這一列根本寫不動」在輸出上不可區分。
        """
        value = 'http://pc-1071-%s.internal:80' % tag
        status, result = self._call_kw(
            _ROUTE, 'write', [[route_id], {'service_url': value}])
        self._assert_ok(
            status, result,
            '陽性對照：租戶對自己 workspace 內的 route 有合法寫入權')
        self.assertEqual(
            self._db(_ROUTE, route_id, 'service_url'), value,
            '陽性對照：合法欄位必須真的落地，否則本測試零鑑別力')

    def _attempt_move(self, route_id, vals, tag, model=_ROUTE, method='write'):
        """跑一次 anchor move 的 RPC，把**量到的**原始輸出記進 log。

        HTTP 狀態碼與攻擊後 DB 讀到的 `security_access_id` 都是量測值（不是字面
        量）——本單可利用性的原始證據就是這兩個數。log 前綴 `[1071]` 是
        `odoo-tests.yml` 的已知指紋，job log 會逐行留下來。
        """
        status, result = self._call_kw(model, method, [[route_id], vals])
        landed = self._db(_ROUTE, route_id, 'security_access_id')
        _logger.info(
            '[1071][%s] RAW anchor-move RPC → http=%s error=%r landed_sa_id=%r',
            tag, status,
            self._error_text(result) if 'error' in result else None, landed)
        return status, result, landed


class TestRouteAnchorMoveRpc1071(_RouteAnchorMove1071Fixture):
    """A：wsB 的普通成員走真 RPC 把自己的 route 搬進 wsB 的 SA 底下。"""

    def _assert_blocked(self, tag, status, result, landed):
        self.assertEqual(
            landed, self.sa_a.id,
            '#1071[%s]：wsB 的普通成員不得把 wsA 的 route 搬進 wsB 的 SA'
            '——實際落地 security_access_id=%r（受害者 SA 是 %r，原值 %r）'
            % (tag, landed, self.sa_b.id, self.sa_a.id))
        self.assertIn(
            'error', result,
            '#1071[%s]：守衛必須**大聲**拒絕（靜默丟棄會讓呼叫端以為搬成功了）；'
            '實際 HTTP %s' % (tag, status))
        text = self._error_text(result)
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        # 受害者 SA **無訂閱** ⇒ `_check_route_quota` 提前 continue ⇒ 配額那條路
        # 的 `AccessError` 與 `ValidationError` 都不會發生 ⇒ 這裡的 AccessError
        # 只可能來自本守衛。（有訂閱那格的鑑別力由 Reach1071 的 B2 用 traceback
        # 提供，class 在那裡零鑑別力。）
        self.assertIn(
            'AccessError', text,
            '#1071[%s]：拒絕的類型必須是 AccessError（授權不足）。量到'
            ' ValidationError 代表擋它的是 write() 之後的 validator、量到'
            ' UserError 代表是 identity-anchor 那道 ⇒ 兩者都不是歸屬守衛，'
            '這一格零鑑別力。實際 %r' % (tag, text))

    def test_A1_common_member_cannot_move_route_into_foreign_sa(self):
        """🔴 本單主格：把 R_A 搬進 wsB 的 SA。

        危害不需要知道任何祕密：Mallory 是 wsB 的**普通成員**（`role='user'`），
        wsB 的 SA id 對她本來就是可讀的（member-read rule 無 role 條件）。搬完
        之後那條 route 就掛在受害者的 Security Access 底下——它決定 tunnel 路由
        與 Cloudflare 對應，而她自己再也收不回來（route 的 `perm_write` 在 wsB
        要 owner／admin）⇒ 單向注入。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'a1')

        status, result, landed = self._attempt_move(
            self.route_a.id, {'security_access_id': self.sa_b.id}, 'A1')

        self._assert_blocked('A1', status, result, landed)

    def test_A2_x2many_update_command_hits_the_same_guard(self):
        """x2many 側門（push）：`sa_a.write({'route_ids': [(1, R_A, {...})]})`。

        One2many 的 UPDATE(1) 指令直接呼叫子 model 的**公開** `write()`，vals
        原封（ORM 不自動塞 inverse）⇒ 走的是同一道守衛。少了這一格，一個裝在
        controller 而不是 model 上的守衛會綠。

        ⚠ 攻擊者對 `sa_a` 本身有 owner ⇒ 外層那次 write 的框架 pre-check 會過，
        真正要擋的是內層那條 route 的錨點搬移。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'a2')

        status, result = self._call_kw(
            _SA, 'write',
            [[self.sa_a.id],
             {'route_ids': [(1, self.route_a.id,
                             {'security_access_id': self.sa_b.id})]}])
        landed = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        _logger.info(
            '[1071][A2] RAW x2many-push RPC → http=%s error=%r landed_sa_id=%r',
            status, self._error_text(result) if 'error' in result else None,
            landed)

        self._assert_blocked('A2', status, result, landed)

    def test_A3_rejected_move_never_syncs_with_the_victim_sa(self):
        """🔴 被拒的搬移不得產生任何指向受害者的 operator side effect。

        `security_access_id` 本身**不在** `write()` 的兩個 sync 觸發集合裡，但
        混合 payload 會讓觸發條件因為別的欄位而成立，而
        `route._sync_access_to_operator()` 是在 `super().write()` **之後**跑的
        ——它內部 `sa = self.security_access_id` 取到的是**新**（受害者）SA
        ⇒ 一次放行的搬移就把這條 route 的 Access 狀態推向受害者那一側。

        ⚠ 否定性斷言必配陽性對照：探針（spy）自己壞掉時「一次都沒被呼叫」與
        「守衛有效」在輸出上不可區分。所以先跑一次**合法**的 `session_duration`
        寫入，證明同一個 spy 在同一個 process 裡真的錄得到 sync 呼叫。
        """
        self.authenticate(self.mallory_login, self.mallory_login)

        RouteCls = type(self.env[_ROUTE])
        original = RouteCls._sync_access_to_operator
        seen = []

        def _spy(records, *args, **kwargs):
            seen.extend(records.ids)
            return original(records, *args, **kwargs)

        # 還原要分兩種：註冊表 class 自己**本來就有**這個屬性才用指派還原，
        # 否則要 `del`。`original` 是經 MRO 取到的（通常來自定義 class），直接
        # 指派回去會把真函式永久寫進註冊表 class 的 `__dict__`，遮掉之後所有
        # 對定義 class 的 patch——#1196 的改名測試就是在 CI 全套裡被這樣遮掉的
        # （mock 沒被呼叫、真的 sync 跑去打 operator），本機單跑重現不了。
        had_own = '_sync_access_to_operator' in RouteCls.__dict__
        RouteCls._sync_access_to_operator = _spy
        try:
            # 陽性對照：合法寫 session_duration 會觸發 route_a 自己的 access sync
            # （liveness gate 讓它在 SA 無 tunnel 時 no-op，但**呼叫本身**照樣
            # 發生 ⇒ spy 錄得到）
            status, result = self._call_kw(
                _ROUTE, 'write',
                [[self.route_a.id], {'session_duration': '8h'}])
            self._assert_ok(
                status, result, '陽性對照：合法改 session_duration 必須成功')
            self.assertIn(
                self.route_a.id, seen,
                '探針前提：spy 必須錄得到合法寫入觸發的 sync 呼叫，否則下面那句'
                '「被拒之後零呼叫」零鑑別力（實際錄到 %r）' % (seen,))
            seen.clear()

            status, result, landed = self._attempt_move(
                self.route_a.id,
                {'security_access_id': self.sa_b.id,
                 'session_duration': '24h'},
                'A3')
        finally:
            if had_own:
                RouteCls._sync_access_to_operator = original
            else:
                del RouteCls._sync_access_to_operator

        _logger.info('[1071][A3] RAW synced_route_ids=%r', seen)
        self._assert_blocked('A3', status, result, landed)
        self.assertEqual(
            seen, [],
            '#1071：被拒的搬移不得呼叫 `_sync_access_to_operator()`——那一次呼叫'
            '會帶著**受害者的** SA 去推 Cloudflare Access 狀態（實際錄到 %r）'
            % (seen,))
        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'session_duration'), '8h',
            '#1071：被拒的混合 payload 不得留下**任何**欄位的變更'
            '（session_duration 應停在陽性對照寫下的 8h）')


class TestRouteAnchorMoveReach1071(_RouteAnchorMove1071Fixture):
    """B2／B2c：#1054 階段 1 的兩格端到端判別式，移進本單。

    ⚠ 這一組刻意走 `with_user()` ＋ ORM 而不是 RPC（違反檔頭紀律，理由在這裡）：
    它們是從 `test_quota_len_o2m_reach_1054.py` **原樣移過來的判別式**，run 2674
    的紅就是這個形狀量出來的；換成 RPC 會換掉被量的東西（`call_kw` 的錯誤封包
    帶不出 traceback，而 B2 的判準正是 traceback 的 frame 名）。RPC 面的覆蓋由
    A1–A3 提供。

    ⚠ `with_user()` 之後的 env **不是** su（下面每格都斷言），所以 ir.rule 照套。
    """

    def _cold_view(self, record, user, warm_fnames):
        """回傳 `record` 在 `user` env 下的視圖，且要被數的 o2m 是**冷的**。

        `route_ids` 的 cache key 不含 `uid` ⇒ fixture 以 su 讀過一次 o2m，換 env
        後會直接命中同一份 cache，配額少算不會發生、每格假綠（#1054 踩過）。
        灌熱的只有守衛在 `self` 上讀的純量／m2o 欄位（真實路徑上剛被寫過、本來
        就在 cache）；⛔ 絕不碰要被數的 o2m。
        """
        self.env.flush_all()
        self.env.invalidate_all()
        record.read(warm_fnames)
        view = record.with_user(user)
        self.assertFalse(
            view.env.su,
            '前提：%s 的 env 不得是 su——su 之下 ir.rule 不套、守衛的 deny 分支'
            '結構上不可達，本格會恆綠'
            '（docs/decisions/patterns/su-env-exempt-guard-false-green.md）'
            % user.login)
        return view

    def _subscribe(self, sa, plan):
        """`sh_partner_id` 必須是錨點 workspace 的 owner（照 #896／#1054 fixture）。"""
        owner = sa.sudo().workspace_id.owner_id
        self.assertTrue(owner, '前提：錨點必須有 workspace owner')
        sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': owner.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.sudo().subscription_id = sub.id
        self.env.flush_all()
        return sub

    def test_B2c_move_into_an_unsubscribed_victim_sa_is_blocked(self):
        """🔴 **本單的判別式**——run 2674 唯一那條紅，原樣移過來。

        受害者 SA **沒有訂閱** ⇒ `_check_route_quota` 讀 `sub` 為空，**在碰到
        `route_ids` 之前就 `continue`** ⇒ 配額守衛的 read 副作用不會發生，而
        write 路徑上在本單之前沒有任何其他授權層 ⇒ 攻擊者的 route 直接落進受害者
        的 SA。

        修法前紅的形狀（CI run 2674，job 8018，line 563）：
        `AssertionError: Exception not raised`。修法後這裡必須拋 `AccessError`
        且值不落地。

        ⚠ 斷言改成裸 `try/except` ＋ `_db()`（原版是 `assertRaises` ＋只驗
        exception）：本格現在多主張「值沒落地」，而 `BaseCase._assertRaises` 的
        savepoint 會把 with 區塊內的寫入整個回捲 ⇒ 那句斷言在回捲後恆真、零鑑別力
        （`odoo-assertraises-savepoint-rollback.md`）。⛔ 不用「給受害 SA 加訂閱」
        讓本格綠——那會把它變成 B2，量的是另一條路。
        """
        self.assertFalse(
            self.sa_b.sudo().subscription_id,
            '前提：受害者 SA 不得有訂閱，否則量到的是 B2 那條路')
        view = self._cold_view(
            self.route_a, self.mallory, ['security_access_id'])

        try:
            view.write({'security_access_id': self.sa_b.id})
        except AccessError as exc:
            _logger.info('[1071][B2c] RAW raised=%s: %s',
                         type(exc).__name__, exc)
        else:
            landed = self._db(_ROUTE, self.route_a.id, 'security_access_id')
            self.fail(
                '#1071：無訂閱的受害者 SA 這條路上必須有守線——搬移沒有被擋，'
                'security_access_id 落地成 %r（受害者 %r、原值 %r）。'
                'run 2674 的紅就是這一格。'
                % (landed, self.sa_b.id, self.sa_a.id))

        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'security_access_id'),
            self.sa_a.id,
            '#1071：被拒的搬移不得留下任何錨點變更')

    def test_B2_move_into_a_subscribed_victim_sa_is_blocked_by_the_guard(self):
        """受害者 SA **有訂閱**時也必須是**新守衛**擋的，不是配額的副作用。

        run 2674 這一格是綠的——但擋住它的是 `_check_route_quota` 為了數
        `len(sa.route_ids)` 去讀 o2m 時觸發的父記錄 read 檢查（`AccessError`），
        那是 incidental 的：配額守衛哪天改成 `sudo()` 讀、或受害者取消訂閱，
        保護就無聲消失。

        🔴 判準因此**不能**是 exception class。改用 traceback 的**函式名存在性**
        （⛔ 不用行號：行號會被任何一次 docstring 增修整段推移，而那種失效是靜默的）。

        ⚠ `except` 刻意收兩類（覆核時推導出來的，⛔ 不要收窄回單一 `AccessError`）：
        本檔的 Mallory **是** wsB 的普通成員（#1054 的 attacker **不是**）⇒ 守衛被
        拿掉時她讀得到受害者 SA、也看得到它底下**整池** route ⇒ `_check_route_quota`
        數到 2 > 上限 1，拋的是 `ValidationError` 而不是 #1054 B2 那趟的
        `AccessError`。只收 `AccessError` 會讓守衛失效那趟變成**未捕捉例外的
        ERROR**，紅在 traceback 頂端而不是紅在下面那句判準上——訊息因此說不出
        「擋它的不是新守衛」這件事。收兩類之後 GREEN 輪不受影響（守衛先拋
        `AccessError`，frame 含守衛 ⇒ 綠）。

        ⚠ 與檔頭「攻擊一律走真 RPC」的紀律相反，理由同 class docstring：
        `call_kw` 的錯誤封包只帶 `name`／`message`，拿不到 traceback。
        """
        plan = self.env['sh.subscription.plan'].sudo().create(
            _plan_vals('R1071b2', sa_max_routes=1))
        self._subscribe(self.sa_b, plan)
        self.assertTrue(
            self.sa_b.sudo().subscription_id.sh_subscription_plan_id,
            '前提：受害者 SA 必須真的掛上方案，否則本格與 B2c 是同一件事')

        view = self._cold_view(
            self.route_a, self.mallory, ['security_access_id'])

        try:
            view.write({'security_access_id': self.sa_b.id})
        except (AccessError, ValidationError) as exc:
            raised = exc
            frames = traceback.extract_tb(sys.exc_info()[2])
        else:
            self.fail('有訂閱的受害者 SA 這條路同樣必須被擋')

        names = [f.name for f in frames]
        _logger.info('[1071][B2] RAW subscribed-victim raised=%s frames=%r',
                     type(raised).__name__, names)
        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'security_access_id'),
            self.sa_a.id,
            '被拒的搬移不得留下任何錨點變更')
        self.assertIn(
            '_assert_route_anchor_move_authorised', names,
            '#1071：有訂閱時擋下來的必須是**新守衛**，不是 `_check_route_quota` 的'
            '副作用（讀不到受害者 SA 的 `AccessError`／數到超額的 `ValidationError`'
            '——兩者都是配額實作的偶然產物，改寫成 `sudo()` 讀或取消訂閱就無聲消失，'
            '⛔ 不可拿它當授權層）。實際擋它的是 %s，traceback 的 frame 依序為 %r'
            % (type(raised).__name__, names))


class TestRouteAnchorMoveCreateBackstop1071(_RouteAnchorMove1071Fixture):
    """B1：create 側的**框架契約**——本單刻意不在 create 加守衛。

    `_create` 尾端對新建列再跑一次 `check_access('create')`，而 route model 上對
    `base.group_user` 唯一 `perm_create=True` 的 ir.rule 是
    `security_access_route_rule_owner_admin_manage`（帶 role 條件）⇒ 一列
    `security_access_id` 指向他人 SA 的新 route 過不了那次 post-check。

    這兩格在 route model 上此前**零覆蓋**（#848／#941 各自對 policy／rule 做過
    對應的 B1，route 沒有）。它們紅了代表框架行為變了，或 create 側是本單範圍外
    的第二個缺口。
    """

    def test_B1_common_member_create_into_foreign_sa_is_rejected(self):
        """共同成員（wsB 的 `role='user'`）走真 RPC 建 route 指向 wsB 的 SA。

        身分刻意與 A1 相同：她讀得到 `sa_b.id`，差別只在這次是 create 不是 move。
        本格斷言「不得成功」且「不得留下任何列」——擋法是 `AccessError`
        （框架 post-check）或 `ValidationError`（某條 constraint）都算通過，
        因為本單的契約是 create 側**不靠**我們自己的守衛。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        before = self.Route.sudo().search_count(
            [('security_access_id', '=', self.sa_b.id)])

        prefix = 'b1x-%s' % uuid.uuid4().hex[:6]
        status, result = self._call_kw(
            _ROUTE, 'create',
            [{'security_access_id': self.sa_b.id, 'subdomain_prefix': prefix}])
        text = self._error_text(result)
        after = self.Route.sudo().search_count(
            [('security_access_id', '=', self.sa_b.id)])
        _logger.info(
            '[1071][B1] RAW foreign-create RPC → http=%s error=%r '
            'victim_routes %d→%d', status, text if 'error' in result else None,
            before, after)

        self.assertIn(
            'error', result,
            '#1071 B1：共同成員不得在受害者的 SA 底下建 route（實際 HTTP %s）'
            % status)
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        self.assertEqual(
            after, before,
            '#1071 B1：被擋的 create 不得留下任何 route（%d→%d）'
            % (before, after))
        self.assertFalse(
            self.Route.sudo().search_count(
                [('subdomain_prefix', '=', prefix)]),
            '#1071 B1：被擋的 create 連 prefix 都不得佔用（那是平台唯一命名空間）')

    def test_B1b_stranger_create_shape_is_also_rejected(self):
        """陌生人（與 wsB 零關聯）走 `with_user()` ＋ ORM 建 route 指向 wsB 的 SA。

        第二種身分＋第二種呼叫形狀。⚠ 本格自己開 savepoint：`_create` 先送
        INSERT 再跑 post-check，例外穿出會讓 transaction 進 aborted 狀態，後面
        所有查詢 `InFailedSqlTransaction`。代價是 DB 狀態被回捲 ⇒ 本格**只**主張
        exception 的性質，⛔ 不做「值沒落地」那種回捲後恆真的斷言。
        """
        carol, _ = _new_group_user(self.env, 'carol1071')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search_count(
                [('workspace_id', '=', self.ws_b.id),
                 ('user_id', '=', carol.id)]),
            '前提：Carol 不得是 wsB 的成員，否則這一格測的是 B1 不是陌生人')

        raised = None
        try:
            with self.env.cr.savepoint():
                _skip_route_sync(self.Route.with_user(carol)).create({
                    'security_access_id': self.sa_b.id,
                    'subdomain_prefix': 'b1y-%s' % uuid.uuid4().hex[:6],
                })
        except Exception as exc:  # noqa: BLE001 —— 例外的性質本身就是待量測的量
            raised = exc

        _logger.info('[1071][B1b] RAW stranger-create raised=%s: %s',
                     type(raised).__name__ if raised else None, raised)
        if raised is None:
            self.fail(
                '#1071 B1b：陌生人不得在受害者的 SA 底下建 route——沒被擋代表'
                'create 側的框架 post-check 失效，那是本單範圍外的第二個缺口')
        self.assertIsInstance(
            raised, (AccessError, ValidationError),
            '#1071 B1b：擋它的必須是授權層或某條 constraint（實際 %s: %s）'
            % (type(raised).__name__, raised))


class TestRouteAnchorMovePositiveControls1071(_RouteAnchorMove1071Fixture):
    """C：守衛不得擋掉合法路徑，也不得取代既有防線。"""

    def test_C1_owner_can_move_route_between_own_sas(self):
        """合法搬移照常過：Mallory 對 SA_A 與 SA_A2 都是 owner。

        這一格紅了代表守衛把正常操作一起擋掉了——那是本單最大的回歸面，也是
        「一律封死 `security_access_id`」那條替代案被否決的理由
        （`test_policy_projection_guard_849.py::test_L1_...` 證明 route 搬 SA
        今天是活的合法路徑）。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'c1')

        status, result, landed = self._attempt_move(
            self.route_a.id, {'security_access_id': self.sa_a2.id}, 'C1')

        self._assert_ok(status, result,
                        '同一個 workspace 內的 route 搬移必須照常成功')
        self.assertEqual(
            landed, self.sa_a2.id,
            '合法搬移的值必須真的落地（實際 %r）' % (landed,))

    def test_C2_superuser_move_is_deliberately_exempt(self):
        """su 豁免是**刻意的**，這裡把它寫成被斷言的契約。

        判準用 `check_access('write')` 打在新 SA 上，而 `check_access()` 在
        `env.su` 下自動 no-op。本欄位的不變量是「值轉移**受限**」不是「值不可變」
        （對照同檔 `_ROUTE_IDENTITY_ANCHOR_FIELDS`：那兩根沒有合法改值路徑，任何
        su 豁免都是洞）——`test_policy_projection_guard_849.py` 的 L1 就是以
        `sudo()` 搬 route 並斷言投影欄重算，一律封死會打紅它。

        su 豁免的代價是守衛的 deny 分支在 su env 下結構上不可達——本檔因此把攻擊
        格全部放在真 RPC／`with_user()` 的非 su 租戶身上，並用這一格把豁免本身釘住。
        """
        _skip_route_sync(self.Route.sudo().browse(self.route_a.id)).write(
            {'security_access_id': self.sa_b.id})

        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'security_access_id'),
            self.sa_b.id,
            'su 路徑的搬移必須照常落地——擋掉它等於擋掉 migration 與所有 sudo '
            '維運路徑，也會打紅 #849 的 L1')

    def test_C6_pull_direction_is_blocked_by_the_framework_pre_check(self):
        """pull 方向（把**別人的** route 拉進自己的 SA）由框架的 pre-check 擋。

        這一側今天靠框架**寫入前**那次 `check_access('write')` 在守：目標列
        `route_b` 掛在 wsB，Mallory 對它只有 member-read（`perm_write=False`）
        ⇒ `super().write()` 之前就被擋下。

        🔴 這一格同時是「**舊**錨點刻意不再驗一次」那個設計決定的守線：本守衛只
        對**新**錨點做 `check_access('write')`，舊錨點方向完全依賴框架這道
        pre-check。它紅了代表那個依賴不再成立，守衛就必須改成新舊都驗。
        形狀照抄 #941 的
        `test_C6_pull_direction_is_blocked_by_the_framework_pre_check`。
        """
        self.authenticate(self.mallory_login, self.mallory_login)

        status, result, landed = self._attempt_move(
            self.route_b.id, {'security_access_id': self.sa_a.id}, 'C6')

        self.assertIn(
            'error', result,
            '#1071 C6：Mallory 對 wsB 的 route 沒有 write，pull 必須被擋'
            '（實際 HTTP %s）' % status)
        text = self._error_text(result)
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        self.assertIn(
            'AccessError', text,
            '#1071 C6：擋它的必須是授權層（框架 pre-check）。量到別的類型代表'
            '舊錨點方向不再由框架守著，本守衛「只驗新錨點」的設計依據就沒了'
            '（實際 %r）' % text)
        self.assertEqual(
            landed, self.sa_b.id,
            '#1071 C6：被拒之後 route_b 必須仍掛在 wsB 的 SA 底下（實際 %r）'
            % (landed,))


class TestRouteAnchorMoveInjectionControls1071(_RouteAnchorMove1071Fixture):
    """🔴 C3a／C4：把守衛停用，證明綠是**守衛造成的**。

    少了這一組，A 系列的綠與「A 系列根本沒打到守衛」（fixture 走偏、RPC 沒到、
    id 打錯、或某條 constraint 順手擋掉了）在輸出上完全不可區分——那正是本 repo
    反覆踩到的假綠形狀。
    """

    def test_C3a_common_member_move_is_rejected_by_the_new_guard(self):
        """A1 的同一次攻擊，但判準是 traceback 的**函式名存在性**。

        ⚠ 本格刻意違反檔頭「攻擊一律走真 RPC」那條紀律：`/web/dataset/call_kw`
        的錯誤封包只帶 `name`／`message`，**拿不到 traceback** ⇒ 走 RPC 就量不到
        「是哪一道擋的」，只量得到「被擋了」——那正是本格唯一要回答的問題。
        RPC 面的覆蓋由 A1–A3 提供，本格補的是它們拿不到的那一格。

        ⛔ 判準不用行號：行號會被任何一次 docstring 增修整段推移，而那種失效是
        靜默的。
        """
        self.env.flush_all()
        self.env.invalidate_all()
        view = self.Route.with_user(self.mallory).browse(self.route_a.id)
        self.assertFalse(view.env.su, '前提：攻擊者的 env 不得是 su')

        try:
            view.write({'security_access_id': self.sa_b.id})
        except AccessError:
            frames = traceback.extract_tb(sys.exc_info()[2])
        else:
            self.fail('共同成員搬 route 必須被拒')

        names = [f.name for f in frames]
        _logger.info('[1071][C3a] RAW frames=%r', names)
        self.assertEqual(
            self._db(_ROUTE, self.route_a.id, 'security_access_id'),
            self.sa_a.id,
            '被拒之後 DB 那一格必須仍是舊 SA')
        self.assertIn(
            '_assert_route_anchor_move_authorised', names,
            '#1071：守衛在的時候，這一格必須紅在**新守衛**上——受害者 SA 無訂閱'
            '⇒ 配額守衛提前 continue、它的 read 副作用不會發生 ⇒ 沒有第二個候選。'
            '實際 traceback 的 frame 依序為 %r' % (names,))

    def test_C4_injection_control_disabling_the_guard_lets_the_attack_land(self):
        """🔴 注入對照：把守衛停用，A1 的攻擊必須**翻回成功且值落地**。

        這是「A1 的綠是守衛造成的」判別式，也是本單端到端可利用性的直接證據
        （真 HTTP session ＋非 su 租戶＋守衛停用）。run 2674 的 B2c 已經量到同一個
        結論；本格把它變成**每次 CI 都重跑**的對照，而不是一次性的觀測。

        ⚠ **本格對「呼叫點被整行拿掉」那一種 mutation 零鑑別力**：`patch(_GUARD)`
        替換的是 module 屬性，`write()` 裡不再呼叫它時 patch 根本不會被觸發 ⇒ 攻擊
        照樣落地、本格照樣綠。⇒ ⛔ 不可拿本格的綠單獨當「守衛還在」的證據；那件事
        由 A1／A2／A3／B2c／B2／C3a 負責，本格負責的是反向那一半（「守衛在的時候，
        把它拿掉攻擊就會成功」）。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'c4')

        with patch(_GUARD):
            status, result, landed = self._attempt_move(
                self.route_a.id, {'security_access_id': self.sa_b.id}, 'C4')

        self._assert_ok(
            status, result,
            '注入對照：守衛停用後這條 RPC 必須成功（它是 A1 的同一條）')
        self.assertEqual(
            landed, self.sa_b.id,
            '注入對照：守衛停用後攻擊必須落地——沒落地代表 A1 的綠不是守衛造成的'
            '（實際落地 %r）' % (landed,))


class TestRouteAnchorMoveEdgeCases1071(_RouteAnchorMove1071Fixture):
    """E：守衛自己那幾條分支的覆蓋（早退、清空、與既有守衛的順序）。"""

    def test_E1_clearing_the_anchor_is_rejected_by_required_not_by_the_guard(self):
        """`security_access_id` 是 `required=True`，守衛的「清空不守」分支是否可達。

        守衛照 #848／#941 同形保留 `if not new_id: return`（沒有「新 SA」要授權）。
        這一格量的是：清空實際上被**哪一層**擋下——只要它被擋，守衛不守清空就不是
        破口。

        ⚠ 包在 `env.cr.savepoint()` 裡是刻意的：清空會在 flush 時撞 NOT NULL，
        那是 SQL 之後才丟的例外，不回捲會毒掉 cursor。因此本格**只**主張「有被
        擋」與被擋的**層次**，⛔ 不主張回捲後的 DB 值。

        判準鎖的是**性質**不是名字（照 #973 F1）：(a) 不是我們自己那三層擋的
        （`AccessError`／`UserError`／`ValidationError` 三類互斥，這一條就是
        「守衛確實早退了」的直接證據）、(b) 是 `psycopg2.IntegrityError`、
        (c) `pgcode` 等於 `psycopg2.errorcodes` 的 `NOT_NULL_VIOLATION`
        （SQLSTATE 是 ISO 標準碼，class 名會隨 driver 版本漂）、(d) 訊息指名
        `security_access_id` 那一根 column。
        """
        raised = None
        try:
            with self.env.cr.savepoint():
                self.Route.with_user(self.mallory).browse(
                    self.route_a.id).write({'security_access_id': False})
        except Exception as exc:  # noqa: BLE001 —— 例外的性質本身就是待量測的量
            raised = exc
        _logger.info(
            '[1071][E1] RAW clear security_access_id → raised=%s pgcode=%r: %s',
            type(raised).__name__ if raised else None,
            getattr(raised, 'pgcode', None), raised)
        if raised is None:
            self.fail(
                '`security_access_id` 宣告 required=True，清空必須被擋'
                '（沒被擋代表 required 沒有生效，那是另一個缺口）')

        self.assertNotIsInstance(
            raised, (AccessError, UserError, ValidationError),
            '#1071 E1：清空**不該**被我們自己那三層擋下——守衛對清空是刻意早退的。'
            '量到 %s 代表擋它的層次跟守衛 docstring 寫的不是同一個（%s: %s）'
            % (type(raised).__name__, type(raised).__name__, raised))
        self.assertIsInstance(
            raised, IntegrityError,
            '#1071 E1：擋它的必須是 DB 的完整性約束（實際 %s: %s）'
            % (type(raised).__name__, raised))
        self.assertEqual(
            getattr(raised, 'pgcode', None), NOT_NULL_VIOLATION,
            '#1071 E1：SQLSTATE 必須是 NOT NULL violation——換成別的完整性約束'
            '（unique／foreign key）代表擋它的是別的東西，守衛 docstring 那句'
            '「required=True ⇒ 撞 NOT NULL」就不再成立（實際 class=%s pgcode=%r'
            ' msg=%s）' % (type(raised).__name__,
                           getattr(raised, 'pgcode', None), raised))
        self.assertIn(
            'security_access_id', str(raised),
            '#1071 E1：訊息必須指名 `security_access_id` 那一根 column——換成別的'
            '欄位的 NOT NULL 代表這一格量到的是別的缺陷（實際 %s）' % (raised,))

    def test_E3_unchanged_anchor_short_circuits_before_check_access(self):
        """送**與現值相同**的 `security_access_id` ⇒ 守衛早退，不做授權檢查。

        ## 🔴 這一格靠什麼紅（設計時就要想清楚的那件事）

        ⛔ **不能**只斷言「沒有 raise」——那是恆綠的：把早退兩行拿掉，守衛會改成
        對**現在這顆** SA 做 `check_access('write')`，而**能寫這一列的人對它必定
        有 owner／admin**（route 與 SA 的 `..._owner_admin_manage` domain 走同一
        條 workspace walk）⇒ 檢查照樣通過、寫入照樣成功、測試照樣綠。**沒有任何
        actor 能讓那個分支的移除變成一個可見的拒絕。**

        ⇒ 判別式改成量**那次呼叫本身發生了沒有**：早退的契約是「省掉一次多餘的
        `check_access`」，不是「放行」。本格在 SA model 的 registry class 上掛
        spy，斷言：

        * **陽性對照**（值真的改變）：spy 必須錄到對**新** SA 的
          `check_access('write')` —— 少了它，「零呼叫」與「spy 根本壞了」不可區分。
        * **本體**（值沒改變）：spy **不得**錄到對那顆 SA 的
          `check_access('write')`，而且那次寫入必須**成功、值仍在原處**。

        ⚠ spy 掛在 `type(env[_SA])`（registry class，同 process 的 HTTP server
        thread 也吃得到），`finally` 還原：原本 class 自己 `__dict__` 裡沒有這個
        名字（`check_access` 繼承自 `BaseModel`）就 `delattr`，有就寫回去——
        ⛔ 不能無條件 `delattr` 也不能無條件覆寫。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'e3')

        SaCls = type(self.env[_SA])
        _MISSING = object()
        own_attr = SaCls.__dict__.get('check_access', _MISSING)
        original = SaCls.check_access
        seen = []

        def _spy(records, operation):
            seen.append((operation, tuple(records.ids)))
            return original(records, operation)

        SaCls.check_access = _spy
        try:
            # 陽性對照：值真的改變 ⇒ 守衛必須對**新** SA 做一次 write 檢查
            status, result, landed = self._attempt_move(
                self.route_a.id, {'security_access_id': self.sa_a2.id},
                'E3-pc')
            self._assert_ok(status, result,
                            '前提：同 workspace 的合法搬移必須成功（＝C1 的形狀）')
            self.assertEqual(
                landed, self.sa_a2.id,
                '前提：合法搬移的值必須真的落地（實際 %r）' % (landed,))
            checked = [i for op, ids in seen if op == 'write' for i in ids]
            _logger.info('[1071][E3-pc] RAW SA check_access calls=%r', seen)
            self.assertIn(
                self.sa_a2.id, checked,
                '探針前提：值真的改變時，spy 必須錄到守衛對新 SA(%r) 的'
                ' `check_access(\'write\')`；錄不到有兩種成因——spy 沒掛上去，'
                '或**守衛的呼叫點／那行 check_access 被拿掉了**——兩者都會讓'
                '下面那句「零呼叫」零鑑別力（實際錄到 %r）'
                % (self.sa_a2.id, seen))
            seen.clear()

            # 本體：送與現值相同的 security_access_id ⇒ 早退分支
            status, result, landed = self._attempt_move(
                self.route_a.id, {'security_access_id': self.sa_a2.id}, 'E3')
        finally:
            if own_attr is _MISSING:
                delattr(SaCls, 'check_access')
            else:
                SaCls.check_access = own_attr

        _logger.info('[1071][E3] RAW SA check_access calls=%r', seen)
        self._assert_ok(
            status, result,
            '#1071 E3：送與現值相同的 security_access_id 是合法的 no-op 寫入，'
            '必須成功（擋掉它會打死 UI 整批送欄位那條路）')
        self.assertEqual(
            landed, self.sa_a2.id,
            '#1071 E3：no-op 寫入之後值必須仍在原處（實際 %r）' % (landed,))
        write_checked = [i for op, ids in seen if op == 'write' for i in ids]
        self.assertNotIn(
            self.sa_a2.id, write_checked,
            '#1071 E3：`security_access_id` 沒有真的改變時，守衛必須在做任何授權'
            '檢查**之前**就早退——實際對 SA %r 做了 `check_access(\'write\')`。'
            '這一格紅通常代表那兩行早退被拿掉了（拿掉之後功能不會壞，只是每次 UI '
            '整批送欄位都多驗一次）。本次錄到的全部呼叫：%r'
            % (self.sa_a2.id, seen))

    def test_E4_mixed_payload_is_rejected_by_the_identity_guard_first(self):
        """`security_access_id` ＋ identity anchor 同時送 ⇒ **identity 那道先拒**。

        `write()` 裡三道守衛的順序是刻意的：`_reject_identity_anchor_writes`
        （值不可變，對 superuser 也擋）→ prefix 不可變 → 本守衛（授權層，su 豁免）。
        與 policy／rule 側「投影欄／identity 守衛先、授權守衛後」同序。

        判別式＝ **exception class**：identity 守衛拋 `UserError`、本守衛拋
        `AccessError`，兩類互斥。順序若被對調，這個 payload 會改拋 `AccessError`
        ⇒ 本格紅。

        ⚠ 攻擊者刻意用**兩個都非法**的值（錨點指向 wsB 的 SA、`dns_record_id`
        指向偽造值）：兩道守衛都會拒，所以「拒了」不具鑑別力，**只有拒的是哪一類**
        才有——這正是本格存在的理由。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        self._tenant_write_positive_control(self.route_a.id, 'e4')

        status, result = self._call_kw(
            _ROUTE, 'write',
            [[self.route_a.id], {'security_access_id': self.sa_b.id,
                                 'dns_record_id': 'forged-1071'}])
        landed_sa = self._db(_ROUTE, self.route_a.id, 'security_access_id')
        landed_dns = self._db(_ROUTE, self.route_a.id, 'dns_record_id')
        text = self._error_text(result)
        _logger.info(
            '[1071][E4] RAW mixed-payload RPC → http=%s error=%r '
            'landed_sa_id=%r landed_dns_record_id=%r',
            status, text if 'error' in result else None, landed_sa, landed_dns)

        self.assertIn(
            'error', result,
            '#1071 E4：混合 payload 必須被拒（實際 HTTP %s）' % status)
        self.assertNotIn(
            'SessionExpired', text,
            '探針前提：回的是 SessionExpired ⇒ RPC 沒帶認證，這一格零意義（%r）'
            % text)
        self.assertIn(
            'UserError', text,
            '#1071 E4：先拒的必須是 identity-anchor 守衛（UserError）。量到'
            ' AccessError 代表三道守衛的順序被對調了——「值不可變的守衛必須排在'
            '授權層之前」那條要求就不再成立（實際 %r）' % text)
        self.assertEqual(
            landed_sa, self.sa_a.id,
            '#1071 E4：被拒之後真錨點必須原封不動（實際 %r）' % (landed_sa,))
        self.assertFalse(
            landed_dns,
            '#1071 E4：被拒之後 dns_record_id 也必須原封不動（實際 %r）'
            % (landed_dns,))


class TestRouteAnchorMoveRegression1071(_RouteAnchorMove1071Fixture):
    """L：守衛不得誤擋今天活著的合法路徑。"""

    def test_L1_legal_move_recomputes_the_policy_projection(self):
        """合法搬移（**非 su**，真租戶）之後，底下 policy／rule 的投影欄必須重算。

        `test_policy_projection_guard_849.py::test_L1_route_moved_to_another_sa_
        recomputes_projection` 已經對 `sudo()` 那條路做過同一件事；本格把它拉到
        **非 su 的真租戶身分**——那是我的守衛唯一會 fire 的路徑，也是 849 L1
        照不到的那一格（su 之下守衛整條 no-op，誤擋在那裡量不到）。

        判準是兩件事一起成立：**(a) 不 raise**、**(b) 投影欄真的被算成新值**。
        只驗 (a) 會把「守衛沒擋、但值也沒更新」放行——那同樣是壞的，而且症狀是
        安靜的（policy 的歸屬停在舊 workspace）。
        """
        pol = self._policy(self.route_a, 'l1')
        rule = pol.sudo().rule_ids
        self.assertEqual(
            len(rule), 1, 'fixture：policy 必須恰有一條 rule')
        self.assertEqual(
            self._db(_POLICY, pol.id, 'security_access_id'), self.sa_a.id,
            '前提：投影欄一開始必須指向舊 SA')

        self.authenticate(self.mallory_login, self.mallory_login)
        status, result, landed = self._attempt_move(
            self.route_a.id, {'security_access_id': self.sa_a2.id}, 'L1')

        self._assert_ok(status, result, '同 workspace 的合法搬移必須成功')
        self.assertEqual(landed, self.sa_a2.id, '搬移的值必須落地')
        self.assertEqual(
            self._db(_POLICY, pol.id, 'security_access_id'), self.sa_a2.id,
            '#1071 L1：搬移之後 policy 的投影欄必須重算成新 SA——停在舊值代表'
            '重算被守衛吃掉了（守衛沒 raise、但值也沒更新，症狀是安靜的）')
        self.assertEqual(
            self._db(_RULE, rule.id, 'security_access_id'), self.sa_a2.id,
            '#1071 L1：rule 的投影欄同樣必須重算成新 SA')
