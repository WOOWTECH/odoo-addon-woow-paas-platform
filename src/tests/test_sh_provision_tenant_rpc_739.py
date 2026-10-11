# -*- coding: utf-8 -*-
"""#739：租戶經 `/woow` 的 Smart Home provision 從來沒有成功過。

## 缺陷（修復前實測、真 HTTP、operator 用 mock、無對外呼叫）

只掛 `base.group_user` 的 workspace owner 打
`POST /api/workspaces/<ws>/smarthomes/<id> {"action": "provision"}`，**必定**拿到

    You do not have enough rights to access the fields "tunnel_token" on
    Smart Home Instance ... Operation: write

而記錄停在 `state='provisioning'`、`tunnel_id` 為空。

機制（行號皆為本檔寫作當下的 `origin/develop 826f5f6d`，覆核時請自己重新
grep 符號名）：

1. `models/smart_home.py:318-321` 的 `tunnel_token` 帶
   `groups='base.group_system'`（自 model 誕生就有）
2. `models/smart_home.py:563` 的 `tunnel_vals` 一律含 `tunnel_token`，
   由 `:584` / `:606` 兩處 `anchored.write(...)` 寫下去
3. `controllers/smart_home.py:860` 的 `smarthome.action_provision()` 拿的是
   `browse(id)` 來的**非 sudo 租戶 recordset**
4. Odoo `write()` 先 `check_access('write')` 再
   `check_field_access_rights('write', vals.keys())`（`odoo/models.py:4717-4718`），
   後者對非 `group_system` 使用者直接 `AccessError`

## 為什麼既有測試抓不到（本檔存在的理由）

全 repo 對 smart_home 呼叫 `action_provision()` 的測試都在 `TransactionCase`
（superuser env）底下，而 `check_field_access_rights` 的第一行就是
`if self.env.su: return ...`（`odoo/models.py:3783-3784`）⇒ **欄位級 `groups=`
在那裡整條不生效**。沿用那批測試的形狀＝重造同一個盲區。
本檔一律 **HttpCase ＋ 真租戶 session ＋ 真 HTTP JSON-RPC**，
⛔ 全檔不使用 `TransactionCase` / `with_user` / `sudo()` 驅動受測路徑
（`sudo()` 只出現在 fixture 的建置與 raw-DB 對照上）。

## 三組的分工

- **A 系列**：主格。租戶身分經真 HTTP 打 provision 必須成功，且憑證欄位
  **確實落地**（用 raw SQL 讀，不用 ORM——`tunnel_token` 對租戶是遮蔽的）。
  修復前 A1/A2/A3 紅在第 3 階（見下方 anti-probe 階梯）。
- **B 系列**：🔴 本檔的重點。改用平台身分寫入之後，那條路徑上**原本由租戶
  身分擋著的檢查**逐條點名重測，證明它們一條都沒有被順帶關掉。
  B 系列在修復前後都必須綠——它們釘的是「修法沒有擴大授權」。
- **C 系列**：票面第 2 個附帶事實（controller `except Exception` 收掉例外、
  記錄停在中途狀態）在主修法之後**是否還在**。這一條刻意釘住**現況形狀**，
  不是在主張那是對的；它被修掉的時候要跟著改，docstring 有寫。

## anti-probe 階梯（每一條主格斷言前都先排除探針故障）

「被擋住了」與「探針壞了」在輸出上長得一模一樣，所以主格一律三階：

1. `assertNotIn('error', res)` —— 排除 404 / 401 / route 不存在 / JSON-RPC 例外
2. `assertTrue(client.create_tunnel.called)` —— 排除「在走到 provision 之前就
   被某道閘擋掉」（那會讓 A1 以完全不同的原因紅，卻長得一樣）
3. 才斷言 `payload['success']`，且失敗訊息**整包帶上 payload**，
   讓 RED 的原始輸出自己點名是哪個欄位、哪個 operation

否定性斷言則反過來配陽性對照：B1/B2/B3/B4 都同時斷言
`create_tunnel` **沒有**被呼叫，否則「被擋下」與「擋在更後面」分不出來。
"""
import json
import uuid
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_OWNER, ROLE_USER,
)
from .operator_contract import operator_client  # #1052：受 spec 約束的 client double
from .publication_receipt_fixture import configure_receipt, routes_receipt
from .smart_home_fixture import sh_model

