"""#768（SH AC T3）：SH access endpoints 的授權矩陣、釘定與白名單（真 HTTP）。

## 為什麼整份走真 HTTP，而不是直呼 controller

姊妹檔 `test_sh_access_model_768.py` 守的是 model 層的不變式（arc／quota 池／
route 子模型），它用 `TransactionCase` 的 superuser env——那個 env 對本檔要守的
三件事**完全免疫**：ir.rule 不套用、欄位級 `groups=` 不生效、`/web/dataset/call_kw`
把呼叫端 context 整包換進 env 這件事重現不出來。#739（CS 的同構缺陷）的形狀正是
「租戶身分下這條路徑從來沒成功過，而 su env 的測試全綠」。

所以本檔的每一條都：只掛 `base.group_user` 的帳號 ＋ `self.authenticate()` ＋
`self.url_open()` 打 `type="json"` 端點。骨架逐字沿用
`test_cs_access_endpoints_745.py`（`_post_json` / `_assert_ok` / `_assert_denied`），
角色矩陣的形狀沿用 `test_smart_home_subscription_flow.py` 的
`TestSmartHomeMutatingEndpointsRoleCheck`。

## 陽性／陰性成對（本檔的硬紀律）

每一條「被擋下來」旁邊都有一條「合法路徑會成功」，而且**放在同一個迴圈／同一個
測試裡**。沒有它，`Insufficient permissions` 可以來自 workspace 找不到、SH 找不
到、route 找不到、訂閱閘——那幾種情況下所有負向斷言都會為了錯的理由變綠，而輸出
與「role gate 有效」不可區分。

歸因斷言一律命中**字面訊息**（`Insufficient permissions` / `Smart home not found`
/ `Route not found` / `Policy not found`），不只斷言「有 error」：這四道 gate 在
同一個 helper（`_get_workspace_smarthome_or_error`）裡前後相鄰，只斷言 falsy 的
話，把 role gate 整條刪掉測試照樣綠（SH 找不到那條會頂上來）。

## 釘定測試刻意讓攻擊者「看得見」目標

跨 workspace 那一格用的是**同時擁有兩個 workspace 的 alice**，而不是一個看不見
目標的外人。理由：外人打過來會先在 `_get_workspace_or_error` 就被擋掉（甚至可能
在讀 `smarthome.workspace_id` 時吃到裸 AccessError），那測到的是 ir.rule 不是
URL 釘定。要證明「SH id 釘在 URL 的 workspace 上」，攻擊者必須對目標**有讀權**
——否則這一格與「ir.rule 擋住了」不可區分。跨 SH 的 route／policy 釘定同理，兩台
SH 都在 W1、都由 alice 擁有。

## operator

這些端點在 SH 沒有 `tunnel_id` 時**不會**打 operator（`_access_is_live` 第一道就
return），所以絕大多數測試的 fixture 刻意不設 tunnel——零 mock、零外部依賴。真的
要驗「租戶身分推得到 Cloudflare」的那一組（J 系列）另建一台已 provision 的 SH，
mock 的回傳是**真實形狀的 dict**（五個 key 一個都不少）：裸 MagicMock 會讓
`access_app_id` 回存拿到一個 MagicMock，測試綠而 prod 炸。
"""
import json
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
# #536：`sa_skip_route_sync` 只認模組私有 sentinel，不能傳 `True`
# （呼叫端 context 會被 `call_kw` 整包換進 env ⇒ truthy 值是租戶可偽造的）。
from ..models.security_access import _skip_route_sync
from ..models.workspace_access import ROLE_ADMIN, ROLE_GUEST, ROLE_USER
from .smart_home_fixture import sh_model

#: ⚠ SH 的 patch 點與 CS 不同：`models/smart_home.py` 把
#: `get_paas_operator_client` import 進**自己的**模組命名空間，patch
#: `services.paas_operator` 的那一份對它無效（會靜默打真的 client，症狀是
#: 「PaaS Operator is not configured」而不是斷言失敗）。
MOCK_SH_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)
_POLICY = 'woow_paas_platform.security_access_access_policy'
_ROUTE = 'woow_paas_platform.smart_home_route'


def _new_group_user(env, name):
    """只有 `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 768' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


def _sync_result(app_id='app-768', policy_ids=None, unresolved=None):
    """operator `/api/access/sync` 的**真實形狀**回傳。

    五個 key 一個都不能少：`_sync_access_to_operator` 逐一 `result.get(...)`，
    裸 `MagicMock()` 每一個 get 都會回一個新的 MagicMock ⇒ `access_app_id` 存進
    去的是 `<MagicMock ...>` 字串、`orphaned_policy_ids` 永遠 truthy，而測試全綠。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': list(unresolved or []),
        'orphaned_policy_ids': [],
    }


