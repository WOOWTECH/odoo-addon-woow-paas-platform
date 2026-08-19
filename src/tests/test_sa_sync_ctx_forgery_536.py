"""#536：SA 的 sync-control context key 家族可被租戶偽造。

## 缺陷（與 #522 同源，另外四支）

`/web/dataset/call_kw` 把**呼叫端傳來的 context 原封不動**換進 env
（`addons/web/controllers/dataset.py` → `odoo/api.py::call_kw` 的
`context = kwargs.pop('context', None) or {}; recs = recs.with_context(context)`）
⇒ **任何拿 context key 當「內部呼叫」逃生門的守衛，都是租戶可偽造的**。

#522 已把**身分錨點**那一支（`sa_internal_identity_write`）改成私有 sentinel
物件；同族還有三個活鍵在 SA 這條線上，全部只判斷「truthy」：

* `sa_skip_route_sync` —— 抑制 route／policy／rule 的 create／write／unlink
  對 operator 的宣告式推送。租戶偽造後 Odoo 有紀錄、Cloudflare 沒有。真正的
  危害在後面：路由推送是**宣告式全量**，之後任何一次合法異動都會把這條沒同步
  過的 route 一起推上去——而 `is_protected` 的 Access app 建立**正是被抑制的
  那一步** ⇒ 使用者標為 protected 的 route 可能以「無 Access gate」的狀態上線。
* `sa_rule_replace` —— 抑制 rule 層的過渡同步，**並且**跳過 `rule.unlink()` 裡
  的 `_check_at_least_one_include()`。那條 `@api.constrains('rule_ids')` 對
  「直接 unlink 一條 rule」根本不會觸發（policy 的 `rule_ids` 沒有被寫），所以
  unlink 裡那行顯式呼叫是唯一防線 ⇒ 偽造後可留下 0 條 include 的 policy。
* `sa_force_access_sync` —— 繞過 Access 推送 liveness gate 的**狀態那一半**
  （仍需 `tunnel_id`）⇒ suspended／deleting 狀態仍能把自己 route 的 Access
  設定推去 CF。

（第四個 `sa_force_delete_primary` 是死鍵——`src/models` 與 `src/controllers`
全域無人讀取，本 PR 一併清掉測試殘留，不需要守線測試。）

## 為什麼一定要走真 HTTP RPC

本檔刻意是 `HttpCase` 而不是 `TransactionCase`：這條攻擊的**前提**就是
「call_kw 會把 client 的 context 換進 env」。在 `TransactionCase` 裡直接
`with_context(sa_skip_route_sync=True)` 只證明了「這個 key 有作用」，證明不了
**租戶構造得出它**——而修法（sentinel）之後，那種寫法會因為換了值而恆綠，測試
就退化成沒有鑑別力的形狀（#739／#753 踩過的假綠）。

## 分層（每一格都要能分辨「守住了」與「探針壞了」）

* **A／B／C** — `sa_skip_route_sync`：route.create、route.write、policy.create
* **D／E** — `sa_rule_replace`：rule.unlink 的 ≥1 include 檢查、rule.write 的同步
* **F** — `sa_force_access_sync`：suspended SA 的 state gate

每一組都是三格，缺一格就分不出「守住了」與「探針壞了」：

1. **不帶 key 的對照**（基準：這一步本來就會 sync／本來就會被擋）——少了它，
   「偽造沒生效」與「這條路徑在本環境根本不會 sync」分不出來，後者會讓本檔
   在守衛被拿掉之後仍然是綠的；
2. **帶偽造 key**（🔴 修前紅）——結果必須與第 1 格**完全相同**；
3. **走 in-process sentinel**（見 `TestInternalSentinelStillWorks536`）——必須
   仍然開閘，否則本檔的「修好了」其實只是「把功能刪掉了」。

F 組的斷言方向是「**不**得呼叫 operator」，這正是最容易被探針故障滿足的形狀，
所以 F0 先用同一支呼叫、同一個 mock 證明「這個觀測值在本環境會亮」。
"""

import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _force_access_sync,
    _internal_identity,
    _rule_replace,
    _skip_route_sync,
)

