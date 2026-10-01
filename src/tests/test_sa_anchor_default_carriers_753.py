"""#753：SA／route 的 create 側錨點守衛擋不住「預設值載體」。

## 缺陷

`security_access.py::_strip_identity_anchors` 用 `vals.pop(field, None)`——只清得掉
呼叫端**寫在 vals 裡**的那一份。而 `super().create()` 內部會呼叫
`_add_missing_default_values(vals)`，把「vals 裡**缺席**」的欄位從三個載體補回來
（`odoo/models.py::default_get` 的順位）：

    1. `default_<field>` context   ← `/web/dataset/call_kw` 把呼叫端 context
                                      整包換進 env（`odoo/api.py::call_kw`）
    2. `ir.default` 的列            ← 租戶對 `ir.default` 有完整 CRUD
                                      （base 的 `access_ir_default_group_user` = 1,1,1,1），
                                      `_get_model_defaults` 收 `user_id = 自己` 那些列
    3. 欄位自身的 `default=`        ← 伺服器端程式碼，呼叫端碰不到

`pop` 恰恰把 anchor 變成**缺席** ⇒ 前兩個（呼叫端可控的）載體在 strip **之後**才落地：

    pop（key 消失） → super().create() → _add_missing_default_values 補回來

同型缺陷本 repo 已修過兩次（smart_home 見 PR #738 覆核 round1、cloud_service 見
#705 F1／PR #752），SA 這份是最後一個還用 `pop` 的。

## 為什麼「壓成 False」對 SA 不夠

`_IDENTITY_ANCHOR_FIELDS` 含 `reference_id`，而它是 `required=True` ＋
`default=lambda self: str(uuid.uuid4())`。無腦壓 False 會讓 required 檢查在
`super().create()` 內炸掉，**破壞所有合法 create**——本檔的 D 系列（陰性對照）就是
擋這個。修法採「壓成**伺服器端會產生的值**」：有欄位級 `default` 的走那個 default
（第 3 順位，呼叫端碰不到），沒有的壓 False。兩者都留在 vals 裡 ⇒
`_add_missing_default_values` 不會再去查前兩個載體。

## 測試分層（每一格都要能分辨「守住了」與「探針壞了」）

* A 系列 — SA 側，`default_<field>` context 與 `ir.default` 兩個載體（真 RPC）
* C 系列 — route 側，同兩個載體
* D 系列 — 陰性對照：合法 create 不得被修法炸掉（reference_id 仍走 uuid4）
* E 系列 — 陽性對照：sentinel 開閘的伺服器端 create 仍寫得進錨點；sudo 本身打不開

A／C 每一格都配一個**同機制、非 anchor 欄位**的對照（`tunnel_status` /
`service_url`）：少了它，「anchor 沒被注入」與「這個載體在本環境根本不作用」分不
出來——後者會讓本檔在守衛被拿掉之後**仍然是綠的**。
"""

import json
import uuid