@tagged('post_install', '-at_install')
class _ShAccessEndpointFixture(HttpCase):
    """W1（alice owner／dave admin／carol user／gary guest）＋ W2（bob）＋ W3（alice）。

    W3 的存在理由見檔頭「釘定測試刻意讓攻擊者看得見目標」：跨 workspace 那一格
    的攻擊者必須對目標 SH 有讀權，否則測到的是 ir.rule 不是 URL 釘定。
    """

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Policy = self.env[_POLICY]
        self.Route = self.env[_ROUTE]
        self.Plan = self.env['sh.subscription.plan']
        self.Subscription = self.env['sh.subscription.subscription']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice768')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob768')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol768')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave768')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary768')
        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 768'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 768'})
        # alice 同時擁有 W3——跨 workspace 釘定那一格要的是「看得見但釘不到」。
        self.w3 = self.Workspace.with_user(self.alice).create({'name': 'W3 768'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            len(self.w1.sudo().access_ids), 4,
            'fixture 前提：W1 必須同時掛 owner/admin/user/guest 四列——'
            '少一列，`access_ids any [...]` 與 `access_ids.role` 點鏈兩種寫法'
            '行為就一樣，role 矩陣零鑑別力（#524 A4/A5 的同一條教訓）')
        self.assertFalse(
            self.w1.sudo().access_ids.filtered(
                lambda a: a.user_id == self.bob),
            'fixture 前提：bob 不得是 W1 的成員（非成員探針靠這一條）')

        # 主受測對象：W1 底下兩台 SH，各自一條 primary route。
        # 兩台都在 W1、都屬 alice ⇒ 跨 SH 釘定測的是「釘在 URL 那台 SH 上」，
        # 不是「別人的東西看不到」。
        self.sh = self._smart_home(self.w1, 'SH A 768')
        self.route = self._route(self.sh)
        self.sh_b = self._smart_home(self.w1, 'SH B 768')
        self.route_b = self._route(self.sh_b)
        # W3 的那一台（alice 讀得到，但釘不到 W1 的 URL 上）
        self.sh_w3 = self._smart_home(self.w3, 'SH W3 768')
        self.route_w3 = self._route(self.sh_w3)

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #
    def _smart_home(self, workspace, name, subdomain=True, **extra):
        """建一台 SH，並把開閘 context **洗掉**再交出去。

        🔴 `with_env(self.env)` 不是排版：`with_context` 是 merge、`.sudo()` 也
        保留把手，所以 `sh_model(...).create(...)` 回來的 recordset 自帶建立閘與
        身分錨點閘兩個 sentinel。不洗掉的話 fixture 自己把受測守衛關掉了
        （`test_smart_home_authz_524.py:150-168` 有實測紀錄）。

        `subdomain=True` ⇒ 產一個唯一子網域（DB 有 `UNIQUE(subdomain)`）；
        `subdomain=None` ⇒ 未 provision 的 SH，`route.hostname` 為空。
        """
        vals = {'name': name, 'workspace_id': workspace.id}
        vals.update(extra)
        sh = sh_model(self.env).sudo().create(vals).with_env(self.env)
        if subdomain is True:
            sh.sudo().write(
                {'subdomain': 'paas-sm-768-%s' % uuid.uuid4().hex[:10]})
        elif subdomain:
            sh.sudo().write({'subdomain': subdomain})
        return sh

    def _route(self, sh):
        """這台 SH 的 primary route。

        建立時抑制 sync（已 provision 的 fixture 會真的打 operator），回傳前把
        旗標**顯式關掉**：`with_context` 是 merge，不關的話之後每一次
        `route.write(...)` 都被抑制，而那個症狀與「sync 沒接上」不可區分
        （`test_cs_access_endpoints_745.py::_policy` 的同一段教訓）。
        """
        route = _sm_skip_access_sync(self.Route.sudo()).create(
            {'smart_home_id': sh.id, 'is_primary': True})
        return route.with_context(sm_skip_access_sync=False)

    def _policy(self, route, name=None):
        """既存 policy 的 fixture。

        刻意**不**帶 `sequence`：precedence 交給 model 的 sparse 探測算
        （10 / 20 / …）。自己指定的話，重排測試比的就是 fixture 塞進去的數字，
        而不是「錨點內遞增」這個真正要守的行為。
        """
        vals = {
            'smart_home_route_id': route.id,
            'name': name or 'sh768-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            # ⚠ 用 email_list 不是 email_domain／everyone：allow × include ×
            # 整批放行 selector 會被 #352 的守衛擋下（那是正確行為），而它的
            # ValidationError 會讓 fixture 死在抵達受測步驟之前。
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        pol = _skip_route_sync(self.Policy.sudo()).create(vals)
        # context 會跟著 recordset 走——不關掉的話之後每一次 write 都被抑制，
        # 而那個症狀與「sync 沒接上」不可區分。
        return pol.with_context(sa_skip_route_sync=False)

    def _subscribe(self, record, **quota):
        """把一份帶額度的方案綁到 `record` 上（state=in_progress ⇒ 訂閱閘放行）。"""
        plan = self.Plan.sudo().create(dict({
            'name': 'SH AC Plan 768 %s' % uuid.uuid4().hex[:6],
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
            # #521：席位 enforcement 上線後 `ac_max_access_seats` 預設 0 ＝「一席
            # 都沒有」⇒ 帶 email_list 的 fixture 會被 `_check_access_seat_quota`
            # 擋住。本檔測的是 policy 數 quota、不是席位，給明顯夠用的額度讓紅燈
            # 的原因只可能是被測的那一條（呼叫端 quota kwargs 仍可覆寫）。
            # ⚠ fixture forward coupling，**不是**產品側豁免。
            'ac_max_access_seats': 99,
        }, **quota))
        sub = self.Subscription.sudo().create({
            'sh_partner_id': self.alice.partner_id.id,
            'state': 'in_progress',
            'sh_start_date': date.today(),
            'sh_end_date': date.today() + timedelta(days=30),
        })
        sub.sudo().sh_subscription_plan_id = plan.id
        record.sudo().subscription_id = sub.id
        return plan, sub

    # ------------------------------------------------------------------ #
    #  真 HTTP helper                                                      #
    # ------------------------------------------------------------------ #
    def _post_json(self, url, payload):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                           'params': payload})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        # 每次 RPC 後洗 cache：先讀過同一筆記錄會把值餵進 env cache，而遮蔽掉
        # 「端點其實沒寫進去」與 AccessError 兩種結果。
        self.env.invalidate_all()
        return resp.json().get('result', resp.json())

    def _access_config(self, payload, workspace=None, smarthome=None,
                       route=None):
        return self._post_json(
            '/api/workspaces/%d/smarthomes/%d/routes/%d/access-config' % (
                (workspace or self.w1).id, (smarthome or self.sh).id,
                (route or self.route).id),
            payload)

    def _access_policies(self, payload, workspace=None, smarthome=None):
        return self._post_json(
            '/api/workspaces/%d/smarthomes/%d/access-policies' % (
                (workspace or self.w1).id, (smarthome or self.sh).id),
            payload)

    def _sh_detail(self, payload, workspace=None, smarthome=None):
        return self._post_json(
            '/api/workspaces/%d/smarthomes/%d' % (
                (workspace or self.w1).id, (smarthome or self.sh).id),
            payload)

    def _sh_list(self, workspace=None):
        return self._post_json(
            '/api/workspaces/%d/smarthomes' % (workspace or self.w1).id,
            {'action': 'list'})

    def _change_plan(self, plan_id, workspace=None, smarthome=None):
        return self._post_json(
            '/api/workspaces/%d/smarthomes/%d/change-plan' % (
                (workspace or self.w1).id, (smarthome or self.sh).id),
            {'plan_id': plan_id})

    def _create_policy_payload(self, name, **extra):
        payload = {
            'action': 'create', 'name': name, 'decision': 'allow',
            'route_id': self.route.id,
            'include': [{'rule_type': 'email_list',
                         'rule_value': '%s@example.com' % name}],
        }
        payload.update(extra)
        return payload

    def _mock(self, result=None):
        client = MagicMock()
        client.sync_access_app.return_value = (
            result if result is not None else _sync_result())
        return client

    # ------------------------------------------------------------------ #
    #  斷言 helper —— 四道 gate 相鄰，訊息必須分得開                        #
    # ------------------------------------------------------------------ #
    def _assert_ok(self, result, msg):
        self.assertTrue(isinstance(result, dict) and result.get('success'),
                        msg + '（回 %r）' % (result,))
        return result

    def _error_text(self, result):
        """端點自己的 error 字串；若整個請求炸成 JSON-RPC error 也一併攤平。

        兩種形狀都要涵蓋：controller 回 `{'success': False, 'error': ...}`，
        而一個沒被 controller 接住的 AccessError 會變成頂層 `error` 信封——只讀
        前者的話後者會拿到空字串，於是「歸因錯了」與「沒有錯誤訊息」不可區分。
        """
        if not isinstance(result, dict):
            return repr(result)
        if isinstance(result.get('error'), str):
            return result['error']
        data = (result.get('error') or {})
        data = data.get('data') or {} if isinstance(data, dict) else {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_denied(self, result, msg):
        """必須是 **role gate** 擋的，不是 SH／route／policy 找不到。"""
        self.assertTrue(isinstance(result, dict), msg + ' 回 %r' % (result,))
        self.assertFalse(result.get('success'), msg + '（沒被擋）')
        self.assertIn(
            'Insufficient permissions', self._error_text(result),
            msg + '（歸因：必須是 role gate。四道 gate 在同一個 helper 裡前後'
                  '相鄰，只斷言 falsy 的話刪掉 role gate 測試照樣綠，回 %r）'
            % (result,))

    def _assert_rejected(self, result, expected, msg):
        self.assertTrue(isinstance(result, dict), msg + ' 回 %r' % (result,))
        self.assertFalse(result.get('success'), msg + '（沒被擋）')
        self.assertIn(expected, self._error_text(result),
                      msg + '（歸因：期望 %r，回 %r）' % (expected, result))
        self.assertFalse(result.get('data'),
                         msg + '（被拒的請求不得順手吐 data）')

    #: role → (login, 是否應該通過)。四列一起跑，陽性對照與負向斷言同一個迴圈。
    def _role_matrix(self):
        return (
            ('owner', self.alice_login, True),
            ('admin', self.dave_login, True),
            ('role=user', self.carol_login, False),
            ('role=guest', self.gary_login, False),
        )


class TestShAccessConfigRoleGate(_ShAccessEndpointFixture):
    """G 系列：`access-config` 的 role 矩陣與非成員。"""

    def test_G1_access_config_role_matrix(self):
        """owner/admin 寫得動；role=user／guest 被 role gate 擋，且**零落地**。

        「被擋」與「寫進去了但沒回應」在 HTTP 層輸出相同，所以每一格都要回頭
        查 DB：denied 那兩格若真的落地，Access 閘門就是全 workspace 成員可改。
        """
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                _sm_skip_access_sync(self.route.sudo()).write(
                        {'is_protected': False, 'session_duration': '24h'})
                self.authenticate(login, login)
                res = self._access_config(
                    {'is_protected': True, 'session_duration': '8h',
                     'login_google': True})
                self.env.invalidate_all()
                if allowed:
                    self._assert_ok(res, '%s 應該設定得了自己 SH 的 Access'
                                    % label)
                    self.assertTrue(self.route.sudo().is_protected)
                    self.assertEqual(self.route.sudo().session_duration, '8h')
                    self.assertTrue(self.route.sudo().login_google)
                else:
                    self._assert_denied(
                        res, '%s 不得改動 Access 閘門' % label)
                    self.assertFalse(
                        self.route.sudo().is_protected,
                        '%s 被擋了，DB 卻留下 is_protected=True ⇒ gate 只是'
                        '沒回應、寫入照樣落地' % label)
                    self.assertEqual(
                        self.route.sudo().session_duration, '24h',
                        '%s 被擋了，session_duration 卻被改掉 ⇒ 白名單那半在'
                        'gate 之前就跑了' % label)

    def test_G2_non_member_cannot_reach_access_config(self):
        """非成員在 workspace 那一步就該被擋（不是 role gate、不是找不到 SH）。"""
        self.authenticate(self.bob_login, self.bob_login)
        res = self._access_config({'is_protected': True})
        self._assert_rejected(
            res, 'Workspace not found or access denied',
            '非 W1 成員不得抵達 W1 的 access-config')
        self.assertFalse(self.route.sudo().is_protected)


class TestShAccessPolicyRoleGate(_ShAccessEndpointFixture):
    """G 系列（續）：`access-policies` 四個 mutation action 的 role 矩陣。"""

    def test_G3_policy_create_role_matrix(self):
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                name = 'g3-%s' % label.replace('=', '-')
                self.authenticate(login, login)
                res = self._access_policies(self._create_policy_payload(name))
                landed = self.Policy.sudo().search([('name', '=', name)])
                if allowed:
                    self._assert_ok(res, '%s 應該建得了 policy' % label)
                    self.assertEqual(
                        len(landed), 1,
                        '%s 的請求回 success 卻沒有 policy 落地 ⇒ 端點在說謊'
                        % label)
                    self.assertEqual(
                        landed.smart_home_route_id, self.route,
                        'policy 必須錨在 URL 釘住的那台 SH 的 route 上')
                else:
                    self._assert_denied(res, '%s 不得新增 Access policy' % label)
                    self.assertFalse(
                        landed,
                        '%s 被擋了，policy 卻落地 ⇒ gate 在 create 之後才跑'
                        % label)

    def test_G4_policy_update_role_matrix(self):
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                pol = self._policy(self.route, name='g4-%s' % label[:6])
                original = pol.name
                self.authenticate(login, login)
                res = self._access_policies({
                    'action': 'update', 'policy_id': pol.id,
                    'name': '%s-renamed' % original})
                self.env.invalidate_all()
                if allowed:
                    self._assert_ok(res, '%s 應該改得動 policy' % label)
                    self.assertEqual(pol.sudo().name, '%s-renamed' % original)
                else:
                    self._assert_denied(res, '%s 不得改 policy' % label)
                    self.assertEqual(
                        pol.sudo().name, original,
                        '%s 被擋了，名稱卻被改掉 ⇒ 寫入在 gate 之前' % label)

    def test_G5_policy_delete_role_matrix(self):
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                pol = self._policy(self.route, name='g5-%s' % label[:6])
                self.authenticate(login, login)
                res = self._access_policies(
                    {'action': 'delete', 'policy_id': pol.id})
                self.env.invalidate_all()
                if allowed:
                    self._assert_ok(res, '%s 應該刪得掉 policy' % label)
                    self.assertFalse(pol.sudo().exists())
                else:
                    self._assert_denied(res, '%s 不得刪除 policy' % label)
                    self.assertTrue(
                        pol.sudo().exists(),
                        '%s 被擋了，policy 卻消失 ⇒ unlink 在 gate 之前'
                        % label)

    def test_G6_policy_reorder_role_matrix(self):
        """reorder 是唯一「不改內容只改 precedence」的 mutation。

        漏掉它的症狀不是拒絕服務，是**任何成員都能重排 Cloudflare 的評估順序**
        ——把一條 deny policy 排到 allow 後面就等於把它關掉，而 policy 本身
        一個字都沒被改（稽核上看不出來）。
        """
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                sh = self._smart_home(self.w1, 'SH G6 %s' % label)
                route = self._route(sh)
                first = self._policy(route, name='g6-a-%s' % label[:6])
                second = self._policy(route, name='g6-b-%s' % label[:6])
                self.assertLess(
                    first.sequence, second.sequence,
                    'fixture 前提：重排前 first 必須排在 second 之前，'
                    '否則 reorder 之後的斷言零鑑別力')
                self.authenticate(login, login)
                res = self._access_policies(
                    {'action': 'reorder',
                     'policy_ids': [second.id, first.id]},
                    smarthome=sh)
                self.env.invalidate_all()
                if allowed:
                    self._assert_ok(res, '%s 應該排得動 precedence' % label)
                    self.assertLess(
                        second.sudo().sequence, first.sudo().sequence,
                        '%s 的 reorder 回 success 卻沒有換順序' % label)
                else:
                    self._assert_denied(res, '%s 不得重排 precedence' % label)
                    self.assertLess(
                        first.sudo().sequence, second.sudo().sequence,
                        '%s 被擋了，順序卻被改掉 ⇒ 寫入在 gate 之前' % label)


class TestShAccessListBranch(_ShAccessEndpointFixture):
    """H 系列：`action='list'` 是唯一跳過 role gate 與訂閱閘的分支。"""

    def test_H1_every_workspace_member_can_list_policies(self):
        """含 role=user／guest——讀不到的話 Access 分頁對他們是壞的。"""
        self._policy(self.route, name='h1-listed')
        for label, login, _allowed in self._role_matrix():
            with self.subTest(role=label):
                self.authenticate(login, login)
                res = self._access_policies({'action': 'list'})
                self._assert_ok(res, '%s 應該讀得到自己 workspace 的 policy'
                                % label)
                self.assertEqual([p['name'] for p in res['data']],
                                 ['h1-listed'])

    def test_H2_list_never_leaks_another_smart_homes_policies(self):
        """list 以 URL 那台 SH 為 scope——別台 SH 的 policy 不得混進來。"""
        self._policy(self.route, name='h2-mine')
        self._policy(self.route_b, name='h2-other-sh')
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies({'action': 'list'})
        self._assert_ok(res, 'owner 讀得到自己那台的')
        self.assertEqual(
            [p['name'] for p in res['data']], ['h2-mine'],
            'list 的 scope 不是 URL 那台 SH ⇒ 前端會把別台的 policy 畫進來，'
            '而且刪除／重排都會打到別台')

    def test_H3_non_member_gets_nothing(self):
        self._policy(self.route, name='h3-secret')
        self.authenticate(self.bob_login, self.bob_login)
        res = self._access_policies({'action': 'list'})
        self._assert_rejected(
            res, 'Workspace not found or access denied',
            '非成員不得讀到 W1 的 policy 清單')
        self.assertNotIn(
            'h3-secret', json.dumps(res),
            'policy 名稱是租戶自填的（可能含 email／組織名），一個字都不該外流')


class TestShAccessScopePinning(_ShAccessEndpointFixture):
    """I 系列：workspace／SH／route／policy 四層釘定。

    每一格的攻擊者對目標都**有讀權**（見檔頭）：這樣「被擋」只可能是釘定，不會
    是 ir.rule 順手擋掉的副作用。
    """

    def test_I1_smart_home_is_pinned_to_the_url_workspace(self):
        """W3 的 SH 掛在 W1 的 URL 上 ⇒ Smart home not found。

        陽性對照就在同一條裡：同一台 SH 掛在 W3 的 URL 上必須成功——沒有它，
        `Smart home not found` 可能只是「這台 SH 壞掉」。
        """
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies({'action': 'list'},
                                    workspace=self.w1, smarthome=self.sh_w3)
        self._assert_rejected(
            res, 'Smart home not found',
            'W3 的 SH 不該從 W1 的 URL 進得去（即使呼叫端同時擁有兩個 ws）')

        res = self._access_policies({'action': 'list'},
                                    workspace=self.w3, smarthome=self.sh_w3)
        self._assert_ok(res, '陽性對照：同一台 SH 從自己的 workspace 進得去')

    def test_I2_cross_workspace_config_write_lands_nowhere(self):
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({'is_protected': True},
                                  workspace=self.w1, smarthome=self.sh_w3,
                                  route=self.route_w3)
        self._assert_rejected(res, 'Smart home not found',
                              '跨 workspace 的 route 不得被設定')
        self.assertFalse(
            self.route_w3.sudo().is_protected,
            '跨 workspace 的 route 一個欄位都不該被寫到')

    def test_I3_route_id_is_pinned_to_the_url_smart_home(self):
        """別台 SH 的 route_id ⇒ Route not found（兩台都在 W1、都屬 alice）。"""
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({'is_protected': True},
                                  smarthome=self.sh, route=self.route_b)
        self._assert_rejected(
            res, 'Route not found',
            'route_id 沒有釘在 URL 那台 SH 上 ⇒ 租戶可以拿自己 A 台的權限去'
            '設定 B 台（甚至別人 workspace 的）Access')
        self.assertFalse(self.route_b.sudo().is_protected)

        res = self._access_config({'is_protected': True},
                                  smarthome=self.sh, route=self.route)
        self._assert_ok(res, '陽性對照：自己那台 SH 的 route 設定得了')

    def test_I4_create_policy_route_id_is_pinned_too(self):
        """`create` 的 `route_id` 走同一個 `_sh_route()`，釘定同樣要成立。"""
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload(
            'i4-foreign', route_id=self.route_b.id))
        self._assert_rejected(res, 'Route not found',
                              'create 的 route_id 沒被釘在 URL 那台 SH 上')
        self.assertFalse(self.Policy.sudo().search([('name', '=', 'i4-foreign')]))

        res = self._access_policies(self._create_policy_payload('i4-own'))
        self._assert_ok(res, '陽性對照：釘在自己那台的 route 上建得起來')

    def test_I5_policy_id_is_pinned_to_the_url_smart_home(self):
        """update／delete 的 policy_id 跨 SH ⇒ Policy not found，且原物不動。"""
        foreign = self._policy(self.route_b, name='i5-foreign')
        self.authenticate(self.alice_login, self.alice_login)

        res = self._access_policies({
            'action': 'update', 'policy_id': foreign.id,
            'name': 'i5-hijacked'}, smarthome=self.sh)
        self._assert_rejected(res, 'Policy not found',
                              '別台 SH 的 policy 不得從這台的 URL 被改')
        self.assertEqual(foreign.sudo().name, 'i5-foreign')

        res = self._access_policies({
            'action': 'delete', 'policy_id': foreign.id}, smarthome=self.sh)
        self._assert_rejected(res, 'Policy not found',
                              '別台 SH 的 policy 不得從這台的 URL 被刪')
        self.assertTrue(foreign.sudo().exists())

        res = self._access_policies({
            'action': 'update', 'policy_id': foreign.id,
            'name': 'i5-renamed'}, smarthome=self.sh_b)
        self._assert_ok(res, '陽性對照：從它自己那台 SH 的 URL 改得動')
        self.env.invalidate_all()
        self.assertEqual(foreign.sudo().name, 'i5-renamed')

    def test_I6_reorder_silently_skips_foreign_policy_ids(self):
        """reorder 的 id 清單混進別台 SH 的 policy ⇒ 略過，不得寫到別人的列。"""
        mine_a = self._policy(self.route, name='i6-a')
        mine_b = self._policy(self.route, name='i6-b')
        foreign = self._policy(self.route_b, name='i6-foreign')
        foreign_seq = foreign.sequence
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies({
            'action': 'reorder',
            'policy_ids': [mine_b.id, foreign.id, mine_a.id]})
        self._assert_ok(res, 'reorder 本身應該成功（外來 id 只是被略過）')
        self.env.invalidate_all()
        self.assertEqual(
            res['data']['reordered'], [mine_b.id, mine_a.id],
            '回報的重排清單必須只含被釘住的那些，否則前端會以為別台也重排了')
        self.assertLess(mine_b.sudo().sequence, mine_a.sudo().sequence)
        self.assertEqual(
            foreign.sudo().sequence, foreign_seq,
            '別台 SH 的 policy sequence 被改到 ⇒ 重排等於跨 SH 寫入')


