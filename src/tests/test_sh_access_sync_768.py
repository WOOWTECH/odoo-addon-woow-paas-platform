"""#768（SH AC）：SH route 的 operator sync／liveness gate／teardown／左集。

## 守什麼

model 層（`test_sh_access_model_768.py`）證明了欄位、arc、配額池都對，但那些測試
**沒有任何一條會打到 operator**。本檔守的是接上去之後的五件事，每一件漏掉的症狀
都與「正常運作」在輸出上不可區分：

1. **payload 形狀**與 SA route / CS 逐欄位對齊。operator 只有一個
   `AccessSyncRequest` schema，三份 payload 形狀分家只會在 422 才發現，而 422 的
   訊息說不出是哪一邊寫錯。`login_idp_types` 的 `email_otp → 'onetimepin'` 是這
   組欄位裡唯一「欄位名 ≠ 送出去的值」的一格，也是最容易被順手寫成欄位名的一格。
2. **CF 命名根帶 env token**。SH 的 `subdomain` 是 provision 當下用 `env=''` 算
   的（對外 URL 刻意 env-clean），所以「拿 stored subdomain 當前綴」在 prod 與
   正確實作**逐位元組相同**、到 stg 才變成一個無法自證環境歸屬的 CF 物件。判別
   式因此必須落在 stg（A4），只在 prod 斷言的測試對這個錯誤天生盲目（A5 把這件
   事本身釘住）。
3. **liveness gate**。SH 是 dedicated tunnel，判準取 `tunnel_id` 那條而不是 CS
   的 `subdomain` 那條；`error` 必須**包含在內**（`dns_failed` 分支留在 error 而
   tunnel 活著），`suspended` 必須**排除**且由 resume 的 forced sync 補收斂。
4. **teardown 的順序與捕獲時機**。id 一旦隨著列消失就沒有任何重試錨點，CF 上的
   app／reusable policy 變成永久孤兒。
5. **reconciler 左集**。SH 的 Access 物件走的是 policy 錨點 arc 的第三條腿，
   `tunnel_id` / `subdomain` 那兩行 walk 在結構上到不了它們。漏掉不是少算——右集
   是 CF 帳號級 list-all，看得到這些活著的物件，不在左集就是孤兒候選 ⇒ **每一個
   正常運作的 SH Access app 都會上孤兒報表**。

## ⚠ patch 點：`models.smart_home`，不是 `services.paas_operator`

`models/smart_home.py` 是 `from ..services.paas_operator import
get_paas_operator_client` —— 名字在 import 當下就綁進 `models.smart_home` 的
namespace 了，patch 來源模組不會動到那個已綁定的參照。patch 錯地方時，「操作
應該打 operator」那些測試會炸在 `PaaS Operator is not configured`（吵，看得
見），但「操作**不該**打 operator」那些會**無聲通過**——因為根本沒有東西被呼叫。
所以本檔每一組否定性斷言都與它的陽性對照放在**同一個測試方法、同一個 mock**
上：陽性那半沒紅，才證明這支 mock 真的接在受測路徑上。

## TDD 紀律

* mock 的回傳一律是**真的 response 形狀**（含 `orphaned_policy_ids`）。裸
  MagicMock 會讓回存那段拿到 MagicMock 而不是字串，測試綠但 prod 炸。
* fixture 的 `subdomain` 用 **production 公式現算**，不是隨手塞字串——A4／A5 的
  判別式是「canonical 與 stored subdomain 的關係」，subdomain 不是真實形狀時那
  兩條會為了無關的理由變綠。
"""
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_forced_access_sync,
    _sm_skip_access_sync,
)
# #536：`sa_skip_route_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.security_access import _skip_route_sync
from ..services.naming import make_sm_access_canonical_name
from ..services.reconciler import collect_left_set
from .smart_home_fixture import sh_model

#: 🔴 見檔頭「patch 點」。這裡寫錯不會讓否定性斷言變紅。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)

_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'

#: `_build_access_payload()` 的完整 key 集合＝operator `AccessSyncRequest` 的欄位。
_PAYLOAD_KEYS = [
    'app_name', 'canonical_name', 'enabled', 'existing_app_id',
    'existing_policy_ids', 'login_idp_types', 'policies', 'primary_hostname',
    'protected_hostnames', 'session_duration',
]


def _sync_result(app_id='app-768', policy_ids=None, unresolved=None,
                 orphaned=None):
    """operator `PUT /api/access/sync` 的真實回傳形狀（五個 key 一個都不少）。"""
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': list(unresolved or []),
        'orphaned_policy_ids': list(orphaned or []),
    }


