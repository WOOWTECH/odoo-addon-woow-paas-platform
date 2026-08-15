"""#744（CS AC T2）：CS 錨定 Access policy 的 ir.rule dual-leg 授權矩陣。

## 守什麼

policy 的錨點變成 exclusive arc 之後，`security_access_id` 在 CS 錨定的列上恆
空。原本的三條 rule 全部只走那一腿 ⇒ **CS 錨定的每一列對誰都不可見**（fail-
closed）。修法是把 workspace walk 改成 dual-leg（route 腿 OR CS 腿），而
dual-leg 有兩個方向都會壞：

* 補太少（漏了 CS 腿、或漏了 sub-model）⇒ 租戶連自己那台服務的 policy 都讀不到；
* 補太寬（CS 腿忘了 role 條件、或把 `access_ids.role` 寫成點鏈）⇒ 一個
  `role='guest'` 成員就能改動 Cloudflare 的 allow/deny 閘門。**點鏈是跨列匹配**
  （A 列滿足 user_id、B 列滿足 role），一個 workspace 只要同時有 owner 和 guest
  兩列，guest 就通過 ⇒ 等於沒有 role 條件（#556 根因之一）。W1 因此刻意掛滿
  owner/admin/user/guest 四列——少一列，兩種寫法的行為就一樣，測試會零鑑別力。

## TDD 紀律（照抄 #556 / #658 / #676）

* **全部走真 HTTP RPC**（`/web/dataset/call_kw`）。ir.rule 對 `TransactionCase`
  的 superuser env 不生效，直呼 model 也繞開真實授權路徑；#739 的教訓是同一族
  ——「只在 su env 驗過」與「這條路徑從來沒成功過」在測試輸出上不可區分。
* fixture user 一律只掛 `base.group_user`（setUp 有前提斷言）。
* **否定性斷言一律配陽性對照**：斷言「被擋下來」之前，先證明同一個操作在
  owner 身分下會成功——否則 AccessError 可能來自欄位不存在／記錄不存在／
  constraint，而「擋住了」跟「探針壞了」輸出長得一模一樣。
* **route 錨定的回歸對照**與 CS 錨定成對出現：dual-leg 改壞既有那一腿的話，
  只測 CS 的話會全綠。
* 每次 RPC 後 `invalidate_all()`（helper 內建）：`assertRaises` 之前若先讀過
  同一筆記錄，值會被餵進 env cache 而遮蔽 AccessError。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)

_POLICY = 'woow_paas_platform.security_access_access_policy'
_POLICY_RULE = 'woow_paas_platform.security_access_access_policy_rule'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 744' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _CsAccessAclFixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Policy = self.env[_POLICY]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice744')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob744')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol744')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave744')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary744')

        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                '前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 744'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 744'})
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            '前提：W1 必須同時掛 owner/admin/user/guest 四列'
            '（少了 admin 那列，跨列匹配的 domain 寫法就不會被抓到）')

        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'ACL 744 tmpl',
                'slug': 'acl-744-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/acl744',
                # ⚠ 必須釘版，否則 fixture 會死在供給閘上，而那個 error 長得
                # 跟「rule 擋住了」一模一樣。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })
        self.svc_w1 = self._service(self.w1, 'svc-w1-744')
        self.svc_w2 = self._service(self.w2, 'svc-w2-744')
        # route 錨定的回歸對照（W1 底下）
        self.sa_w1 = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA W1 744', 'workspace_id': self.w1.id})
        self.route_w1 = self.env[
            'woow_paas_platform.security_access_route'].sudo().create({
                'security_access_id': self.sa_w1.id,
                'subdomain_prefix': 'acl744-%s' % uuid.uuid4().hex[:6]})

        self.cs_policy = self._policy(cloud_service_id=self.svc_w1.id)
        self.route_policy = self._policy(route_id=self.route_w1.id)
        self.cs_policy_w2 = self._policy(cloud_service_id=self.svc_w2.id)

    # ------------------------------------------------------------------ #
    #  fixture builders（一律 sudo：fixture 不該被受測的 rule 擋住）        #
    # ------------------------------------------------------------------ #
    def _service(self, workspace, name):
        return _internal_provision(self.Service.sudo()).create({
            'name': name,
            'workspace_id': workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'subdomain': '%s-%s' % (name, uuid.uuid4().hex[:6]),
        })

    def _policy(self, **anchor):
        vals = {
            'name': 'acl744-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        }
        vals.update(anchor)
        return self.Policy.sudo().create(vals)

    # ------------------------------------------------------------------ #
    #  RPC helper                                                         #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
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

    def _read_policy(self, policy):
        return self._call_kw(_POLICY, 'read', [[policy.id], ['name']])

    def _rename_policy(self, policy, new_name='renamed-744'):
        return self._call_kw(
            _POLICY, 'write', [[policy.id], {'name': new_name}])

    def _unlink_policy(self, policy):
        return self._call_kw(_POLICY, 'unlink', [[policy.id]])

    def _create_policy(self, **anchor):
        vals = {
            'name': 'rpc-744-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [[0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'b@example.com'}]],
        }
        vals.update(anchor)
        return self._call_kw(_POLICY, 'create', [vals])

    def _create_selector_rule(self, policy):
        return self._call_kw(_POLICY_RULE, 'create', [{
            'policy_id': policy.id, 'group': 'exclude',
            'rule_type': 'email_domain', 'rule_value': 'evil.com'}])

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                         #
    # ------------------------------------------------------------------ #
    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_denied(self, result, msg):
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % text)

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)

    def _assert_empty_read(self, result, msg):
        """讀取方向被 rule 擋下時，Odoo 的 `read` 會回 AccessError；
        `search_read` 則是回空集。兩種都算「讀不到」，但要**明確涵蓋**，
        否則哪一種行為都能讓斷言為了不同理由變綠。"""
        if 'error' in result:
            self.assertIn('AccessError', self._error_text(result), msg)
        else:
            self.assertEqual(result.get('result'), [], msg)


class TestCsAnchoredPolicyAcl(_CsAccessAclFixture):
    """E 系列：CS 錨定 policy 的 read/write 切分。"""

    # ---------------------------------------------------------------- #
    #  陽性對照——先證明這些操作在 owner/admin 身分下真的做得到           #
    # ---------------------------------------------------------------- #
    def test_E1_owner_can_manage_cs_anchored_policy(self):
        """owner：讀、改、建、刪四向全通（**這是 E3-E6 的陽性對照**）。"""
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._read_policy(self.cs_policy), 'owner 讀不到')
        self._assert_ok(self._rename_policy(self.cs_policy), 'owner 改不動')
        self._assert_ok(
            self._create_policy(cloud_service_id=self.svc_w1.id),
            'owner 建不了')
        self._assert_ok(self._unlink_policy(self.cs_policy), 'owner 刪不掉')

    def test_E2_admin_can_manage_cs_anchored_policy(self):
        """admin 與 owner 同權。少了這條，把 role 條件寫成 `['owner']` 不會紅。"""
        self.authenticate(self.dave_login, self.dave_login)
        self._assert_ok(self._rename_policy(self.cs_policy), 'admin 改不動')
        self._assert_ok(
            self._create_policy(cloud_service_id=self.svc_w1.id),
            'admin 建不了')

    # ---------------------------------------------------------------- #
    #  負向——同 workspace 的低權角色                                     #
    # ---------------------------------------------------------------- #
    def test_E3_member_user_can_read_but_not_write(self):
        """role=user：讀得到（BFF 的 list/get 需要），寫入四向全擋。"""
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(self._read_policy(self.cs_policy),
                        'role=user 應該讀得到（dual-leg 的 CS 腿漏了？）')
        self._assert_denied(self._rename_policy(self.cs_policy),
                            'role=user 不該改得動 Access policy')
        self._assert_denied(
            self._create_policy(cloud_service_id=self.svc_w1.id),
            'role=user 不該建得了 Access policy')
        self._assert_denied(self._unlink_policy(self.cs_policy),
                            'role=user 不該刪得掉 Access policy')

    def test_E4_guest_can_read_but_not_write(self):
        """role=guest 同上。guest 這一格是**跨列匹配**的判別式：把 domain
        寫成 `access_ids.role` 點鏈的話，W1 同時有 owner 列 ⇒ guest 會通過。"""
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_ok(self._read_policy(self.cs_policy), 'guest 應該讀得到')
        self._assert_denied(self._rename_policy(self.cs_policy),
                            'guest 改得動 Access policy ⇒ role 條件形同虛設')
        self._assert_denied(
            self._create_policy(cloud_service_id=self.svc_w1.id),
            'guest 建得了 Access policy ⇒ role 條件形同虛設')

    # ---------------------------------------------------------------- #
    #  負向——跨 workspace                                                #
    # ---------------------------------------------------------------- #
    def test_E5_cross_workspace_is_denied_both_directions(self):
        """W2 的 owner 對 W1 的 policy 讀不到也寫不動（隔離沒有被 dual-leg 放寬）。"""
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._call_kw(_POLICY, 'search_read',
                          [[['id', '=', self.cs_policy.id]], ['name']]),
            '跨 workspace 讀得到 W1 的 Access policy')
        self._assert_denied(self._rename_policy(self.cs_policy),
                            '跨 workspace 改得動 W1 的 Access policy')
        # 陽性對照：bob 對**自己** workspace 的那筆照樣管得到——否則上面兩條
        # 可能只是「bob 對這個 model 什麼都做不了」。
        self._assert_ok(self._rename_policy(self.cs_policy_w2),
                        'bob 對自己 workspace 的 policy 應該改得動')

    def test_E6_cannot_anchor_a_new_policy_on_someone_elses_service(self):
        """把新 policy 錨在別人的服務上 ⇒ 擋。

        create 方向的 domain 是對**新列**評估的，這一格證明 CS 腿沒有寫成
        永真（永真的話任何人都能往別人的服務塞一條 allow 規則）。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_denied(
            self._create_policy(cloud_service_id=self.svc_w2.id),
            'W1 的 owner 把 policy 錨在 W2 的服務上竟然成功')