class TestShAccessPayloadWhitelist(_ShAccessEndpointFixture):
    """J 系列（本檔最重要的一組）：client payload 的欄位白名單。

    🔴 為什麼白名單是唯一擋得住的地方：policy 的 `anchor_arc` 是 **arity** 檢查
    （恰好一個錨點非空），**不是 ownership 檢查**。一條只帶「別台 SH 的
    `smart_home_route_id`」的 policy 完全通得過 arc、通得過 ir.rule（它掛在攻擊
    者讀得到的 workspace 上），也通得過 SQL CHECK。三層守線全綠，而那條 policy
    會被推上**別台 SH 的 Cloudflare Access application**。
    """

    def setUp(self):
        super().setUp()
        # 夾帶用的第三方錨點：刻意用**不同 model**（cloud_service），這是 arc
        # 「只數非空個數」最無感的一格。
        self.cs_template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'SH AC smuggle tmpl 768',
                'slug': 'sh-ac-768-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/sh-ac-768',
                # ⚠ 必須釘版：未釘版的 template 會被 CS 的供給閘擋下，fixture 會
                # 死在抵達受測步驟之前，而那個 error 長得跟「守衛擋住了」一樣。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        self.svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'svc-768-smuggle',
                'workspace_id': self.w1.id,
                'template_id': self.cs_template.id,
                'state': 'running',
            })

    def test_J1_create_policy_ignores_every_smuggled_anchor(self):
        """夾帶 `smart_home_route_id`／`cloud_service_id`／`cf_policy_id` 全部無效。

        陽性對照在**同一個請求**裡：合法欄位（name／decision／include）照樣落地，
        證明被丟掉的只有那幾個，不是整個請求失敗。
        """
        decoy = self._policy(self.route_b, name='j1-decoy')
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload(
            'j1',
            smart_home_route_id=self.route_b.id,   # 別台 SH 的 route
            smart_home_id=self.sh_b.id,
            cloud_service_id=self.svc.id,          # 另一個 model 的錨點
            cf_policy_id='forged-cf',              # 伺服器擁有的 CF 物件 id
        ))
        self._assert_ok(res, '合法欄位那半應該成功（只有錨點由伺服器決定）')

        pol = self.Policy.sudo().search([('name', '=', 'j1')])
        self.assertEqual(len(pol), 1)
        self.assertEqual(
            pol.smart_home_route_id, self.route,
            '錨點必須是 URL＋route_id 釘出來的那條 route。落在 %r 代表 payload '
            '決定了錨點——arc 是 arity 不是 ownership，這裡是唯一擋得住的地方'
            % (pol.smart_home_route_id,))
        self.assertEqual(pol.smart_home_id, self.sh)
        self.assertFalse(
            pol.cloud_service_id,
            'CS 錨點若一起落地，arc 會因為兩個非空而拒絕整筆——沒拒絕代表它被'
            '丟掉了，但仍要斷言，否則將來 arc 放寬就無聲失守')
        self.assertFalse(pol.route_id, 'SA route 那條腿必須是空的')
        self.assertFalse(
            pol.cf_policy_id,
            'cf_policy_id 是 sync 回存的伺服器值；讓 client 填就等於讓它指定'
            '要覆寫／刪除哪一顆 Cloudflare reusable policy')
        self.assertEqual(
            len(self.Policy.sudo().search(
                [('smart_home_route_id', '=', self.route_b.id)])), 1,
            '別台 SH 的 route 底下不得因為這個請求多出 policy（原本只有 decoy）')
        self.assertEqual(decoy.sudo().name, 'j1-decoy')

    def test_J2_rule_commands_are_whitelisted_field_by_field(self):
        """子規則只吃 group／rule_type／rule_value，夾帶 `policy_id` 無效。

        `policy_id` 若被吃進去，這條 include rule 會掛到**別台 SH 的 policy**
        底下 ⇒ 受害者的 Cloudflare policy 多一條攻擊者寫的放行規則，而本筆
        policy 因為沒有 include 會被 `_check_at_least_one_include` 擋掉——兩種
        後果都要不到，所以兩邊都斷言。
        """
        victim = self._policy(self.route_b, name='j2-victim')
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies({
            'action': 'create', 'name': 'j2', 'decision': 'allow',
            'route_id': self.route.id,
            'include': [{'rule_type': 'email_list',
                         'rule_value': 'j2@example.com',
                         'policy_id': victim.id,
                         'cf_policy_id': 'forged-rule-cf'}],
        })
        self._assert_ok(res, '合法的 include 那半應該成功')
        pol = self.Policy.sudo().search([('name', '=', 'j2')])
        self.assertEqual(len(pol.rule_ids), 1,
                         'include 規則必須留在本筆 policy 底下')
        self.assertEqual(pol.rule_ids.policy_id, pol)
        self.assertEqual(
            len(victim.sudo().rule_ids), 1,
            '受害者 policy 的規則數被改變 ⇒ 子規則的 policy_id 被 client 決定了')

    def test_J3_access_config_server_owned_fields_are_never_settable(self):
        """`access_app_id`／`access_unresolved_idp_types`／綁定欄位一律丟掉。

        這三類的失效樣態不同，所以要一起送：
        * `access_app_id` 有 model 層 sentinel（丟 UserError）——白名單先丟掉它，
          所以一個順手多帶欄位的合法請求不會整個失敗；
        * `access_unresolved_idp_types` **完全沒有** model 層守衛，白名單是它
          唯一的防線（偽造成空值＝把 fail-open 放寬的訊號蓋掉）；
        * `smart_home_id`／`is_primary` 是 route 的綁定，改掉等於把整列重新瞄準
          另一台 SH 的 Cloudflare 物件。
        """
        _internal_sm_route_identity(self.route.sudo()).write(
            {'access_app_id': 'cf-app-real'})
        _sm_skip_access_sync(self.route.sudo()).write(
            {'access_unresolved_idp_types': 'okta'})
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({
            'session_duration': '1h',
            'access_app_id': 'forged-app',
            'access_unresolved_idp_types': '',
            'smart_home_id': self.sh_b.id,
            'is_primary': False,
        })
        self._assert_ok(res, '合法欄位那半應該成功（forged 欄位只是被忽略）')
        self.env.invalidate_all()
        route = self.route.sudo()
        self.assertEqual(route.session_duration, '1h',
                         '陽性對照：白名單內的欄位必須真的寫進去')
        self.assertEqual(
            route.access_app_id, 'cf-app-real',
            'client 送的 access_app_id 被吃下去 ⇒ 這條 route 從此驅動別人的'
            'Cloudflare Access application，teardown 會拿平台 key 去刪它')
        self.assertEqual(
            route.access_unresolved_idp_types, 'okta',
            'client 抹得掉 unresolved 訊號 ⇒ 登入限制被靜默放寬而 UI 一片乾淨')
        self.assertEqual(route.smart_home_id, self.sh,
                         'route 的綁定不得由 payload 改動')
        self.assertTrue(route.is_primary)

    def test_J4_access_config_response_never_exposes_the_cf_app_id(self):
        """回應本身也不得吐 `access_app_id`（值是真的、不是碰巧為空）。"""
        _internal_sm_route_identity(self.route.sudo()).write(
            {'access_app_id': 'cf-app-leak-probe'})
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({'session_duration': '8h'})
        self._assert_ok(res, 'access-config 應該成功')
        self.assertNotIn('access_app_id', res['data'])
        self.assertNotIn(
            'cf-app-leak-probe', json.dumps(res),
            '伺服器擁有的 CF 物件 id 不得出現在租戶 payload 的任何角落')
        self.assertEqual(
            self.route.sudo().access_app_id, 'cf-app-leak-probe',
            '判準的另一格：值必須真的存在，否則「沒外流」只是因為它是空的')


