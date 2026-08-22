"""#798：`cf_policy_id` 沒有欄位級寫入守衛——租戶塞進去的值會被當成可信。

## 缺陷

`security_access_access_policy.cf_policy_id` 是**伺服器擁有的外部物件 id**（CF
reusable policy），但這一格今天沒有任何欄位級守衛：

* 欄位定義沒有 `groups=`（`models/security_access_access_policy.py` 的
  `cf_policy_id = fields.Char(...)`）⇒ `check_field_access_rights('write')` 不擋；
* 該 model 的 `create()` / `write()` 沒有 strip／reject 這一對守衛（route 側的
  `_ROUTE_IDENTITY_ANCHOR_FIELDS` 三件套在這支 model 上一件都沒有）；
* ACL 對 `base.group_user` 是 `1,1,1,1`（`security/ir.model.access.csv`），而
  ir.rule 過濾的是**列**不是**欄位** ⇒ workspace owner／admin 對自己列有完整寫入。

而這個欄位決定的是 operator **唯一的刪除通道** `existing_policy_ids` 裡有哪些
帳號級 CF Access policy：三腿的 `_build_existing_policy_ids()` 與帳本的
`cf_access_policy_reap._trusted_ids_for()` 都逐字從列上讀它，並把「列上讀到的
值」當成**伺服器擁有、因此可信**的那一半（#781／#792 為 #783 立的判準）。列上
的值其實租戶寫得進來，那道判準就不成立。

## 本檔補的正是 #798 自陳缺的那一格

#798 的證據強度自陳：「**沒有跑過真的租戶 RPC**……推論鏈每一環都實查過，但
端到端未驗」。A 系列就是那一格——以 `base.group_user` 租戶身分走真的
`/web/dataset/call_kw`，斷言偽造值**不得落地到 Postgres**。修法之前它必須是
紅的，而紅的內容（實際落地值 = 攻擊者選的字串）就是那條推論鏈的端到端證據。

## 為什麼一定要走真 RPC

`with_user()` ＋ `assertRaises` 比真 RPC 弱兩格：

1. `TransactionCase` 預設是 su env，任何寫成 `env.su` 豁免的守衛在那裡整條
   跳過（`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）；
2. `/web/dataset/call_kw` 把**呼叫端 context 整包**換進 env
   （`odoo/api.py::call_kw`），這正是 `default_<field>` 與逃生門偽造的載體，
   in-process 呼叫模擬不到。

## 測試分層（每一格都要能分辨「守住了」與「探針壞了」）

* **A** — write 側：租戶真 RPC 改自己列上的 `cf_policy_id`。
  每一條攻擊前面都先跑一次**同一條 RPC、同一列、租戶合法欄位**的寫入當陽性
  對照：少了它，「攻擊被擋」與「這個租戶對這一列根本寫不動」不可區分。
* **B** — create 側 squat 的三個載體：vals 直接帶、`default_<field>` context、
  `ir.default` 的列。B 每一格都配一個**同機制、非守衛欄位**的對照（`decision`）
  ——少了它，「沒被注入」與「這個載體在本環境根本不作用」分不出來，後者會讓
  本檔在守衛被拿掉之後**仍然是綠的**。
  B 另有陰性對照：`create` 本身必須成功、其他欄位正常。「create 直接炸掉」對
  「攻擊者的值沒落地」那條斷言同樣是綠的（同型翻車見
  `test_sa_anchor_default_carriers_753.py` 的 D 系列）。
* **E** — 無 su 豁免：`sudo()` 直接 write（不帶任何逃生門）必須也被拒。理由逐字
  寫在 `models/security_access.py` 的 `_INTERNAL_IDENTITY_WRITE` 註解裡。

🔴 **本檔任何「預期例外 ＋ 之後驗 DB 狀態」的格子一律用裸 `try/except` ＋
`else: self.fail(...)`，不用 `assertRaises`**。Odoo 的 `BaseCase._assertRaises`
把 with 區塊包進 `env.cr.savepoint()`（`_FlushingSavepoint`：`cr.clear()` ＋
`ROLLBACK TO SAVEPOINT`）⇒ 區塊內的寫入不論有沒有 flush 都被回捲，於是「守衛擋在
寫入之前」與「寫入發生過又被回捲」在斷言輸出上不可區分。詳見 E1 的 docstring。
之後新增的格子（E3 的偽造 context 值、任何錯誤路徑對照）**直接照這個形狀寫**，
不要先寫 `assertRaises` 版再改。

⚠ 本檔**刻意不 import 任何尚未存在的 sentinel**：RED 這一輪要看到的紅必須落在
「值落地了」那條斷言上，import error 是另一種紅。逃生門的正向對照（開閘仍寫得
進、偽造的 context 值打不開閘）與三腿回存的陽性對照在修法落地後才加得進來。

## fixture 為什麼用 SQL 種 `cf_policy_id`

守衛上線後，`pol.cf_policy_id = ...` 這種 fixture 寫法會**被自己要測的守衛擋
掉**，測試會紅在 fixture 上。種值改走 `cr.execute`：它與守衛正交，修法前後同一
份 fixture 都成立。
"""

import ast
import inspect
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

