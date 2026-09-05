# -*- coding: utf-8 -*-
"""#805 (b)：租戶經 `/woow` 的 Security Access provision 從來沒有成功過。

## 缺陷（修復前，真 HTTP、operator 用 mock、無對外呼叫）

只掛 `base.group_user` 的 workspace owner 打
`POST /api/workspaces/<ws>/security-accesses/<sa> {"action": "provision"}`，**必定**拿到

    You do not have enough rights to access the fields "tunnel_token" on
    Security Access ... Operation: write

而記錄停在 `state='provisioning'`、`tunnel_id` 為空。

機制（符號名定位；行號會漂，覆核時自己重新 grep）：

1. `models/security_access.py` 的 `tunnel_token` 欄位帶 `groups='base.group_system'`
2. `action_provision` stage-2 的 `_internal_identity(self).write({...})` 一律含
   `tunnel_token`——而 `_internal_identity` 只是 `with_context` 塞一個模組私有
   sentinel，**不動 `env.su`／`env.uid`**（原始碼註解自己就寫明它「does NOT
   widen the tunnel_token field-group ACL」）
3. `controllers/security_access.py` 的 `action == "provision"` 分支拿的是
   `_get_sa_or_error` 回來的**非 sudo 租戶 recordset**，`sa.action_provision()`
   裸呼叫
4. Odoo `write()` 會 `check_field_access_rights('write', vals.keys())`，其唯一的
   early-return 是 `if self.env.su:` ⇒ 非 system 呼叫者直接 `AccessError`

## 為什麼這不是「刻意的 fail-closed」

`#739`／PR #797 在 sibling model smart_home 上已裁定同一個形狀是 bug（租戶端點
永遠不可能成功）。SA 這一側的入口盤點（本票 #805 (b)）證實同構：

* BFF 那條 route 對 workspace owner **四環全通**（route 存在／`auth="user"`／
  workspace＋role＋訂閱三道閘都過／全路徑無 sudo）；
* 而且不是理論路徑——租戶建 SA 的 modal **create 成功後無條件自動串接
  provision**（`static/src/paas/components/modal/CreateSecurityAccessModal.js`），
  detail page 另有重試鈕。

⚠ 以下這段是**讀碼推導、本檔沒有任何測試鎖它**（那部分是另案範圍，見下），
寫在這裡是為了讓下一個人知道失敗的形狀，不要當成已驗事實：`state='provisioning'`
在 `try` 之外先落地，`except AccessError` 刻意 re-raise 而不寫 `'error'`，
controller 的 `except Exception` 又把它吞成 HTTP 200 的 `{"success": false}`
⇒ 交易照常收尾、SA 停在 `provisioning`；而 detail page 的重試鈕只認
`pending`／`error`、`action_refresh_status` 在無 `tunnel_id` 時 early-return、
也沒有 cron 會收斂它。**孤兒 tunnel 與這個卡死狀態的補償邏輯不在本票範圍**
（issue-leader 2026-08-22 裁定另案），本檔只鎖 provision 的身分語意。

## 為什麼既有測試抓不到（本檔存在的理由）

`test_security_access.py` 對 SA 呼叫 `action_provision()` 的測試除一條外全在
`TransactionCase`（superuser env），而 `check_field_access_rights` 第一行就是
`if self.env.su: return ...` ⇒ **欄位級 `groups=` 在那裡整條不生效**。
唯一有鑑別力的 `test_provision_token_write_fails_closed_for_non_system_owner`
走的是 `with_user` 直呼 model，**不經過 controller** ⇒ 它證得到 model 層的
身分語意，證不到「BFF 端點對租戶到底通不通」。本檔補的正是那一格。

本檔一律 **HttpCase ＋ 真租戶 session ＋ 真 HTTP JSON-RPC**，
⛔ 全檔不使用 `TransactionCase` / `with_user` / `sudo()` 驅動受測路徑
（`sudo()` 只出現在 fixture 的建置與 raw-DB 對照上）。

## 四組的分工

- **A 系列**：主格。租戶身分經真 HTTP 打 provision 必須成功，且憑證欄位
  **確實落地**（用 raw SQL 讀，不用 ORM——`tunnel_token` 對租戶是遮蔽的）。
  修復前 A1–A4 紅在第 3 階（見下方 anti-probe 階梯）。
- **B 系列**：🔴 本檔的重點。改用平台身分驅動之後，那條路徑上**原本由租戶
  身分擋著的檢查**逐條點名重測，證明它們一條都沒有被順帶關掉——釘的是
  「修法沒有擴大授權」。
  ⚠ **B 系列內部分兩層，驗收預期不同，不要當成同一組**：
  * **B1–B4** 擋點都在 `action_provision` **之前**（workspace 歸屬／角色／
    訂閱），完全在修法的作用範圍之外 ⇒ **修復前後都必須綠**。
  * **B5–B8** 各自以「這一筆已經 provision 成功」為硬前提（讀側遮罩要有東西
    可讀、回應要有東西可洩、錨點要有值可改、重試要有第一次可比）⇒ 修復前
    那個前提本身就不成立，**它們在第一輪必然紅在 `前提：` 那一行**，那是
    預期紅不是回歸。判準是**紅落在哪一行**：落在 `前提：` 那句＝預期；
    落在該條自己的斷言＝真回歸。
- **C 系列**：修法的**範圍限定**。sudo 只加在 BFF 那一條 route 上；Odoo 原生的
  `/web/dataset/call_kw` 是另一條到得了 `action_provision` 的路（方法名無底線、
  無 `@api.private`），它**不經過本 controller**，所以欄位遮罩在那裡照樣 fail-closed。
  這一組把那個殘留形狀變成實測，讓 `test_security_access.py` 那條 ORM 級鎖有
  對得上的真實路徑，也讓「修法涵蓋到哪裡」不是我的斷言。
- **D 系列**：釘住「欄位遮罩只擋得住第一次 provision」這個容易被誤讀的事實——
  `action_provision` 的 re-sync 分支（`if self.tunnel_id:`）**完全不寫
  tunnel_token**，所以那條路在修法前後對租戶身分都是通的。

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

_MODEL = 'woow_paas_platform.security_access'
_ROUTE_MODEL = 'woow_paas_platform.security_access_route'

# 兩條呼叫路徑、兩個 patch target（namespace 不同，缺一不可）——理由與
# test_security_access_subscription_flow.py 檔頭那段相同，pattern 見
# docs/decisions/patterns/unittest-mock-dual-namespace-patch.md：
#   1. controller._check_route_prefix 走 function-local import，每次從
#      services.paas_operator 現抓 attribute；
#   2. model 的 provision / route sync 用的是 models/security_access.py 頂部
#      import 進自己 namespace 的那份 copy。
# 只 patch 其中一個，另一條路會 fall through 到真實 client（sandbox 有設
# operator URL 時直接打 HTTP，被 Odoo BlockedRequest 擋下而紅）。
# fail-loud 哨兵：每條主格都斷言 `create_tunnel.called`，漏 patch 不會靜默通過。
_PATCH_MODEL_NS = ('odoo.addons.woow_paas_platform.models.security_access'
                   '.get_paas_operator_client')
_PATCH_SERVICE_NS = ('odoo.addons.woow_paas_platform.services.paas_operator'
                     '.get_paas_operator_client')


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 805' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


class _TwoNamespacePatch:
    """把兩個 namespace 的 `get_paas_operator_client` 一起換掉的 context manager。"""

    def __init__(self, client):
        self._patches = [
            patch(_PATCH_MODEL_NS, return_value=client),
            patch(_PATCH_SERVICE_NS, return_value=client),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


@tagged('post_install', '-at_install')
class _Sa805Fixture(HttpCase):
    """W1（alice owner、dave admin、carol role=user）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SA = self.env[_MODEL]

        # 下單流程要開得出訂閱，否則 `_check_operation_allowed` 會因為
        # 「沒有訂閱＝不受限」而讓 B4 失去鑑別力（形狀沿用
        # test_sh_provision_tenant_rpc_739 / test_create_service_validation_rollback_520）。
        # ⚠ 這支是 best-effort：找不到 sale journal 就靜默不設、也不 raise，
        # 而下單失敗會被 controller 收成一句通用字串 ⇒ 「fixture 建不起來」與
        # 「本票的 bug」在輸出上長得一樣。把它變成明講的前提斷言。
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            self.env.company.sudo().sh_journal_id,
            'fixture 前提：公司要有 sh_journal_id，否則下單流程開不出訂閱，'
            '整檔會紅在下單那一步而不是受測行為')

        self.alice, self.alice_login = _new_group_user(self.env, 'alice805')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave805')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol805')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob805')

        for u in (self.alice, self.dave, self.carol, self.bob):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 805'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 805'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER]),
            'fixture 前提：W1 要同時掛 owner/admin/user 三列'
            '（少了 role=user 那列，B1 會零鑑別力）')

        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'SA 805 Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 3,
        })
        self.env['product.template'].sudo().create({
            'name': 'SA 805 Plan Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'list_price': 100.0,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].sudo().create({
                'name': 'SA 805',
                'slug': 'sa-805-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })

    # ------------------------------------------------------------------ #
    #  RPC helper                                                         #
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
        """階梯第 1 階：排除 404／401／route 不存在／JSON-RPC 例外。

        兩格都要驗：沒有 `error` **且** `result` 真的在。少了後者，`result`
        缺席時會回 None，後續斷言以 `AttributeError` 收場——那個紅看起來像
        測試寫壞了，而不是端點沒回東西。
        """
        self.assertNotIn('error', res,
                         msg + '（探針故障，不是受測行為：%s）'
                         % self._error_text(res))
        self.assertIn('result', res,
                      msg + '：JSON-RPC 回應沒有 result 欄位（實回 %r）' % res)
        return res['result']

    # ------------------------------------------------------------------ #
    #  raw DB 讀（ORM 讀不到 `tunnel_token` —— 那正是本票的欄位）          #
    # ------------------------------------------------------------------ #
    def _db(self, record_id, field_name, model=_MODEL):
        """直接讀 Postgres 的那一格。

        刻意不用 ORM：`tunnel_token` 帶 `groups='base.group_system'`，而
        「沒有例外」與「沒有寫入」是兩個主張——只有 raw 讀分得開。
        `flush_all()` 先把尚未 flush 的寫入推下去。
        """
        self.env.flush_all()
        self.assertIn(field_name, self.env[model]._fields)
        table = self.env[model]._table
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s' % (field_name, table),
            (record_id,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '%s#%s 不在 DB 裡' % (model, record_id))
        return row[0]

    # ------------------------------------------------------------------ #
    #  operator mock（本檔一律不對外送）                                   #
    # ------------------------------------------------------------------ #
    def _operator_client(self, **extra):
        # #1052：受 PaaSOperatorClient spec 約束的 double——配到 client 上沒有的
        # method 直接 AttributeError，不會再靠 MagicMock 真值假裝通過（下方那段
        # 「回傳形狀要給全」的警語正是這類假綠的形狀）。
        client = operator_client()
        self.expected_token = 'tok-805-%s' % uuid.uuid4().hex[:12]
        self.expected_tunnel_id = 'tun-805-%s' % uuid.uuid4().hex[:8]
        self.expected_tunnel_name = 'name-805-%s' % uuid.uuid4().hex[:6]
        client.create_tunnel.return_value = {
            'tunnel_id': self.expected_tunnel_id,
            'tunnel_token': self.expected_token,
            'tunnel_name': self.expected_tunnel_name,
        }
        # 零 route 時 `_sync_routes_to_operator` 仍會呼叫（tunnel_id 已存在），
        # 所以回傳形狀要給全，否則 KeyError／MagicMock 真值會冒充成受測失敗。
        client.sync_tunnel_routes.return_value = {'routes': []}
        client.sync_access_app.return_value = {}
        client.check_route_availability.return_value = {
            'status': 'free', 'available': True}
        client.get_tunnel_status.return_value = {'connections': []}
        for k, v in extra.items():
            setattr(client, k, v)
        return client

    def _patched_operator(self, client):
        """同時蓋掉 model 與 services 兩個 namespace（見檔頭常數的說明）。"""
        return _TwoNamespacePatch(client)

    # ------------------------------------------------------------------ #
    #  下單／provision 的驅動                                              #
    # ------------------------------------------------------------------ #
    def _order_security_access(self, login, workspace, name):
        """走**正常下單流程**開一個 SA（帶真訂閱，B4 需要）。"""
        self.authenticate(login, login)
        res = self._post_json(
            '/api/workspaces/%d/security-accesses' % workspace.id,
            {'action': 'create', 'name': name,
             'plan_id': self.plan.id, 'template_id': self.template.id})
        payload = self._assert_no_rpc_error(res, '下單端點必須可達')
        self.assertTrue(payload.get('success'),
                        '陽性對照：正常下單流程必須成功（回 %r）' % payload)
        sa = self.SA.sudo().browse(payload['data']['id'])
        self.assertFalse(sa.tunnel_id, '前提：剛下單的還沒有 tunnel')
        self.assertEqual(sa.state, 'pending', '前提：剛下單的是 pending')
        return sa

    def _provision(self, login, workspace_id, sa_id, client):
        """以 `login` 的真 session 打 provision 端點，回原始 JSON-RPC 結果。"""
        self.authenticate(login, login)
        with self._patched_operator(client):
            res = self._post_json(
                '/api/workspaces/%d/security-accesses/%d'
                % (workspace_id, sa_id),
                {'action': 'provision'})
        return res


@tagged('post_install', '-at_install')
class TestSa805TenantProvisionSucceeds(_Sa805Fixture):
    """A 系列：主格 —— 租戶身分經真 HTTP 必須 provision 得起來。"""

    def _assert_provisioned(self, login, label, sa=None, client=None):
        if sa is None:
            sa = self._order_security_access(
                login, self.w1, 'to provision by %s' % label)
        if client is None:
            client = self._operator_client()
        res = self._provision(login, self.w1.id, sa.id, client)

        # 階梯 1：端點可達（排除 404 / 401 / route 不存在）
        payload = self._assert_no_rpc_error(res, '%s 打 provision 端點' % label)
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
            '#805 (b)：%s 經真 HTTP provision 必須成功（實回 %r）'
            % (label, payload))
        # 階梯 4：`success: True` **不等於** provision 成功。`action_provision`
        # 的 broad except 會把非 AccessError 的例外寫成 state='error' 然後正常
        # return，controller 照樣回 True ⇒ 只看 success 的話，「開通成功」與
        # 「中途失敗躺在 error」分不出來。本票的故障（AccessError）確實會 raise
        # 出來被階梯 3 抓到，但這一格成本近零、且擋掉整類旁生的假綠。
        self.assertFalse(
            self._db(sa.id, 'error_message'),
            '%s：success=True 但 error_message 有值 ⇒ provision 其實在中途失敗'
            '（error_message=%r）' % (label, self._db(sa.id, 'error_message')))
        self.assertIn(
            self._db(sa.id, 'state'), ('active', 'active_no_route'),
            '%s：success=True 但終態不是 active/active_no_route（實為 %r，'
            'error_message=%r）' % (label, self._db(sa.id, 'state'),
                                    self._db(sa.id, 'error_message')))
        return sa, client, payload

    def test_A1_owner_can_provision_over_real_http(self):
        """workspace owner（只有 base.group_user）打 provision 必須成功。

        終態斷言是 `active_no_route` 而不是 `active`：軸2 之後 SA 建的是**裸
        tunnel**（無 hostname、無自動 primary route），零 application route 是
        合法狀態，`action_provision` 末尾寫的就是這一格。
        """
        sa, _client, _payload = self._assert_provisioned(
            self.alice_login, 'owner')
        self.assertEqual(
            self._db(sa.id, 'state'), 'active_no_route',
            '#805 (b)：成功的 provision 必須把零 route 的 SA 推到 '
            'active_no_route（實為 %r，error_message=%r）'
            % (self._db(sa.id, 'state'), self._db(sa.id, 'error_message')))
        self.assertEqual(
            self._db(sa.id, 'tunnel_id'), self.expected_tunnel_id,
            '#805 (b)：operator 回的 tunnel_id 必須落地')

    def test_A2_admin_role_can_provision_over_real_http(self):
        """role=admin 與 owner 同一條路（B1 的陽性對照面）。"""
        self._assert_provisioned(self.dave_login, 'admin')

    def test_A3_tunnel_token_actually_lands_in_postgres(self):
        """🔴 「不再 AccessError」不等於「憑證真的存下來了」。

        `tunnel_token` 對租戶是遮蔽欄位，所以這一條用 raw SQL 讀那一格。
        `tunnel_name` / `deployed_at` 是**同一次 write** 的鄰居，一併驗——只驗
        token 的話，「整個 write 沒跑」與「write 跑了但 token 沒進去」分不出來。
        """
        sa, _client, _payload = self._assert_provisioned(
            self.alice_login, 'owner')
        self.assertEqual(
            self._db(sa.id, 'tunnel_token'), self.expected_token,
            '#805 (b)：平台鑄造的 tunnel_token 必須真的落地（raw DB 讀）')
        self.assertEqual(
            self._db(sa.id, 'tunnel_name'), self.expected_tunnel_name,
            '同一次 write 的鄰居欄位也要落地')
        self.assertTrue(
            self._db(sa.id, 'deployed_at'),
            '同一次 write 的鄰居欄位也要落地')

    def test_A4_preconfigured_route_is_synced_and_visible_to_the_tenant(self):
        """configure-then-provision：pending 時加的 route 要在 provision 當下同步。

        `action_provision` 的註解自述這條流程存在（「a user may have added
        routes on the still-pending SA」）。它排在含 `tunnel_token` 的那次 write
        **之後**，所以修復前它連跑都沒跑到——順帶證明「AccessError 是在那一步
        死的」不是推論。

        🔴 第二半才是本條真正的鑑別項：`dns_record_id` 是**平台身分**（sudo 之後
        的 `_internal_identity`）寫回 route 的，而回應的序列化留在**租戶身分**。
        如果 route 的 ir.rule 讓租戶看不見它，端點會回一個 `routes: []` 的成功，
        前端的 Access 分頁就此長不出來，而且**沒有任何錯誤浮出來**。
        只驗「DB 裡有一條」抓不到這個形狀。
        """
        sa = self._order_security_access(
            self.alice_login, self.w1, 'configure then provision')
        prefix = 'sa805-%s' % uuid.uuid4().hex[:8]
        client = self._operator_client()
        client.sync_tunnel_routes.return_value = {
            'routes': [{'subdomain': prefix, 'dns_record_id': 'rec-805-x'}]}

        self.authenticate(self.alice_login, self.alice_login)
        with self._patched_operator(client):
            created = self._post_json(
                '/api/workspaces/%d/security-accesses/%d/routes'
                % (self.w1.id, sa.id),
                {'action': 'create', 'subdomain_prefix': prefix,
                 'service_url': 'http://localhost:8080'})
        route_payload = self._assert_no_rpc_error(created, 'route create 端點')
        self.assertTrue(
            route_payload.get('success'),
            '前提：pending SA 上加 route 必須成功（實回 %r）' % route_payload)
        route_id = route_payload['data']['id']

        # 同一個 client 續用：它的 sync_tunnel_routes 才帶得回那條 route 的
        # dns_record_id（換一顆新 mock 會回空 routes，下面的可見性斷言就換了
        # 受測對象）。
        self._assert_provisioned(
            self.alice_login, 'owner (configure-then-provision)',
            sa=sa, client=client)
        self.assertTrue(client.sync_tunnel_routes.called,
                        'provision 必須把既有 route 推給 operator')

        self.assertEqual(
            self._db(sa.id, 'state'), 'active',
            '有 route 的 SA provision 完必須是 active（不是 active_no_route）')

        # 🔴 本條真正的鑑別項：`dns_record_id` 是**平台身分**（sudo 之後的
        # `_internal_identity`）寫回 route 的，raw DB 讀那一格才證得到那次
        # 回寫真的發生過（route.to_dict() 不吐這個欄位，只驗 route 出現在
        # 清單裡是驗不到的）。
        self.assertEqual(
            self._db(route_id, 'dns_record_id', model=_ROUTE_MODEL),
            'rec-805-x',
            '#805 (b)：operator 回的 dns_record_id 必須由平台身分回寫到 route')

        payload = self._assert_no_rpc_error(
            self._post_json(
                '/api/workspaces/%d/security-accesses/%d' % (self.w1.id, sa.id),
                {'action': 'get'}),
            'detail 端點')
        self.assertTrue(payload.get('success'),
                        '前提：detail 端點必須成功（實回 %r）' % payload)
        self.assertIn(
            route_id,
            [r['id'] for r in payload['data'].get('routes', [])],
            '#805 (b)：provision 過的 route 必須對租戶身分的序列化可見'
            '（實回 %r）' % payload['data'].get('routes'))


@tagged('post_install', '-at_install')
class TestSa805ChecksThatMustSurvive(_Sa805Fixture):
    """🔴 B 系列：改用平台身分之後，逐條點名「原本由租戶身分擋著的檢查」。

    修法把 controller 的 `sa.action_provision()` 改成平台身分驅動，因此 Odoo 那
    三層（`ir.model.access` / `ir.rule` / 欄位級 `groups=`）在**那一次呼叫內**
    全部停用。本組把「那些檢查是不是還在別的地方擋著」變成可執行的證據，
    而不是修法者的斷言：

    | 原本擋在哪 | sudo 之後還在嗎 | 由誰接手 | 本組哪一條 |
    | --- | --- | --- | --- |
    | ir.rule `security_access_rule_owner_admin_manage`（write） | 那一次呼叫內**沒了** | controller 自己的 owner/admin 閘（且**更嚴**，它另外要求 workspace `state='active'`） | B1 |
    | workspace 歸屬 | 不受影響（在 sudo 之外） | `_get_workspace_or_error` | B2 |
    | SA ∈ workspace | 不受影響（在 sudo 之外） | `_get_sa_or_error` 與它之前的讀 | B3 |
    | 訂閱生效閘 | 不受影響（在 sudo 之外，且 `sudo()` 不改 `env.user`） | `_check_operation_allowed` | B4 |
    | 欄位級 `groups=` 的**讀**側 | **完全不受影響** | `check_field_access_rights('read', …)` | B5 / B6 |
    | 身分錨點不可變守衛 | **不受影響**（該守衛明示無 su 豁免） | `_reject_identity_anchor_writes` | B7 |
    | 重複 provision 的 stored-id guard | **不受影響**（純業務邏輯） | `action_provision` 開頭 | B8 |

    B4 的一則機制註記（不要憑直覺）：Odoo 的 `sudo()` **不會**改變 `env.user`，
    所以 `_check_operation_allowed` 開頭那個 `has_group('base.group_system')`
    提早 return **不會**因為修法而變成 True。B4 是這句話的實測面——那句話哪天
    在框架層變了，閘會**靜默失效**（不是報錯），B4 是它的活體警報器。

    ⚠ **本組內部分兩層，驗收預期不同**（詳見檔頭）：B1–B4 的擋點都在
    `action_provision` **之前**，修復前後都必須綠；B5–B8 各自以「這一筆已經
    provision 成功」為硬前提，**修復前必然紅在 `前提：` 那一行**——那是預期紅
    不是回歸，判準是紅落在哪一行。
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

        `role=user` 是 W1 的**合法成員**（讀得到那個 SA），所以它撞的必然是
        角色閘而不是歸屬閘——這正是 ir.rule `perm_write` 原本擋的那一格。
        """
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'for role=user')
        client = self._operator_client()
        res = self._provision(self.carol_login, self.w1.id, sa.id, client)
        self._assert_blocked(res, 'Insufficient permissions', client,
                             'role=user 打 provision')
        self.assertEqual(
            self._db(sa.id, 'state'), 'pending',
            'role=user 被擋之後記錄不得有任何位移')
        self.assertFalse(self._db(sa.id, 'tunnel_token'),
                         'role=user 被擋之後不得寫進任何憑證')

    def test_B2_non_member_is_still_refused_at_the_workspace_gate(self):
        """非成員連 workspace 都拿不到（`_get_workspace_or_error`）。"""
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'for outsider')
        client = self._operator_client()
        res = self._provision(self.bob_login, self.w1.id, sa.id, client)
        self._assert_blocked(res, 'Workspace not found or access denied',
                             client, '非成員打 provision')
        self.assertFalse(self._db(sa.id, 'tunnel_token'),
                         '非成員被擋之後不得寫進任何憑證')

    def test_B3_cross_workspace_pair_is_still_refused(self):
        """自己 workspace 的 id ＋ 別人那個 SA 的 id。

        這道閘在 `sa.action_provision()` **之前**，完全在修法改用平台身分的
        作用範圍之外 ⇒ 修法前後必須逐字相同。本條在 base 碼與修法後各跑一次，
        兩次都要綠。

        ⚠ 擋點的**形狀**刻意不寫死：`_get_sa_or_error` 對別人那筆
        `browse().exists()` 之後要讀 `workspace_id`，SA 的 member-read ir.rule
        可能在那次**讀**上先丟裸 `AccessError`（那一段沒有 try/except，端點會回
        JSON-RPC error 而不是 `{"success": false}` 的信封）；也可能走到
        controller 自己那句 `"Security access not found"`。兩種都算「被擋下」，
        所以本條只斷言三件不受形狀影響的事實，並把實際回應整包印進失敗訊息。
        """
        victim = self._order_security_access(self.bob_login, self.w2,
                                             'tenant B SA')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, victim.id, client)

        blocked = ('error' in res) or not (res.get('result') or {}).get('success')
        self.assertTrue(
            blocked, '跨 workspace 必須被擋（實回 %r）' % res)
        self.assertFalse(
            client.create_tunnel.called,
            '被擋下就不該走到 operator（走到了＝擋在更後面）')
        self.assertFalse(self._db(victim.id, 'tunnel_token'),
                         '別人那個不得被開通')
        self.assertEqual(self._db(victim.id, 'state'), 'pending',
                         '別人那個不得有任何位移')

    def test_B4_cancelled_subscription_is_still_refused(self):
        """訂閱閘（`_check_operation_allowed`）在 sudo 之外，必須照常擋。"""
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'cancelled sub')
        sub = sa.sudo().subscription_id
        self.assertTrue(sub, '前提：下單流程必須真的建出訂閱'
                             '（沒有訂閱＝不受限，本條會零鑑別力）')
        sub.sudo().write({'state': 'cancel'})

        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sa.id, client)
        self._assert_blocked(res, 'subscription', client,
                             '訂閱已取消時打 provision')
        self.assertFalse(self._db(sa.id, 'tunnel_token'),
                         '訂閱閘擋下之後不得寫進任何憑證')

    def test_B5_tunnel_token_read_is_still_masked_for_the_tenant(self):
        """🔴 修法只改**寫**側；欄位級 `groups=` 的**讀**側必須完好。

        陽性對照是同一次 RPC 讀 `tunnel_id`（同一筆記錄、同一個身分、同一條
        `read` 路徑）——它必須成功，否則「讀不到 token」可能只是「整筆讀不到」。
        """
        sa = self._order_security_access(self.alice_login, self.w1, 'read mask')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sa.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功——本條只在修法後有意義，'
                        '修法前紅在這一行是預期紅（實回 %r）' % payload)

        ok = self._call_kw(_MODEL, 'read', [[sa.id], ['tunnel_id']])
        self.assertNotIn('error', ok,
                         '陽性對照：租戶讀得到同一筆的 tunnel_id'
                         '（讀不到的話下面那條零鑑別力，回 %s）'
                         % self._error_text(ok))

        denied = self._call_kw(_MODEL, 'read', [[sa.id], ['tunnel_token']])
        self.assertIn('error', denied,
                      '#805 (b)：租戶讀 tunnel_token 必須仍被擋（實回 %r）'
                      % denied)
        text = self._error_text(denied)
        self.assertIn('AccessError', text,
                      '歸因：必須是欄位級遮罩（實回 %r）' % text)
        self.assertIn('tunnel_token', text,
                      '歸因：訊息要點名那個欄位（實回 %r）' % text)

    def test_B6_provision_response_never_carries_the_token(self):
        """端點回應不得含憑證值；陽性對照＝那個值確實存在於 DB。

        少了陽性對照的話，「回應裡沒有」與「根本沒寫進去」長得一樣。
        """
        sa = self._order_security_access(self.alice_login, self.w1, 'no leak')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sa.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功——本條只在修法後有意義，'
                        '修法前紅在這一行是預期紅（實回 %r）' % payload)
        self.assertEqual(
            self._db(sa.id, 'tunnel_token'), self.expected_token,
            '陽性對照：那個值真的在 DB 裡（不在的話下面那條零鑑別力）')
        self.assertNotIn(
            self.expected_token, json.dumps(payload),
            '#805 (b)：provision 回應絕不得夾帶 tunnel_token')
        self.assertNotIn(
            'tunnel_token', json.dumps(payload),
            '#805 (b)：連欄位名都不該出現在 to_dict() 的 envelope 裡')

    def test_B7_identity_anchor_guard_is_still_closed(self):
        """身分錨點守衛明示無 su 豁免 —— 修法不得把它一起打開。

        走真 RPC 對**自己那筆**寫 `tunnel_id`（record rule 擋不到這一步，
        #522 的原始論證），必須仍被錨點守衛的 `UserError` 擋下。
        """
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'anchor guard')
        client = self._operator_client()
        res = self._provision(self.alice_login, self.w1.id, sa.id, client)
        payload = self._assert_no_rpc_error(res, 'provision 端點')
        self.assertTrue(payload.get('success'),
                        '前提：這一條要先 provision 成功——本條只在修法後有意義，'
                        '修法前紅在這一行是預期紅（實回 %r）' % payload)
        provisioned = self._db(sa.id, 'tunnel_id')

        stolen = 'stolen-tunnel-%s' % uuid.uuid4()
        denied = self._call_kw(_MODEL, 'write', [[sa.id], {'tunnel_id': stolen}])
        text = self._error_text(denied)
        self.assertIn('UserError', text,
                      '#522：必須是錨點守衛擋下（實回 %r）' % text)
        self.assertIn('immutable', text,
                      '歸因：訊息要點名不可變（實回 %r）' % text)
        self.assertEqual(self._db(sa.id, 'tunnel_id'), provisioned,
                         '被擋之後 DB 的值不得改變')

    def test_B8_reprovision_does_not_open_a_second_tunnel(self):
        """stored-id guard 是純業務邏輯，sudo 不該讓它失效。

        ⚠ SA 這一側的 guard 與 smart_home **不同形狀**：SA 不是 fail-loud
        raise，而是 `if self.tunnel_id:` 走 re-sync 分支（重跑宣告式 route sync
        並把 state 收回 active）。所以本條斷言的不是「第二次被拒絕」，而是
        「第二次**沒有再開一條 tunnel**」——那才是這道 guard 存在的理由
        （多一條在計費的孤兒）。
        """
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'reprovision')
        client = self._operator_client()

        first = self._assert_no_rpc_error(
            self._provision(self.alice_login, self.w1.id, sa.id, client),
            '第一次 provision')
        self.assertTrue(first.get('success'),
                        '前提：第一次必須成功——本條只在修法後有意義，'
                        '修法前紅在這一行是預期紅（實回 %r）' % first)
        self.assertEqual(client.create_tunnel.call_count, 1,
                         '前提：第一次恰好開一條')

        second = self._assert_no_rpc_error(
            self._provision(self.alice_login, self.w1.id, sa.id, client),
            '第二次 provision')
        self.assertTrue(
            second.get('success'),
            'SA 的重試走 re-sync 分支，應回成功（實回 %r）' % second)
        self.assertEqual(
            client.create_tunnel.call_count, 1,
            '#484：已有 tunnel 的重試不得再開一條（實際開了 %d 條）'
            % client.create_tunnel.call_count)
        self.assertEqual(
            self._db(sa.id, 'tunnel_id'), self.expected_tunnel_id,
            '重試不得換掉已存的 tunnel_id')
        # re-sync 分支的 broad except 會把失敗寫成 state='error' 然後正常
        # return ⇒ controller 照樣回 success=True。少了這一格，「重試乾淨收斂」
        # 與「重試炸了但沒再開 tunnel」分不出來，而本條要證的是前者。
        self.assertEqual(
            self._db(sa.id, 'state'), 'active_no_route',
            '重試必須乾淨收斂回 active_no_route（實為 %r，error_message=%r）'
            % (self._db(sa.id, 'state'), self._db(sa.id, 'error_message')))