class TestShAccessQuotaPreCheck(_ShAccessEndpointFixture):
    """K 系列：controller 的 quota 前置檢查（`0 = unlimited`，#743）。"""

    def test_K1_zero_quota_means_unlimited_through_the_endpoint(self):
        """SH 的 40 筆 AC 變體 `sa_max_access_policies` 全走 default 0。

        沒有這個豁免，`existing >= quota` 在 `0 >= 0` 就成立 ⇒ 租戶連第一條
        policy 都建不出來，而 model 層的豁免**從 UI 不可達**（controller 先
        return，根本走不到 model）。這一格是那個豁免唯一的端點側證據。
        """
        self._subscribe(self.sh, sa_max_access_policies=0)
        self.authenticate(self.alice_login, self.alice_login)
        for i in range(3):
            res = self._access_policies(
                self._create_policy_payload('k1-%d' % i))
            self._assert_ok(
                res, 'quota=0（unlimited）時第 %d 條 policy 必須建得起來' % (i + 1))
        self.assertEqual(
            self.Policy.sudo().search_count(
                [('smart_home_id', '=', self.sh.id)]), 3)

    def test_K2_a_real_quota_still_blocks_the_overflow(self):
        """陽性／陰性對照：quota=1 時第一條過、第二條被擋且不落地。

        沒有這一條，K1 只證明了「這個 gate 什麼都不擋」——把整段 quota 檢查刪掉
        K1 照樣綠。
        """
        self._subscribe(self.sh, sa_max_access_policies=1)
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload('k2-first'))
        self._assert_ok(res, '額度內的第一條必須建得起來')

        res = self._access_policies(self._create_policy_payload('k2-second'))
        self.assertFalse(res.get('success'), '超額的第二條沒被擋')
        self.assertIn(
            'quota', self._error_text(res).lower(),
            '歸因：必須是配額訊息。回 %r 的話可能是 role gate／arc／#352 守衛'
            '頂替上來，而那三者與「配額有效」在 falsy 上不可區分' % (res,))
        self.assertEqual(
            self.Policy.sudo().search_count(
                [('smart_home_id', '=', self.sh.id)]), 1,
            '被擋的 policy 落地了 ⇒ 前置檢查跑在 create 之後')

    def test_K3_quota_pool_is_scoped_to_this_smart_home(self):
        """額度算的是**這台 SH**的 policy，不是全 workspace／全表。

        數錯與數對在單一 SH 的測試裡輸出相同，所以要有「B 台用掉額度不會吃到
        A 台」那一格。
        """
        self._subscribe(self.sh, sa_max_access_policies=1)
        self._policy(self.route_b, name='k3-other-sh')
        self._policy(self.route_b, name='k3-other-sh-2')
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload('k3-mine'))
        self._assert_ok(
            res, '別台 SH 已有兩條 policy，本台的第一條不該因此超額——'
                 '被擋代表計數的 scope 不是 smart_home_id')