# C／F／E3 用：三腿各自的開閘 helper（fixture 要擺的是**伺服器端**前提，
# 不是受測動作），以及本單新增的那把。
from ..models.cloud_service import (
    _internal_cs_access_identity,
    _internal_provision,
)
from ..models.security_access import (
    _INTERNAL_IDENTITY_WRITE,
    _internal_identity,
    _internal_policy_cf_write,
    _is_internal_identity_write,
    _is_internal_policy_cf_write,
    _reject_identity_anchor_writes,
    _skip_route_sync,
    _strip_identity_anchors,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from .smart_home_fixture import sh_model

#: SA 腿的 operator client seam（patch 這裡才攔得到 route sync 那一次呼叫）。
_MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                   'get_paas_operator_client')

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 798' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _PolicyCfId798Fixture(HttpCase):
    """W1（alice owner）＋ 一條 route ＋ 一列已經有伺服器端 cf id 的 policy。

    SA 刻意**不給 `tunnel_id`**：`_access_sync_skip_reason()` 的第一道就是
    「the Security Access has no live tunnel」⇒ 本檔任何 create／write 都不會
    真的打 operator（否則測試會紅在網路上，那不是本檔要驗的分支）。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice798')
        self.assertFalse(
            self.alice.has_group('base.group_system'),
            '前提：攻擊者只能有 base.group_user（%s 撞了）' % self.alice.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 798'})
        if not self.WorkspaceAccess.sudo().search(
                [('workspace_id', '=', self.w1.id),
                 ('user_id', '=', self.alice.id)]):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id,
                'user_id': self.alice.id, 'role': 'owner'})

        self.sa = self.SA.sudo().create(
            {'name': 'SA 798', 'workspace_id': self.w1.id})
        self.assertFalse(
            self.sa.tunnel_id,
            '前提：SA 不得有 live tunnel，否則 sync 會真的打 operator')
        self.route = self.Route.sudo().create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'r798-%s' % uuid.uuid4().hex[:6],
        })
        self.pol = self._make_policy('seeded')
        # 伺服器端「真正的」那顆 CF policy id。攻擊要做的就是把它換掉。
        self.server_cf_id = 'cf-server-798-%s' % uuid.uuid4().hex[:8]
        self._seed_cf_policy_id(self.pol.id, self.server_cf_id)

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #
    def _make_policy(self, name):
        """一列合法 policy（`_check_at_least_one_include` 要求 ≥1 include）。"""
        return self.Policy.sudo().create({
            'route_id': self.route.id,
            'name': 'pol-798-%s' % name,
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })

    def _seed_cf_policy_id(self, policy_id, value):
        """直接寫 Postgres 那一格——刻意繞開 ORM。

        本檔要立的守衛會擋掉一切非開閘的 `cf_policy_id` 寫入，包含 fixture 自己
        的。用 SQL 種值讓同一份 fixture 在修法前後都成立（修法後 fixture 紅在
        自己身上，是本 repo 已經踩過的形狀）。
        """
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE "%s" SET cf_policy_id = %%s WHERE id = %%s'
            % self.env[_POLICY]._table, (value, policy_id))
        self.env.invalidate_all()

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。

        `odoo/api.py::call_kw` 會 `kwargs.pop('context')` 再
        `recs.with_context(context)` ⇒ 呼叫端 context 整包換進 env，這正是
        `default_<field>` 載體與 context 逃生門偽造的可達性來源。
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
        return resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result,
                         msg + '（回 %s）' % self._error_text(result))
        return result.get('result')

    def _db(self, model_name, record_id, field_name):
        """直接讀 Postgres 的那一格。

        `flush_all()` 先把尚未 flush 的寫入推下去，才不會讓「值其實被接受了」
        讀起來像沒變——「沒有例外」與「沒有寫入」是兩個主張。
        """
        self.env.flush_all()
        # 欄位名一律來自 model 自己的 _fields，不來自使用者輸入。
        self.assertIn(field_name, self.env[model_name]._fields)
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, self.env[model_name]._table), (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '記錄 %s#%s 不在 DB 裡' % (model_name, record_id))
        return row[0]

    def _set_ir_default(self, model_name, field_name, value):
        """以**租戶身分經 RPC** 寫一列 `ir.default`。

        第 4 個參數是 `user_id`（不是 `for_all_users`）：`True` ⇒ 當下這個
        使用者自己那一列，正是 `_get_model_defaults` 的 `d.user_id = %s` 會收的。
        """
        return self._call_kw(
            'ir.default', 'set', [model_name, field_name, value, True])

    def _assert_ir_default_stored(self, model_name, field_name, value, user):
        """陽性對照：注入本身要成功。

        少了它，「欄位是空的」與「`ir.default.set` 根本沒寫進去」分不出來。
        """
        stored = self.env['ir.default'].sudo().search([
            ('field_id.model', '=', model_name),
            ('field_id.name', '=', field_name),
            ('user_id', '=', user.id),
        ])
        self.assertTrue(
            stored,
            '陽性對照：租戶的 ir.default.set(%s) 必須真的落庫' % field_name)
        self.assertIn(
            json.dumps(value), stored.mapped('json_value'),
            '陽性對照：落庫的值必須是攻擊者選的那個，否則本測試零鑑別力')

    def _tenant_policy_vals(self, name, **extra):
        vals = {
            'route_id': self.route.id,
            'name': name,
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'b@example.com'})],
        }
        vals.update(extra)
        return vals


class TestPolicyCfIdRpcWriteGuard798(_PolicyCfId798Fixture):
    """A：租戶走真 RPC 改自己列上的 `cf_policy_id`。"""

    def test_A1_tenant_rpc_write_must_not_land_a_forged_cf_policy_id(self):
        """🔴 #798 主格，也是該 issue 自陳「端到端未驗」的那一格。

        危害不需要知道任何別人的 id：把自己列上那顆**真實存在**的 cf id 覆寫
        掉，那顆 CF reusable policy 就從 DB 消失、不在任何帳本上，而下一次 sync
        送出去的 `existing_policy_ids` 帶的是假 id（operator DELETE 一顆不存在
        的 policy，CF 回 404 被當成功 ⇒ 靜默）⇒ 永久 CF 孤兒。
        """
        self.authenticate(self.alice_login, self.alice_login)

        # 陽性對照：同一條 RPC、同一列，租戶合法欄位必須寫得進去。少了這一條，
        # 下面的「攻擊被擋」與「alice 對這一列根本寫不動」不可區分。
        self._assert_ok(
            self._call_kw(_POLICY, 'write',
                          [[self.pol.id], {'name': 'renamed-by-tenant-798'}]),
            '陽性對照：租戶對自己 workspace 內的 policy 有合法寫入權')
        self.assertEqual(
            self._db(_POLICY, self.pol.id, 'name'), 'renamed-by-tenant-798',
            '陽性對照：合法欄位必須真的落地，否則本測試零鑑別力')

        forged = 'forged-cf-798-%s' % uuid.uuid4().hex[:8]
        result = self._call_kw(
            _POLICY, 'write', [[self.pol.id], {'cf_policy_id': forged}])
        landed = self._db(_POLICY, self.pol.id, 'cf_policy_id')
        self.assertEqual(
            landed, self.server_cf_id,
            '#798：租戶 RPC 不得改動 `cf_policy_id`（伺服器擁有的 CF 物件 id）'
            '——實際落地 %r，伺服器端原值 %r' % (landed, self.server_cf_id))
        self.assertIn(
            'error', result,
            '#798：守衛必須**大聲**拒絕（靜默丟棄會讓呼叫端以為寫成功了）')

    def test_A2_tenant_rpc_write_must_not_seed_cf_policy_id_from_empty(self):
        """從**空**寫成任意字串——格 2（跨租戶刪除）的形狀。

        A1 是覆寫既有值，這一格是從零種進去：它才是「把別人的 policy id 塞進
        自己的列、讓下一次 sync 去刪別人的東西」那條路徑的實際形狀。兩者的守衛
        是同一道，但只驗 A1 的話，一個「只擋覆寫、不擋從空填入」的實作會綠。
        """
        pol = self._make_policy('empty')
        self.assertFalse(
            self._db(_POLICY, pol.id, 'cf_policy_id'),
            '前提：一列全新的 policy 出生時 cf_policy_id 必須是空的')

        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(
            self._call_kw(_POLICY, 'write',
                          [[pol.id], {'name': 'renamed-798-a2'}]),
            '陽性對照：租戶對這一列有合法寫入權')

        victim = 'victim-cf-798-%s' % uuid.uuid4().hex[:8]
        result = self._call_kw(
            _POLICY, 'write', [[pol.id], {'cf_policy_id': victim}])
        landed = self._db(_POLICY, pol.id, 'cf_policy_id')
        self.assertFalse(
            landed,
            '#798：租戶不得把任意 CF policy id 種進自己的列（實際落地 %r）'
            % (landed,))
        self.assertIn('error', result,
                      '#798：守衛必須大聲拒絕')


class TestPolicyCfIdCreateSquat798(_PolicyCfId798Fixture):
    """B：create 側的三個載體都不得種進 `cf_policy_id`。

    `decision` 是**刻意挑的**對照欄位：有 `default='allow'`、租戶合法設定得了、
    且不是任何守衛的標的。它必須被注入成功，否則下面每一條「沒被注入」都可能
    只是這個載體在本環境根本不作用。
    """

    def test_B1_vals_cannot_squat_cf_policy_id_at_create(self):
        """呼叫端把 `cf_policy_id` 直接寫在 create 的 vals 裡。"""
        self.authenticate(self.alice_login, self.alice_login)
        squat = 'squat-cf-798-%s' % uuid.uuid4().hex[:8]
        pol_id = self._assert_ok(self._call_kw(
            _POLICY, 'create',
            [self._tenant_policy_vals('b1-798', decision='deny',
                                      cf_policy_id=squat)]),
            '陰性對照：帶了 cf_policy_id 的 create **本身**不得失敗——'
            '「create 直接炸掉」對「攻擊者的值沒落地」同樣是綠的')

        landed = self._db(_POLICY, pol_id, 'cf_policy_id')
        self.assertFalse(
            landed,
            '#798：vals 帶的 cf_policy_id 不得落地（實際落地 %r）' % (landed,))
        # 陰性對照：這一列必須是一列**正常**的 policy，不是半殘品。
        self.assertEqual(self._db(_POLICY, pol_id, 'name'), 'b1-798',
                         '陰性對照：其他欄位必須正常落地')
        self.assertEqual(self._db(_POLICY, pol_id, 'decision'), 'deny',
                         '陰性對照：租戶合法設定的 decision 必須留著')
        self.assertTrue(
            self.Policy.sudo().browse(pol_id).rule_ids,
            '陰性對照：巢狀 rule_ids 必須跟著建起來')

    def test_B2_default_context_cannot_seed_cf_policy_id(self):
        """載體 1：`default_<field>` context（`call_kw` 整包換進 env）。"""
        self.authenticate(self.alice_login, self.alice_login)
        squat = 'ctxdef-cf-798-%s' % uuid.uuid4().hex[:8]
        pol_id = self._assert_ok(self._call_kw(
            _POLICY, 'create', [self._tenant_policy_vals('b2-798')],
            {'context': {'default_cf_policy_id': squat,
                         'default_decision': 'deny'}}),
            'create 不該回 JSON-RPC error')

        self.assertEqual(
            self._db(_POLICY, pol_id, 'decision'), 'deny',
            '陽性對照：`default_<field>` context 對**非守衛欄位**必須真的生效'
            '——不生效的話，下面那條就算守衛被拿掉也會是綠的')
        landed = self._db(_POLICY, pol_id, 'cf_policy_id')
        self.assertFalse(
            landed,
            '#798：`default_cf_policy_id` 不得落地（實際落地 %r）' % (landed,))

    def test_B3_ir_default_cannot_seed_cf_policy_id(self):
        """載體 2：租戶自己那一列 `ir.default`。

        比 context 載體更隱蔽——它與呼叫端 payload 無關，連走 `type="json"` 的
        BFF 端點（context 來自 session）也照樣生效。只堵 context 的修法會在這
        一格紅。
        """
        self.authenticate(self.alice_login, self.alice_login)
        squat = 'irdef-cf-798-%s' % uuid.uuid4().hex[:8]
        self._assert_ok(
            self._set_ir_default(_POLICY, 'cf_policy_id', squat),
            'ir.default.set(cf_policy_id) 必須成功'
            '（租戶對 ir.default 有完整 CRUD）')
        self._assert_ir_default_stored(
            _POLICY, 'cf_policy_id', squat, self.alice)
        self._assert_ok(
            self._set_ir_default(_POLICY, 'decision', 'deny'),
            'ir.default.set（對照欄位）必須成功')

        pol_id = self._assert_ok(self._call_kw(
            _POLICY, 'create', [self._tenant_policy_vals('b3-798')]),
            'create 不該回 JSON-RPC error')

        self.assertEqual(
            self._db(_POLICY, pol_id, 'decision'), 'deny',
            '陽性對照：`ir.default` 對**非守衛欄位**必須真的生效')
        landed = self._db(_POLICY, pol_id, 'cf_policy_id')
        self.assertFalse(
            landed,
            '#798：`ir.default` 注入的 cf_policy_id 不得落地（實際落地 %r）'
            % (landed,))


class TestPolicyCfIdNoSuExemption798(_PolicyCfId798Fixture):
    """E：守衛對 su／sudo 一律生效，只有模組私有 sentinel 開得了門。

    寫成 `env.su` 豁免的守衛會在每個 `TransactionCase`（su env）與每條
    `sudo()` 路徑上靜默停用——正是測試本該證明它有效的地方
    （`docs/decisions/patterns/su-env-exempt-guard-false-green.md`，理由也逐字
    寫在 `models/security_access.py` 的 `_INTERNAL_IDENTITY_WRITE` 註解裡）。
    """

    def test_E1_sudo_write_without_the_hatch_is_rejected(self):
        """`sudo()` **不帶**逃生門直接改值：必須被拒，而且值不得落地。

        🔴 **刻意用裸 `try/except` 而不是 `assertRaises`**，這不是風格：

        Odoo 的 `BaseCase._assertRaises`（`odoo/tests/common.py:472-492`）在
        with 區塊外面包了 `self.env.cr.savepoint()`，而那是 `_FlushingSavepoint`
        （`odoo/sql_db.py:123-141`，`savepoint()` 的 `flush` 預設為 True）——它的
        `rollback()` 是 `cr.clear()`（丟掉尚未 flush 的 towrite）＋
        `ROLLBACK TO SAVEPOINT`。例外穿出時，**區塊內的寫入不論有沒有 flush 都被
        回捲**。

        後果正好打掉本格的判別式：「守衛在 `super().write()` **之前** raise」與
        「守衛在 `super().write()` **之後**才 raise、寫入被 savepoint 回捲」，對
        下面那句 `assertEqual` 的輸出**完全一樣**。而兩者不是同一種守衛——後者
        代表偽造值真的進過 DB。

        裸 `try/except` 沒有那層 savepoint：寫入若真的發生過，同一條 transaction
        的 `self.env.cr` 讀得到（未 commit 也看得見）⇒ 下面那句會紅。所以這個寫法
        不只是避開假綠，它是把判別式**裝回去**。

        `else: self.fail(...)` 不可省：少了它，「根本沒 raise」也會靜靜通過。
        """
        forged = 'sudo-forged-798-%s' % uuid.uuid4().hex[:8]
        try:
            # 型別刻意收緊成 `UserError`：route／CS／SH 三支同型守衛逐字都是
            # `UserError`。用裸 `except Exception` 的話，fixture 自己爆掉的紅
            # 同樣會被吞成綠。
            self.Policy.sudo().browse(self.pol.id).write(
                {'cf_policy_id': forged})
        except UserError:
            pass
        else:
            self.fail(
                '#798：sudo 不是開閘，`cf_policy_id` 的寫入必須被拒，'
                '但 write() 沒有拋出 UserError')
        self.assertEqual(
            self._db(_POLICY, self.pol.id, 'cf_policy_id'), self.server_cf_id,
            '#798：被拒之後伺服器端原值必須原封不動——這一句是「守衛擋在寫入'
            '之前」的判別式，所以上面刻意不用 assertRaises（見 docstring）')

    def test_E2_sudo_create_without_the_hatch_cannot_squat(self):
        """create 側同理：`sudo()` 帶著 vals 也不得種進伺服器擁有的 id。

        create 側刻意**不 raise 而是壓值**（`copy()` 與一般資料載入照樣要能跑），
        所以這一格斷言的是「值被換成伺服器端值」，不是「炸掉」。
        """
        squat = 'sudo-squat-798-%s' % uuid.uuid4().hex[:8]
        pol = self.Policy.sudo().create(self._tenant_policy_vals(
            'e2-798', cf_policy_id=squat))
        landed = self._db(_POLICY, pol.id, 'cf_policy_id')
        self.assertFalse(
            landed,
            '#798：sudo 不是開閘，vals 的 cf_policy_id 必須被壓成伺服器端值'
            '（實際落地 %r）' % (landed,))


# ---------------------------------------------------------------------- #
#  C：伺服器端合法回存路徑必須仍寫得動（守衛不得把 sync 管線一起擋死）      #
# ---------------------------------------------------------------------- #
#
# 🔴 只寫 4 格是刻意的。三腿 13 個伺服器端寫入站點裡，**9 個已經有既存測試鎖著**
# ——那些測試本身就是漏改偵測器（漏包 sentinel ⇒ 它們會紅）。再寫一份等於同一件事
# 測兩遍。這裡只補**今天真的沒有偵測器**的那 4 條盲區：
#
#   C2  security_access.py    SecurityAccessRoute._sync_access_to_operator（第二個迴圈）
#   C4  security_access.py    SecurityAccessRoute._access_mark_protection_lost
#   C7  cloud_service.py      CloudService._access_mark_protection_lost
#   C11 smart_home_route.py   SmartHomeRoute._access_mark_protection_lost
#
# 那 4 條為什麼沒有鎖：`test_access_failopen_compensation_794.py` 確實呼叫
# `_access_mark_protection_lost()`，但**只斷言 route 那三個欄位**；該檔的
# `_policy_cf()` helper 全檔零呼叫（dead helper）⇒「看起來有守、其實沒有」。
#
# 其餘 9 條各自由誰鎖住，逐條列在 PR body（不在這裡重述，兩份散文一定會漂）。


def _db_cf_ids(env, policy_ids):
    """直接讀 Postgres 的 cf_policy_id（繞開 ORM cache）。"""
    env.flush_all()
    env.cr.execute(
        'SELECT id, cf_policy_id FROM %s WHERE id IN %%s ORDER BY id'
        % env[_POLICY]._table, (tuple(policy_ids),))
    return dict(env.cr.fetchall())


@tagged('post_install', '-at_install')
class TestPolicyCfIdServerWritebackStillWorks798(TransactionCase):
    """C：三腿的伺服器端清值／回存在守衛上線後仍然寫得動。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.Policy = self.env[_POLICY]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 798C %s' % uuid.uuid4().hex[:6]})

    # ------------------------------------------------------------------ #
    #  共用：擺一顆「列上真的有 cf id」的前提，並回傳它                    #
    # ------------------------------------------------------------------ #
    def _policy_on(self, anchor_field, anchor, cf_id):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            anchor_field: anchor.id,
            'name': 'pol-798c-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })
        _internal_policy_cf_write(pol).cf_policy_id = cf_id
        # 陽性對照：**動作之前**先證明值真的在列上。少了它，「清掉了」與
        # 「這條路徑沒跑到／fixture 根本沒種進去」不可區分——而守衛上線後
        # fixture 種不進去正是最可能的失效模式。
        self.assertEqual(
            _db_cf_ids(self.env, [pol.id])[pol.id], cf_id,
            '前提：動作之前這一列必須真的有 cf id（fixture 種不進去就沒得測）')
        return pol.with_context(sa_skip_route_sync=False)

    def _sa_route(self, protected=True, app_id='app-798c'):
        SA = self.env['woow_paas_platform.security_access']
        sa = SA.sudo().create(
            {'name': 'SA 798C', 'workspace_id': self.workspace.id})
        _internal_identity(sa).write(
            {'state': 'active', 'tunnel_id': 'tun-798c-%s' % uuid.uuid4().hex[:8]})
        Route = self.env['woow_paas_platform.security_access_route']
        route = _skip_route_sync(Route.sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'r798c-%s' % uuid.uuid4().hex[:6],
            'is_protected': protected,
        })
        if app_id:
            _internal_identity(route).access_app_id = app_id
        return sa, route.with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    def test_C2_sa_route_sync_torn_down_branch_still_clears_cf_ids(self):
        """SA route sync 回 ``app_id=None`` ⇒ 這條 route 底下 policy 的 cf id 清空。

        這是 `_sync_access_to_operator` 裡的**第二個**迴圈——與對映回存那個是
        不同的迴圈，漏包 sentinel 只會在這一格出事（對映回存那格照樣綠）。
        """
        sa, route = self._sa_route()
        pol = self._policy_on('route_id', route, 'cf-798c-torn')

        client = MagicMock()
        client.sync_access_app.return_value = {
            'app_id': None, 'policy_ids': [], 'allowed_idps': [],
            'unresolved_idp_types': [], 'orphaned_policy_ids': []}
        with patch(_MOCK_SA_CLIENT, return_value=client):
            pushed = route._sync_access_to_operator()

        self.assertTrue(pushed, '前提：這一輪必須真的推出去（不是被 gate 擋掉）')
        self.assertFalse(
            _db_cf_ids(self.env, [pol.id])[pol.id],
            '#798：守衛不得擋死 torn-down 的清值（漏包 sentinel 會在這裡 UserError）')

    def test_C4_sa_route_mark_protection_lost_still_clears_cf_ids(self):
        """`_access_mark_protection_lost()`：清 policy cf ＋ 打開 fail-open 旗標。

        **兩件一起斷言**是刻意的：只驗旗標的話，漏包 sentinel 造成的
        `UserError` 會讓整個方法炸掉——但那條斷言根本不會執行到。
        """
        sa, route = self._sa_route()
        pol = self._policy_on('route_id', route, 'cf-798c-lost')

        route._access_mark_protection_lost()

        self.assertFalse(
            _db_cf_ids(self.env, [pol.id])[pol.id],
            '#798：補償路徑的清值不得被守衛擋死')
        self.assertTrue(route.access_protection_lost,
                        'fail-open 旗標必須打開（#794 的語意，不得被本單改掉）')
        self.assertFalse(route.access_app_id, 'app id 同一趟要清掉')

    def test_C7_cloud_service_mark_protection_lost_still_clears_cf_ids(self):
        """CS 腿同型。"""
        Service = self.env['woow_paas_platform.cloud_service']
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'C798 tmpl',
            'slug': 'c798-%s' % uuid.uuid4().hex[:6],
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/c798',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        svc = _internal_provision(Service.sudo()).create({
            'name': 'svc-798c',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
        })
        _internal_cs_access_identity(svc).access_app_id = 'app-798c-cs'
        pol = self._policy_on('cloud_service_id', svc, 'cf-798c-cs')

        svc._access_mark_protection_lost()

        self.assertFalse(
            _db_cf_ids(self.env, [pol.id])[pol.id],
            '#798：CS 腿的補償清值不得被守衛擋死')
        self.assertTrue(svc.access_protection_lost)

    def test_C11_sh_route_mark_protection_lost_still_clears_cf_ids(self):
        """SH route 腿同型。"""
        sh = sh_model(self.env).create({
            'name': 'sh-798c-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-798c-sh-%s' % uuid.uuid4().hex[:8],
        }).with_env(self.env)
        ShRoute = self.env['woow_paas_platform.smart_home_route']
        route = _sm_skip_access_sync(ShRoute.sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})
        _internal_sm_route_identity(route).access_app_id = 'app-798c-sh'
        pol = self._policy_on('smart_home_route_id', route, 'cf-798c-sh')

        route._access_mark_protection_lost()

        self.assertFalse(
            _db_cf_ids(self.env, [pol.id])[pol.id],
            '#798：SH route 腿的補償清值不得被守衛擋死')
        self.assertTrue(route.access_protection_lost)