@tagged('post_install', '-at_install')
class TestSa805FixScope(_Sa805Fixture):
    """C 系列：修法的**範圍限定** —— sudo 只加在 BFF 那一條 route 上。

    ⚠ **本組釘的是現況形狀，不是在主張那是對的。**

    `action_provision` 是 public method（無底線前綴、無 `@api.private`），所以
    Odoo 原生的 `/web/dataset/call_kw` 也到得了它，而那條路**不經過本 addon 的
    controller** ⇒ 沒有那句 `.sudo()`，欄位遮罩照樣 fail-closed。這一組把它變成
    實測，作用有三：

    1. 讓 `test_security_access.py` 的
       `test_provision_token_write_fails_closed_for_non_system_owner`（ORM 級、
       `with_user` 直呼）對得上一條**真實可達**的路徑，而不是只描述一個假想身分；
    2. 讓「本修法涵蓋到哪裡」是實測而不是我的斷言；
    3. 這條路徑也跳過 controller 的 `_check_operation_allowed`（訂閱閘），
       本組順帶把那個事實記在案。

    孤兒 tunnel 與「記錄卡在 provisioning」的補償邏輯**不在本票範圍**
    （issue-leader 2026-08-22 裁定另案），本組刻意只斷言身分語意那一格。
    """

    def test_C1_direct_call_kw_still_fails_closed_for_the_tenant(self):
        """繞過 BFF 直接 call_kw ⇒ 仍撞 tunnel_token 的欄位級 AccessError。

        真正的陽性對照只有一層：`create_tunnel` **被呼叫過**——它證明擋點就在
        那次 write，不是更前面的某道閘（少了它，本條與「route 打不到」「session
        沒登入」紅得一模一樣）。

        ⚠ 下面那條「`tunnel_token` 仍是空的」**不是**陽性對照，鑑別力接近 0：
        這條路徑丟例外出 handler ⇒ Odoo 會 rollback 到該次 request 自己的
        savepoint，那一格本來就會回到寫入前的值。留著只是把「沒有憑證外洩到
        DB」寫成可執行的事實，不要拿它當「守衛有效」的證據。
        """
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'direct call_kw')
        client = self._operator_client()
        self.authenticate(self.alice_login, self.alice_login)
        with self._patched_operator(client):
            res = self._call_kw(_MODEL, 'action_provision', [[sa.id]])

        self.assertIn('error', res,
                      '#805 (b) 範圍限定：直接 call_kw 必須仍被擋（實回 %r）'
                      % res)
        text = self._error_text(res)
        self.assertIn('AccessError', text,
                      '歸因：必須是欄位級遮罩（實回 %r）' % text)
        self.assertIn('tunnel_token', text,
                      '歸因：訊息要點名那個欄位（實回 %r）' % text)
        self.assertTrue(
            client.create_tunnel.called,
            '陽性對照：operator 必須真的被呼叫過，否則擋點不在那次 write '
            '（那樣這條就換了受測對象）')
        self.assertFalse(
            self._db(sa.id, 'tunnel_token'),
            '陽性對照：被擋下就不得有任何憑證落地')