class TestShAccessProtectionToggle(_ShAccessEndpointFixture):
    """L 系列：`is_protected` 的**關閉方向**與「沒有 hostname 不得開啟」。"""

    def test_L1_turning_protection_off_actually_turns_it_off(self):
        """送 `False` 必須真的關掉。

        🔴 這一格守的是 `in kwargs` vs `kwargs.get()`：後者對 `False` 是 falsy
        ⇒ `vals` 空 ⇒ 端點**不寫任何東西卻回 success**。租戶按了「關閉保護」、
        畫面回成功、Cloudflare 上那個 Access app 還在，而且沒有任何錯誤訊號。
        """
        _sm_skip_access_sync(self.route.sudo()).write(
            {'is_protected': True})
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({'is_protected': False})
        self._assert_ok(res, '關閉保護必須成功')
        self.env.invalidate_all()
        self.assertFalse(
            self.route.sudo().is_protected,
            '端點回 success 但 DB 還是 True ⇒ `False` 被 truthy 判斷吃掉了')
        self.assertFalse(res['data']['is_protected'],
                         '回應也要反映關閉後的狀態，否則前端會畫回舊值')

        # 陽性對照：同一支端點打得開，證明上面的 False 不是「兩個方向都壞掉」
        res = self._access_config({'is_protected': True})
        self._assert_ok(res, '開啟方向必須也走得通')
        self.env.invalidate_all()
        self.assertTrue(self.route.sudo().is_protected)

    def test_L2_cannot_protect_a_smart_home_with_no_public_address(self):
        """未 provision（無 subdomain ⇒ 無 hostname）不得開啟保護。

        不擋的話這個寫入會**成功**、UI 顯示「已保護」，而
        `_sync_access_to_operator` 的 liveness gate 第一道就 return ⇒ Cloudflare
        上一個物件都沒有。那是**對租戶無聲的謊**，不是 no-op。
        """
        sh = self._smart_home(self.w1, 'SH L2 768', subdomain=None)
        route = self._route(sh)
        self.assertFalse(route.hostname, 'fixture 前提：這條 route 沒有 hostname')
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_SH_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': True},
                                      smarthome=sh, route=route)
        self.assertFalse(res.get('success'), '沒有 hostname 卻能開啟保護')
        self.assertIn('no public address', self._error_text(res))
        self.assertFalse(
            client.sync_access_app.called,
            '被擋的請求不該打到 operator（打了代表 gate 在 sync 之後）')
        self.env.invalidate_all()
        self.assertFalse(
            route.sudo().is_protected,
            'DB 不得留下一個什麼都沒保護到的 True')

    def test_L3_closing_direction_is_never_blocked_by_the_hostname_gate(self):
        """陰性對照：同一台沒有 hostname 的 SH，關閉方向必須做得到。

        gate 若寫成「沒有 hostname 就整支拒絕」，租戶會被鎖在「已保護」狀態裡
        （例如 provision 失敗把 subdomain 清掉之後），而那是不可逆的。
        """
        sh = self._smart_home(self.w1, 'SH L3 768', subdomain=None)
        route = self._route(sh)
        _sm_skip_access_sync(route.sudo()).write(
            {'is_protected': True})
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_config({'is_protected': False},
                                  smarthome=sh, route=route)
        self._assert_ok(res, '關閉方向在任何情況下都必須做得到')
        self.env.invalidate_all()
        self.assertFalse(route.sudo().is_protected)


