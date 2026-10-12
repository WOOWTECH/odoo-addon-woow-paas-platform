# -*- coding: utf-8 -*-
"""#787：SH 的三把 sync-control context key 可被租戶偽造（#522／#536 家族）。

## 缺陷

`/web/dataset/call_kw` 把**呼叫端傳來的 context 原封不動**換進 env
（`addons/web/controllers/dataset.py` → `odoo/api.py::call_kw` 的
`context = kwargs.pop('context', None) or {}; recs = recs.with_context(context)`）
⇒ **任何拿 context key 當「內部呼叫」憑證、而且只判 truthy 的守衛，都是租戶
可偽造的**。

`smart_home_route.py` 有三把同形態的鑰匙，本檔一次守完：

| key | 讀取點 | 偽造後 |
| --- | --- | --- |
| `sm_skip_access_sync` | `create()` / `write()` 後的 Access 推送閘 | DB 落地、Cloudflare 不知情（fail-open 且無聲） |
| `sm_route_cascade_unlink` | `unlink()` 的 primary 不可刪守衛 | 守衛被關掉，流程往下撞 ACL |
| `sm_force_access_sync` | `_access_sync_skip_reason()` 的 liveness gate | 停權中的 SH 仍把 Access 設定推去 Cloudflare |

可達性是**實測**過的（#787 的 `sh-787-reach` 探針，真 call_kw），不是推論：

* 租戶自建 workspace 的當下就是 owner，`smart_home_route_rule_owner_admin_manage`
  給 owner／admin 對 `smart_home_route` 完整寫入，而且 domain **不濾 SH 的 state**
  ⇒ 連停權中那台的 route 都寫得動。
* `ir.model.access` 對 `base.group_user` 是 `read=1,write=1,create=0,unlink=0`
  ⇒ `create()` 那個讀取點租戶到不了（本檔因此不對它下攻擊斷言，改由 G 組守它的
  形狀）；`unlink()` 那把被 ACL 擋死 ⇒ 它是 **defense-in-depth**，判準因此是
  「守衛有沒有被關掉」而不是「刪得掉沒」。

## 為什麼一定要走真 HTTP RPC

A／B／C／D 四組刻意是 `HttpCase` 而不是 `TransactionCase`，理由與
`test_cs_sync_ctx_forgery_787.py`／`test_sa_sync_ctx_forgery_536.py` 逐字相同：
這條攻擊的**前提**就是「call_kw 會把 client 的 context 換進 env」。在
`TransactionCase` 裡直接 `with_context(sm_skip_access_sync=True)` 只證明了「這個
key 有作用」，證明不了**租戶構造得出它**；一旦哪天有人把守衛改成「看呼叫端是誰」
之類的替代設計，那種寫法會恆綠而本檔的攻擊格仍然會紅。

## 分層（每一格都要能分辨「守住了」與「探針壞了」）

* **A** — 未保護的 route 被 RPC 打開 `is_protected`（Access app 的**建立**）。
* **B** — 已保護的 route 被 RPC 改 `session_duration`（重推既有 app 的狀態）。
* **C** — primary route 被 RPC `unlink`（守衛有沒有被 forge 關掉）。
* **D** — **停權中** SH 的 route 被 RPC 改設定（liveness gate 的狀態那一半）。
* **G** — sentinel 對**伺服器端程式碼**必須照舊開得了（修法的陰性對照；修法落地
  後才加得進來，因為 helper 在修法之前不存在）。

每組三格，缺一格就分不出「守住了」與「探針壞了」：

1. **不帶 key 的對照**——少了它，「偽造沒生效」與「這條路徑在本環境根本不會
   sync」分不出來，後者會讓本檔在守衛被拿掉之後仍然是綠的；
2. **帶偽造 key**（🔴 修前紅）——結果必須與第 1 格**完全相同**；
3. **fail-loud**（🔴 修前紅）——偽造留下 WARNING。少了它，「守住了」與「守衛
   把整個 key 當成不存在」不可區分，而後者會讓「還有 in-process 碼照舊傳 True」
   永遠沒有訊號。

## 🔴 C 組的判準是**例外型別翻轉**，不是「刪成功沒」

`smart_home_route` 對 `base.group_user` 是 `unlink=0`，所以租戶**本來就刪不掉**
任何 route。拿「刪掉了沒」當觀測是零鑑別力——兩組都刪不掉。真正的判別式是**被
誰擋下來**：

| | 修前 | 修後 |
| --- | --- | --- |
| 不帶 key | `UserError`（primary 不可刪，守衛先擋） | 同左 |
| 帶偽造 key | **`AccessError`**（守衛被關掉，往下撞 ACL）🔴 | 回到 `UserError` |

⇒ `AccessError` 就是「守衛真的被 forge 關掉了」的簽名。

## 母體為什麼不使用任何抑制 key

用了受測的 key 就會讓 RED 相依於修法後才存在的 API（#536 已明列這個陷阱）。
本檔改用**時序**達成同一件事：SH 先在**沒有 `tunnel_id`** 的狀態下建立，route
全部在那時候建好（`_access_is_live()` 因此恆為 False ⇒ 一次 operator 都不會被
呼叫），最後才用身分錨點 sentinel 把 `tunnel_id` 補上去。

母體另有一道天然自檢：測試環境沒有設定 operator URL／API key，任何非預期的推送
都會以 ``PaaS Operator is not configured`` 當場炸掉，而不是安靜通過。

## 界線

sentinel 的判準是 **context 值的物件同一性**，與 ACL／欄位權限無關 ⇒ G 組用
in-process（superuser）env 是正確的，而且刻意如此：sentinel 的全部意義就是
「呼叫端構造不出來」，它只可能在 in-process 呼叫端出現。反過來也成立——本檔
**沒有**用 superuser env 去驗任何跟欄位權限有關的斷言（那是 #739／#753 的假綠
形狀）；租戶身分下的欄位權限鏈由 `test_sh_access_acl_768.py` 守。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..models.smart_home import _internal_identity
from ..models.smart_home_route import (
    _sm_forced_access_sync,
    _sm_route_cascade_unlink,
    _sm_skip_access_sync,
)
from .smart_home_fixture import sh_model
from .publication_receipt_fixture import install_publication_receipts

_ROUTE = 'woow_paas_platform.smart_home_route'
#: 🔴 patch 點寫錯不會讓否定性斷言變紅（`sync_access_app.called` 會恆為 False）。
#: `smart_home.py` 是 **module 層 import** `get_paas_operator_client`，所以要 patch
#: 的是它 module 裡的那個名字，不是 `services.paas_operator` 裡的原件。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)
#: 判定函式印 WARNING 的 logger（fail-loud 那一格的觀測點）。
_ROUTE_LOGGER = 'odoo.addons.woow_paas_platform.models.smart_home_route'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 787' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


def _sync_result(app_id='app-787', policy_ids=None):
    """operator `PUT /api/access/sync` 的**真實**回傳形狀（五個 key 一個不少）。

    不用裸 `MagicMock`：回存那一段會把回傳值寫進 `access_app_id` 這類 Char 欄位，
    裸 mock 會讓測試綠而 prod 炸（#745／#768 已各寫下同一條）。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': [],
    }