@tagged('post_install', '-at_install')
class TestSa805ResyncBranchNeverWritesTheToken(_Sa805Fixture):
    """D 系列：欄位遮罩只擋得住**第一次** provision。

    `action_provision` 的 re-sync 分支（`if self.tunnel_id:`）完全不碰
    `tunnel_token`，所以那條路在修法前後對租戶身分都是通的。這件事很容易被
    誤讀成「遮罩擋住了所有 provision」，而那個誤讀正是把 #805 (b) 拖成一輪
    調查的原因之一 ⇒ 用一條測試把它固定下來。
    """

    def test_D1_tenant_can_resync_when_the_tunnel_already_exists(self):
        """已有 tunnel 時，租戶身分直接 call_kw 跑 re-sync 分支必須成功。

        本條刻意走 **call_kw**（不經 BFF 的 sudo），才證得到「這條分支本身不
        需要平台身分」。fixture 用 sentinel 以平台身分先塞好 tunnel_id ——
        那是伺服器端錨點唯一的合法寫入方式。
        """
        from odoo.addons.woow_paas_platform.models.security_access import (
            _internal_identity,
        )
        sa = self._order_security_access(self.alice_login, self.w1,
                                         'resync branch')
        _internal_identity(sa.sudo()).write({'tunnel_id': 'tun-805-preexisting'})
        self.assertEqual(self._db(sa.id, 'tunnel_id'), 'tun-805-preexisting',
                         '前提：fixture 必須真的塞進 tunnel_id')
        self.assertFalse(self._db(sa.id, 'tunnel_token'),
                         '前提：這一格刻意留空，用來證明 re-sync 不寫它')

        client = self._operator_client()
        self.authenticate(self.alice_login, self.alice_login)
        with self._patched_operator(client):
            res = self._call_kw(_MODEL, 'action_provision', [[sa.id]])

        self.assertNotIn(
            'error', res,
            '#805 (b)：re-sync 分支不寫 tunnel_token ⇒ 租戶身分必須通過'
            '（實回 %s）' % self._error_text(res))
        self.assertFalse(
            client.create_tunnel.called,
            '已有 tunnel_id 就不得再開一條（走到了＝進錯分支，本條換了受測對象）')
        self.assertEqual(
            self._db(sa.id, 'state'), 'active_no_route',
            're-sync 分支必須把零 route 的 SA 收回 active_no_route')
        self.assertFalse(
            self._db(sa.id, 'tunnel_token'),
            '🔴 本條的核心：re-sync 分支全程不碰 tunnel_token')
