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

⚠ 本檔**刻意不 import 任何尚未存在的 sentinel**：RED 這一輪要看到的紅必須落在
「值落地了」那條斷言上，import error 是另一種紅。逃生門的正向對照（開閘仍寫得
進、偽造的 context 值打不開閘）與三腿回存的陽性對照在修法落地後才加得進來。

## fixture 為什麼用 SQL 種 `cf_policy_id`

守衛上線後，`pol.cf_policy_id = ...` 這種 fixture 寫法會**被自己要測的守衛擋
掉**，測試會紅在 fixture 上。種值改走 `cr.execute`：它與守衛正交，修法前後同一
份 fixture 都成立。
"""

import json
import uuid

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

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
        """`sudo()` **不帶**逃生門直接改值：必須被拒，而且值不得落地。"""
        forged = 'sudo-forged-798-%s' % uuid.uuid4().hex[:8]
        # 型別刻意收緊成 `UserError`：route／CS／SH 三支同型守衛逐字都是
        # `UserError`。用裸 `Exception` 的話，一個 fixture 自己爆掉的紅同樣會綠。
        with self.assertRaises(
                UserError,
                msg='#798：sudo 不是開閘，`cf_policy_id` 的寫入必須被拒'):
            self.Policy.sudo().browse(self.pol.id).write(
                {'cf_policy_id': forged})
        self.assertEqual(
            self._db(_POLICY, self.pol.id, 'cf_policy_id'), self.server_cf_id,
            '#798：被拒之後伺服器端原值必須原封不動')

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