class TestRouteAnchoredPolicyRegression(_CsAccessAclFixture):
    """F 系列：route 錨定那一腿的回歸鎖。

    dual-leg 是在既有 domain 上動刀——只測 CS 那腿的話，把 route 腿寫壞
    （例如兩腿都改成 CS）會全綠。
    """

    def test_F1_owner_still_manages_route_anchored_policy(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._read_policy(self.route_policy),
                        'owner 讀不到 route 錨定的 policy')
        self._assert_ok(self._rename_policy(self.route_policy),
                        'owner 改不動 route 錨定的 policy')

    def test_F2_member_user_still_blocked_on_route_anchored_policy(self):
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(self._read_policy(self.route_policy),
                        'role=user 應該讀得到 route 錨定的 policy')
        self._assert_denied(self._rename_policy(self.route_policy),
                            'role=user 改得動 route 錨定的 policy')


class TestPolicyRuleSubModelParity(_CsAccessAclFixture):
    """G 系列：selector rule 子模型必須與 parent 對等。

    [[unbound-project-acl-dual-leg]] 明列「sub-model 必須對等改」。這裡兩個
    方向都要測：漏改 ⇒ 租戶連自己那條 include 都建不出來（fail-closed）；
    改太寬 ⇒ guest 可以自己加一條 include 把受保護的主機打開，而 parent 那層
    的 role 條件完全擋不到（它擋的是 policy 表，不是 rule 表）。
    """

    def test_G1_owner_can_add_a_selector_rule_to_a_cs_anchored_policy(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(
            self._create_selector_rule(self.cs_policy),
            'owner 在 CS 錨定的 policy 底下建不了 selector rule '
            '（子模型的 CS 腿漏了？）')

    def test_G2_guest_cannot_add_a_selector_rule(self):
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_denied(
            self._create_selector_rule(self.cs_policy),
            'guest 在 CS 錨定的 policy 底下建得了 selector rule '
            '⇒ 可自行打開受保護主機')

    def test_G3_cross_workspace_cannot_read_selector_rules(self):
        self.authenticate(self.bob_login, self.bob_login)
        self._assert_empty_read(
            self._call_kw(_POLICY_RULE, 'search_read',
                          [[['policy_id', '=', self.cs_policy.id]], ['group']]),
            '跨 workspace 讀得到 W1 policy 的 selector rule')
        # 陽性對照：自己 workspace 的讀得到
        result = self._call_kw(
            _POLICY_RULE, 'search_read',
            [[['policy_id', '=', self.cs_policy_w2.id]], ['group']])
        self._assert_ok(result, 'bob 讀不到自己 workspace 的 selector rule')
        self.assertTrue(result.get('result'),
                        'bob 應該讀得到自己 workspace 的 selector rule')


class TestCsAccessAnchorOverTenantRpc(_CsAccessAclFixture):
    """H 系列：`access_app_id` 身分錨點在**真實租戶 RPC 路徑**上也擋得住。

    覆核意見（非阻塞）：C 系列全部跑在 superuser `TransactionCase`。守衛本身是
    `write()` override 的一般碼、判準只比對 context 值的物件身分，su 與否不影響
    ——但「不影響」是推導，這裡把它換成觀測：同一條 `/web/dataset/call_kw`
    路徑，同一個只掛 `base.group_user` 的租戶帳號。

    這也是 #522 當年真正被利用的那條路徑（`call_kw` 把呼叫端 context 整包換進
    env），su env 的測試在定義上碰不到它。

    ⚠ 歸因是這組的重點：alice 是 W1 的 owner，對這筆 cloud_service **有** ir.rule
    給的完整寫入權（H1 陽性對照證明），所以擋下來的必須是**身分錨點守衛的
    `UserError`**，不是 ir.rule 的 `AccessError`。只斷言「被擋了」的話，把守衛
    整段刪掉、改成任何一種讓她沒有寫入權的世界，測試照樣綠。
    """

    _CS = 'woow_paas_platform.cloud_service'

    def _write_service(self, vals, ctx=None):
        return self._call_kw(
            self._CS, 'write', [[self.svc_w1.id], vals],
            {'context': ctx} if ctx is not None else None)

    def _assert_anchor_guard_denied(self, result, msg):
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('UserError', text,
                      msg + '（歸因：必須是身分錨點守衛，不是 ir.rule 的 '
                            'AccessError，回 %r）' % text)
        self.assertIn('access_app_id', text,
                      msg + '（訊息要指名是哪個欄位，回 %r）' % text)

    def test_H1_owner_can_write_ordinary_access_fields(self):
        """陽性對照：同一個租戶、同一筆記錄、同一條 RPC，一般 access 欄位寫得動。

        沒有它，H2/H3 的「被擋」可能只是因為 alice 對這筆記錄根本沒有寫入權。

        #745 之後這個 write 會**真的**去打 operator（`is_protected` 進了
        `_CS_ACCESS_SYNC_FIELDS`），所以要 mock 掉出站呼叫。刻意**不**用
        `cs_skip_access_sync` 繞過：這條的全部價值就是「租戶身分的 write 從頭到
        尾走得完」，用 skip 會讓它少走 sync 那一段——而 sync 正是 #739 同構缺陷
        最可能落在的位置。
        """
        client = MagicMock()
        client.sync_access_app.return_value = {
            'app_id': 'app-h1-744', 'policy_ids': [], 'allowed_idps': [],
            'unresolved_idp_types': [], 'orphaned_policy_ids': []}
        self.authenticate(self.alice_login, self.alice_login)
        with patch('odoo.addons.woow_paas_platform.services.paas_operator.'
                   'get_paas_operator_client', return_value=client):
            result = self._write_service({'is_protected': True,
                                          'session_duration': '8h'})
        self._assert_ok(
            result, 'W1 owner 應該改得動自己服務的 access 設定')
        self.assertTrue(
            client.sync_access_app.called,
            '租戶身分的 access 寫入必須真的推到 operator（#745 H 系列同源）')
        self.svc_w1.invalidate_recordset()
        self.assertTrue(self.svc_w1.is_protected)

    def test_H2_tenant_cannot_move_the_access_app_anchor(self):
        """租戶經真 RPC 搬錨點 ⇒ 被身分錨點守衛擋下。"""
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_anchor_guard_denied(
            self._write_service({'access_app_id': 'victim-app-id'}),
            '租戶經 RPC 改得動 access_app_id')
        self.svc_w1.invalidate_recordset()
        self.assertFalse(self.svc_w1.access_app_id)

    def test_H3_forged_context_does_not_open_the_hatch_over_rpc(self):
        """偽造 context 值打不開逃生口——這正是 #522 實際被利用的形狀。

        `call_kw` 把呼叫端 context **整包**換進 env，所以任何 JSON 表達得出的
        判準在這條路徑上都是敞開的。sentinel 是物件身分，JSON 載不動。
        """
        self.authenticate(self.alice_login, self.alice_login)
        for value in (True, 1, 'true', ['x'], {'k': 'v'}):
            self._assert_anchor_guard_denied(
                self._write_service(
                    {'access_app_id': 'victim-app-id'},
                    ctx={'woow_cs_access_identity_write': value}),
                '偽造值 %r 經 RPC 打開了逃生口' % (value,))
        self.svc_w1.invalidate_recordset()
        self.assertFalse(self.svc_w1.access_app_id)