class TestShAccessApiPayloadShape(_ShAccessEndpointFixture):
    """M 系列：detail 帶 `routes`、list **不帶**，且兩者都不吐 CF app id。"""

    def test_M1_detail_carries_routes_and_list_does_not(self):
        """`include_details` 的兩側必須真的分家。

        list 端點會為 workspace 的**每一台** SH 各 walk 一次
        `access_policy_ids`，而 Access 分頁只存在於詳情頁。漏掉這個切分不會壞
        功能，只會讓清單頁隨 policy 數線性變慢——沒有任何錯誤訊號。
        """
        self._policy(self.route, name='m1-policy')
        self.authenticate(self.alice_login, self.alice_login)

        res = self._sh_detail({'action': 'get'})
        self._assert_ok(res, 'detail 端點應該成功')
        routes = res['data'].get('routes')
        self.assertIsInstance(
            routes, list, 'detail payload 必須帶 routes，否則前端無從畫 Access '
                          '分頁（回 %r）' % (res['data'].keys(),))
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]['id'], self.route.id)
        self.assertEqual([p['name'] for p in routes[0]['access_policies']],
                         ['m1-policy'])
        for key in ('hostname', 'is_protected', 'session_duration',
                    'login_google', 'login_email_otp',
                    'access_unresolved_idp_types'):
            self.assertIn(key, routes[0],
                          'route payload 少了 %s（AccessAppCard 的共用契約）'
                          % key)

        res = self._sh_list()
        self._assert_ok(res, 'list 端點應該成功')
        row = next(d for d in res['data'] if d['id'] == self.sh.id)
        self.assertNotIn(
            'routes', row,
            'list 端點帶了 routes ⇒ 每一台 SH 各 walk 一次 policy，清單頁的'
            '成本隨 policy 數線性成長而沒有任何訊號')
        self.assertIn('subscription', row,
                      '顯示條件（ac_max_access_seats）走 subscription，'
                      '清單頁不得因為拿掉 routes 而少掉它')

    def test_M2_route_payload_never_leaks_the_cf_app_id(self):
        """`routes[0]` 不得出現 `access_app_id`——而且值是真的存在的。

        判準的第三格：只斷言 `assertNotIn` 的話，`access_app_id` 恰好為空時這
        條測試會為了錯的理由變綠（探針壞了被塞進通過）。
        """
        _internal_sm_route_identity(self.route.sudo()).write(
            {'access_app_id': 'cf-app-m2'})
        self.authenticate(self.alice_login, self.alice_login)
        res = self._sh_detail({'action': 'get'})
        self._assert_ok(res, 'detail 端點應該成功')
        route_payload = res['data']['routes'][0]
        self.assertNotIn('access_app_id', route_payload)
        self.assertNotIn('cf-app-m2', json.dumps(res))
        self.assertEqual(
            self.route.sudo().access_app_id, 'cf-app-m2',
            '這個結果不該出現：值若是空的，上面兩條 assertNotIn 就毫無資訊')