_MODEL = 'woow_paas_platform.smart_home'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 739' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _Sh739Fixture(HttpCase):
    """W1（alice owner、dave admin、carol role=user）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SmartHome = self.env[_MODEL]

        # 下單流程要開得出訂閱，否則 `_check_operation_allowed` 會因為
        # 「沒有訂閱＝不受限」而讓 B4 失去鑑別力（本 helper 只連結既有的
        # sale journal，不新建；前例見 test_create_service_validation_rollback_520）。
        self.env['res.company'].sudo()._ensure_subscription_journal()

        self.alice, self.alice_login = _new_group_user(self.env, 'alice739')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave739')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol739')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob739')

        for u in (self.alice, self.dave, self.carol, self.bob):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 739'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 739'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER]),
            'fixture 前提：W1 要同時掛 owner/admin/user 三列'
            '（少了 role=user 那列，B1 會零鑑別力）')

        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'SH 739 Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.env['product.template'].sudo().create({
            'name': 'SH 739 Plan Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'list_price': 100.0,
        })
        self.template = self.env[
            'woow_paas_platform.smart_home_template'].sudo().create({
                'name': 'HA 739',
                'slug': 'ha-739-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })

    # ------------------------------------------------------------------ #
    #  RPC helper（形狀沿用 test_smart_home_authz_524）                    #
    # ------------------------------------------------------------------ #
    def _post_json(self, url, payload):
        """打 `/api/workspaces/*` 這種 `type="json"` 的產品端點。"""
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(url, data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw` —— ORM 層的觀測必須走這條真實 RPC 路徑。"""
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open('/web/dataset/call_kw', data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_no_rpc_error(self, res, msg):
        """階梯第 1 階：排除 404／401／route 不存在／JSON-RPC 例外。"""
        self.assertNotIn('error', res,
                         msg + '（探針故障，不是受測行為：%s）'
                         % self._error_text(res))
        return res.get('result')

    # ------------------------------------------------------------------ #
    #  raw DB 讀（ORM 讀不到 `tunnel_token` —— 那正是本票的欄位）          #
    # ------------------------------------------------------------------ #
    def _db(self, record_id, field_name):
        """直接讀 Postgres 的那一格。

        刻意不用 ORM：`tunnel_token` 帶 `groups='base.group_system'`，而
        「沒有例外」與「沒有寫入」是兩個主張——只有 raw 讀分得開。
        `flush_all()` 先把尚未 flush 的寫入推下去。
        """
        self.env.flush_all()
        self.assertIn(field_name, self.env[_MODEL]._fields)
        table = self.env[_MODEL]._table
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s' % (field_name, table),
            (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, 'smart_home#%s 不在 DB 裡' % record_id)
        return row[0]

    # ------------------------------------------------------------------ #
    #  operator mock（本檔一律不對外送）                                   #
    # ------------------------------------------------------------------ #
    def _operator_client(self, **extra):
        # #1052：受 PaaSOperatorClient spec 約束的 double（同 test_sa_provision_tenant_rpc_805）。
        client = operator_client()
        self.expected_token = 'tok-739-%s' % uuid.uuid4().hex[:12]
        client.create_tunnel.return_value = {
            'tunnel_id': 'tun-739-%s' % uuid.uuid4().hex[:8],
            'tunnel_token': self.expected_token,
            'tunnel_name': 'name-739-%s' % uuid.uuid4().hex[:6],
        }
        client.configure_tunnel.side_effect = configure_receipt
        client.sync_tunnel_routes.side_effect = routes_receipt
        client.get_tunnel_status.return_value = {'connections': []}
        for k, v in extra.items():
            setattr(client, k, v)
        return client

    def _patched_operator(self, client):
        return patch('odoo.addons.woow_paas_platform.models.smart_home'
                     '.get_paas_operator_client', return_value=client)

    # ------------------------------------------------------------------ #
    #  下單／provision 的驅動                                              #
    # ------------------------------------------------------------------ #
    def _order_smart_home(self, login, workspace, name):
        """走**正常下單流程**開一台（帶真訂閱，B4 需要）。"""
        self.authenticate(login, login)
        res = self._post_json(
            '/api/workspaces/%d/smarthomes' % workspace.id,
            {'action': 'create', 'name': name,
             'plan_id': self.plan.id, 'template_id': self.template.id})
        payload = self._assert_no_rpc_error(res, '下單端點必須可達')
        self.assertTrue(payload.get('success'),
                        '陽性對照：正常下單流程必須成功（回 %r）' % payload)
        sh = self.SmartHome.sudo().browse(payload['data']['id'])
        self.assertFalse(sh.tunnel_id, '前提：剛下單的還沒有 tunnel')
        self.assertEqual(sh.state, 'pending', '前提：剛下單的是 pending')
        return sh

    def _provision(self, login, workspace_id, smarthome_id, client):
        """以 `login` 的真 session 打 provision 端點，回 (res, payload)。"""
        self.authenticate(login, login)
        with self._patched_operator(client):
            res = self._post_json(
                '/api/workspaces/%d/smarthomes/%d'
                % (workspace_id, smarthome_id),
                {'action': 'provision'})
        return res


@tagged('post_install', '-at_install')
class TestSh739TenantProvisionSucceeds(_Sh739Fixture):
    """A 系列：主格 —— 租戶身分經真 HTTP 必須 provision 得起來。"""

    def _assert_provisioned(self, login, label):
        sh = self._order_smart_home(login, self.w1, 'to provision by %s' % label)
        client = self._operator_client()
        res = self._provision(login, self.w1.id, sh.id, client)

        # 階梯 1：端點可達（排除 404 / 401 / route 不存在）
        payload = self._assert_no_rpc_error(
            res, '%s 打 provision 端點' % label)
        # 階梯 2：真的走進了 provision（排除「更早的閘擋掉」——那會讓下面
        # 那條紅得一模一樣，卻是完全不同的原因）
        self.assertTrue(
            client.create_tunnel.called,
            '%s：operator 必須真的被呼叫過，否則紅的是更前面的閘而不是本票'
            '（實回 %r）' % (label, payload))
        # 階梯 3：本票的斷言。訊息整包帶 payload —— RED 的原始輸出要自己
        # 點名是哪個欄位、哪個 operation。
        self.assertTrue(
            payload.get('success'),
            '#739：%s 經真 HTTP provision 必須成功（實回 %r）' % (label, payload))
        return sh, client

    def test_A1_owner_can_provision_over_real_http(self):
        """workspace owner（只有 base.group_user）打 provision 必須成功。"""
        sh, client = self._assert_provisioned(self.alice_login, 'owner')
        self.assertEqual(
            self._db(sh.id, 'state'), 'active',
            '#739：成功的 provision 必須把記錄推到 active'
            '（停在 provisioning ＝ 例外在中途被吞掉）')
        self.assertEqual(
            self._db(sh.id, 'tunnel_id'),
            client.create_tunnel.return_value['tunnel_id'],
            '#739：operator 回的 tunnel_id 必須落地')

    def test_A2_admin_role_can_provision_over_real_http(self):
        """role=admin 與 owner 同一條路（B1 的陽性對照面）。"""
        self._assert_provisioned(self.dave_login, 'admin')

    def test_A3_tunnel_token_actually_lands_in_postgres(self):
        """🔴 「不再 AccessError」不等於「憑證真的存下來了」。

        `tunnel_token` 對租戶是遮蔽欄位，所以這一條用 raw SQL 讀那一格。
        `tunnel_name` / `subdomain` 是同一次 write 的鄰居，一併驗——只驗
        token 的話，「整個 write 沒跑」與「write 跑了但 token 沒進去」
        分不出來。
        """
        sh, client = self._assert_provisioned(self.alice_login, 'owner')
        self.assertEqual(
            self._db(sh.id, 'tunnel_token'), self.expected_token,
            '#739：平台鑄造的 tunnel_token 必須真的落地（raw DB 讀）')
        self.assertEqual(
            self._db(sh.id, 'tunnel_name'),
            client.create_tunnel.return_value['tunnel_name'],
            '同一次 write 的鄰居欄位也要落地')
        self.assertTrue(
            self._db(sh.id, 'subdomain'),
            '同一次 write 的鄰居欄位也要落地')

    def test_A4_primary_route_is_created_and_visible_to_the_tenant(self):
        """provision 成功當下要建出 primary route（#768 的 Access 錨點載體）。

        它在 `action_provision` 裡排在那次 write **之後**，所以修復前它連跑
        都沒跑到。順帶證明「AccessError 是在 write 那一步死的」不是推論。

        🔴 第二半才是本條真正的鑑別項：route 是**平台身分**建的，而回應的
        序列化（`to_dict(include_details=True)` → `self.route_ids`）刻意留在
        **租戶身分**。如果 route 的 ir.rule 讓租戶看不見它，端點會回一個
        `routes: []` 的成功——前端的 Access 分頁就此長不出來，而且**沒有任何
        錯誤浮出來**。只驗「DB 裡有一條」抓不到這個形狀。
        """
        sh, _client = self._assert_provisioned(self.alice_login, 'owner')
        routes = self.env[
            'woow_paas_platform.smart_home_route'].sudo().search([
                ('smart_home_id', '=', sh.id), ('is_primary', '=', True)])
        self.assertEqual(len(routes), 1,
                         '#768：provision 成功必須留下恰好一條 primary route')

        payload = self._assert_no_rpc_error(
            self._post_json(
                '/api/workspaces/%d/smarthomes/%d' % (self.w1.id, sh.id),
                {'action': 'get'}),
            'detail 端點')
        self.assertTrue(payload.get('success'),
                        '前提：detail 端點必須成功（實回 %r）' % payload)
        self.assertEqual(
            [r['id'] for r in payload['data'].get('routes', [])],
            [routes.id],
            '#739：平台身分建出來的 route 必須對租戶身分的序列化可見'
            '（實回 %r）' % payload['data'].get('routes'))


@tagged('post_install', '-at_install')
class TestSh739ChecksThatMustSurvive(_Sh739Fixture):
    """🔴 B 系列：改用平台身分之後，逐條點名「原本由租戶身分擋著的檢查」。

    修法把 `_provision_smarthome` 的呼叫改成平台身分，因此 Odoo 那三層
    （`ir.model.access` / `ir.rule` / 欄位級 `groups=`）在**那一次呼叫內**
    全部停用。本組把「那些檢查是不是還在別的地方擋著」變成可執行的證據，
    而不是修法者的斷言：

    | 原本擋在哪 | sudo 之後還在嗎 | 由誰接手 | 本組哪一條 |
    | --- | --- | --- | --- |
    | ir.rule `smart_home_rule_owner_admin_manage`（write） | 那一次呼叫內**沒了** | controller 自己的 owner/admin 閘（且**更嚴**，它另外要求 workspace `state='active'`） | B1 |
    | workspace 歸屬 | 不受影響（在 sudo 之外） | `_get_workspace_or_error` | B2 |
    | SH ∈ workspace | 不受影響（在 sudo 之外） | 實測是 ir.rule `smart_home_rule_member_read` 在 `_get_smarthome_or_error` 的 `workspace_id` **讀**上先擋，不是 controller 那句 `"Smart home not found"` | B3 |
    | 訂閱生效閘 | 不受影響（在 sudo 之外，且 `sudo()` 不改 `env.user`） | `_check_operation_allowed` | B4 |
    | 欄位級 `groups=` 的**讀**側 | **完全不受影響** | `check_field_access_rights('read', …)` | B5 / B6 |
    | 身分錨點不可變守衛 | **不受影響**（該守衛明示無 su 豁免） | `_reject_identity_anchor_writes` | B7 |
    | 重複 provision 的 stored-id guard | **不受影響**（純業務邏輯） | `action_provision` 開頭 | B8 |

    B4 的一則機制註記（不要憑直覺）：Odoo 的 `sudo()` **不會**改變
    `env.user`（`odoo/models.py:6290-6291` 逐字：「The superuser mode does not
    change the current user, and simply bypasses access rights checks.」），
    所以 `_check_operation_allowed` 開頭那個
    `self.env.user.has_group('base.group_system')` 提早 return **不會**因為
    修法而變成 True。B4 是這句話的實測面。
    """

    def _assert_blocked(self, res, expected_fragment, client, label):
        """否定性斷言 ＋ 兩個陽性對照。"""
        payload = self._assert_no_rpc_error(res, label)
        self.assertFalse(
            payload.get('success'),
            '%s：必須被擋下（實回 %r）' % (label, payload))
        self.assertIn(
            expected_fragment, str(payload.get('error', '')),
            '%s：歸因——擋下來的必須是那一道閘（實回 %r）' % (label, payload))
        self.assertFalse(
            client.create_tunnel.called,
            '%s：被擋下就不該走到 operator（走到了＝擋在更後面，'
            '這條斷言等於沒有鑑別力）' % label)
        return payload

    def test_B1_role_user_is_still_refused(self):
        """ir.rule 的 owner/admin 收窄由 controller 的角色閘接手。

        `role=user` 是 W1 的**合法成員**（讀得到那台 SH），所以它撞的必然是
        角色閘而不是歸屬閘——這正是 ir.rule `perm_write` 原本擋的那一格。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'for role=user')
        client = self._operator_client()
        res = self._provision(self.carol_login, self.w1.id, sh.id, client)
        self._assert_blocked(res, 'Insufficient permissions', client,
                             'role=user 打 provision')
        self.assertEqual(
            self._db(sh.id, 'state'), 'pending',
            'role=user 被擋之後記錄不得有任何位移')
        self.assertFalse(self._db(sh.id, 'tunnel_token'),
                         'role=user 被擋之後不得寫進任何憑證')

    def test_B2_non_member_is_still_refused_at_the_workspace_gate(self):
        """非成員連 workspace 都拿不到（`_get_workspace_or_error`）。"""
        sh = self._order_smart_home(self.alice_login, self.w1, 'for outsider')
        client = self._operator_client()
        res = self._provision(self.bob_login, self.w1.id, sh.id, client)
        self._assert_blocked(res, 'Workspace not found or access denied',
                             client, '非成員打 provision')
        self.assertFalse(self._db(sh.id, 'tunnel_token'),
                         '非成員被擋之後不得寫進任何憑證')

    def test_B3_cross_workspace_pair_is_still_refused(self):
        """自己 workspace 的 id ＋ 別人那台 SH 的 id。

        #739 當時的 browse→讀 workspace_id 會先撞 ir.rule，回裸 JSON-RPC
        AccessError。Portal capability 修復改成 id＋workspace_id 的 scoped
        search；internal 分支仍使用呼叫者 ORM/rules，但不可見列變成空集合。
        因此現在明確要求結構化拒絕，不再把洩露技術 model 名的舊錯誤形狀
        當作不變式。真正的不變式不變：不得開通、不得碰 operator 或改受害列。
        """
        victim = self._order_smart_home(self.bob_login, self.w2, 'tenant B SH')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, victim.id, client)

        self.assertNotIn('error', res, '產品 API 不應暴露原始 ORM 錯誤')
        self.assertIs(res['result'].get('success'), False, res)
        self.assertEqual(res['result'].get('error'), 'Smart home not found')
        self.assertFalse(res['result'].get('data'), res)
        self.assertNotIn('woow_paas_platform.smart_home', str(res))
        self.assertNotIn('Traceback', str(res))
        self.assertFalse(
            client.create_tunnel.called,
            '被擋下就不該走到 operator（走到了＝擋在更後面）')
        self.assertFalse(self._db(victim.id, 'tunnel_token'),
                         '別人那台不得被開通')
        self.assertEqual(self._db(victim.id, 'state'), 'pending',
                         '別人那台不得有任何位移')

    def test_B4_expired_subscription_is_still_refused(self):
        """訂閱閘（`_check_operation_allowed`）在 sudo 之外，必須照常擋。

        用 `state='cancel'` ⇒ `_is_subscription_active()` False、
        `_is_past_grace_period()` True ⇒ 走「已停權」那一句。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'expired sub')
        sub = sh.sudo().subscription_id
        self.assertTrue(sub, '前提：下單流程必須真的建出訂閱'
                             '（沒有訂閱＝不受限，本條會零鑑別力）')
        sub.sudo().write({'state': 'cancel'})

        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sh.id, client)
        self._assert_blocked(res, 'subscription', client,
                             '訂閱已取消時打 provision')
        self.assertFalse(self._db(sh.id, 'tunnel_token'),
                         '訂閱閘擋下之後不得寫進任何憑證')

    def test_B5_tunnel_token_read_is_still_masked_for_the_tenant(self):
        """🔴 修法只改**寫**側；欄位級 `groups=` 的**讀**側必須完好。

        陽性對照是同一次 RPC 讀 `tunnel_id`（同一筆記錄、同一個身分、同一條
        `read` 路徑）——它必須成功，否則「讀不到 token」可能只是「整筆讀不到」。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'read mask')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功（實回 %r）' % payload)

        ok = self._call_kw(_MODEL, 'read', [[sh.id], ['tunnel_id']])
        self.assertNotIn('error', ok,
                         '陽性對照：租戶讀得到同一筆的 tunnel_id'
                         '（讀不到的話下面那條零鑑別力，回 %s）'
                         % self._error_text(ok))

        denied = self._call_kw(_MODEL, 'read', [[sh.id], ['tunnel_token']])
        self.assertIn('error', denied,
                      '#739：租戶讀 tunnel_token 必須仍被擋（實回 %r）' % denied)
        text = self._error_text(denied)
        self.assertIn('AccessError', text,
                      '歸因：必須是欄位級遮罩（實回 %r）' % text)
        self.assertIn('tunnel_token', text,
                      '歸因：訊息要點名那個欄位（實回 %r）' % text)

    def test_B6_provision_response_never_carries_the_token(self):
        """端點回應不得含憑證值；陽性對照＝那個值確實存在於 DB。

        少了陽性對照的話，「回應裡沒有」與「根本沒寫進去」長得一樣。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'no leak')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功（實回 %r）' % payload)
        self.assertEqual(
            self._db(sh.id, 'tunnel_token'), self.expected_token,
            '陽性對照：那個值真的在 DB 裡（不在的話下面那條零鑑別力）')
        self.assertNotIn(
            self.expected_token, json.dumps(payload),
            '#739：provision 回應絕不得夾帶 tunnel_token')
        self.assertNotIn(
            'tunnel_token', json.dumps(payload),
            '#739：連欄位名都不該出現在 to_dict() 的 envelope 裡')

    def test_B7_identity_anchor_guard_is_still_closed(self):
        """身分錨點守衛明示無 su 豁免 —— 修法不得把它一起打開。

        走真 RPC 對**自己那筆**寫 `tunnel_id`（record rule 擋不到這一步，
        #524 的原始論證），必須仍被錨點守衛的 `UserError` 擋下。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'anchor guard')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功（實回 %r）' % payload)
        provisioned = self._db(sh.id, 'tunnel_id')

        stolen = 'stolen-tunnel-%s' % uuid.uuid4()
        denied = self._call_kw(_MODEL, 'write', [[sh.id], {'tunnel_id': stolen}])
        text = self._error_text(denied)
        self.assertIn('UserError', text,
                      '#524：必須是錨點守衛擋下（實回 %r）' % text)
        self.assertIn("'tunnel_id' is immutable", text,
                      '歸因：訊息要點名欄位（實回 %r）' % text)
        self.assertEqual(self._db(sh.id, 'tunnel_id'), provisioned,
                         '被擋之後 DB 的值不得改變')

    def test_B8_reprovision_guard_still_fires(self):
        """stored-id guard 是純業務邏輯，sudo 不該讓它失效。

        第二次 provision 必須被擋，且 `create_tunnel` 全程只被呼叫一次——
        少了後面這半，「回了錯誤訊息」與「其實又開了一條 tunnel 才報錯」
        分不出來，而後者正是那道 guard 存在的理由（多一條在計費的孤兒）。
        """
        sh = self._order_smart_home(self.alice_login, self.w1, 'reprovision')
        client = self._operator_client()

        first = self._assert_no_rpc_error(
            self._provision(self.alice_login, self.w1.id, sh.id, client),
            '第一次 provision')
        self.assertTrue(first.get('success'),
                        '前提：第一次必須成功（實回 %r）' % first)
        self.assertEqual(client.create_tunnel.call_count, 1,
                         '前提：第一次恰好開一條')

        second = self._assert_no_rpc_error(
            self._provision(self.alice_login, self.w1.id, sh.id, client),
            '第二次 provision')
        self.assertFalse(second.get('success'),
                         '#484：已有 tunnel 就不得再開一條（實回 %r）' % second)
        self.assertIn('already has a Cloudflare tunnel',
                      str(second.get('error', '')),
                      '歸因：必須是 stored-id guard（實回 %r）' % second)
        self.assertEqual(
            client.create_tunnel.call_count, 1,
            '#484：被擋下的重試不得再打 operator（實際打了 %d 次）'
            % client.create_tunnel.call_count)


@tagged('post_install', '-at_install')
class TestSh739SwallowedFailureShape(_Sh739Fixture):
    """C 系列：票面第 2 個附帶事實 —— 例外被 controller 收掉、記錄停在中途。

    ⚠ **本組釘的是現況形狀，不是在主張那是對的。**

    `controllers/smart_home.py::_provision_smarthome` 的 `except Exception`
    把任何例外轉成 `{"success": False, "error": str(e)}` 回 HTTP 200，而
    `action_provision` 開頭那次 `state='provisioning'` 已經落地。#739 主修法
    只拿掉了**這一票的觸發源**（`tunnel_token` 的 AccessError），沒有動
    這個形狀本身——任何一個 `action_provision` 內部兩個 `except` 都接不住的
    例外，仍然會讓記錄停在 `provisioning`。

    本組用一個 `RuntimeError`（既不是 `PaaSOperatorError` 也不是
    `PaaSOperatorConnectionError`）把那條路徑打出來，讓「(b) 仍在」是實測而
    不是我的斷言。⇒ 這個形狀被修掉的那一天，本組要跟著改，不是「壞掉了」。

    🔴 未驗到的一項，寫在這裡而不是報告裡就好忘記：本組**沒有**證明
    「交易真的 commit 進 Postgres」。`HttpCase` 底下 HTTP handler 與測試共用
    同一顆 test cursor，`commit()` 被攔截，所以本組只證明到
    「端點回 200 ＋ 那一格在（同交易內）已經是 provisioning」。
    真實 commit 語意要在沙盒真跑一次才算驗過。
    """

    def test_C1_non_operator_error_is_swallowed_and_strands_provisioning(self):
        """形狀仍在：非 operator 類例外 ⇒ 200 + success False + 卡 provisioning。"""
        sh = self._order_smart_home(self.alice_login, self.w1, 'stranded')
        client = self._operator_client()
        client.create_tunnel.side_effect = RuntimeError('boom-739')

        res = self._provision(self.alice_login, self.w1.id, sh.id, client)

        payload = self._assert_no_rpc_error(
            res, 'provision 端點（例外被吞掉時仍是 HTTP 200 JSON-RPC result）')
        self.assertFalse(payload.get('success'),
                         '前提：這一次必須是失敗（實回 %r）' % payload)
        self.assertIn('boom-739', str(payload.get('error', '')),
                      '歸因：吞掉的必須是我丟的那個例外（實回 %r）' % payload)
        self.assertEqual(
            self._db(sh.id, 'state'), 'provisioning',
            '#739 附帶事實 (b)：記錄停在 provisioning —— 前台會看到一個'
            '永遠 provisioning 的實例。這一條紅掉代表形狀被修掉了，'
            '請連同本組的 docstring 一起更新，不要當成回歸。')
        self.assertFalse(
            self._db(sh.id, 'tunnel_id'),
            '陽性對照：這一次 operator 沒有回任何東西，所以不該有 tunnel_id'
            '（有的話代表我打到的不是我以為的那條分支）')

    def test_C2_operator_connection_failure_is_not_success(self):
        from ..services.paas_operator import PaaSOperatorConnectionError
        sh = self._order_smart_home(self.alice_login, self.w1, 'connection failed')
        client = self._operator_client()
        client.create_tunnel.side_effect = PaaSOperatorConnectionError('qa-no-operator')
        result = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(result, 'connection failure response')
        self.assertTrue(client.create_tunnel.called)
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload['data']['state'], 'error')
        self.assertIn('qa-no-operator', payload['error'])
        self.assertNotIn('tunnel_token', payload['data'])

    def test_C3_operator_failure_is_not_success(self):
        from ..services.paas_operator import PaaSOperatorError
        sh = self._order_smart_home(self.alice_login, self.w1, 'operator failed')
        client = self._operator_client()
        client.create_tunnel.side_effect = PaaSOperatorError('qa-create-rejected')
        result = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(result, 'operator failure response')
        self.assertTrue(client.create_tunnel.called)
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload['data']['state'], 'error')
        self.assertIn('qa-create-rejected', payload['error'])

    def test_C4_dns_failure_keeps_retry_anchor_but_is_not_success(self):
        sh = self._order_smart_home(self.alice_login, self.w1, 'dns failed')
        client = self._operator_client()
        client.create_tunnel.return_value['dns_failed'] = True
        result = self._provision(self.alice_login, self.w1.id, sh.id, client)
        payload = self._assert_no_rpc_error(result, 'DNS failure response')
        self.assertEqual(client.create_tunnel.call_count, 1)
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload['data']['state'], 'error')
        self.assertEqual(payload['data']['tunnel_id'], client.create_tunnel.return_value['tunnel_id'])
        self.assertTrue(payload['data']['routes'], 'retry must retain its route')
        self.assertNotIn('tunnel_token', payload['data'])