from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _INTERNAL_IDENTITY_WRITE,
    _sa_platform_order,
)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_CTX_KEY = 'sa_internal_identity_write'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 753' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _SaAnchor753Fixture(HttpCase):
    """W1（alice owner）＋ W2（bob owner，受害者）。"""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice753')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob753')
        for u in (self.alice, self.bob):
            self.assertFalse(
                u.has_group('base.group_system'),
                '前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 753'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 753'})
        if not self.WorkspaceAccess.sudo().search(
                [('workspace_id', '=', self.w1.id),
                 ('user_id', '=', self.alice.id)]):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id,
                'user_id': self.alice.id, 'role': 'owner'})

        # 受害者（W2）真的存在的伺服器端錨點值——攻擊者要搶的就是這些。
        self.victim_tunnel = 'victim-tunnel-%s' % uuid.uuid4()
        self.victim_ref = 'victim-ref-%s' % uuid.uuid4()
        self.sa_w2 = self.SA.sudo().create(
            {'name': 'SA of tenant B', 'workspace_id': self.w2.id})
        self._internal(self.sa_w2).write({'tunnel_id': self.victim_tunnel})

        # C 系列的 route 母體：SA 停在 'pending'，所以 route 的 create/write
        # 都不會打到 operator（每個 sync 都 gate 在 active/active_no_route/error）。
        self.sa_w1 = self.SA.sudo().create(
            {'name': 'SA of tenant A', 'workspace_id': self.w1.id})
        self.assertEqual(
            self.sa_w1.state, 'pending',
            '前提：SA 必須停在 pending，否則 route create 會真的打 operator')

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _internal(records):
        """唯一合法的開閘方式：sentinel 物件本身，不是 `True`。"""
        return records.with_context(**{_CTX_KEY: _INTERNAL_IDENTITY_WRITE})

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。

        `odoo/api.py::call_kw` 會 `kwargs.pop('context')` 再
        `recs.with_context(context)` ⇒ 呼叫端 context 整包換進 env，這正是
        `default_<field>` 載體的可達性來源。
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

    def _tenant_create_sa(self, vals, context=None):
        """以**租戶身分**建 SA（#1298 起 SA 有建立閘）。

        #1298 之後租戶經 `/web/dataset/call_kw` 裸 create SA 一律被建立閘擋下
        （`SecurityAccessOrderGateError`）⇒ 「`default_<anchor>` context 經 call_kw
        送進來」這條路在 SA 上已結構性關閉。錨點剝除仍是唯一合法建立點（/woow
        下單，租戶身分＋開閘）的守線，所以 A／D 系列改成 in-process：以同一個
        租戶 env、帶同一份 context（`call_kw` 本來就是把 context 整包換進 env，
        兩者對 `create()` 而言等價），只多開建立閘、**不**開錨點閘。
        """
        handle = self.SA.with_user(self.alice)
        if context:
            handle = handle.with_context(**context)
        sa = _sa_platform_order(handle).create(vals)
        self.env.invalidate_all()
        return sa.id

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result,
                         msg + '（回 %s）' % self._error_text(result))
        return result.get('result')

    def _db(self, model_name, record_id, field_name):
        """直接讀 Postgres 的那一格。

        刻意不用 ORM 讀：`tunnel_token` 帶 `groups='base.group_system'`，租戶
        身分讀不到；而 `flush_all()` 先把尚未 flush 的寫入推下去，才不會讓
        「值其實被接受了」讀起來像沒變（「沒有例外」與「沒有寫入」是兩個主張）。
        """
        self.env.flush_all()
        # 欄位名一律來自 model 自己的 _fields，不來自使用者輸入。
        self.assertIn(field_name, self.env[model_name]._fields)
        table = self.env[model_name]._table
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s' % (field_name, table),
            (record_id,))
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
        """陽性對照 (a)：注入本身要成功。

        少了它，「anchor 是空的」與「`ir.default.set` 根本沒寫進去」分不出來。
        讀回刻意用 ORM 直查 `ir.default` 的列而不是 `ir.default.get`——後者在
        Odoo 18 不是 RPC 可呼叫的 public method，會讓測試紅在探針上。
        """
        stored = self.env['ir.default'].sudo().search([
            ('field_id.model', '=', model_name),
            ('field_id.name', '=', field_name),
            ('user_id', '=', user.id),
        ])
        self.assertTrue(
            stored,
            '陽性對照 (a)：租戶的 ir.default.set(%s) 必須真的落庫' % field_name)
        self.assertIn(
            json.dumps(value), stored.mapped('json_value'),
            '陽性對照 (a)：落庫的值必須是攻擊者選的那個，否則本測試零鑑別力')