class TestShAccessTenantOperatorChain(_ShAccessEndpointFixture):
    """N 系列：租戶身分走完「寫入 → operator → 回存」整條鏈。

    #739 的教訓是這條鏈在租戶身分下可能**從來沒成功過**，而 su env 的測試看不見
    （`access_app_id` 由 module-level sentinel 保護，租戶 env 下最容易靜默失敗）。
    """

    def setUp(self):
        super().setUp()
        # 已 provision 的 SH：`_access_is_live` 要 tunnel_id ＋ hostname ＋
        # state ∈ (active, error)。tunnel_id 是身分錨點，只能在開閘的 create
        # 當下寫進去。
        self.sh_live = self._smart_home(
            self.w1, 'SH Live 768', state='active',
            tunnel_id='tun-768-%s' % uuid.uuid4().hex[:8])
        self.route_live = self._route(self.sh_live)

    def test_N1_tenant_write_reaches_the_operator_and_stores_the_app_id(self):
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock(_sync_result(app_id='app-n1'))
        with patch(MOCK_SH_GET_CLIENT, return_value=client):
            res = self._access_config(
                {'is_protected': True, 'login_google': True},
                smarthome=self.sh_live, route=self.route_live)
        self._assert_ok(res, '租戶開啟保護必須成功')
        self.assertTrue(
            client.sync_access_app.called,
            '租戶身分的寫入沒有打到 operator ⇒ 這條路徑等於不存在，'
            '而端點仍然回 success')
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['primary_hostname'],
                         '%s.woowtech.io' % self.sh_live.sudo().subdomain)
        self.assertIn('google', payload['login_idp_types'])
        self.env.invalidate_all()
        self.assertEqual(
            self.route_live.sudo().access_app_id, 'app-n1',
            'sync 回存在租戶 env 下失敗＝#739 的同構缺陷：sentinel 寫不進去，'
            '而端點仍回 success，之後每次 sync 都會在 Cloudflare 上另建一個 app')

    def test_N2_canonical_name_on_the_tenant_path_carries_the_env_token(self):
        """租戶路徑送出去的命名根也要帶 env token。

        canonical_name 是整組 Access 物件裡**唯一**穩定帶得動 env token 的欄位；
        少了它，stg 建出來的 CF 物件無法自證環境歸屬（orphan 盤點時分不出來）。
        """
        self.ICP.set_param('woow_paas_platform.deploy_env', 'stg')
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_SH_GET_CLIENT, return_value=client):
            self._access_config({'is_protected': True},
                                smarthome=self.sh_live, route=self.route_live)
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(
            payload['canonical_name'].startswith('paas-stg-sm-'),
            'CF reusable policy 的前綴沒帶 env token，得到 %r'
            % payload['canonical_name'])
        self.assertNotEqual(
            payload['canonical_name'], self.sh_live.sudo().subdomain,
            'canonical_name 不得等於 stored subdomain——後者是 env-token-FREE '
            '算出來存的，拿它當前綴在 prod 逐位元組相同、到 stg 才錯')

    def test_N3_tenant_policy_create_reaches_the_operator(self):
        _sm_skip_access_sync(self.route_live.sudo()).write(
            {'is_protected': True})
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock(_sync_result(app_id='app-n3',
                                         policy_ids=['cf-n3']))
        with patch(MOCK_SH_GET_CLIENT, return_value=client):
            res = self._access_policies({
                'action': 'create', 'name': 'n3', 'decision': 'allow',
                'route_id': self.route_live.id,
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'n3@example.com'}],
            }, smarthome=self.sh_live)
        self._assert_ok(res, '租戶建 policy 必須成功')
        self.assertTrue(
            client.sync_access_app.called,
            '租戶建的 policy 沒有推到 Cloudflare ⇒ 設定了等於沒設定')
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn('n3', [p['name'] for p in payload['policies']])
        self.env.invalidate_all()
        pol = self.Policy.sudo().search([('name', '=', 'n3')])
        self.assertEqual(
            pol.cf_policy_id, 'cf-n3',
            'CF 回的 reusable policy id 要在租戶路徑上落地，否則之後刪不掉它'
            '（operator 唯一的刪除通道就是 existing_policy_ids）')

    def test_N4_operator_failure_surfaces_and_does_not_persist(self):
        """sync 失敗 ⇒ 端點誠實回錯，且 DB 不得留下「已保護」。

        沒有這條，route.write 的 savepoint 可能在 controller 這一層被 `except`
        吃掉：回 `success: False` 而交易照樣 commit ＝ DB 說已保護、Cloudflare
        上什麼都沒有（fail-open 方向的靜默分歧）。
        """
        self.authenticate(self.alice_login, self.alice_login)
        client = MagicMock()
        client.sync_access_app.side_effect = RuntimeError('operator down')
        with patch(MOCK_SH_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': True},
                                      smarthome=self.sh_live,
                                      route=self.route_live)
        self.assertFalse(res.get('success'), 'sync 失敗不得回 success')
        self.env.invalidate_all()
        self.assertFalse(
            self.route_live.sudo().is_protected,
            '失敗後 DB 留下「已保護」＝租戶看到已保護、Cloudflare 上沒有閘門')


class TestShChangePlanTemplateGate(_ShAccessEndpointFixture):
    """O 系列（#768 新加）：change-plan 的「plan 必須屬於 SH template」閘。

    為什麼是本單一起補：`ac_max_access_seats` 在本單之前只是計費欄位，換到別家
    的方案是「計費不一致」；本單把它變成**能力 gate**（Access 分頁的顯示判準）
    之後，同一條路徑就升級成**能力越權**——租戶可以把自己的 SH 換到任何帶席位
    的變體，包含 CS／SA 家族那 222 筆。
    """

    def setUp(self):
        super().setUp()
        self.plan_in = self.Plan.sudo().create({
            'name': 'SH O Plan In 768',
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })
        self.template = self.env[
            'woow_paas_platform.smart_home_template'].sudo().create({
                'name': 'SH O Tmpl 768',
                'slug': 'sh-o-768-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.plan_in.id])],
            })
        # 存在、但不屬於**任何** active SH template 的方案（CS／SA 家族的變體
        # 在真實資料裡就是這個形狀）。
        self.plan_foreign = self.Plan.sudo().create({
            'name': 'SH O Plan Foreign 768',
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': 999,
            'sh_no_of_billing_cycle': -1,
            'ac_max_access_seats': 50,
        })
        self.plan_now, self.sub = self._subscribe(self.sh)

    def test_O1_plan_outside_any_active_sh_template_is_rejected(self):
        self.authenticate(self.alice_login, self.alice_login)
        res = self._change_plan(self.plan_foreign.id)
        self.assertFalse(res.get('success'), '不屬於任何 SH template 的方案'
                                             '不該換得過去')
        self.assertIn(
            'not available for Smart Home', self._error_text(res),
            '歸因：必須是 template 閘。回 %r 的話可能是 role gate／「No active '
            'subscription」頂替上來，而那些與「閘有效」在 falsy 上不可區分'
            % (res,))
        self.env.invalidate_all()
        self.assertEqual(
            self.sub.sudo().sh_subscription_plan_id, self.plan_now,
            '被拒的 change-plan 不得留下任何訂閱異動')
        self.assertEqual(self.sub.sudo().state, 'in_progress')

    def test_O2_inactive_template_does_not_launder_a_plan(self):
        """閘讀的是 **active** template——下架的 template 不得繼續當通行證。"""
        self.template.sudo().is_active = False
        self.authenticate(self.alice_login, self.alice_login)
        res = self._change_plan(self.plan_in.id)
        self.assertFalse(res.get('success'))
        self.assertIn('not available for Smart Home', self._error_text(res))

    def test_O3_plan_inside_an_active_template_still_goes_through(self):
        """陽性對照：template 底下的方案必須換得過去。

        沒有它，把閘改成「一律拒絕」O1／O2 照樣綠——那是最容易寫出來的壞閘。

        下游的計費機器（發票／錢包／`sh_subscription_confirm`）在這裡 stub 掉：
        它有自己的測試，而把它拉進來會讓本條的紅燈同時代表三件事。stub 的是
        **閘之後**的那一步，所以閘本身仍是被真的執行到的。
        """
        self.authenticate(self.alice_login, self.alice_login)
        with patch.object(
            type(self.sh), '_create_and_confirm_subscription',
            return_value=self.sub,
        ) as mocked:
            res = self._change_plan(self.plan_in.id)
        self._assert_ok(res, 'template 底下的方案必須換得過去')
        self.assertEqual(res['data']['new_plan'], self.plan_in.name)
        self.assertTrue(
            mocked.called,
            '端點回 success 卻沒有走到開新訂閱那一步 ⇒ 它只是提早 return')
        # 用 `in` 而不是固定索引：`patch.object` 換上去的 MagicMock 不是
        # descriptor（`__get__` 不在 MagicMock 的預設魔法方法裡），所以 `self`
        # 到底有沒有被帶進 call_args 取決於 mock 的實作細節——把斷言綁在那上面，
        # 未來 mock 改行為時會紅在一個與本測試無關的地方。
        self.assertIn(
            self.plan_in, mocked.call_args.args,
            '開的必須是使用者選的那個方案（實際傳入 %r）'
            % (mocked.call_args.args,))

    def test_O4_change_plan_is_owner_admin_only(self):
        """role gate 擋得住換方案，而且**排在 template 閘之前**。

        本單新加的 template 閘就插在 role gate 後面，順序寫反不會讓任何既有測試
        變紅：兩者都回 falsy，結果看起來一樣。差別在**訊息**——順序反了的話，
        一個 role=user 成員送一個不屬於 SH 的方案會收到「not available for
        Smart Home」，也就是端點替他查了一次方案目錄的歸屬並把結果告訴他，而他
        本來連問的資格都沒有。所以被擋那兩格另外送一次 `plan_foreign` 當判別式。
        """
        for label, login, allowed in self._role_matrix():
            with self.subTest(role=label):
                # 🔴 每一輪都把訂閱推回 in_progress：上一輪成功的 change-plan 會
                # 把它關成 `close`，而下一輪就會撞到「No active subscription」
                # 而不是 role gate ⇒ owner 之後的每一格都在測別的東西
                # （admin 那格會紅、user／guest 那兩格會為了錯的理由變綠）。
                self.sub.sudo().write({'state': 'in_progress'})
                self.authenticate(login, login)
                with patch.object(
                    type(self.sh), '_create_and_confirm_subscription',
                    return_value=self.sub,
                ):
                    res = self._change_plan(self.plan_in.id)
                if allowed:
                    self._assert_ok(res, '%s 應該換得動方案' % label)
                    continue
                self._assert_denied(res, '%s 不得換方案' % label)
                self._assert_denied(
                    self._change_plan(self.plan_foreign.id),
                    '%s 送一個不屬於 SH 的方案，回的必須還是 role gate——'
                    '收到 template 閘的訊息代表兩道閘的順序反了' % label)