@tagged('post_install', '-at_install')
class _ShAccessSyncFixture(TransactionCase):
    """一台 active、有 tunnel 的 SH ＋ 它的 primary route。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.SmartHome = sh_model(self.env)
        self.ShRoute = self.env[_ROUTE_MODEL]
        self.Policy = self.env[_POLICY]
        self.workspace = self.env[
            'woow_paas_platform.workspace'].sudo().create(
                {'name': 'WS 768 %s' % uuid.uuid4().hex[:6]})
        self.sh = self._smart_home()
        self.route = self._route(self.sh)

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #

    def _smart_home(self, name=None, state='active', tunnel=True,
                    subdomain=True):
        """建一台 SH。``subdomain`` 走 **production 公式**現算。

        隨手塞一個字串也能讓大部分斷言通過，但 A4／A5 的判別式正是「canonical
        與 stored subdomain 的關係」——subdomain 不是真實形狀時，那兩條會因為
        無關的理由變綠（CS #745 A2 的同一條教訓）。

        `tunnel_id` 是身分錨點，只能經 `sh_model()` 開的閘在 create 當下落地；
        建完立刻 `.with_env(self.env)` 把閘洗掉，免得 fixture 把受測守衛一起
        關掉（`with_context` 是 merge，`.sudo()` / `.browse()` 都會保留它）。
        """
        name = name or 'sh-768-%s' % uuid.uuid4().hex[:6]
        vals = {'name': name, 'workspace_id': self.workspace.id,
                'state': state}
        if tunnel:
            vals['tunnel_id'] = 'tun-%s' % uuid.uuid4().hex[:8]
        sh = self.SmartHome.create(vals)
        if subdomain:
            sh.write({'subdomain': sh._generate_subdomain()})
        return sh.with_env(self.env)

    def _route(self, sh=None, **extra):
        """建一條 route（預設 primary）。

        🔴 `create()` 回的 recordset **帶著呼叫時的 context** —— 直接回它的話，
        之後對這條 route 的每一次 write 都還套著 `sm_skip_access_sync=True`，
        於是「sync 沒接好」與「fixture 把 sync 關掉了」在測試輸出上長得一模一樣
        （CS #745 的 `_policy()` 實際踩過）。所以回傳前顯式關掉那把開關。
        """
        sh = self.sh if sh is None else sh
        vals = {'smart_home_id': sh.id, 'is_primary': True}
        vals.update(extra)
        route = _sm_skip_access_sync(self.ShRoute).create(vals)
        return route.with_context(sm_skip_access_sync=False)

    def _configure(self, route=None, **vals):
        """改 route 的 Access 設定但**不**觸發 sync（擺前提用，不是受測動作）。"""
        route = self.route if route is None else route
        _sm_skip_access_sync(route).write(vals)
        return route

    def _app_id(self, route, app_id):
        """替 route 擺上「CF 上已經有一個 app」這個前提（身分錨點，走 sentinel）。"""
        _internal_sm_route_identity(route).access_app_id = app_id
        return route

    def _policy(self, route=None, name=None, sequence=None, cf_policy_id=None):
        route = self.route if route is None else route
        vals = {
            'smart_home_route_id': route.id,
            'name': name or 'pol-768-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        if sequence is not None:
            vals['sequence'] = sequence
        pol = _skip_route_sync(self.Policy.sudo()).create(vals)
        if cf_policy_id:
            pol.cf_policy_id = cf_policy_id
        # 同 `_route()`：回傳前把 fixture 用的 skip 開關關掉。
        return pol.with_context(sa_skip_route_sync=False)

    def _mock(self, result=None):
        client = MagicMock()
        client.sync_access_app.return_value = (
            _sync_result() if result is None else result)
        return client

    def _route_row(self, route_id):
        """直接問 DB：這一列現在還在不在交易裡（順便回傳它的 app id）。

        ⚠ 刻意不用 `browse().exists()`：`unlink()` 之後 recordset 的 `exists()`
        會受 ORM cache 與 savepoint `cr.clear()` 影響，而 D6 測的正是「已經
        `super().unlink()` 掉、又被 savepoint 救回來」——那恰好是兩者最容易分歧
        的一格（`test_poll_teardown_rollback_525.py` 記過同一條）。raw SQL 問的
        是「這一列現在在不在交易裡」，就是「殘留／救回」的定義本身。
        """
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id FROM woow_paas_platform_smart_home_route '
            'WHERE id = %s', (route_id,))
        return self.env.cr.fetchone()


class TestShAccessPayload(_ShAccessSyncFixture):
    """A 系列：payload 形狀與 CF 命名根。"""

    def test_A1_payload_mirrors_the_shared_operator_schema(self):
        self._configure(is_protected=True, login_google=True,
                        login_email_otp=True, session_duration='8h')
        self._policy(name='p1', sequence=10)
        payload = self.route._build_access_payload()

        self.assertEqual(
            sorted(payload), _PAYLOAD_KEYS,
            'payload 的 key 集合必須與 operator 的 AccessSyncRequest 一致；'
            '多一個／少一個都只會在 422 才發現，而 422 說不出是哪一邊錯')
        host = '%s.woowtech.io' % self.sh.subdomain
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['canonical_name'],
                         self.sh._access_canonical_name(),
                         'canonical 必須來自 SH 的單一裁決處，不可就地再算一次')
        self.assertEqual(
            payload['app_name'], self.sh.subdomain,
            'primary route 的 app 顯示名＝SH 的 subdomain')
        self.assertEqual(payload['primary_hostname'], host)
        self.assertEqual(payload['protected_hostnames'], [host])
        self.assertEqual(payload['session_duration'], '8h')
        self.assertEqual(sorted(payload['login_idp_types']),
                         ['google', 'onetimepin'])
        self.assertEqual(len(payload['policies']), 1)
        self.assertEqual(payload['policies'][0]['name'], 'p1')
        self.assertEqual(payload['policies'][0]['decision'], 'allow')
        self.assertEqual(payload['policies'][0]['precedence'], 10)
        self.assertEqual(
            payload['policies'][0]['include'],
            [{'rule_type': 'email_list', 'rule_value': 'a@example.com'}])
        self.assertEqual(payload['policies'][0]['require'], [])
        self.assertEqual(payload['policies'][0]['exclude'], [])
        self.assertIsNone(payload['existing_app_id'])
        self.assertEqual(payload['existing_policy_ids'], [])

    def test_A2_unprotected_payload_is_a_teardown_declaration(self):
        """`is_protected=False` ⇒ enabled False ＋ **空** destinations。

        operator 的 teardown 分支判的是 `not enabled or not
        protected_hostnames`——兩者都要對。只有 enabled 對而 hostname 還留在
        `protected_hostnames` 裡的 payload，在別的分支下會變成「重建一個沒人保護
        得到的 app」。`primary_hostname` 則**必須留著**（它是 app 的錨點，不是
        保護清單）。
        """
        self._app_id(self.route, 'app-old')
        payload = self.route._build_access_payload()
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['protected_hostnames'], [])
        self.assertEqual(payload['primary_hostname'],
                         '%s.woowtech.io' % self.sh.subdomain)
        self.assertEqual(payload['existing_app_id'], 'app-old')

    def test_A3_login_idp_types_use_the_cloudflare_vocabulary(self):
        """`email_otp` 在 Cloudflare 叫 `onetimepin`。

        這是整組欄位裡唯一「Odoo 欄位名 ≠ 送出去的值」的一格，也是最容易被順手
        寫成 `'email_otp'` 的一格。送錯字串時 CF 解析不到那個 IdP ⇒ 解析後集合
        變空 ⇒ Access app **fallback 到所有可用 IdP**：登入限制被靜默放寬，而端
        點照樣回報成功。
        """
        cases = (
            ({'login_google': True, 'login_email_otp': False}, ['google']),
            ({'login_google': False, 'login_email_otp': True}, ['onetimepin']),
            ({'login_google': True, 'login_email_otp': True},
             ['google', 'onetimepin']),
            ({'login_google': False, 'login_email_otp': False}, []),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                self._configure(is_protected=True, **flags)
                types = self.route._build_access_payload()['login_idp_types']
                self.assertEqual(sorted(types), expected)
                self.assertNotIn(
                    'email_otp', types,
                    'Odoo 欄位名不得原樣送給 Cloudflare（正確值是 onetimepin）')

    def test_A4_canonical_name_carries_env_token_while_subdomain_stays_clean(
            self):
        """stg：canonical 帶 token、對外 subdomain **不帶**（對稱測法）。

        兩半缺一不可：
        * 只斷言 canonical 有 token ⇒ 把實作改成 `self.subdomain` 再手動接一個
          token 也會綠；
        * 只斷言 subdomain 沒 token ⇒ 那是 `test_smart_home.py` 已經守著的
          DP2，與本單無關。

        真正的判別式是**兩者不相等**：SH 的 subdomain 是 provision 當下用
        `env=''` 算的，任何「拿 stored subdomain 當 Access 命名前綴」的實作在
        prod 逐位元組正確、只有 stg 這一格會紅（見 A5）。
        """
        self.ICP.set_param('woow_paas_platform.deploy_env', 'stg')
        canonical = self.sh._access_canonical_name()

        self.assertTrue(
            canonical.startswith('paas-stg-sm-'),
            'stg 的 CF Access 命名根必須帶 env token，得到 %r' % canonical)
        self.assertNotIn(
            'stg', self.sh.subdomain,
            '對外 subdomain 刻意 env-clean（DP2）；它帶了 token 代表前提壞了，'
            '下面那條「兩者不相等」會為了錯的理由變綠')
        self.assertNotEqual(
            canonical, self.sh.subdomain,
            'Access 命名根不可反推自 stored subdomain —— 那在 prod 看不出差別，'
            '在 stg 會產生一個無法自證環境歸屬的 CF 物件')
        self.assertEqual(
            canonical,
            make_sm_access_canonical_name(
                self.workspace.ws_hash, self.sh.reference_id, self.sh.name,
                env='stg'),
            'env token 要走 naming.py 的單一根，不要在別處自己拼')
        self.assertEqual(
            canonical.split('sm-', 1)[1], self.sh.subdomain.split('sm-', 1)[1],
            '同一資源、只差 env token：hash 尾段必須相同（否則兩個名字指的不是'
            '同一台 SH）')

    def test_A5_prod_makes_the_wrong_implementation_indistinguishable(self):
        """prod／空值＝不加 token，且 canonical **恰好等於** subdomain。

        這條不是在守一個行為，是在把「為什麼 A4 必須跑在 stg」釘成可執行的事實：
        prod 下兩個字串相同 ⇒ 拿 subdomain 當前綴的錯誤實作在這裡完全看不出來。
        誰想把 A4 搬回 prod「比較簡單」時，這條會提醒他那等於刪掉整個判別式。
        """
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        canonical = self.sh._access_canonical_name()
        self.assertTrue(canonical.startswith('paas-sm-'), canonical)
        self.assertNotIn('paas-prod-', canonical)
        self.assertEqual(canonical, self.sh.subdomain)

    def test_A6_existing_policy_ids_include_the_doomed_ones(self):
        """被 unlink 掉那一列的 cf id 必須經 context 一起送去刪。

        operator 唯一的刪除通道就是這份清單（delete-old-then-create）。不送＝
        每刪一條 policy 就漏一顆永久 CF 孤兒。
        """
        self._policy(cf_policy_id='cf-live')
        payload = self.route.with_context(
            sa_unlinked_cf_policy_ids=['cf-doomed'])._build_access_payload()
        self.assertEqual(sorted(payload['existing_policy_ids']),
                         ['cf-doomed', 'cf-live'])


class TestShAccessLivenessGate(_ShAccessSyncFixture):
    """B 系列：什麼時候該打 operator、什麼時候一次都不能打。

    ⚠ 本系列一律斷言 `client.sync_access_app.called` **本身**，不是「沒有例外」：
    「完全沒打 operator」與「打了而且成功」在例外層面上不可區分。
    """

    def test_B1_enabling_protection_pushes_and_stores_the_app_id(self):
        client = self._mock(_sync_result(app_id='app-new'))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertTrue(client.sync_access_app.called,
                        '開啟保護必須打 operator')
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(self.route.access_app_id, 'app-new',
                         'operator 回的 app_id 必須回存（sentinel 路徑）')

    def test_B2_no_tunnel_never_calls_the_operator(self):
        """沒有 tunnel ⇒ hostname 不會解析，沒有可掛 Access 的錨點。

        判準取 `tunnel_id`（SA 那條）而不是 `subdomain`（CS 那條）：SH 是
        dedicated tunnel，subdomain 落地不代表流量到得了。陽性對照就在本方法
        末尾——同一支 mock、同一組 builder 產的另一條 route，兩者只差 tunnel。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh)
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            route.write({'is_protected': True})
        self.assertFalse(
            client.sync_access_app.called,
            '沒有 tunnel 的 SH 不該建 Access app（DB 有、CF 沒有的幽靈保護）')

        # 陽性對照：唯一的差異是 tunnel_id ⇒ 上面的 False 不是 mock 沒接上。
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertTrue(
            client.sync_access_app.called,
            '同一支 mock 在有 tunnel 的 route 上必須被呼叫；沒有的話上面那條'
            '否定性斷言什麼都沒證明（patch 點錯了也長這樣）')

    def test_B3_no_hostname_never_calls_the_operator(self):
        """未 provision（無 subdomain ⇒ 無 hostname）的 route 沒有錨點可掛。"""
        sh = self._smart_home(subdomain=False)
        route = self._route(sh)
        self.assertFalse(route.hostname, '前提：這條 route 沒有 hostname')
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            route.write({'is_protected': True})
        self.assertFalse(client.sync_access_app.called)

        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertTrue(client.sync_access_app.called,
                        '陽性對照：有 hostname 的 route 必須打得出去')

    def test_B4_only_active_and_error_states_push(self):
        """state 閘：`active` / `error` 推，其餘一律不推。

        `error` 在推的那一側是**刻意**的（見 B6）；其餘四個狀態在不推的那一側。
        本方法把兩側放在同一支 mock 上跑完，否定性那半才有意義。
        """
        client = self._mock()
        self._app_id(self.route, 'app-live')
        for state in ('pending', 'provisioning', 'suspended', 'deleting'):
            with self.subTest(state=state):
                self.sh.write({'state': state})
                client.sync_access_app.reset_mock()
                with patch(MOCK_GET_CLIENT, return_value=client):
                    self.route.write({'is_protected': True})
                self.assertFalse(
                    client.sync_access_app.called,
                    'state=%s 的 SH 不該推 Access（tunnel 尚未／已不再承載'
                    '流量）' % state)

        self.sh.write({'state': 'active'})
        client.sync_access_app.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertTrue(
            client.sync_access_app.called,
            '陽性對照：同一支 mock、同一條 route，state=active 必須推得出去')

    def test_B5_forced_context_bypasses_the_state_gate(self):
        """`sm_force_access_sync` 是 resume 的收斂通道，必須略過 state 閘。"""
        self.sh.write({'state': 'suspended'})
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertFalse(client.sync_access_app.called,
                         '前提：停權中的一般寫入不推（state 閘）')

        with patch(MOCK_GET_CLIENT, return_value=client):
            _sm_forced_access_sync(self.route)._sync_access_to_operator()
        self.assertTrue(
            client.sync_access_app.called,
            'forced 沒生效 ⇒ 停權期間被改過的 Access 設定永遠不會收斂，而症狀'
            '與「保護正常運作」完全相同')

    def test_B6_error_state_still_pushes(self):
        """`error` 必須推 —— 這一格排除掉會讓 DB 與 CF 靜默分歧。

        `action_provision` 的 `dns_failed` 分支把記錄留在 `error`，而 tunnel
        **活著**、訂閱有效、租戶照樣能編輯 Access 設定（那條路徑也會建 primary
        route）。把 error 排除的話，租戶在 UI 上改的每一項都只寫進 DB，端點回報
        成功，Cloudflare 那邊什麼都沒發生。
        """
        self.sh.write({'state': 'error'})
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertTrue(
            client.sync_access_app.called,
            'state=error（dns_failed）的 SH tunnel 仍活著，Access 設定必須推')

    def test_B7_skip_context_suppresses_the_push_but_not_the_db_write(self):
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            _sm_skip_access_sync(self.route).write(
                {'is_protected': True})
        self.assertFalse(client.sync_access_app.called)
        self.assertTrue(self.route.is_protected, 'DB 那一側仍然要寫進去')


