"""#787：`cs_skip_access_sync` 可被租戶偽造（#522／#536 家族的第 6 把）。

## 缺陷

`/web/dataset/call_kw` 把**呼叫端傳來的 context 原封不動**換進 env
（`addons/web/controllers/dataset.py` → `odoo/api.py::call_kw` 的
`context = kwargs.pop('context', None) or {}; recs = recs.with_context(context)`）
⇒ **任何拿 context key 當「內部呼叫」憑證、而且只判 truthy 的守衛，都是租戶
可偽造的**。

`cloud_service.py::CloudService.write()` 用 `cs_skip_access_sync` 抑制 #745 接上
的那一段 operator Access 推送。修前的判定是裸 truthy，所以租戶送
``context={"cs_skip_access_sync": true}`` 就能讓 `is_protected` /
`session_duration` / `login_*` **落地進 Postgres 而 Cloudflare 完全不知情**：

* 方向是 **fail-open**——租戶（與平台自己、以及 `/api/...` 回傳的 payload）看到
  的是一個開著的保護開關，那個 subdomain 其實完全公開。
* 而且是**無聲**的：`write()` 正常回 `True`，端點正常回 `success`，DB 值也真的
  在。「已保護」與「以為已保護」在任何一個現有觀測點上都不可區分。

可達性是**實測**過的，不是推論（#787 的 `cs-787-reach` 探針，真 call_kw）：租戶
自建 workspace 的當下就是 owner，而 `cloud_service_rule_owner_admin_manage`
給 owner／admin 對 `cloud_service` 完整寫入 ⇒ RPC 直接打得進 `write()`。這一點
與 SH route（`create=0`）不同，**不要互相照抄結論**。

## 為什麼一定要走真 HTTP RPC

本檔的 A／B 兩組刻意是 `HttpCase` 而不是 `TransactionCase`，理由與
`test_sa_sync_ctx_forgery_536.py` 逐字相同：這條攻擊的**前提**就是「call_kw 會
把 client 的 context 換進 env」。在 `TransactionCase` 裡直接
`with_context(cs_skip_access_sync=True)` 只證明了「這個 key 有作用」，證明不了
**租戶構造得出它**；一旦哪天有人把守衛改成「看呼叫端是誰」之類的替代設計，
那種寫法會恆綠而本檔的 A2／B2 仍然會紅。

## 分層（每一格都要能分辨「守住了」與「探針壞了」）

* **A** — 未保護的服務被 RPC 打開 `is_protected`（Access app 的**建立**那一步）。
* **B** — 已保護的服務被 RPC 改 `session_duration`（重推既有 app 的狀態）。
* **G** — sentinel 對**伺服器端程式碼**必須照舊開得了（修法的陰性對照）。

A／B 各三格，缺一格就分不出「守住了」與「探針壞了」：

1. **不帶 key 的對照**——少了它，「偽造沒生效」與「這條路徑在本環境根本不會
   sync」分不出來，後者會讓本檔在守衛被拿掉之後仍然是綠的；
2. **帶偽造 key**（🔴 修前紅）——結果必須與第 1 格**完全相同**；
3. **fail-loud**（🔴 修前紅）——偽造留下 WARNING。少了它，「守住了」與「守衛
   把整個 key 當成不存在」不可區分，而後者會讓「還有 in-process 的碼照舊傳
   True」永遠沒有訊號。

母體刻意**不**使用任何抑制 key：`create()` 不經過 `write()` 的 access sync
分支，所以 `is_protected` 在 create 當下落地不會打 operator；`subdomain` 的那一
次 write 不含任何 `_CS_ACCESS_SYNC_FIELDS` 欄位，同樣不會。用了受測的 key 就會
讓 RED 相依於修法後才存在的 API（#536 已明列這個陷阱）。母體另有一道天然自
檢：測試環境沒有設定 operator URL／API key，任何非預期的推送都會以
``PaaS Operator is not configured`` 當場炸掉，而不是安靜通過。

## 界線

sentinel 的判準是 **context 值的物件同一性**，與 ACL／欄位權限無關 ⇒ G 組用
in-process（superuser）env 是正確的，而且刻意如此：sentinel 的全部意義就是
「呼叫端構造不出來」，它只可能在 in-process 呼叫端出現。反過來也成立——本檔
**沒有**用 superuser env 去驗任何跟欄位權限有關的斷言（那是 #739／#753 的假綠
形狀）；租戶身分下的欄位權限鏈由 `test_cs_access_endpoints_745.py` 的 H 系列守。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _cs_skip_access_sync, _internal_provision
from ..services.naming import make_service_subdomain

_CS = 'woow_paas_platform.cloud_service'
_CS_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_service'

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)


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
    裸 mock 會讓測試綠而 prod 炸（#745 已寫下同一條）。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': [],
    }


@tagged('post_install', '-at_install')
class _Cs787Fixture(HttpCase):
    """alice（自建 workspace ⇒ owner）＋ 兩台有 subdomain 的 running 服務。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.Service = self.env[_CS]
        self.alice, self.alice_login = _new_group_user(self.env, 'alice787')
        self.assertFalse(
            self.alice.has_group('base.group_system'),
            '前提：攻擊者是普通租戶，只能有 base.group_user')

        # 🔴 自建 workspace ＝ 自己就是 owner。這正是本缺陷的可達性來源，所以
        #    它是**前提斷言**而不是隨手的 fixture：哪天 workspace 建立不再自動
        #    給 owner，本檔 A／B 兩組的攻擊路徑就整組失效（而輸出仍會是綠的）。
        self.workspace = self.env[
            'woow_paas_platform.workspace'].with_user(self.alice).create(
                {'name': 'WS 787'})
        grant = self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', self.workspace.id),
            ('user_id', '=', self.alice.id)])
        self.assertEqual(
            grant.role, 'owner',
            '前提：租戶自建 workspace 必須拿到 owner，否則 ir.rule 不給 write，'
            '本檔的 RPC 攻擊面根本不存在')

        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'CS 787 tmpl',
                'slug': 'cs-787-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cs787',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        # A 組打「開啟保護」那一步；B 組打「改既有 app 的狀態」那一步。
        self.svc = self._service('svc787-open', protected=False)
        self.svc_p = self._service('svc787-prot', protected=True)
        self.assertFalse(
            self.svc.is_protected, '前提：A 組的母體必須是未保護的')
        self.assertTrue(
            self.svc_p.is_protected, '前提：B 組的母體必須已經是保護中的')
        self.env.flush_all()

    def _service(self, name, protected):
        """建母體。`is_protected` 走 **create**（不經過 write 的 sync 分支）。

        `subdomain` 用 production 公式算（`env=''`，見 `controllers/paas.py`），
        而且是分開的一次 write——它不在 `_CS_ACCESS_SYNC_FIELDS` 裡，所以同樣
        不會觸發推送。
        """
        svc = _internal_provision(self.Service.sudo()).create({
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'is_protected': protected,
        })
        svc.subdomain = make_service_subdomain(
            self.workspace.ws_hash, svc.reference_id, name)
        return svc

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _mock(self, result=None):
        client = MagicMock()
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

    def _login_alice(self):
        self.authenticate(self.alice_login, self.alice_login)

    def _forged(self):
        """租戶在 JSON payload 裡表達得出來的那個值。"""
        return {'context': {'cs_skip_access_sync': True}}