@tagged('post_install', '-at_install')
class _Sh787Fixture(HttpCase):
    """alice（自建 workspace ⇒ owner）＋ 四台 SH，各一條 primary route。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.Route = self.env[_ROUTE]
        self.alice, self.alice_login = _new_group_user(self.env, 'alice787sh')
        self.assertFalse(
            self.alice.has_group('base.group_system'),
            '前提：攻擊者是普通租戶，只能有 base.group_user')

        # 🔴 自建 workspace ＝ 自己就是 owner。這正是本缺陷的可達性來源，所以
        #    它是**前提斷言**而不是隨手的 fixture：哪天 workspace 建立不再自動
        #    給 owner，本檔四組的攻擊路徑就整組失效（而輸出仍會是綠的）。
        self.workspace = self.env[
            'woow_paas_platform.workspace'].with_user(self.alice).create(
                {'name': 'WS 787 SH'})
        grant = self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', self.workspace.id),
            ('user_id', '=', self.alice.id)])
        self.assertEqual(
            grant.role, 'owner',
            '前提：租戶自建 workspace 必須拿到 owner，否則 ir.rule 不給 write，'
            '本檔的 RPC 攻擊面根本不存在')

        # A：未保護的 route（打開保護那一步）
        self.sh_a, self.route_a = self._sh_with_route(protected=False)
        # B：已保護的 route（改既有 app 的狀態）
        self.sh_b, self.route_b = self._sh_with_route(protected=True)
        # C：cascade unlink 守衛（primary 不可刪）
        self.sh_c, self.route_c = self._sh_with_route(protected=False)
        # D：停權中的 SH（liveness gate 的狀態那一半）
        self.sh_d, self.route_d = self._sh_with_route(
            protected=True, state='suspended')

        self.assertFalse(
            self.route_a.is_protected, '前提：A 組的母體必須是未保護的')
        self.assertTrue(
            self.route_b.is_protected, '前提：B 組的母體必須已經是保護中的')
        self.assertTrue(
            self.route_c.is_primary, '前提：C 組打的必須是 primary route')
        self.assertEqual(
            self.sh_d.sudo().state, 'suspended',
            '前提：D 組的 SH 必須是停權中——擋住它的若不是 state 閘，'
            '這一組就測不到東西')
        for label, route in (('A', self.route_a), ('B', self.route_b),
                             ('C', self.route_c), ('D', self.route_d)):
            self.assertTrue(
                route.sudo().hostname,
                '前提：%s 組的 route 必須有 hostname，否則 liveness gate 的第一格'
                '就擋掉了，量到的不是要測的那一格' % label)
            self.assertTrue(
                route.smart_home_id.sudo().tunnel_id,
                '前提：%s 組的 SH 必須有 tunnel_id（同上）' % label)
            self.assertFalse(
                route.sudo().access_app_id,
                '前提：%s 組的 route 尚未有 CF Access app——母體建立過程中不得'
                '有任何一次推送真的跑掉' % label)
        self.env.flush_all()

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #

    def _sh_with_route(self, protected, state='active'):
        """一台 SH ＋ 它的 primary route，**過程中一次 operator 都不呼叫**。

        🔴 時序是判準的一部分，不是排版：SH 先在沒有 `tunnel_id` 的狀態下建立，
        route 在那時候建好（`_access_is_live()` 第一格就 return False），最後才
        用身分錨點 sentinel 補上 `tunnel_id`。這樣母體就**不需要**用到任何一把
        受測的抑制 key——用了的話 RED 會相依於修法後才存在的 API（#536 明列）。

        `with_env(self.env)` 不是排版：`sh_model()` 回的 handle 開著 SH 的建立閘
        與身分錨點閘，而 `with_context` 是 merge、`.sudo()` / `.browse()` 都保留
        它 ⇒ 不洗掉的話 fixture 會把別的守衛一起關掉。
        """
        sh = sh_model(self.env).sudo().create({
            'name': 'sh787-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'state': state,
            # subdomain 有 DB UNIQUE，且 primary route 的 hostname 由它 compute。
            'subdomain': 'paas-sm-787-%s' % uuid.uuid4().hex[:8],
        }).with_env(self.env)
        route = self.Route.sudo().create({
            'smart_home_id': sh.id,
            'is_primary': True,
            'is_protected': protected,
        }).with_env(self.env)
        _internal_identity(sh.sudo()).write(
            {'tunnel_id': 'tun-787-%s' % uuid.uuid4().hex[:8]})
        return sh, route

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _mock(self, result=None):
        client = MagicMock()
        install_publication_receipts(client)
        client.sync_access_app.side_effect = None
        client.sync_access_app.return_value = result or _sync_result()
        return client

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw` —— context 偽造攻擊的真實路徑。"""
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

    def _assert_error_is(self, result, exc_name, msg, contains=None):
        """被擋下來，**而且是被哪一層、哪一道守衛擋的**要說得出來。

        只斷言「有 error」的話，C 組的兩格會為了不同的理由一起變綠——而那兩個
        理由（守衛擋 vs ACL 擋）正是本組唯一的判別式。

        ⚠ `contains` 不是裝飾：`smart_home_route.unlink()` 裡有**兩處** raise
        `UserError`——primary 不可刪那道，以及 Access teardown 失敗那道。今天後者
        在本組母體上不可達（setUp 斷言過 route 沒有 `access_app_id`，
        `_capture_access_teardown()` 因此回 None），但那是**母體的性質**，不是判準
        的性質：母體哪天多擺一個 app id，只驗型別的 C2 就會被 teardown 那道
        `UserError` 為了錯的理由餵綠。錨在「primary」這個字上讓那件事會紅。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn(exc_name, text, msg + '（實際回 %r）' % text)
        if contains:
            self.assertIn(
                contains, text,
                msg + '（型別對了但擋下來的不是預期那道守衛；實際回 %r）' % text)

    def _login_alice(self):
        self.authenticate(self.alice_login, self.alice_login)

    @staticmethod
    def _forged(key):
        """租戶在 JSON payload 裡表達得出來的那個值。"""
        return {'context': {key: True}}


class TestSmSkipAccessSyncForgery787(_Sh787Fixture):
    """A／B：`sm_skip_access_sync` 不得被租戶從 RPC payload 打開。"""

    # ---- A：開啟保護（Access app 的建立） ------------------------------ #

    def test_A1_control_tenant_write_without_key_syncs(self):
        """對照：不帶 key 的 `is_protected` 寫入一定會推 operator。

        這一格是 A2／A3 的**鑑別力來源**：少了它，A2 的「偽造沒生效」與「這條
        路徑在本環境根本不會 sync」分不出來。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_a.id], {'is_protected': True}]),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：租戶打開 is_protected 必須建立它的 CF Access app')

    def test_A2_forged_key_cannot_suppress_the_access_push(self):
        """🔴 #787 主格：租戶在 call_kw 的 context 塞 `sm_skip_access_sync`。

        修前：守衛只判 truthy ⇒ 推送整段被抑制，Odoo 記著「已保護」而
        Cloudflare 上沒有任何 Access app ⇒ 那個 hostname 完全公開，而所有觀測
        點（write 的回傳、端點的 success、DB 的值、UI 上的開關）都說它被保護了。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_a.id], {'is_protected': True}],
                              self._forged('sm_skip_access_sync')),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：偽造的 `sm_skip_access_sync` 不得抑制 Access 推送——這正是'
            '「DB 說已保護、CF 上什麼都沒有」的來源')
        self.assertTrue(
            self.route_a.sudo().is_protected,
            'DB 那一半本來就會落地——缺陷的形狀正是「值進去了、CF 不知道」，'
            '修法不是把寫入擋掉，而是不讓推送被跳過')

    def test_A3_forged_key_is_fail_loud(self):
        """🔴 偽造必須留下 WARNING。

        少了這一格，「守住了」與「守衛把這個 key 當成完全不存在」不可區分，而
        後者會讓「還有 in-process 的碼照舊傳 True」永遠沒有任何訊號——那是本
        修法**唯一**的無聲失效面（傳 True 從此不開閘）。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            with self.assertLogs(_ROUTE_LOGGER, level='WARNING') as captured:
                self._assert_ok(
                    self._call_kw(_ROUTE, 'write',
                                  [[self.route_a.id], {'is_protected': True}],
                                  self._forged('sm_skip_access_sync')),
                    'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            any('sm_skip_access_sync' in line for line in captured.output),
            '#787：偽造的開關必須點名 key 記一行 WARNING，實際收到 %r'
            % captured.output)

    # ---- B：改既有 Access app 的狀態 ---------------------------------- #

    def test_B1_control_session_duration_write_without_key_syncs(self):
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_b.id], {'session_duration': '8h'}]),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：改 session_duration 必須重推該 route 的 Access 狀態')

    def test_B2_forged_key_cannot_suppress_an_update_push(self):
        """🔴 同一支 write，加上偽造的 key 之後結果必須完全一樣。

        與 A 組是**兩條不同的可達路徑**：A 是「保護從無到有」，B 是「已保護的
        route 被改設定」。只守 A 的話，把 session 長度改成 168h 而 CF 上仍是舊值
        這種偏移不會有任何一格抓得到。

        ⚠ 母體刻意已經 `is_protected=True`：`_access_sync_skip_reason()` 的第二
        道閘（「未保護且從沒有過 app id」）否則會讓兩格**一起**變成 no-op，把整
        把誤判成「這個 key 無效」。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_b.id], {'session_duration': '8h'}],
                              self._forged('sm_skip_access_sync')),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：偽造的 `sm_skip_access_sync` 不得抑制既有 Access app 的'
            '狀態更新（DB 改了、CF 沒改）')
        self.assertEqual(
            self.route_b.sudo().session_duration, '8h',
            'DB 那一半照樣落地——同 A2')


class TestSmRouteCascadeUnlinkForgery787(_Sh787Fixture):
    """C：`sm_route_cascade_unlink` 不得被租戶從 RPC payload 打開。

    🔴 判準是**例外型別翻轉**，不是「刪成功沒」——見檔頭那張表。
    """

    def test_C1_control_primary_unlink_is_blocked_by_the_guard(self):
        """對照：不帶 key 時，擋下來的必須是**守衛**（`UserError`）。

        這一格同時是 C2 的鑑別力來源與型別判準的錨：少了它，C2 的
        `AccessError` 會被讀成「租戶本來就刪不掉」而不是「守衛被關掉了」。
        """
        self._login_alice()
        result = self._call_kw(_ROUTE, 'unlink', [[self.route_c.id]])
        self._assert_error_is(
            result, 'UserError',
            '對照：primary route 的刪除必須先被 unlink() 的守衛擋下',
            contains='primary route')
        self.assertTrue(
            self.route_c.sudo().exists(),
            '對照：這條 route 不該真的被刪掉')

    def test_C2_forged_key_cannot_disarm_the_primary_guard(self):
        """🔴 #787 主格：偽造的 `sm_route_cascade_unlink` 把守衛關掉了。

        修前：守衛只判 truthy ⇒ 租戶送 `context={"sm_route_cascade_unlink":
        true}` 就讓 `unlink()` 的 primary 檢查整段跳過，流程往下走到
        `super().unlink()` 才被 ACL 的 `unlink=0` 擋住 ⇒ 例外型別從 `UserError`
        翻成 `AccessError`。

        刪除本身**不會**成功（ACL 擋死，所以這把是 defense-in-depth），但「守衛
        還在不在」才是本格量的東西：ACL 哪天鬆一格，守衛就是最後一道。
        """
        self._login_alice()
        result = self._call_kw(
            _ROUTE, 'unlink', [[self.route_c.id]],
            self._forged('sm_route_cascade_unlink'))
        self._assert_error_is(
            result, 'UserError',
            '#787：偽造的 `sm_route_cascade_unlink` 不得把 primary 守衛關掉'
            '（修前這裡會是 AccessError ＝ 守衛已被繞過、流程撞到 ACL）',
            contains='primary route')
        self.assertTrue(
            self.route_c.sudo().exists(),
            'route 不該被刪掉——但這一格零鑑別力（ACL 本來就擋死），'
            '真正的判準是上面那個例外型別')

    def test_C3_forged_key_is_fail_loud(self):
        """🔴 偽造必須留下 WARNING（理由同 A3）。"""
        self._login_alice()
        with self.assertLogs(_ROUTE_LOGGER, level='WARNING') as captured:
            self._call_kw(_ROUTE, 'unlink', [[self.route_c.id]],
                          self._forged('sm_route_cascade_unlink'))
        self.assertTrue(
            any('sm_route_cascade_unlink' in line for line in captured.output),
            '#787：偽造的開關必須點名 key 記一行 WARNING，實際收到 %r'
            % captured.output)


class TestSmForceAccessSyncForgery787(_Sh787Fixture):
    """D：`sm_force_access_sync` 不得被租戶從 RPC payload 打開。

    停權中的 SH：ingress 已被打成 503，Access app 留著無害，所以 liveness gate
    刻意排除 `suspended`。偽造這把 ⇒ 停權中的租戶仍能把自己 route 的 Access 設定
    推去 Cloudflare（並把回傳的 `app_id` 寫回 DB）。
    """

    def test_D1_control_suspended_write_without_key_does_not_sync(self):
        """對照：不帶 key 時，state 閘必須擋住推送。

        少了這一格，D2 的「推出去了」會被讀成「這條路徑本來就會推」，整組零
        鑑別力。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_d.id], {'session_duration': '8h'}]),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertFalse(
            mc.sync_access_app.called,
            '對照：停權中的 SH，它的 route 不得推 Access 設定給 operator')

    def test_D2_forged_key_cannot_bypass_the_state_gate(self):
        """🔴 #787 主格：偽造的 `sm_force_access_sync` 繞過 liveness gate。

        修前：`forced = bool(ctx.get('sm_force_access_sync'))` ⇒ 租戶一行 JSON
        就把 state 那一半關掉，停權中的 route 照樣推去 Cloudflare。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_ROUTE, 'write',
                              [[self.route_d.id], {'session_duration': '8h'}],
                              self._forged('sm_force_access_sync')),
                'smart_home_route write 不該回 JSON-RPC error')
        self.assertFalse(
            mc.sync_access_app.called,
            '#787：偽造的 `sm_force_access_sync` 不得繞過 state 閘——'
            '停權中的 SH 不該把 Access 設定推去 Cloudflare')
        self.assertFalse(
            self.route_d.sudo().access_app_id,
            '推送被擋住 ⇒ 不得有 app_id 被回寫')

    def test_D3_forged_key_is_fail_loud(self):
        """🔴 偽造必須留下 WARNING（理由同 A3）。"""
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            with self.assertLogs(_ROUTE_LOGGER, level='WARNING') as captured:
                self._assert_ok(
                    self._call_kw(
                        _ROUTE, 'write',
                        [[self.route_d.id], {'session_duration': '8h'}],
                        self._forged('sm_force_access_sync')),
                    'smart_home_route write 不該回 JSON-RPC error')
        self.assertTrue(
            any('sm_force_access_sync' in line for line in captured.output),
            '#787：偽造的開關必須點名 key 記一行 WARNING，實際收到 %r'
            % captured.output)


class TestSmSyncSentinelStillWorks787(_Sh787Fixture):
    """G：三把逃生門對**伺服器端程式碼**必須照舊開得了（修法的陰性對照）。

    少了這一組，「偽造打不開」與「這三個功能被整個刪掉了」在 A／B／C／D 每一格
    的輸出上完全一樣。

    ⚠ 本組刻意**不**走 RPC：sentinel 的全部意義就是「呼叫端構造不出來」，所以它
    只可能在 in-process 呼叫端出現。判準是 context 值的物件同一性，與 ACL／欄位
    權限無關 ⇒ 用 in-process env 不是本組的取巧，是它唯一正確的形狀。

    每一把都配一格 `=True`（in-process 也不得開閘 ⇒ 釘住「判準是值的同一性」而
    不是「呼叫端是誰」）與一格 `=False`（key 名沒改，語意不得變）。
    """

    # ---- sm_skip_access_sync ------------------------------------------ #

    def test_G1_skip_sentinel_still_suppresses_the_push(self):
        """`_sm_skip_access_sync()` 開閘 ⇒ 只搬 DB 狀態、不打 operator。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            _sm_skip_access_sync(self.route_a.sudo()).write(
                {'is_protected': True})
        self.assertFalse(
            mc.sync_access_app.called,
            '陰性對照：sentinel 路徑必須仍然抑制推送，否則修法等於把功能刪了')
        self.assertTrue(
            self.route_a.sudo().is_protected,
            '陰性對照：DB 那一半必須照樣落地（只是不打 operator）')

    def test_G2_plain_true_does_not_open_the_hatch_even_in_process(self):
        """守衛比對的是**值的同一性**，不是「呼叫端是誰」。

        這一格釘住修法的形狀：若哪天有人把判定改回 `ctx.get(key)`，A2／B2 會紅；
        但若改成「in-process 就放行」之類的替代設計，A2／B2 仍會綠，只有這一格
        會紅。它同時是 G1 那條否定性斷言的陽性對照——同一組母體、同一個 mock、
        同一支 write，只差 context 裡那個值。
        """
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.route_a.sudo().with_context(
                sm_skip_access_sync=True).write({'is_protected': True})
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：`True` 不是 sentinel——即使是 in-process 呼叫端也不得開閘')

    def test_G3_skip_explicit_false_keeps_its_meaning(self):
        """`=False` 的「不要抑制」語意原封不動（key 名沒改，改的是值）。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.route_a.sudo().with_context(
                sm_skip_access_sync=False).write({'is_protected': True})
        self.assertTrue(
            mc.sync_access_app.called,
            '`sm_skip_access_sync=False` 一直都是「照常推送」，修法不得改動它')

    def test_G4_skip_sentinel_covers_the_create_read_point(self):
        """`create()` 那個讀取點同樣走 sentinel（租戶到不了它，gate 仍要求覆蓋）。

        `ir.model.access` 對 `base.group_user` 是 `create=0` ⇒ 這個讀取點沒有 RPC
        攻擊格；但 `INTERNAL_CTX_KEY_REGISTRY` 的判準 (i) 對 enforced key 的**所有**
        生產讀取點生效，所以它的形狀一樣要被釘住。
        """
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            route = _sm_skip_access_sync(self.Route.sudo()).create({
                'smart_home_id': self.sh_a.id,
                'is_primary': False,
                'subdomain_prefix': 'g4-%s' % uuid.uuid4().hex[:6],
                'is_protected': True,
            })
        self.assertTrue(route.exists(), '前提：這一列必須真的建起來')
        self.assertFalse(
            mc.sync_access_app.called,
            '陰性對照：create 側的 sentinel 也必須仍然抑制推送')

    def test_G5_create_plain_true_does_not_open_the_hatch(self):
        """G4 的陽性對照：同一支 create，`True` 不得開閘 ⇒ 推送照跑。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.Route.sudo().with_context(sm_skip_access_sync=True).create({
                'smart_home_id': self.sh_a.id,
                'is_primary': False,
                'subdomain_prefix': 'g5-%s' % uuid.uuid4().hex[:6],
                'is_protected': True,
            })
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：create 側的 `True` 同樣不是 sentinel，不得抑制推送')

    # ---- sm_route_cascade_unlink --------------------------------------- #

    def test_G6_cascade_sentinel_still_deletes_the_primary_route(self):
        """`_sm_route_cascade_unlink()` 開閘 ⇒ primary route 刪得掉。

        這是 SH 自己 cascade 刪除時**唯一**能走的路（`smart_home.unlink()` 顯式
        呼叫它）。這一格若紅，刪 SH 會在 primary 守衛上炸掉，租戶刪不掉自己的
        Smart Home。
        """
        _sm_route_cascade_unlink(self.route_c.sudo()).unlink()
        self.assertFalse(
            self.route_c.sudo().exists(),
            '陰性對照：sentinel 路徑必須刪得掉 primary route，'
            '否則 SH 的 cascade 刪除整條會斷')

    def test_G7_cascade_plain_true_does_not_open_the_hatch(self):
        """`True` 不是 sentinel ⇒ 守衛照擋（in-process 也一樣）。"""
        with self.assertRaises(UserError):
            self.route_c.sudo().with_context(
                sm_route_cascade_unlink=True).unlink()
        self.assertTrue(
            self.route_c.sudo().exists(),
            '#787：`True` 不得把 primary 守衛關掉')

    def test_G8_cascade_explicit_false_keeps_its_meaning(self):
        with self.assertRaises(UserError):
            self.route_c.sudo().with_context(
                sm_route_cascade_unlink=False).unlink()

    # ---- sm_force_access_sync ------------------------------------------ #

    def test_G9_force_sentinel_still_bypasses_the_state_gate(self):
        """`_sm_forced_access_sync()` 開閘 ⇒ 停權中的 route 推得出去。

        這是 `_do_resume` 的收斂通道。這一格若紅，「停權期間被關掉保護的 route」
        永遠不會被推回 Cloudflare，而症狀與「保護正常運作」完全一樣。
        """
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            _sm_forced_access_sync(self.route_d.sudo())._sync_access_to_operator()
        self.assertTrue(
            mc.sync_access_app.called,
            '陰性對照：sentinel 路徑必須仍能繞過 state 閘，'
            '否則 resume 的收斂整條失效')

    def test_G10_force_plain_true_does_not_open_the_hatch(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.route_d.sudo().with_context(
                sm_force_access_sync=True)._sync_access_to_operator()
        self.assertFalse(
            mc.sync_access_app.called,
            '#787：`True` 不是 sentinel——in-process 也不得繞過 state 閘')

    def test_G11_force_explicit_false_keeps_its_meaning(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.route_d.sudo().with_context(
                sm_force_access_sync=False)._sync_access_to_operator()
        self.assertFalse(
            mc.sync_access_app.called,
            '`sm_force_access_sync=False` 一直都是「照常受 state 閘管」')