class TestSaAnchorDefaultCarriers753(_SaAnchor753Fixture):
    """A：SA 側兩個預設值載體都不得種進身分錨點。"""

    def test_A1_default_context_cannot_seed_sa_anchors(self):
        """🔴 #753 主格：租戶帶 `default_<anchor>` context 走真 RPC create。

        `tunnel_status` 是**刻意挑的**對照欄位——它有 `default='disconnected'`、
        且被 `_IDENTITY_ANCHOR_FIELDS` 上方的註解明列為「NOT 錨點」。它必須被
        注入成功，否則下面每一條「沒被注入」都可能只是 context 根本沒到。
        """
        sa_id = self._tenant_create_sa(
            {'name': 'ctx default injection', 'workspace_id': self.w1.id},
            {
                'default_tunnel_id': self.victim_tunnel,
                'default_tunnel_name': 'stolen-tunnel-name',
                'default_tunnel_token': 'stolen-token',
                'default_canonical_name': 'paas-sa-victim-squat',
                'default_tunnel_status': 'connected',
            })

        self.assertEqual(
            self._db(_SA, sa_id, 'tunnel_status'), 'connected',
            '陽性對照：`default_<field>` context 對**非 anchor 欄位**必須真的'
            '生效——不生效的話，下面幾條就算守衛被拿掉也會是綠的')
        self.assertFalse(
            self._db(_SA, sa_id, 'tunnel_id'),
            '#753：`default_tunnel_id` 不得落地（實得 %r）'
            % self._db(_SA, sa_id, 'tunnel_id'))
        self.assertFalse(
            self._db(_SA, sa_id, 'tunnel_name'),
            '#753：`default_tunnel_name` 不得落地')
        self.assertFalse(
            self._db(_SA, sa_id, 'tunnel_token'),
            '#753：`default_tunnel_token` 不得落地（tunnel credential 本身）')
        self.assertNotEqual(
            self._db(_SA, sa_id, 'canonical_name'), 'paas-sa-victim-squat',
            '#753：`default_canonical_name` 不得落地（決定子網域與 CF 命名）')

    def test_A2_default_context_cannot_seed_reference_id(self):
        """🔴 reference_id 是 canonical_name／subdomain 的 hash salt。

        它與其他錨點不同：`required=True` ＋ `default=uuid4`。守衛不能只是
        「不處理它」（那就是本 issue 的洞），也不能壓 False（那會炸掉 required，
        見 D1）——落地值必須是**伺服器產生的 uuid4**。
        """
        sa_id = self._tenant_create_sa(
            {'name': 'ctx ref injection', 'workspace_id': self.w1.id},
            {
                'default_reference_id': self.victim_ref,
                'default_tunnel_status': 'connected',
            })

        self.assertEqual(
            self._db(_SA, sa_id, 'tunnel_status'), 'connected',
            '陽性對照：同一個載體對非 anchor 欄位必須真的生效')
        landed = self._db(_SA, sa_id, 'reference_id')
        self.assertNotEqual(
            landed, self.victim_ref,
            '#753：`default_reference_id` 不得落地——攻擊者控制 salt 就能推導'
            '出受害者命名空間的 canonical_name／subdomain 去 squat')
        self.assertTrue(landed, 'reference_id 是 required，不得為空（見 D1）')
        # 必須是伺服器端 uuid4 default 的產物，不是任何呼叫端可控的字串。
        self.assertEqual(
            str(uuid.UUID(landed)), landed,
            '#753：reference_id 必須由伺服器的 uuid4 default 產生（實得 %r）'
            % landed)

    def test_A3_ir_default_cannot_seed_sa_anchors(self):
        """🔴 另一個載體：租戶自己那一列 `ir.default`。

        比 context 載體更隱蔽——它與呼叫端 payload 無關，連走 `type="json"` 的
        BFF 端點（context 來自 session）也照樣生效。只堵 context 的修法會在這
        一格紅。

        不注入 `tunnel_token`：`ir.default` 寫入對非 su 身分另有欄位級檢查，
        帶 `groups=` 的它走不通；恰好**沒有欄位級遮罩的那幾個才是可達的**，
        而它們正是決定「打向哪一個 Cloudflare 物件」的那幾個。
        """
        self.authenticate(self.alice_login, self.alice_login)
        for field, value in (('tunnel_id', self.victim_tunnel),
                             ('tunnel_name', 'stolen-tunnel-name'),
                             ('reference_id', self.victim_ref)):
            self._assert_ok(
                self._set_ir_default(_SA, field, value),
                'ir.default.set(%s) 必須成功（租戶對 ir.default 有 CRUD）' % field)
            self._assert_ir_default_stored(_SA, field, value, self.alice)
        self._assert_ok(
            self._set_ir_default(_SA, 'tunnel_status', 'connected'),
            'ir.default.set（對照欄位）必須成功')

        sa_id = self._tenant_create_sa(
            {'name': 'ir.default injection', 'workspace_id': self.w1.id})

        self.assertEqual(
            self._db(_SA, sa_id, 'tunnel_status'), 'connected',
            '陽性對照 (b)：`ir.default` 對**非 anchor 欄位**必須真的生效')
        self.assertFalse(
            self._db(_SA, sa_id, 'tunnel_id'),
            '#753：`ir.default` 注入的 tunnel_id 不得落地（實得 %r）'
            % self._db(_SA, sa_id, 'tunnel_id'))
        self.assertFalse(
            self._db(_SA, sa_id, 'tunnel_name'),
            '#753：`ir.default` 注入的 tunnel_name 不得落地')
        self.assertNotEqual(
            self._db(_SA, sa_id, 'reference_id'), self.victim_ref,
            '#753：`ir.default` 注入的 reference_id 不得落地')