# ---------------------------------------------------------------------- #
#  F：共用 helper 加了 `is_open=` 之後，既有兩支 model 的行為零改變          #
# ---------------------------------------------------------------------- #
#
# 本單把 `_strip_identity_anchors` / `_reject_identity_anchor_writes` 加了一個具名
# 參數 `is_open`（預設＝既有那支 reader）。「既有呼叫端零改變」如果只寫在 PR body
# 裡就沒有守線——這一組把它釘住。
#
# 🔴 為什麼傳的是 **reader 函式**而不是 key 字串：repo-lint 第 28 條
# （`check_internal_ctx_key_registry`）算「判定點」時只解得出**字面值／module 常數**
# 的 context key。把 key 變成參數 ⇒ `env.context.get(ctx_key)` 解不出來 ⇒ 連既有那把
# `sa_internal_identity_write` 都會掉到 0 個判定點而被判成假綠。實測過：把 reader
# 內的字面 key 換成區域變數，repo-lint 立刻紅並指名該 key。


@tagged('post_install', '-at_install')
class TestSharedAnchorHelperContract798(TransactionCase):
    """F：共用守衛 helper 的契約（結構 ＋ 行為 ＋ 兩把鑰匙互不通用）。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 798F %s' % uuid.uuid4().hex[:6]})
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[_POLICY]
        self.sa = self.SA.sudo().create(
            {'name': 'SA 798F', 'workspace_id': self.workspace.id})
        self.route = _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'r798f-%s' % uuid.uuid4().hex[:6],
        })
        self.pol = _skip_route_sync(self.Policy.sudo()).create({
            'route_id': self.route.id,
            'name': 'pol-798f',
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })

    # ------------------------------------------------------------------ #
    def test_F1_is_open_default_is_the_existing_reader_object(self):
        """預設引數必須**就是**既有那支 reader（同一性，不是同名）。

        🔴 用 `is` 而不是 `__name__` / 字串比對：名字相等而物件不同正是這一族的
        翻車形狀——換成一支長得一樣、判定卻鬆掉的函式，名字比對照樣是綠的。
        """
        for fn in (_strip_identity_anchors, _reject_identity_anchor_writes):
            params = inspect.signature(fn).parameters
            self.assertIn(
                'is_open', params,
                '%s 必須有 `is_open` 具名參數（#798 形狀 1）' % fn.__name__)
            self.assertIs(
                params['is_open'].default, _is_internal_identity_write,
                '%s 的 `is_open` 預設值必須**就是** `_is_internal_identity_write` '
                '這個物件；預設值被換掉 ⇒ 另外兩支 model 的守衛靜默失效'
                % fn.__name__)

    def test_F2_existing_call_sites_do_not_pass_is_open(self):
        """既有 4 個呼叫端**一個都不傳** `is_open` ⇒ 行為走預設值。

        陽性對照的必要性：光有 F1 不夠——default 沒被改，但某個呼叫端顯式傳了
        別的值，行為一樣會變。

        判準用**所屬方法名**不用行號（行號會漂）。也刻意不數「命中幾個」：
        `smart_home.py` 有**同名的第二組** helper（簽章 arity 2，不是 3），
        `git grep` 名字會把兩組混成一堆。
        """
        from ..models import security_access as sa_mod
        src = inspect.getsource(sa_mod)
        tree = ast.parse(src)

        owners = {}
        def walk(node, prefix):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.FunctionDef):
                    nm = prefix + ch.name
                    owners[(ch.lineno, ch.end_lineno)] = nm
                    walk(ch, nm + '.')
                elif isinstance(ch, ast.ClassDef):
                    walk(ch, prefix + ch.name + '.')
                else:
                    walk(ch, prefix)
        walk(tree, '')

        found = {}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fname = (n.func.id if isinstance(n.func, ast.Name)
                     else getattr(n.func, 'attr', None))
            if fname not in ('_strip_identity_anchors',
                             '_reject_identity_anchor_writes'):
                continue
            owner = None
            for (a, b), nm in owners.items():
                if a <= n.lineno <= b and (owner is None
                                           or a > owner[0]):
                    owner = (a, nm)
            found.setdefault(owner[1] if owner else '<module>', []).append(
                (fname, [kw.arg for kw in n.keywords]))

        expected = {
            'SecurityAccess.create', 'SecurityAccess.write',
            'SecurityAccessRoute.create', 'SecurityAccessRoute.write',
        }
        self.assertEqual(
            set(found), expected,
            '共用 helper 的呼叫端應恰好是這四支方法（實得 %s）' % sorted(found))
        for owner, calls in found.items():
            for fname, kwargs in calls:
                self.assertNotIn(
                    'is_open', kwargs,
                    '%s 呼叫 %s 時顯式傳了 is_open ⇒ 既有行為已被改變（#798 '
                    '的前提是這四處一行都不改）' % (owner, fname))

    def test_F3_existing_guards_still_behave_identically(self):
        """行為面：既有兩組錨點**不帶 hatch 擋得住、帶既有 sentinel 寫得進**。

        只有 F1／F2 的話，helper 內部邏輯被改壞仍然是綠的。
        """
        # (a) 不帶 hatch：write 側擋得住（對 sudo 也生效）
        for rec, field, value in ((self.sa, 'tunnel_id', 'f3-forged-tun'),
                                  (self.route, 'access_app_id', 'f3-forged-app')):
            try:
                rec.sudo().write({field: value})
            except UserError:
                pass
            else:
                self.fail('%s 的既有守衛必須仍然擋住裸 write（實際沒 raise）'
                          % field)

        # (b) 帶既有 sentinel：仍然寫得進
        _internal_identity(self.sa).write({'tunnel_id': 'f3-server-tun'})
        self.assertEqual(self.sa.tunnel_id, 'f3-server-tun',
                         '既有 sentinel 必須仍然開得了門（否則 provisioning 全斷）')
        _internal_identity(self.route).access_app_id = 'f3-server-app'
        self.assertEqual(self.route.access_app_id, 'f3-server-app')

        # (c) create 側壓值仍然生效
        squat = _skip_route_sync(self.Route.sudo()).create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'f3c-%s' % uuid.uuid4().hex[:6],
            'access_app_id': 'f3-create-squat',
        })
        self.assertFalse(squat.access_app_id,
                         'create 側的壓值必須仍然生效')

    def test_F4_the_two_keys_are_not_interchangeable(self):
        """🔴 兩把鑰匙互不通用——少了這一條，「分成兩把」只是命名不是隔離。"""
        # 新 key 打不開既有錨點
        try:
            _internal_policy_cf_write(self.sa).write({'tunnel_id': 'f4-x'})
        except UserError:
            pass
        else:
            self.fail('`sa_internal_policy_cf_write` 不得打開 SA 身分錨點的閘')

        # 既有 key 打不開 policy 的 cf_policy_id
        try:
            _internal_identity(self.pol).write({'cf_policy_id': 'f4-y'})
        except UserError:
            pass
        else:
            self.fail('`sa_internal_identity_write` 不得打開 cf_policy_id 的閘')

        # 正向：各自那把都開得了自己的門
        _internal_identity(self.sa).write({'tunnel_id': 'f4-ok-tun'})
        self.assertEqual(self.sa.tunnel_id, 'f4-ok-tun')
        _internal_policy_cf_write(self.pol).cf_policy_id = 'f4-ok-cf'
        self.assertEqual(self.pol.cf_policy_id, 'f4-ok-cf')


# ---------------------------------------------------------------------- #
#  E3：JSON 表達不出物件同一性——偽造的 context 值打不開閘                   #
# ---------------------------------------------------------------------- #


@tagged('post_install', '-at_install')
class TestPolicyCfIdHatchIdentity798(TransactionCase):
    """E3：閘只認模組私有 sentinel 物件本身。"""

    _CTX_KEY = 'sa_internal_policy_cf_write'

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 798E3 %s' % uuid.uuid4().hex[:6]})
        self.Policy = self.env[_POLICY]
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 798E3', 'workspace_id': self.workspace.id})
        self.route = _skip_route_sync(
            self.env['woow_paas_platform.security_access_route'].sudo()).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'r798e3-%s' % uuid.uuid4().hex[:6],
            })

    def _make_policy(self, **extra):
        vals = {
            'route_id': self.route.id,
            'name': 'pol-798e3-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(extra)
        return vals

    def test_E3a_forged_context_values_do_not_open_the_hatch_at_write(self):
        """`True` / `1` / `'true'` / list / dict——JSON 送得出來的都不得開閘。

        用裸 `try/except` ＋ `else: self.fail(...)`（不是 `assertRaises`）：
        本格在例外之後要讀 DB，而 `assertRaises` 的 savepoint 會把區塊內的寫入
        回捲，讓「擋在寫入之前」與「寫了又被回捲」不可區分（見 E1 的 docstring）。
        """
        pol = _skip_route_sync(self.Policy.sudo()).create(self._make_policy())
        for forged in (True, 1, 'true', [1], {'x': 1}):
            with self.subTest(forged=forged):
                try:
                    pol.sudo().with_context(
                        **{self._CTX_KEY: forged}).write(
                            {'cf_policy_id': 'e3-forged'})
                except UserError:
                    pass
                else:
                    self.fail('context 值 %r 不得打開 cf_policy_id 的閘'
                              % (forged,))
                self.env.flush_all()
                self.env.cr.execute(
                    'SELECT cf_policy_id FROM %s WHERE id = %%s'
                    % self.env[_POLICY]._table, (pol.id,))
                self.assertFalse(
                    self.env.cr.fetchone()[0],
                    '被拒之後 DB 那一格必須仍是空的（值 %r）' % (forged,))

    def test_E3b_forged_context_values_do_not_open_the_hatch_at_create(self):
        """create 側同理：偽造值不得讓 vals 的 cf_policy_id 落地。"""
        for forged in (True, 1, 'true'):
            with self.subTest(forged=forged):
                pol = _skip_route_sync(self.Policy.sudo()).with_context(
                    **{self._CTX_KEY: forged}).create(
                        self._make_policy(cf_policy_id='e3-create-forged'))
                self.assertFalse(
                    pol.cf_policy_id,
                    'context 值 %r 不得讓 create 的 cf_policy_id 落地'
                    % (forged,))

    def test_E3c_the_real_sentinel_still_opens_it(self):
        """正向對照：閘必須是「關得好」，不是「永遠打不開」。

        少了這一條，E3a／E3b 的綠與「守衛把伺服器端路徑一起鎖死」不可區分——
        那會把三腿的 sync 回存全部擋掉（本 issue 危害的另一半）。
        """
        pol = _skip_route_sync(self.Policy.sudo()).create(self._make_policy())
        _internal_policy_cf_write(pol).cf_policy_id = 'e3-server-owned'
        self.assertEqual(pol.cf_policy_id, 'e3-server-owned')
        # create 側：帶真 sentinel 時 vals 的值必須落地（不被壓掉）
        created = _skip_route_sync(_internal_policy_cf_write(
            self.Policy.sudo())).create(
                self._make_policy(cf_policy_id='e3-server-create'))
        self.assertEqual(created.cf_policy_id, 'e3-server-create')

    def test_E3d_the_sentinel_object_is_the_module_private_one(self):
        """鑑別項：閘認的就是 `_INTERNAL_IDENTITY_WRITE` 這個物件。

        沒有這一條，「reader 被改成永遠回 True」也會讓 E3c 綠。
        """
        env = self.env(context=dict(
            self.env.context, **{self._CTX_KEY: _INTERNAL_IDENTITY_WRITE}))
        self.assertTrue(_is_internal_policy_cf_write(env))
        env2 = self.env(context=dict(self.env.context,
                                     **{self._CTX_KEY: object()}))
        self.assertFalse(
            _is_internal_policy_cf_write(env2),
            '另一個 object() 不得被判成開閘（判準必須是同一性，不是 truthy）')