_SA = 'woow_paas_platform.security_access'
_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_RULE = 'woow_paas_platform.security_access_access_policy_rule'

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 536' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _Sync536Fixture(HttpCase):
    """一個租戶（alice，workspace owner）＋ 一台 active＋live-tunnel 的 SA ＋
    一條 is_protected 的 route。這是「合法異動就會推 Cloudflare」的最小母體。
    """

    def setUp(self):
        super().setUp()
        self.SA = self.env[_SA]
        self.Route = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.Rule = self.env[_RULE]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice536')
        self.assertFalse(
            self.alice.has_group('base.group_system'),
            '前提：攻擊者是普通租戶，只能有 base.group_user')

        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 536', 'owner_id': self.alice.id})
        WA = self.env['woow_paas_platform.workspace_access'].sudo()
        if not WA.search([('workspace_id', '=', self.workspace.id),
                          ('user_id', '=', self.alice.id)]):
            WA.create({'workspace_id': self.workspace.id,
                       'user_id': self.alice.id, 'role': 'owner'})

        setup_mock = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=setup_mock):
            # SA 先停在 'pending'：此時每個 sync 都被 liveness gate 擋住，
            # 所以建立母體本身不會打 operator，也就不需要在 fixture 裡用到
            # 任何一個本檔要測的抑制 key（用了就會讓 RED 相依於修法後才存在
            # 的 API）。
            self.sa = self.SA.sudo().create(
                {'name': 'GW 536', 'workspace_id': self.workspace.id})
            self.assertEqual(
                self.sa.state, 'pending',
                '前提：SA 必須停在 pending，母體建立才不會打 operator')
            # tunnel_id 是 #522 的身分錨點，只有 sentinel 路徑寫得動。
            _internal_identity(self.sa).write(
                {'state': 'active', 'tunnel_id': 'tun-536'})
            self.route = self.Route.sudo().create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'sa536-route',
                'is_protected': True,
            })
        # route create 已經把 mock 的 app_id 回填進來——F 組要的就是這個
        # 「suspended 時仍握著 CF Access app id」的狀態。
        self.assertEqual(
            self.route.access_app_id, 'app-1',
            '前提：母體 route 必須已經有 CF Access app id')
        self.env.flush_all()

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': ['pol-1'],
            'allowed_idps': ['idp-g', 'idp-otp']}
        return m

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw` —— context 偽造攻擊的真實路徑。

        `odoo/api.py::call_kw` 會 `kwargs.pop('context')` 再
        `recs.with_context(context)` ⇒ 呼叫端 context 整包換進 env。
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

    @staticmethod
    def _error_text(result):
        data = (result.get('error', {}) or {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn(
            'error', result, msg + '（回 %s）' % self._error_text(result))
        return result.get('result')

    def _assert_rpc_error(self, result, needle, msg):
        self.assertIn('error', result, msg + '（實際成功回 %r）' % result)
        text = self._error_text(result)
        self.assertIn(needle, text, msg + '（錯誤是 %s，不是預期的那個）' % text)
        return text

    def _login_alice(self):
        self.authenticate(self.alice_login, self.alice_login)

    def _make_policy(self, name, rules):
        """以 sudo 建一條 policy（母體，不是受測動作）。

        必須包在 patch 裡：母體 SA 是 active＋live tunnel，所以建 policy 這一步
        本來就會推 operator，而測試環境沒有設定 operator URL／API key
        （`_get_operator_client` 會 raise UserError）。這裡刻意**不**用
        `sa_skip_route_sync` 來閃過——那正是本檔要測的 key，用了就會讓母體
        相依於受測對象。
        """
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            pol = self.Policy.sudo().create({
                'route_id': self.route.id, 'name': name, 'decision': 'allow',
                'rule_ids': [(0, 0, r) for r in rules],
            })
            self.env.flush_all()
        return pol


class TestSkipRouteSyncForgery536(_Sync536Fixture):
    """A／B／C：`sa_skip_route_sync` 不得被租戶從 RPC payload 打開。"""

    # ---- A：route.create ---------------------------------------------- #

    def test_A1_control_route_create_without_key_syncs(self):
        """對照：不帶 key 的 route create 一定會推 operator。

        這一格是 A2 的**鑑別力來源**：少了它，A2 的「沒被抑制」與「這條路徑
        本來就不會 sync」分不出來。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(_ROUTE, 'create', [{
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'sa536-a1',
                'is_protected': True,
            }]), 'route create 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_tunnel_routes.called,
            '對照：不帶 key 的 route create 必須推 ingress／DNS 全量狀態')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：is_protected route create 必須建立它自己的 CF Access app')

    def test_A2_forged_skip_route_sync_cannot_suppress_route_create(self):
        """🔴 #536 主格：租戶在 call_kw 的 context 塞 `sa_skip_route_sync`。

        修前：守衛只判 truthy ⇒ 整個推送被抑制，Odoo 有這條 protected route、
        Cloudflare 沒有它的 Access app ⇒ 之後任一次合法異動的全量推送會把這條
        route 的 ingress／DNS 一起送上去，而 Access gate 從來沒被建立過。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'create',
                [{'security_access_id': self.sa.id,
                  'subdomain_prefix': 'sa536-a2',
                  'is_protected': True}],
                {'context': {'sa_skip_route_sync': True}},
            ), 'route create 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_tunnel_routes.called,
            '#536：偽造的 `sa_skip_route_sync` 不得抑制 route 全量推送')
        self.assertTrue(
            mc.sync_access_app.called,
            '#536：偽造的 `sa_skip_route_sync` 不得抑制 protected route 的 '
            'Access app 建立——這正是「protected 卻沒有 gate」的來源')

    # ---- B：route.write ----------------------------------------------- #

    def test_B1_control_route_write_without_key_syncs(self):
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'write',
                [[self.route.id], {'session_duration': '8h'}]),
                'route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：改 session_duration 必須重推該 route 的 Access 狀態')

    def test_B2_forged_skip_route_sync_cannot_suppress_route_write(self):
        """🔴 同一支 write，加上偽造的 key 之後結果必須完全一樣。"""
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'write',
                [[self.route.id], {'session_duration': '8h'}],
                {'context': {'sa_skip_route_sync': True}}),
                'route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#536：偽造的 `sa_skip_route_sync` 不得抑制 route.write 的 '
            'Access 推送（DB 改了、CF 沒改）')

    # ---- C：policy.create --------------------------------------------- #

    def test_C1_control_policy_create_without_key_syncs(self):
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(_POLICY, 'create', [{
                'route_id': self.route.id, 'name': 'c1', 'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'a@acme.com'})],
            }]), 'policy create 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：新增 Access policy 必須推到 Cloudflare')

    def test_C2_forged_skip_route_sync_cannot_suppress_policy_create(self):
        """🔴 policy 是「誰進得來」的那張表——偽造後 CF 端不會知道它存在。"""
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _POLICY, 'create',
                [{'route_id': self.route.id, 'name': 'c2',
                  'decision': 'allow',
                  'rule_ids': [(0, 0, {'group': 'include',
                                       'rule_type': 'email_list',
                                       'rule_value': 'b@acme.com'})]}],
                {'context': {'sa_skip_route_sync': True}}),
                'policy create 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#536：偽造的 `sa_skip_route_sync` 不得抑制 policy 的 CF 推送')


class TestRuleReplaceForgery536(_Sync536Fixture):
    """D／E：`sa_rule_replace` 不得被租戶從 RPC payload 打開。"""

    # ---- D：rule.unlink 的「≥1 include」檢查 --------------------------- #

    def test_D1_control_rule_unlink_without_key_is_rejected(self):
        """對照：刪掉唯一一條 include rule，不帶 key 時必須被擋下。

        `_check_at_least_one_include` 是 `@api.constrains('rule_ids')`，而直接
        unlink 一條 rule **不會**寫 policy 的 `rule_ids` ⇒ 那條 constrains 根本
        不會觸發，`rule.unlink()` 裡的顯式呼叫是唯一防線。
        """
        pol = self._make_policy('d1', [
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'only@acme.com'}])
        rule = pol.rule_ids
        self.assertEqual(len(rule), 1, '前提：policy 只有一條 include rule')
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            result = self._call_kw(_RULE, 'unlink', [[rule.id]])
        self._assert_rpc_error(
            result, 'at least one Include',
            '對照：刪掉唯一的 include rule 必須被 ≥1 include 檢查擋下')
        self.assertTrue(
            rule.exists(),
            '對照：被擋下的 unlink 必須整個 rollback，rule 列還要在')

    def test_D2_forged_rule_replace_cannot_skip_include_check(self):
        """🔴 #536：偽造 `sa_rule_replace` 就跳過了 ≥1 include 檢查。

        這是這個 key 比 issue 描述更嚴重的那一面——它不只抑制過渡同步，還是一道
        **validation bypass**：偽造後租戶留下 0 條 include 的 policy，之後任何一
        次合法 Access 編輯都會被 Cloudflare 以 422 打回。
        """
        pol = self._make_policy('d2', [
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'only@acme.com'}])
        rule = pol.rule_ids
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            result = self._call_kw(
                _RULE, 'unlink', [[rule.id]],
                {'context': {'sa_rule_replace': True}})
        self._assert_rpc_error(
            result, 'at least one Include',
            '#536：偽造的 `sa_rule_replace` 不得跳過 ≥1 include 檢查')
        self.assertTrue(
            rule.exists(),
            '#536：被擋下的 unlink 必須整個 rollback，rule 列還要在')
        self.assertTrue(
            pol.rule_ids.filtered(lambda r: r.group == 'include'),
            '#536：policy 不得被留在 0 條 include 的狀態')

    # ---- E：rule.write 的過渡同步抑制 ---------------------------------- #

    def test_E1_control_rule_write_without_key_syncs(self):
        pol = self._make_policy('e1', [
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'one@acme.com'},
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'two@acme.com'}])
        rule = pol.rule_ids[0]
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _RULE, 'write',
                [[rule.id], {'rule_value': 'changed@acme.com'}]),
                'rule write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：改一條 selector rule 必須重推該 route 的 Access 狀態')

    def test_E2_forged_rule_replace_cannot_suppress_rule_sync(self):
        """🔴 偽造 `sa_rule_replace` ⇒ 規則改了但 Cloudflare 不知道，全程無聲。"""
        pol = self._make_policy('e2', [
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'one@acme.com'},
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'two@acme.com'}])
        rule = pol.rule_ids[0]
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _RULE, 'write',
                [[rule.id], {'rule_value': 'changed@acme.com'}],
                {'context': {'sa_rule_replace': True}}),
                'rule write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#536：偽造的 `sa_rule_replace` 不得抑制 rule.write 的 CF 推送')


class TestForceAccessSyncForgery536(_Sync536Fixture):
    """F：`sa_force_access_sync` 不得被租戶用來繞過 liveness gate 的狀態那半。"""

    def _suspend(self):
        self.sa.sudo().write({'state': 'suspended'})
        self.env.flush_all()
        self.assertEqual(self.sa.state, 'suspended')
        self.assertTrue(
            self.sa.tunnel_id,
            '前提：tunnel 仍在（gate 的另一半），否則測到的是 tunnel 那條分支')

    def test_F0_control_observable_is_live_while_active(self):
        """對照（探針自檢）：SA 還是 active 時，同一支 write 會打 operator。

        F1／F2 的斷言方向是「**不**得呼叫」——那是最容易被探針故障滿足的形狀
        （mock 沒掛上、欄位打錯、request 根本沒到，輸出都跟「守住了」一樣）。
        這一格用**同一支呼叫、同一個 mock** 先證明這個觀測值會亮。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'write', [[self.route.id], {'session_duration': '1h'}]),
                'route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '探針自檢：active SA 上這支 write 必須打得到 operator')

    def test_F1_control_suspended_without_key_does_not_sync(self):
        """對照：停權中不帶 key ⇒ state gate 擋住，不得推 CF。"""
        self._suspend()
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'write', [[self.route.id], {'session_duration': '1h'}]),
                'route write 不該回 JSON-RPC error')
        mc.sync_access_app.assert_not_called()

    def test_F2_forged_force_access_sync_cannot_bypass_state_gate(self):
        """🔴 #536：偽造 `sa_force_access_sync` ⇒ 停權中仍推 Access 設定到 CF。

        ⚠ 本 PR 只堵這個 key。背後「停權未全面阻斷 operator 呼叫」的家族問題
        （issue #536 明列）屬**另案**，不在此處理。
        """
        self._suspend()
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(self._call_kw(
                _ROUTE, 'write',
                [[self.route.id], {'session_duration': '1h'}],
                {'context': {'sa_force_access_sync': True}}),
                'route write 不該回 JSON-RPC error')
        self.assertFalse(
            mc.sync_access_app.called,
            '#536：偽造的 `sa_force_access_sync` 不得繞過 state gate '
            '（停權中仍把自己 route 的 Access 設定推去 Cloudflare）')


class TestInternalSentinelStillWorks536(_Sync536Fixture):
    """G：三個逃生門對**伺服器端程式碼**必須照舊開得了。

    這一組是修法的陰性對照。少了它，「偽造打不開」與「這個功能被整個刪掉了」
    在上面每一格的輸出上完全一樣——而後者會讓 migration 在升級途中對 live SA
    觸發真的 Cloudflare 呼叫（`sa_skip_route_sync` 存在的原因），也會讓
    controller 的整組 rule 替換在瞬時空集上被 operator 以 422 打回
    （`sa_rule_replace` 存在的原因）。

    ⚠ 這一組刻意**不**走 RPC：sentinel 的全部意義就是「呼叫端構造不出來」，
    所以它只可能在 in-process 呼叫端出現。
    """

    def test_G1_sentinel_skip_route_sync_still_suppresses(self):
        """`_skip_route_sync()` 開閘 ⇒ 只搬 DB 狀態、不打 operator。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            route = _skip_route_sync(self.Route.sudo()).create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'sa536-g1',
                'is_protected': True,
            })
        mc.sync_tunnel_routes.assert_not_called()
        mc.sync_access_app.assert_not_called()
        self.assertTrue(
            route.exists(),
            '陰性對照：DB 那一半必須照樣落地（只是不打 operator）')

    def test_G2_sentinel_rule_replace_still_allows_whole_ruleset_replace(self):
        """`_rule_replace()` 開閘 ⇒ controller 的 (5,0,0)＋creates 整組替換可行。

        同時釘住它存在的理由：整個替換過程中，**沒有任何一次**推送帶著空的
        include（真 operator 對空 include 直接 422，整個編輯 abort）。
        """
        pol = self._make_policy('g2', [
            {'group': 'include', 'rule_type': 'email_list',
             'rule_value': 'before@acme.com'}])
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            _rule_replace(pol).write({
                'name': 'g2-edited',
                'rule_ids': [(5, 0, 0),
                             (0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'after@acme.com'})],
            })
        self.assertEqual(
            pol._rules_by_group('include'),
            [{'rule_type': 'email_list', 'rule_value': 'after@acme.com'}],
            '陰性對照：整組替換的最終狀態必須是新的那一條')
        self.assertTrue(
            mc.sync_access_app.called,
            '陰性對照：替換結束後 policy.write() 仍要做一次全量推送')
        for call in mc.sync_access_app.call_args_list:
            for p in call.args[0].get('policies', []):
                self.assertTrue(
                    p.get('include'),
                    'operator 收到空 include policy %r（替換瞬時狀態被外推）'
                    % p.get('name'))

    def test_G3_sentinel_force_access_sync_still_bypasses_state_gate(self):
        """`_force_access_sync()` 開閘 ⇒ 停權中仍可收斂（resume 靠這條）。"""
        self.sa.sudo().write({'state': 'suspended'})
        self.env.flush_all()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.sa._sync_all_routes_access()
        self.assertTrue(
            mc.sync_access_app.called,
            '陰性對照：provision／resume 的收斂點必須繞得過 state gate，'
            '否則停權期間被關掉的 route 永遠拆不掉它的 CF Access app')

    def test_G4_plain_true_does_not_open_the_hatch_even_in_process(self):
        """守衛比對的是**值的同一性**，不是「呼叫端是誰」。

        這一格釘住修法的形狀：如果哪天有人把判定改回 `ctx.get(key)`，本檔
        A2/B2/C2 會紅——但如果改成「in-process 就放行」之類的替代設計，那些格
        仍會綠，只有這一格會紅。
        """
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.Route.sudo().with_context(sa_skip_route_sync=True).create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'sa536-g4',
                'is_protected': True,
            })
        self.assertTrue(
            mc.sync_access_app.called,
            '#536：`True` 不是 sentinel——即使是 in-process 呼叫端也不得開閘')