class TestRouteAnchorDefaultCarriers753(_SaAnchor753Fixture):
    """C：route 側同一對載體。issue body 未點名 route，但守衛是同一支。"""

    @staticmethod
    def _victim_route_anchors():
        return ('victim-dns-%s' % uuid.uuid4().hex[:8],
                'victim-app-%s' % uuid.uuid4().hex[:8])

    def test_C1_default_context_cannot_seed_route_anchors(self):
        """`dns_record_id` / `access_app_id` 決定之後的 reconcile／teardown 會
        動到**誰的** Cloudflare 物件。兩者都沒有欄位級 default ⇒ 壓 False。"""
        dns, app = self._victim_route_anchors()
        self.authenticate(self.alice_login, self.alice_login)
        route_id = self._assert_ok(self._call_kw(
            _ROUTE, 'create',
            [{'security_access_id': self.sa_w1.id,
              'subdomain_prefix': 'ctx-753-%s' % uuid.uuid4().hex[:6]}],
            {'context': {
                'default_dns_record_id': dns,
                'default_access_app_id': app,
                'default_service_url': 'http://ctrl.example:9999',
            }}), 'route create 不該回 JSON-RPC error')

        self.assertEqual(
            self._db(_ROUTE, route_id, 'service_url'),
            'http://ctrl.example:9999',
            '陽性對照：`default_<field>` context 對非 anchor 欄位必須真的生效')
        self.assertFalse(
            self._db(_ROUTE, route_id, 'dns_record_id'),
            '#753：`default_dns_record_id` 不得落地（實得 %r）'
            % self._db(_ROUTE, route_id, 'dns_record_id'))
        self.assertFalse(
            self._db(_ROUTE, route_id, 'access_app_id'),
            '#753：`default_access_app_id` 不得落地')

    def test_C2_ir_default_cannot_seed_route_anchors(self):
        dns, app = self._victim_route_anchors()
        self.authenticate(self.alice_login, self.alice_login)
        for field, value in (('dns_record_id', dns), ('access_app_id', app)):
            self._assert_ok(self._set_ir_default(_ROUTE, field, value),
                            'ir.default.set(%s) 必須成功' % field)
            self._assert_ir_default_stored(_ROUTE, field, value, self.alice)
        self._assert_ok(
            self._set_ir_default(_ROUTE, 'service_url',
                                 'http://ctrl.example:8888'),
            'ir.default.set（對照欄位）必須成功')

        route_id = self._assert_ok(self._call_kw(
            _ROUTE, 'create',
            [{'security_access_id': self.sa_w1.id,
              'subdomain_prefix': 'ird-753-%s' % uuid.uuid4().hex[:6]}]),
            'route create 不該回 JSON-RPC error')

        self.assertEqual(
            self._db(_ROUTE, route_id, 'service_url'),
            'http://ctrl.example:8888',
            '陽性對照 (b)：`ir.default` 對非 anchor 欄位必須真的生效')
        self.assertFalse(
            self._db(_ROUTE, route_id, 'dns_record_id'),
            '#753：`ir.default` 注入的 dns_record_id 不得落地')
        self.assertFalse(
            self._db(_ROUTE, route_id, 'access_app_id'),
            '#753：`ir.default` 注入的 access_app_id 不得落地')


class TestSaAnchorGuardDoesNotBreakLegitCreate753(_SaAnchor753Fixture):
    """D：陰性對照——修法不得把合法 create 一起炸掉。

    這一組是 `reference_id` 陷阱的守線：把錨點無腦壓成 False 會讓
    `required=True` 在 `super().create()` 內炸掉，而 A／C 全部仍是綠的
    （它們只斷言「攻擊者的值沒落地」，`create` 直接失敗同樣滿足）。
    """

    def test_D1_ordinary_tenant_create_still_gets_a_server_uuid(self):
        sa_id = self._tenant_create_sa(
            {'name': 'plain create', 'workspace_id': self.w1.id})
        landed = self._db(_SA, sa_id, 'reference_id')
        self.assertTrue(
            landed,
            '#753 陰性對照：reference_id 是 required，必須由伺服器 uuid4 填入')
        self.assertEqual(str(uuid.UUID(landed)), landed,
                         'reference_id 必須是 uuid4 default 的產物（實得 %r）'
                         % landed)
        canonical = self._db(_SA, sa_id, 'canonical_name')
        self.assertTrue(canonical, 'create() 必須仍然蓋上 canonical_name')
        self.assertTrue(canonical.startswith('paas-'), canonical)

    def test_D2_multi_create_gets_distinct_reference_ids(self):
        """`@api.model_create_multi`：每一筆都要有自己的 uuid4。

        把伺服器端值算一次再套用到整個 vals_list 的實作會在這一格紅——而
        D1 對它是綠的（單筆看不出來）。reference_id 是 hash salt，同 salt
        ＋同 workspace 的兩筆會推導出撞在一起的 canonical_name。
        """
        sas = self.SA.sudo().create([
            {'name': 'multi a', 'workspace_id': self.w1.id},
            {'name': 'multi b', 'workspace_id': self.w1.id},
            {'name': 'multi c', 'workspace_id': self.w1.id},
        ])
        refs = [self._db(_SA, sa.id, 'reference_id') for sa in sas]
        self.assertTrue(all(refs), 'reference_id 不得為空')
        self.assertEqual(
            len(set(refs)), 3,
            '#753：每一筆 create 都要拿到自己的 uuid4（實得 %r）' % refs)

    def test_D3_route_create_still_works_without_anchors(self):
        self.authenticate(self.alice_login, self.alice_login)
        route_id = self._assert_ok(self._call_kw(
            _ROUTE, 'create',
            [{'security_access_id': self.sa_w1.id,
              'subdomain_prefix': 'plain-753-%s' % uuid.uuid4().hex[:6]}]),
            'route 的一般合法 create 不得失敗')
        self.assertTrue(route_id, 'route 必須真的被建立')
        self.assertFalse(self._db(_ROUTE, route_id, 'dns_record_id'),
                         '沒帶錨點的 route 出生時錨點就該是空的')


