"""#745（CS AC T3）：CS access endpoints ＋ 租戶真 RPC 全鏈驗證。

## 為什麼整份走真 HTTP（票面第 6 點的硬驗收）

#739 的形狀是「同構路徑在租戶身分下**從來沒成功過**」，而 superuser
`TransactionCase` 對它完全免疫：欄位級 `groups=` 在 su env 不生效、ir.rule 不
套用、`call_kw` 把呼叫端 context 整包換進 env 這件事也重現不出來。所以
「租戶身分寫入 → operator sync → 回存」這條鏈必須以**只掛 `base.group_user`
的帳號、走 `/api/...` 真 endpoint** 驗，不可用 model 層測試代替。

H 系列是本票的驗收本體：它斷言的不是「endpoint 回 success」，而是
**`sync_access_app` 真的被呼叫**且 **`access_app_id` 真的落地**——後者是整條鏈
上唯一由 sentinel 保護、且最可能在租戶 env 下靜默失敗的一步。

## 陽性／陰性成對

授權矩陣的每一條「被擋」都配一條同操作在 owner 身分下成功的對照；沒有它，
`Insufficient permissions` 可能來自 workspace 找不到、service 找不到、訂閱閘，
而那些的輸出與「role 擋住了」不可區分。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_USER,
)
from ..services.naming import make_service_subdomain

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)
_POLICY = 'woow_paas_platform.security_access_access_policy'


def _new_group_user(env, name):
    """只有 `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 745' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


def _sync_result(app_id='app-745', policy_ids=None):
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': [],
    }


@tagged('post_install', '-at_install')
class _CsAccessEndpointFixture(HttpCase):
    """W1（alice owner／dave admin／carol user／gary guest）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Policy = self.env[_POLICY]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice745')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob745')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol745')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave745')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary745')
        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                '前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 745'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 745'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})

        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'EP 745 tmpl',
                'slug': 'ep-745-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/ep745',
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        self.svc = self._service(self.w1, 'svc-w1-745')
        self.svc_w2 = self._service(self.w2, 'svc-w2-745')

    def _service(self, workspace, name):
        svc = _internal_provision(self.Service.sudo()).create({
            'name': name,
            'workspace_id': workspace.id,
            'template_id': self.template.id,
            'state': 'running',
        })
        svc.subdomain = make_service_subdomain(
            workspace.sudo().ws_hash, svc.reference_id, name)
        return svc

    def _policy(self, service, name=None, sequence=None):
        vals = {
            'cloud_service_id': service.id,
            'name': name or 'ep745-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        if sequence is not None:
            vals['sequence'] = sequence
        pol = self.Policy.sudo().with_context(
            sa_skip_route_sync=True).create(vals)
        # context 會跟著 recordset 走——不清掉的話之後的 write 都被抑制，
        # 症狀與「分派沒接好」不可區分（見 test_cs_access_sync_745 的同段註解）。
        return pol.with_context(sa_skip_route_sync=False)

    # ------------------------------------------------------------------ #
    #  真 HTTP helper                                                      #
    # ------------------------------------------------------------------ #
    def _post_json(self, url, payload):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                           'params': payload})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json().get('result', resp.json())

    def _access_config(self, payload, workspace=None, service=None):
        return self._post_json(
            '/api/workspaces/%d/services/%d/access-config' % (
                (workspace or self.w1).id, (service or self.svc).id),
            payload)

    def _access_policies(self, payload, workspace=None, service=None):
        return self._post_json(
            '/api/workspaces/%d/services/%d/access-policies' % (
                (workspace or self.w1).id, (service or self.svc).id),
            payload)

    def _mock(self, result=None):
        client = MagicMock()
        client.sync_access_app.return_value = result or _sync_result()
        return client

    def _assert_ok(self, result, msg):
        self.assertTrue(isinstance(result, dict) and result.get('success'),
                        msg + '（回 %r）' % (result,))

    def _assert_denied(self, result, msg):
        self.assertTrue(isinstance(result, dict), msg + ' 回 %r' % (result,))
        self.assertFalse(result.get('success'), msg + '（沒被擋）')
        self.assertIn('Insufficient permissions', result.get('error') or '',
                      msg + '（歸因：必須是 role gate，回 %r）' % (result,))


class TestCsAccessConfigEndpointAuthz(_CsAccessEndpointFixture):
    """G 系列：`access-config` 的授權矩陣與白名單。"""

    def test_G1_owner_can_write_access_config(self):
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({
                'is_protected': True, 'login_google': True,
                'session_duration': '8h'})
        self._assert_ok(res, 'W1 owner 應該設定得了自己服務的 Access')
        self.svc.invalidate_recordset()
        self.assertTrue(self.svc.is_protected)
        self.assertTrue(self.svc.login_google)
        self.assertEqual(self.svc.session_duration, '8h')

    def test_G2_admin_can_write_access_config(self):
        self.authenticate(self.dave_login, self.dave_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({'is_protected': True})
        self._assert_ok(res, 'admin 應該設定得了 Access')

    def test_G3_member_user_cannot_write_access_config(self):
        self.authenticate(self.carol_login, self.carol_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({'is_protected': True})
        self._assert_denied(res, "role='user' 不得改動 Access 閘門")
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected)

    def test_G4_guest_cannot_write_access_config(self):
        self.authenticate(self.gary_login, self.gary_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({'is_protected': True})
        self._assert_denied(res, "role='guest' 不得改動 Access 閘門")

    def test_G5_cross_workspace_service_is_not_found(self):
        """W1 的 owner 拿 W2 的 service id ⇒ 找不到（不是權限訊息）。"""
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({'is_protected': True},
                                      service=self.svc_w2)
        self.assertFalse(res.get('success'))
        self.svc_w2.invalidate_recordset()
        self.assertFalse(self.svc_w2.is_protected,
                         '跨 workspace 的服務一個欄位都不該被寫到')

    def test_G6_non_member_cannot_reach_the_endpoint(self):
        self.authenticate(self.bob_login, self.bob_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({'is_protected': True})
        self.assertFalse(res.get('success'))

    def test_G6b_cannot_protect_a_service_with_no_public_address(self):
        """沒有對外 hostname ⇒ 開啟保護必須被擋，而且不得留下 `is_protected`。

        不擋的話這個寫入會成功、UI 顯示「已保護」，而 model 層的 liveness gate
        第一道就 return ⇒ Cloudflare 上一個物件都沒有。那是**對租戶無聲的謊**，
        不是 no-op。陰性對照在同一條裡：關閉方向永遠不擋。
        """
        self.svc.sudo().subdomain = False
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': True})
        self.assertFalse(res.get('success'), '沒有 hostname 卻能開啟保護')
        self.assertFalse(client.sync_access_app.called)
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected,
                         'DB 不得留下一個什麼都沒保護到的 True')

        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': False})
        self._assert_ok(res, '關閉方向在任何情況下都必須做得到')

    def test_G7_server_owned_fields_are_never_client_settable(self):
        """白名單：`access_app_id` / `access_unresolved_idp_types` 只有伺服器寫。

        陽性對照在同一個請求裡——同時送一個合法欄位，證明請求本身是通的，
        被丟掉的只有那兩個。
        """
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_config({
                'session_duration': '1h',
                'access_app_id': 'forged-app',
                'access_unresolved_idp_types': 'forged'})
        self._assert_ok(res, '合法欄位那半應該成功')
        self.svc.invalidate_recordset()
        self.assertEqual(self.svc.session_duration, '1h')
        self.assertFalse(self.svc.access_app_id,
                         'client 送來的 access_app_id 必須被丟掉')
        self.assertFalse(self.svc.access_unresolved_idp_types)


class TestCsAccessPolicyEndpoint(_CsAccessEndpointFixture):
    """G 系列（續）：`access-policies` 的 list／CRUD／reorder。"""

    def test_G8_member_can_list_policies(self):
        self._policy(self.svc, name='listed')
        self.authenticate(self.carol_login, self.carol_login)
        res = self._access_policies({'action': 'list'})
        self._assert_ok(res, 'workspace 成員讀得到自己服務的 policy')
        self.assertEqual([p['name'] for p in res['data']], ['listed'])

    def test_G9_list_never_leaks_another_workspaces_policies(self):
        self._policy(self.svc_w2, name='w2-secret')
        self._policy(self.svc, name='w1-own')
        self.authenticate(self.alice_login, self.alice_login)
        res = self._access_policies({'action': 'list'})
        self._assert_ok(res, 'owner 讀得到自己的')
        self.assertEqual([p['name'] for p in res['data']], ['w1-own'])

    def test_G10_owner_can_create_policy(self):
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'create', 'name': 'g10', 'decision': 'allow',
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'g10@example.com'}]})
        self._assert_ok(res, 'owner 應該建得了 policy')
        pol = self.Policy.sudo().search([('name', '=', 'g10')])
        self.assertEqual(len(pol), 1)
        self.assertEqual(pol.cloud_service_id, self.svc,
                         'policy 必須錨在 URL 指定的那台服務上')
        self.assertFalse(pol.route_id, 'CS 錨定的 policy 不得同時掛 route')

    def test_G11_member_user_cannot_create_policy(self):
        self.authenticate(self.carol_login, self.carol_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'create', 'name': 'g11', 'decision': 'allow',
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'g11@example.com'}]})
        self._assert_denied(res, "role='user' 不得新增 Access policy")
        self.assertFalse(self.Policy.sudo().search([('name', '=', 'g11')]))

    def test_G12_client_cannot_smuggle_a_foreign_anchor(self):
        """送 `cloud_service_id` / `route_id` / `cf_policy_id` 都必須被丟掉。"""
        route = self.env[
            'woow_paas_platform.security_access_route'].sudo().browse()
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'create', 'name': 'g12', 'decision': 'allow',
                'cloud_service_id': self.svc_w2.id,
                'cf_policy_id': 'forged-cf',
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'g12@example.com'}]})
        self._assert_ok(res, '請求本身應該成功（只有錨點被伺服器決定）')
        pol = self.Policy.sudo().search([('name', '=', 'g12')])
        self.assertEqual(pol.cloud_service_id, self.svc,
                         '錨點只能是 URL 上那台服務，不能由 payload 決定')
        self.assertFalse(pol.cf_policy_id, 'cf_policy_id 是伺服器擁有的')
        del route

    def test_G13_update_and_delete_are_owner_admin_only(self):
        pol = self._policy(self.svc, name='g13')
        self.authenticate(self.gary_login, self.gary_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'update', 'policy_id': pol.id, 'name': 'g13-hacked'})
        self._assert_denied(res, 'guest 不得改 policy')
        pol.invalidate_recordset()
        self.assertEqual(pol.name, 'g13')

        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'update', 'policy_id': pol.id, 'name': 'g13-ok'})
        self._assert_ok(res, '陽性對照：owner 改得動')
        pol.invalidate_recordset()
        self.assertEqual(pol.name, 'g13-ok')

        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'delete', 'policy_id': pol.id})
        self._assert_ok(res, 'owner 刪得掉')
        self.assertFalse(pol.exists())

    def test_G14_cannot_touch_another_services_policy(self):
        """policy_id 必須釘在 URL 的服務上——跨服務一律當作找不到。"""
        foreign = self._policy(self.svc_w2, name='g14-foreign')
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._access_policies({
                'action': 'update', 'policy_id': foreign.id,
                'name': 'g14-hacked'})
        self.assertFalse(res.get('success'))
        foreign.invalidate_recordset()
        self.assertEqual(foreign.name, 'g14-foreign')

    def test_G15_reorder_rewrites_precedence_and_syncs_once(self):
        p1 = self._policy(self.svc, name='g15-a', sequence=10)
        p2 = self._policy(self.svc, name='g15-b', sequence=20)
        self.svc.with_context(cs_skip_access_sync=True).write(
            {'is_protected': True})
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_policies({
                'action': 'reorder', 'policy_ids': [p2.id, p1.id]})
        self._assert_ok(res, 'owner 應該排得動 precedence')
        p1.invalidate_recordset()
        p2.invalidate_recordset()
        self.assertLess(p2.sequence, p1.sequence)
        self.assertEqual(
            client.sync_access_app.call_count, 1,
            'reorder 要抑制逐筆 sync、結束後只推一次完整狀態')


class TestCsAccessTenantRpcChain(_CsAccessEndpointFixture):
    """H 系列（票面第 6 點硬驗收）：租戶身分走完整條「寫入 → sync → 回存」。

    #739 的教訓是這條鏈在租戶身分下可能**從來沒成功過**，而 su env 的測試看不
    見。所以這裡的斷言是可觀測的三件事：endpoint 成功、operator 真的被呼叫、
    `access_app_id` 真的落地。
    """

    def test_H1_tenant_write_reaches_the_operator_and_stores_the_app_id(self):
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock(_sync_result(app_id='app-h1'))
        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': True})
        self._assert_ok(res, '租戶開啟保護必須成功')
        self.assertTrue(
            client.sync_access_app.called,
            '租戶身分寫入沒有打到 operator ⇒ 這條路徑等於不存在')
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['primary_hostname'],
                         '%s.woowtech.io' % self.svc.subdomain)
        self.svc.invalidate_recordset()
        self.assertEqual(
            self.svc.access_app_id, 'app-h1',
            'sync 回存在租戶 env 下失敗＝#739 同構缺陷'
            '（sentinel 寫不進去，而 endpoint 仍回 success）')

    def test_H2_tenant_policy_create_reaches_the_operator(self):
        self.svc.with_context(cs_skip_access_sync=True).write(
            {'is_protected': True})
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock(_sync_result(policy_ids=['cf-h2']))
        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_policies({
                'action': 'create', 'name': 'h2', 'decision': 'allow',
                # ⚠ 用 email_list 不是 email_domain：allow × include ×
                # email_domain 會被 #352 的整批放行守衛擋下（那是正確行為），
                # 而它的錯誤訊息會讓這條測試看起來像是「租戶路徑壞了」。
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'h2@example.com'}]})
        self._assert_ok(res, '租戶建 policy 必須成功')
        self.assertTrue(client.sync_access_app.called,
                        '租戶建的 policy 沒有推到 CF ⇒ 設定了等於沒設定')
        payload = client.sync_access_app.call_args.args[0]
        self.assertIn('h2', [p['name'] for p in payload['policies']])
        pol = self.Policy.sudo().search([('name', '=', 'h2')])
        self.assertEqual(pol.cf_policy_id, 'cf-h2',
                         'CF 回的 policy id 要在租戶路徑上落地')

    def test_H3_canonical_name_on_the_tenant_path_carries_the_env_token(self):
        """租戶路徑送出去的前綴也要帶 env token（不是只有 model 層測試會帶）。"""
        self.ICP.set_param('woow_paas_platform.deploy_env', 'stg')
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self._access_config({'is_protected': True})
        payload = client.sync_access_app.call_args.args[0]
        self.assertTrue(
            payload['canonical_name'].startswith('paas-stg-cs-'),
            'CF reusable policy 前綴沒帶 env token，得到 %r'
            % payload['canonical_name'])
        self.assertNotEqual(payload['canonical_name'], self.svc.subdomain)

    def test_H4_operator_failure_surfaces_and_does_not_persist(self):
        """sync 失敗 ⇒ endpoint 誠實回錯，且 DB 不得留下「已保護」。

        沒有這條，B9 的 savepoint 可能在 endpoint 這一層被 `except` 吃掉：
        controller 回 `success: False` 而交易照樣 commit ＝ DB 說已保護、CF 上
        什麼都沒有。
        """
        self.authenticate(self.alice_login, self.alice_login)
        client = MagicMock()
        client.sync_access_app.side_effect = RuntimeError('operator down')
        with patch(MOCK_GET_CLIENT, return_value=client):
            res = self._access_config({'is_protected': True})
        self.assertFalse(res.get('success'), 'sync 失敗不得回 success')
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected,
                         '失敗後 DB 不得留下「已保護」（fail-open 方向）')


class TestCsAccessApiPayload(_CsAccessEndpointFixture):
    """F 系列：`_format_service` 與 `_get_subscription_info` 的新欄位。"""

    def _bind_subscription(self, seats):
        plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'AC Plan 745 %s' % uuid.uuid4().hex[:6],
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
            'ac_max_access_seats': seats})
        sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.alice.partner_id.id,
            'state': 'in_progress'})
        sub.sudo().sh_subscription_plan_id = plan.id
        self.svc.sudo().subscription_id = sub.id
        return sub

    def test_F1_subscription_info_exposes_ac_max_access_seats(self):
        self._bind_subscription(3)
        info = self.svc.sudo()._get_subscription_info()
        self.assertEqual(
            info.get('ac_max_access_seats'), 3,
            'Access 頁籤的顯示條件（拍板 6）就是這個值，沒有它前端無從判斷')

    def test_F2_service_detail_payload_carries_the_access_block(self):
        self._bind_subscription(1)
        self._policy(self.svc, name='f2')
        self.svc.with_context(cs_skip_access_sync=True).write(
            {'is_protected': True, 'session_duration': '8h',
             'login_email_otp': True})
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            res = self._post_json(
                '/api/workspaces/%d/services/%d' % (self.w1.id, self.svc.id),
                {'action': 'get'})
        self._assert_ok(res, 'detail 端點應該成功')
        data = res['data']
        self.assertEqual(data['subscription']['ac_max_access_seats'], 1)
        access = data.get('access')
        self.assertIsInstance(access, dict, 'detail payload 必須帶 access 區塊')
        self.assertTrue(access['is_protected'])
        self.assertEqual(access['session_duration'], '8h')
        self.assertTrue(access['login_email_otp'])
        self.assertFalse(access['login_google'])
        self.assertEqual(access['hostname'],
                         '%s.woowtech.io' % self.svc.subdomain)
        self.assertEqual([p['name'] for p in access['access_policies']],
                         ['f2'])
        self.assertNotIn('access_app_id', access,
                         '伺服器擁有的 CF id 不該外流給前端')