class TestCsSkipAccessSyncForgery787(_Cs787Fixture):
    """A／B：`cs_skip_access_sync` 不得被租戶從 RPC payload 打開。"""

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
                self._call_kw(_CS, 'write',
                              [[self.svc.id], {'is_protected': True}]),
                'cloud_service write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：租戶打開 is_protected 必須建立它的 CF Access app')

    def test_A2_forged_key_cannot_suppress_the_access_push(self):
        """🔴 #787 主格：租戶在 call_kw 的 context 塞 `cs_skip_access_sync`。

        修前：守衛只判 truthy ⇒ 推送整段被抑制，Odoo 記著「已保護」而
        Cloudflare 上沒有任何 Access app ⇒ 那個 subdomain 完全公開，而所有觀測
        點（write 的回傳、端點的 success、DB 的值、UI 上的開關）都說它被保護了。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_CS, 'write',
                              [[self.svc.id], {'is_protected': True}],
                              self._forged()),
                'cloud_service write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：偽造的 `cs_skip_access_sync` 不得抑制 Access 推送——這正是'
            '「DB 說已保護、CF 上什麼都沒有」的來源')
        self.assertTrue(
            self.svc.is_protected,
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
            with self.assertLogs(_CS_LOGGER, level='WARNING') as captured:
                self._assert_ok(
                    self._call_kw(_CS, 'write',
                                  [[self.svc.id], {'is_protected': True}],
                                  self._forged()),
                    'cloud_service write 不該回 JSON-RPC error')
        self.assertTrue(
            any('cs_skip_access_sync' in line for line in captured.output),
            '#787：偽造的開關必須點名 key 記一行 WARNING，實際收到 %r'
            % captured.output)

    # ---- B：改既有 Access app 的狀態 ---------------------------------- #

    def test_B1_control_session_duration_write_without_key_syncs(self):
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_CS, 'write',
                              [[self.svc_p.id], {'session_duration': '8h'}]),
                'cloud_service write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '對照：改 session_duration 必須重推該服務的 Access 狀態')

    def test_B2_forged_key_cannot_suppress_an_update_push(self):
        """🔴 同一支 write，加上偽造的 key 之後結果必須完全一樣。

        與 A 組是**兩條不同的可達路徑**：A 是「保護從無到有」，B 是「已保護的
        服務被改設定」。只守 A 的話，把 session 長度改成 168h 而 CF 上仍是舊值
        這種偏移不會有任何一格抓得到。
        """
        self._login_alice()
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self._assert_ok(
                self._call_kw(_CS, 'write',
                              [[self.svc_p.id], {'session_duration': '8h'}],
                              self._forged()),
                'cloud_service write 不該回 JSON-RPC error')
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：偽造的 `cs_skip_access_sync` 不得抑制既有 Access app 的'
            '狀態更新（DB 改了、CF 沒改）')
        self.assertEqual(
            self.svc_p.session_duration, '8h',
            'DB 那一半照樣落地——同 A2')


class TestCsSentinelStillWorks787(_Cs787Fixture):
    """G：這把逃生門對**伺服器端程式碼**必須照舊開得了（修法的陰性對照）。

    少了這一組，「偽造打不開」與「這個功能被整個刪掉了」在 A／B 每一格的輸出上
    完全一樣。

    ⚠ 這一組刻意**不**走 RPC：sentinel 的全部意義就是「呼叫端構造不出來」，
    所以它只可能在 in-process 呼叫端出現。判準是 context 值的物件同一性，與
    ACL／欄位權限無關 ⇒ 用 in-process env 不是本組的取巧，是它唯一正確的形狀。
    """

    def test_G1_sentinel_still_suppresses_the_push(self):
        """`_cs_skip_access_sync()` 開閘 ⇒ 只搬 DB 狀態、不打 operator。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            _cs_skip_access_sync(self.svc).write({'is_protected': True})
        self.assertFalse(
            mc.sync_access_app.called,
            '陰性對照：sentinel 路徑必須仍然抑制推送，否則修法等於把功能刪了')
        self.assertTrue(
            self.svc.is_protected,
            '陰性對照：DB 那一半必須照樣落地（只是不打 operator）')

    def test_G2_plain_true_does_not_open_the_hatch_even_in_process(self):
        """守衛比對的是**值的同一性**，不是「呼叫端是誰」。

        這一格釘住修法的形狀：若哪天有人把判定改回 `ctx.get(key)`，A2／B2 會
        紅；但若改成「in-process 就放行」之類的替代設計，A2／B2 仍會綠，只有
        這一格會紅。它同時是 G1 那條否定性斷言的陽性對照——同一組母體、同一個
        mock、同一支 write，只差 context 裡那個值。
        """
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.svc.with_context(cs_skip_access_sync=True).write(
                {'is_protected': True})
        self.assertTrue(
            mc.sync_access_app.called,
            '#787：`True` 不是 sentinel——即使是 in-process 呼叫端也不得開閘')

    def test_G3_explicit_false_keeps_its_meaning(self):
        """`=False` 的「不要抑制」語意原封不動（key 名沒改，改的是值）。"""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            self.svc.with_context(cs_skip_access_sync=False).write(
                {'is_protected': True})
        self.assertTrue(
            mc.sync_access_app.called,
            '`cs_skip_access_sync=False` 一直都是「照常推送」，修法不得改動它')