class TestShAccessResultPersistence(_ShAccessSyncFixture):
    """C 系列：operator 回傳值的回存。"""

    def test_C1_app_id_is_persisted_through_the_identity_sentinel(self):
        client = self._mock(_sync_result(app_id='app-c1'))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})
        self.assertEqual(
            self.route.access_app_id, 'app-c1',
            'app_id 沒回存 ⇒ 下一次 sync 的 existing_app_id 是 None ⇒ operator '
            '會再建一個 app，舊的變成永久孤兒')

    def test_C2_policy_ids_map_back_in_precedence_order_not_creation_order(
            self):
        """回傳的 cf id 依 **(sequence, id)** 對應，不是依建立順序。

        故意讓兩者不一致（先建 sequence 20 的那條）：照 id 排的實作在這裡會把
        cf-1 掛到錯的 policy 上，而兩種排法在「每條 policy 都拿到一個 cf id」
        這個層級的斷言下輸出相同。錯掛的後果是之後的 delete-then-recreate 刪錯
        CF 物件。
        """
        later = self._policy(name='p-late', sequence=20)
        earlier = self._policy(name='p-early', sequence=10)
        client = self._mock(_sync_result(policy_ids=['cf-1', 'cf-2']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True})

        payload = client.sync_access_app.call_args.args[0]
        self.assertEqual(
            [p['name'] for p in payload['policies']], ['p-early', 'p-late'],
            'payload 裡的 policy 順序就是 operator 回傳 cf id 的順序，'
            '必須照 precedence 排')
        self.assertEqual(earlier.cf_policy_id, 'cf-1')
        self.assertEqual(later.cf_policy_id, 'cf-2')

    def test_C3_unresolved_idp_types_are_persisted(self):
        client = self._mock(_sync_result(unresolved=['google']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': True, 'login_google': True})
        self.assertEqual(
            self.route.access_unresolved_idp_types, 'google',
            '登入限制被靜默放寬的訊號必須落地——它是租戶唯一看得到這件事的地方')

    def test_C4_a_none_app_id_clears_the_stored_ids(self):
        """teardown（回傳 app_id=None）之後，DB 這側的 id 全部要清乾淨。

        留著已被刪除的 app id ⇒ 下一次 sync 拿一個 CF 上不存在的 id 當
        `existing_app_id`，operator 的 delete-then-recreate 會對著空氣操作。
        """
        pol = self._policy(cf_policy_id='cf-old')
        self._app_id(self.route, 'app-live')
        client = self._mock(_sync_result(app_id=None))
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': False})
        self.assertTrue(client.sync_access_app.called)
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-live',
                         '拆除請求必須帶著要拆的那個 id')
        self.assertFalse(self.route.access_app_id, '拆完才可以清掉 app id')
        self.assertFalse(pol.cf_policy_id, '拆完 policy 的 cf id 也要清掉')


class TestShAccessTeardown(_ShAccessSyncFixture):
    """D 系列：teardown 的捕獲時機、順序與失敗處置。"""

    def test_D1_nothing_to_tear_down_captures_nothing(self):
        """沒有 app 也沒有 cf policy ⇒ 回 None，operator 一次都不打。

        陽性對照在同一個方法裡：擺上一個 app id 之後必須捕獲得到，否則「回
        None」可能只是因為 `_capture_access_teardown` 對所有輸入都回 None。
        """
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.assertIsNone(self.route._capture_access_teardown())
        self.assertFalse(
            client.sync_access_app.called,
            '從來沒有 Access app 的 route 不必打 operator')

        self._app_id(self.route, 'app-live')
        info = self.route._capture_access_teardown()
        self.assertTrue(info, '陽性對照：有 app id 時必須捕獲得到拆除資訊')
        self.assertEqual(info['app_id'], 'app-live')

    def test_D2_smart_home_unlink_tears_access_down_before_the_tunnel(self):
        """順序：先拆 Access、後拆 tunnel；且 id 在列消失**之前**捕獲。

        兩件事各自的失敗形狀：
        * 順序反了 ⇒ tunnel 先沒，`_capture_access_teardown` 的 liveness gate
          （讀 `sh.tunnel_id`）從此回 False，Access app 一輩子拆不掉。
        * 捕獲晚了 ⇒ `super().unlink()` 已經把 `access_app_id` 與每條 policy 的
          `cf_policy_id` 帶走，operator 沒有 id 可刪，而且**連一列可以驅動重試
          的 DB 記錄都不剩**。
        """
        self._configure(is_protected=True)
        self._policy(cf_policy_id='cf-1')
        self._app_id(self.route, 'app-live')
        route_id = self.route.id
        client = self._mock(_sync_result(app_id=None))

        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sh.unlink()

        names = [c[0] for c in client.mock_calls]
        self.assertIn('sync_access_app', names, 'Access teardown 必須被推出去')
        self.assertIn('delete_tunnel', names, 'tunnel 仍然要被拆')
        self.assertLess(
            names.index('sync_access_app'), names.index('delete_tunnel'),
            'Access 必須在 tunnel 之前拆：tunnel 一沒，route 的 liveness gate '
            '就回 False，CF 上的 Access app 再也沒有路徑拆得掉')

        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(
            payload['existing_app_id'], 'app-live',
            'app id 必須在列消失之前捕獲（拿到 None＝永久孤兒 + 無重試錨點）')
        self.assertEqual(
            payload['existing_policy_ids'], ['cf-1'],
            'policy 的 cf id 同上——cascade 刪除不會呼叫子模型的 Python unlink')
        self.assertTrue(payload['canonical_name'].startswith('paas-'),
                        'teardown 也要帶命名根（reusable policy 前綴同源）')
        self.assertIsNone(self._route_row(route_id), 'route 列必須跟著消失')

    def test_D3_unlink_fails_loud_and_keeps_the_retry_anchor(self):
        """Access 拆不掉 ⇒ raise、記錄留著、**tunnel 不動**。

        吞掉這個失敗會留下一組活著的 CF Access app ＋ reusable policy，而且
        DB 那一列已經沒了 ⇒ 沒有任何東西能驅動重試，也沒有錯誤訊號。
        """
        self._configure(is_protected=True)
        self._app_id(self.route, 'app-live')
        client = MagicMock()
        client.sync_access_app.side_effect = RuntimeError('cf down')

        with patch(MOCK_GET_CLIENT, return_value=client):
            with self.assertRaises(UserError):
                self.sh.unlink()

        self.assertTrue(self.sh.exists(), 'SH 必須留下來當重試錨點')
        self.assertTrue(self.route.exists(), 'route 同上')
        self.assertEqual(
            self.route.access_app_id, 'app-live',
            '失敗時 app id 是唯一找得回那個 CF 物件的線索，不可清除')
        self.assertFalse(
            client.delete_tunnel.called,
            'Access 還活著就先拆 tunnel ⇒ 之後的重試連 liveness gate 都過不了')

    def test_D4_cron_cleanup_lands_in_error_without_touching_the_tunnel(self):
        """`_do_cleanup()`（cron 路徑）不走 `unlink()`，失敗處置也不同。

        這條路徑的 fail 不能 raise（cron 會整批中斷），要落 `state='error'` 等
        下一輪；同樣不得先拆 tunnel。陽性對照在方法末尾：teardown 成功時必須
        真的走到 `delete_tunnel` 並落 `deleting`，否則「落 error」可能只是因為
        `_do_cleanup` 對所有輸入都落 error。
        """
        self._configure(is_protected=True)
        self._app_id(self.route, 'app-live')
        tunnel_id = self.sh.tunnel_id
        client = MagicMock()
        client.sync_access_app.side_effect = RuntimeError('cf down')

        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sh._do_cleanup()

        self.assertEqual(self.sh.state, 'error',
                         'Access 拆不掉時必須落 error 等下一輪 cron')
        self.assertIn(
            'Access teardown failed', self.sh.error_message or '',
            '訊息要指出是 Access 那一段失敗——與 tunnel 失敗落同一個 state，'
            '訊息是唯一分得出「這輪卡在哪」的東西')
        self.assertFalse(client.delete_tunnel.called,
                         'Access 還活著就不該拆 tunnel')
        self.assertEqual(self.sh.tunnel_id, tunnel_id, 'tunnel 錨點必須留著')
        self.assertEqual(self.route.access_app_id, 'app-live')

        # 陽性對照：同一台 SH，teardown 成功之後必須走完 tunnel 那一段。
        client.sync_access_app.side_effect = None
        client.sync_access_app.return_value = _sync_result(app_id=None)
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sh._do_cleanup()
        self.assertTrue(client.delete_tunnel.called,
                        'Access 拆乾淨之後必須繼續拆 tunnel')
        self.assertEqual(self.sh.state, 'deleting')

    def test_D5_secondary_route_unlink_pushes_the_ids_it_captured(self):
        """非 primary route 自己被刪時，同樣要在列消失前捕獲並推 teardown。

        `app_name` 一併驗：它讀的是 `subdomain_prefix`，列沒了就取不到——payload
        裡是正確的 prefix，本身就證明捕獲發生在刪除之前。
        """
        route = self._route(is_primary=False, subdomain_prefix='r768x')
        self._configure(route, is_protected=True)
        self._app_id(route, 'app-secondary')
        route_id = route.id
        client = self._mock(_sync_result(app_id=None))

        with patch(MOCK_GET_CLIENT, return_value=client):
            route.unlink()

        self.assertTrue(client.sync_access_app.called)
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-secondary')
        self.assertEqual(
            payload['app_name'], 'r768x',
            '非 primary route 的 app 名取自 subdomain_prefix；一台 SH 底下多條'
            'route 不得撞名')
        self.assertIsNone(self._route_row(route_id), 'route 列必須真的消失')

    def test_D6_secondary_route_unlink_rolls_back_when_teardown_fails(self):
        """route 的 teardown 失敗 ⇒ savepoint 把已刪掉的列**救回來**。

        這裡的順序是 `super().unlink()` 先跑、teardown 後推，所以「列還在」不是
        自然發生的——只有 raise 觸發 savepoint 回滾才會發生。少了它，CF 上留著
        一個 Access app 而 DB 那一列已經沒了。
        """
        route = self._route(is_primary=False, subdomain_prefix='r768y')
        self._configure(route, is_protected=True)
        self._app_id(route, 'app-secondary')
        route_id = route.id
        client = MagicMock()
        client.sync_access_app.side_effect = RuntimeError('cf down')

        with patch(MOCK_GET_CLIENT, return_value=client):
            with self.assertRaises(UserError):
                route.unlink()

        row = self._route_row(route_id)
        self.assertIsNotNone(
            row, 'teardown 失敗時整個 unlink 必須回滾；列沒了就沒有重試錨點')
        self.assertEqual(
            row[0], 'app-secondary',
            '救回來的列必須連 app id 一起救回來——沒有它，那個列救回來也指不回'
            'CF 上那個還活著的 app')


class TestShPolicyUnlinkReapsTheOrphan(_ShAccessSyncFixture):
    """E 系列：SH 錨定 policy 被刪時，它的 CF reusable policy 要一起送去刪。"""

    def test_E1_unlinking_a_policy_injects_its_doomed_cf_id(self):
        """`sa_unlinked_cf_policy_ids` 這條通道對第三個錨點也要接上。

        `_build_existing_policy_ids` 只看得到**存活**的列，被刪那一列的 cf id
        永遠進不了清單；而那是 operator 唯一的刪除通道 ⇒ 每刪一條 policy 就漏
        一顆永久 CF 孤兒（成本 + 一個沒人知道還在生效的規則）。
        """
        self._configure(is_protected=True)
        self._app_id(self.route, 'app-live')
        self._policy(name='keep', cf_policy_id='cf-live')
        doomed = self._policy(name='doomed', cf_policy_id='cf-doomed')
        client = self._mock(_sync_result(app_id='app-live',
                                         policy_ids=['cf-live']))

        with patch(MOCK_GET_CLIENT, return_value=client):
            doomed.unlink()

        self.assertTrue(
            client.sync_access_app.called,
            'policy unlink 必須讓錨點重推一次完整狀態（錨點分派沒接上 SH 那條'
            '腿時這裡是空集，什麼都不會發生）')
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn(
            'cf-doomed', payload['existing_policy_ids'],
            '被刪那一列的 cf id 必須一起送去刪，否則是永久 CF 孤兒')
        self.assertIn(
            'cf-live', payload['existing_policy_ids'],
            '存活列的 cf id 也要照送（delete-then-recreate 的前半段）')
        self.assertEqual(
            [p['name'] for p in payload['policies']], ['keep'],
            'desired state 裡不該再有被刪掉的那條')


class TestReconcilerLeftSetCoversSmartHome(_ShAccessSyncFixture):
    """F 系列：SH 的 Access 資源要進 L4 左集。

    右集是 Cloudflare 帳號級的 list-all，**看得到**這些活著的 app／policy。
    左集漏掉它們不是「少算」——是每一個正常運作的 SH Access app 都變成孤兒候選、
    上孤兒報表（然後有人照著報表去清）。
    """

    def test_F1_route_access_app_id_enters_the_left_set(self):
        self._app_id(self.route, 'cf-app-f1')
        ls = collect_left_set(self.env)
        self.assertIn(
            'cf-app-f1', ls.access_app_ids,
            'SH route 的 Access app 走的是 policy 錨點 arc 的第三條腿，'
            'tunnel_id／subdomain 那兩行 walk 在結構上到不了它')

    def test_F2_policy_cf_id_enters_the_left_set(self):
        self._policy(cf_policy_id='cf-pol-f2')
        ls = collect_left_set(self.env)
        self.assertIn(
            'cf-pol-f2', ls.access_policy_ids,
            'SA 的 route→policy walk 到不了 SH 錨定的 policy（不是漏一個欄位，'
            '是漏一整條 arc）')

    def test_F3_empty_ids_do_not_enter_the_left_set(self):
        """空值不入左集（否則 `''` 會變成一把萬用鑰匙，讓真孤兒逃掉）。"""
        ls = collect_left_set(self.env)
        self.assertNotIn('', ls.access_app_ids)
        self.assertNotIn(False, ls.access_app_ids)
        self.assertNotIn('', ls.access_policy_ids)
        self.assertNotIn(False, ls.access_policy_ids)


class TestShResumeForcedSync(_ShAccessSyncFixture):
    """G 系列：resume 是停權期間所有 Access 變更的唯一收斂點。"""

    def test_G1_resume_pushes_the_divergence_accumulated_while_suspended(self):
        """停權中關掉保護 → resume 必須把那個變更推上 Cloudflare。

        BFF 沒有 state 閘，租戶在停權期間照樣改得動 Access 設定；而 route 的
        liveness gate 刻意排除 `suspended`（那時 ingress 已是 503，Access app
        留著無害）。兩件事合起來的後果是：DB 說「未保護」、Cloudflare 上那個
        app 還活著，**而症狀與「保護正常運作」完全相同**——沒有例外、沒有 log、
        端點回報成功。resume 的 forced sync 是唯一會收斂它的地方。
        """
        self._configure(is_protected=True)
        self._app_id(self.route, 'app-live')
        self.sh.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        client = self._mock(_sync_result(app_id=None))

        # 停權期間的變更：只寫進 DB，不推 CF（前提，也是 B4 的具體案例）
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.route.write({'is_protected': False})
        self.assertFalse(client.sync_access_app.called,
                         '前提：停權期間不推 Cloudflare')
        self.assertEqual(self.route.access_app_id, 'app-live',
                         '前提：CF 上那個 app 還在')

        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sh._do_resume()

        self.assertEqual(self.sh.state, 'active')
        self.assertTrue(
            client.sync_access_app.called,
            'resume 沒補這一次 forced sync ⇒ 停權期間被關掉保護的 route 永遠'
            '不會收斂，而症狀與「保護正常運作」完全相同')
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'],
                         '推上去的必須是停權期間改成的那個狀態')
        self.assertEqual(payload['existing_app_id'], 'app-live')
        self.assertFalse(self.route.access_app_id,
                         '收斂完成後 DB 這側的 app id 要跟著清掉')