class TestSaAnchorHatchStillOpen753(_SaAnchor753Fixture):
    """E：陽性對照——伺服器端合法路徑仍寫得進錨點，而 sudo 本身打不開閘。"""

    def test_E1_sentinel_still_writes_anchors_at_create(self):
        """開閘的伺服器端 create 必須寫得進**每一個**錨點，reference_id 也是。

        少了這一條，A／C／D 的綠燈與「create 側乾脆把錨點永遠鎖死」分不出來
        ——那會把 provisioning 與資料修復的正當路徑一起擋掉。
        """
        chosen_ref = 'server-chosen-ref-%s' % uuid.uuid4().hex[:8]
        sa = self._internal(self.SA.sudo()).with_context(
            default_tunnel_id='should-be-ignored',
        ).create({
            'name': 'server side anchored create',
            'workspace_id': self.w1.id,
            'reference_id': chosen_ref,
            'tunnel_id': 'server-chosen-tunnel',
            'tunnel_name': 'server-chosen-name',
        })
        self.assertEqual(self._db(_SA, sa.id, 'reference_id'), chosen_ref,
                         '開閘的伺服器端 create 必須寫得進 reference_id')
        self.assertEqual(self._db(_SA, sa.id, 'tunnel_id'),
                         'server-chosen-tunnel',
                         '開閘的伺服器端 create 必須寫得進 tunnel_id'
                         '（vals 的明確值優先於 default）')
        self.assertEqual(self._db(_SA, sa.id, 'tunnel_name'),
                         'server-chosen-name')

    def test_E2_sudo_alone_does_not_open_the_gate_at_create(self):
        """鑑別項：E1 用 sudo 驅動，所以必須證明 sudo **本身**打不開閘。

        錨點守衛對 su／sudo 一律生效（見 `_INTERNAL_IDENTITY_WRITE` 的註解）；
        沒有這一條，E1 的綠燈就與「守衛對 sudo 無效」不可區分。
        """
        forged_ref = 'sudo-forged-ref-%s' % uuid.uuid4().hex[:8]
        sa = self.SA.sudo().with_context(
            default_tunnel_id=self.victim_tunnel,
        ).create({
            'name': 'sudo create',
            'workspace_id': self.w1.id,
            'reference_id': forged_ref,
            'tunnel_id': 'sudo-forged-tunnel',
        })
        self.assertFalse(self._db(_SA, sa.id, 'tunnel_id'),
                         'sudo 不是開閘：vals 的 tunnel_id 必須被壓掉')
        self.assertNotEqual(self._db(_SA, sa.id, 'reference_id'), forged_ref,
                            'sudo 不是開閘：vals 的 reference_id 必須被換成'
                            '伺服器產生的 uuid4')

    def test_E3_forged_context_value_does_not_open_the_gate_at_create(self):
        """JSON 載不了物件身分：帶同名 key 但值是 JSON 表達得出的，閘不開。"""
        for forged in (True, 1, 'true', [1], {'x': 1}):
            with self.subTest(forged=forged):
                sa = self.SA.sudo().with_context(
                    **{_CTX_KEY: forged}).create({
                        'name': 'forged ctx create',
                        'workspace_id': self.w1.id,
                        'tunnel_id': 'forged-tunnel',
                    })
                self.assertFalse(
                    self._db(_SA, sa.id, 'tunnel_id'),
                    'context 值 %r 不得打開錨點閘' % (forged,))