@tagged('post_install', '-at_install')
class TestShAccessQuotaBffSudoCrossUser1053(_ShAccessEndpointFixture):
    """C 系列（#1053）：`_create_sh_access_policy` 那顆新 `sudo()` 的
    cross-user negative。

    `repo-lint` 的 `controller-sudo` gate 要求「controller 每多一個 `sudo()`，
    就要有一格 HttpCase cross-user negative test」（判準＝
    `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` 的 carve-out
    三格）。本組就是 SH 錨點的那一格。

    ⚠ **與 K 系列的分工**（不是重複，但有一格是刻意的重疊）：

    * `K3` 已經測「別台 SH 的池不吃本台額度」，但它的外池 `sh_b` 在**同一個
      workspace**（同租戶、alice 自己的另一台）⇒ 那一格在 sudo 之前就會綠，
      因為 ir.rule 本來就讓 alice 看得到自己 workspace 的整池。
      `C1` 把外池搬到 **bob 的 W2**——只有加了 sudo 之後，計數才「看得到」那
      些列，也才有機會把它們錯算進來。這是 K3 照不到的那一半。
    * `C2` 與 `K2` 測同一個性質（自己的池額滿仍照擋），**刻意保留**：它是 C1
      在同一個 class 內的陽性對照——少了它，一個「sudo 之後恆回 0」的壞實作也
      會讓 C1 綠。⛔ 不要因為「K2 有了」就把它拿掉，那會讓 C1 的鑑別力依賴另一
      個 class 的存續。
    * `C3` 全新：非成員打同一個端點，不得從錯誤訊息得知配額狀態。

    ⚠ **這三格不是 RED 判別式**，理由同 CS 那組（可見度 0-或-全、呼叫者必然是
    成員）⇒ 修前修後都綠是預期的。⛔ 不要為了湊 RED 放寬斷言。
    """

    def _pool(self, smarthome):
        """池計數一律以 superuser 讀——這是真值，不是呼叫者視角。

        用 `smart_home_id`（SH 級池欄位，`_ANCHOR_POOL_FIELD` 登記的那根、也是
        model 端 `_check_policy_quota` 數的同一根），不是 `smart_home_route_id`。
        """
        return self.Policy.sudo().search_count(
            [('smart_home_id', '=', smarthome.id)])

    # ------------------------------------------------------------------ #
    def test_C1_another_tenants_pool_is_not_counted_against_this_smarthome(
            self):
        """sudo 只放寬可見度，**不放寬 domain**：別的**租戶**的池不算進來。

        K3 的外池在同 workspace，未 sudo 時 alice 本來就看得到 ⇒ 那一格對這顆
        sudo 沒有鑑別力。本格把外池放到 bob 的 W2：只有 sudo 之後計數才「看得
        到」那些列，domain 一被放寬就會誤擋。
        """
        sh_foreign = self._smart_home(self.w2, 'SH W2 1053')
        route_foreign = self._route(sh_foreign)
        for i in range(5):
            self._policy(route_foreign, name='c1-foreign-%d' % i)
        self._policy(self.route, name='c1-mine')
        self._subscribe(self.sh, sa_max_access_policies=2)
        self.assertEqual(self._pool(sh_foreign), 5, '前提：外池要夠大')
        self.assertEqual(self._pool(self.sh), 1, '前提：自己的池 1 條')

        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload('c1-second'))
        self._assert_ok(res, '別的租戶的 5 條不得吃掉這台 SH 的額度')
        self.assertEqual(self._pool(self.sh), 2)
        self.assertEqual(self._pool(sh_foreign), 5,
                         '外池不得被這次請求動到')

    def test_C2_own_pool_still_hits_the_quota(self):
        """自己的池額滿仍照擋——C1 的同 class 陽性對照（與 K2 同性質，刻意保留）。"""
        self._policy(self.route, name='c2-first')
        self._subscribe(self.sh, sa_max_access_policies=1)
        before = self._pool(self.sh)

        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies(self._create_policy_payload('c2-overflow'))
        self.assertFalse(res.get('success'), '超額的第二條沒被擋（回 %r）' % (res,))
        self.assertIn(
            'quota', self._error_text(res).lower(),
            '歸因：必須是配額那條。回 %r 的話可能是 role gate／arc／#352 守衛'
            '頂替上來，而那幾者與「配額有效」在 falsy 上不可區分' % (res,))
        self.assertEqual(self._pool(self.sh), before,
                         '被擋的請求不得留下任何 policy')

    def test_C3_non_member_denied_at_the_gate_and_learns_nothing(self):
        """🔴 gate 要求的 cross-user negative：非成員被擋，且**拿不到配額狀態**。

        alice 的 SH 已額滿。bob（W2 的 owner、W1 的完全非成員）打同一個端點：
        必須停在 `_get_workspace_or_error`，⛔ 錯誤訊息**不得**是配額那條。
        """
        self._policy(self.route, name='c3-first')
        self._subscribe(self.sh, sa_max_access_policies=1)
        before = self._pool(self.sh)
        self.assertFalse(
            self.w1.sudo().access_ids.filtered(
                lambda a: a.user_id == self.bob),
            '前提：bob 不得是 W1 成員，否則本格零鑑別力')

        self.authenticate(self.bob_login, self.bob_login)
        res = self._access_policies(self._create_policy_payload('c3-injected'))
        self.assertTrue(isinstance(res, dict), '端點爆掉而不是結構化拒絕：%r' % (res,))
        self.assertFalse(res.get('success'), '非成員沒被擋（回 %r）' % (res,))
        self.assertNotIn(
            'quota', self._error_text(res).lower(),
            '非成員不得從錯誤訊息得知這台 SH 的配額狀態（%r）' % (res,))
        self.assertEqual(self._pool(self.sh), before,
                         '被拒的請求不得建出任何 policy')
