# -*- coding: utf-8 -*-
"""#698：`project` / `task` ↔ `cloud_service` 關聯層的授權守線。

## 缺陷

`project.cloud_service_ids` 與 `task.extra_cloud_service_ids` 是 Many2many。
Odoo 的 ``Many2many.write_real``（``odoo/fields.py``）對 ``(3)``/``(4)``/``(5)``/
``(6)`` **只改 relation 表**（直接 SQL ``INSERT``/``DELETE``），程式碼路徑上
**沒有任何 comodel 權限檢查——連 ``read`` 都不檢查**；只有 ``(0)``/``(1)``/``(2)``
會分別走 ``comodel.create/write/unlink``，那三個才碰得到 #676 收窄的 ir.rule。

⇒ #676 對 `cloud_service` 收窄的那條線**管不到關聯層**。修復前實測：任何
`base.group_user`（**不需要屬於任何 workspace**）建一個自己的 project、對它下
``(4, <別的租戶的 service id>)`` 就綁得上，只要知道那個整數 id。下游：
`workspace_ids` 是 `compute_sudo` 的 stored compute ⇒ 受害者 workspace 進入
reach 集合 ⇒ `_reconcile_agent_runtime_reach` 推給 operator（pod egress
allowlist ＋ agent ns 的 ``reach-ws-*`` label）。另兩條下游不需要 K8s：
受害者 service 的 name／URL／template／config 會進攻擊者的 agent system
prompt；以及 ``(3, id)`` 解掉最後一個關聯會把 project 的可見性從「整個
workspace」翻成「建立者一人」。

## 修法

`woow_paas_platform.cloud_service_binding_guard`（AbstractModel mixin）——
**唯一實作**，四個入口共用，比對寫入前後的**實際綁定**而不解析 x2many 指令。
界線 owner／admin（與 #676 一致，Eugene 拍板：guest／user 連自己 workspace
的 service 也不能綁）。設計理由見該檔檔頭。

## 這個檔在守什麼

| 測到的東西 | class |
| --- | --- |
| 陽性對照：owner／admin 綁得上、解得掉 | `TestBindingGuardPositiveControls` |
| 新增方向（跨租戶／同 ws 非管理者／零 workspace 帳號） | `TestBindingGuardAddDirection` |
| 移除方向（E7 可見性翻轉） | `TestBindingGuardRemoveDirection` |
| create 路徑（**與 write 是兩條路**） | `TestBindingGuardCreatePath` |
| 另外三個入口（task ×2、deprecated M2O） | `TestBindingGuardOtherEntries` |
| 下游不再被污染（workspace_ids／operator／agent context） | `TestBindingGuardDownstream` |
| #676 那三格仍由 cloud_service 的 rule 擋（沒被本修法取代） | `TestCloudServiceRuleStillGuards676` |

## TDD 紀律（照 #676 / #556 的樣板）

- **全部走真 HTTP RPC**（`/web/dataset/call_kw`）。ir.rule 與 `env.su` 豁免對
  `TransactionCase` 的 superuser env 不生效，直呼 model 會系統性假綠。
- **否定性斷言一律配陽性對照**：斷言「被擋」之前先證明同一個操作在 owner／
  admin 身分下會成功——否則 AccessError 可能來自欄位不存在／記錄不存在／
  constraint，而「擋住了」跟「探針壞了」輸出長得一模一樣。
- **主斷言落在不變式上**（relation 實際有沒有改變），不是落在「走了哪條
  code path」；歸因斷言是**輔助**，只用來分辨「是本修法擋的」還是「project
  的 rule 順手擋的」——#698 調查踩過：`(3, victim)` 的 AccessError 來自
  `project.project` 而不是 cloud_service，長得一樣但意義完全不同。
- fixture user 一律只掛 `base.group_user`（setUp 有前提斷言）。
- 每次 RPC 後 `invalidate_all()`（#676 檔頭記載：先讀過同一筆會把值餵進 env
  cache 而遮蔽 AccessError）。

## 角色圖

- `wv`（受害者 workspace）：`victim` 是 owner。`svc_v` 住在 wv。
- `wa`（攻擊者 workspace）：`aowner` 是 owner、`wamate` 是 admin、`gary` 是
  **guest**、`carol` 是 **user**。`svc_a` 住在 wa。
- `lone` 不屬於任何 workspace。
- `gary` / `carol` / `lone` 都**不是** wv 的任何角色。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)

_CS = 'woow_paas_platform.cloud_service'
_PROJECT = 'project.project'
_TASK = 'project.task'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 698' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _Binding698Fixture(HttpCase):

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Service = self.env[_CS]
        self.Project = self.env[_PROJECT]
        self.Task = self.env[_TASK]

        self.victim, self.victim_login = _new_group_user(self.env, 'victim698')
        self.aowner, self.aowner_login = _new_group_user(self.env, 'aowner698')
        self.wamate, self.wamate_login = _new_group_user(self.env, 'wamate698')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol698')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary698')
        # `lone` 刻意**不屬於任何 workspace**：`project_rule_workspace_member`
        # 的 unbound 分支只要求 `create_uid = user.id` ⇒ 若他也做得到，受影響
        # 面是**每一個 internal user**，不只是「某 workspace 的 guest」。
        self.lone, self.lone_login = _new_group_user(self.env, 'lone698')

        for u in (self.victim, self.aowner, self.wamate, self.carol,
                  self.gary, self.lone):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)
            self.assertFalse(
                u.has_group('project.group_project_user'),
                'fixture 前提：不得有 project.group_project_user '
                '（否則測到的是原生 project 規則，不是本 repo 那條）%s' % u.login)

        self.wv = self.Workspace.with_user(self.victim).create({'name': 'WV 698'})
        self.wa = self.Workspace.with_user(self.aowner).create({'name': 'WA 698'})
        # wa 掛滿 owner/admin/user/guest：界線是 owner+admin vs user+guest，
        # 少了 user 那一列，「user 也綁不上」那格就沒有 fixture 可測。
        for user, role in ((self.wamate, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.wa.id, 'user_id': user.id, 'role': role})

        self.assertEqual(
            sorted(self.wa.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            'fixture 前提：WA 必須同時掛四種角色')
        self.assertFalse(
            self.wv.sudo().access_ids.filtered(
                lambda a: a.user_id in (self.gary | self.carol | self.lone)),
            'fixture 前提：攻擊者不得是 WV 的成員（否則整份測試零鑑別力）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([('user_id', '=', self.lone.id)]),
            'fixture 前提：lone 不得屬於任何 workspace')

        self.template = self._template('t698')
        self.svc_v = self._service(self.template, self.wv, 'SVC victim 698')
        self.svc_a = self._service(self.template, self.wa, 'SVC attacker 698')

    # ------------------------------------------------------------------ #
    #  fixture builders（一律 sudo：fixture 不該被受測的守衛擋住）          #
    # ------------------------------------------------------------------ #
    def _template(self, slug, **extra):
        vals = {
            'name': '698 %s' % slug,
            'slug': '%s-%s' % (slug, uuid.uuid4().hex[:6]),
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            # 必須釘版：未釘版會被 create() 的供給閘擋下，fixture 死在抵達
            # 受測步驟之前，而那個 error 長得跟「守衛擋住了」一模一樣。
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        }
        vals.update(extra)
        return self.env['woow_paas_platform.cloud_app_template'].sudo().create(vals)

    def _service(self, template, workspace, name, state='running', **extra):
        vals = {
            'name': name,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-test-698',
            'helm_release_name': 'svc-698-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return self.Service.sudo().create(vals)

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

    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)

    def _bound(self, project_id):
        """該 project 目前實際綁定的 service id 集合（sudo ⇒ 不受 read rule 影響）。"""
        return set(self.Project.sudo().browse(project_id).cloud_service_ids.ids)

    def _assert_guard_denied(self, result, service, msg):
        """斷言被 **#698 綁定守衛**擋下，而不是 project 的 ir.rule 順手擋的。

        兩者都是 AccessError，長得一樣但意義完全不同：rule 擋的意思是「這個
        project 不歸你管」（換一個你管得到的 project 就通了），守衛擋的意思
        才是「這個 service 你綁不得」。訊息裡帶被拒的 service id，所以歸因
        可以精確到「擋的正是這一筆」。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % text)
        self.assertIn('cloud_service', text,
                      msg + '（歸因：必須是 cloud_service 綁定守衛，回 %r）' % text)
        self.assertIn('綁定', text,
                      msg + '（歸因：project 的 ir.rule 是 Odoo 英文訊息，'
                            '本守衛的訊息必須可分辨，回 %r）' % text)
        self.assertIn('#%s' % service.id, text,
                      msg + '（歸因：被拒的必須正是 service #%s，回 %r）'
                      % (service.id, text))

    # ------------------------------------------------------------------ #
    #  攻擊者的起手式：建一個自己的 unbound project                        #
    # ------------------------------------------------------------------ #
    def _new_project_as(self, login, name='P698'):
        self.authenticate(login, login)
        res = self._call_kw(_PROJECT, 'create', [{'name': name}])
        self._assert_ok(res, '前提：%s 必須建得起 unbound project' % login)
        return res['result']


@tagged('post_install', '-at_install')
class TestBindingGuardPositiveControls(_Binding698Fixture):
    """陽性對照。**沒有這個 class，下面每一條 AccessError 都可能來自別的原因。**"""

    def test_P1_owner_can_bind_own_workspace_service(self):
        pid = self._new_project_as(self.aowner_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}])
        self._assert_ok(res, 'owner 必須綁得上自己 workspace 的 service')
        self.assertEqual(self._bound(pid), {self.svc_a.id},
                         'owner 的綁定必須真的落地')

    def test_P2_admin_can_bind_and_unbind(self):
        """admin（非 owner）兩個方向都要通——守衛的界線是 owner+admin。"""
        pid = self._new_project_as(self.wamate_login)
        bind = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}])
        self._assert_ok(bind, 'admin 必須綁得上')
        self.assertEqual(self._bound(pid), {self.svc_a.id}, 'admin 綁定要落地')

        unbind = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(3, self.svc_a.id)]}])
        self._assert_ok(unbind, 'admin 必須解得掉')
        self.assertEqual(self._bound(pid), set(), 'admin 解綁要落地')

    def test_P3_owner_can_write_task_service_fields(self):
        """task 兩個入口的陽性對照。"""
        pid = self._new_project_as(self.aowner_login)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：先把 service 綁上 project')
        created = self._call_kw(_TASK, 'create', [
            {'name': 'T698 ok', 'project_id': pid}])
        self._assert_ok(created, 'owner 必須建得起 task')
        tid = created['result']
        self._assert_ok(
            self._call_kw(_TASK, 'write', [
                [tid], {'extra_cloud_service_ids': [(4, self.svc_a.id)]}]),
            'owner 必須寫得動 extra_cloud_service_ids')
        self._assert_ok(
            self._call_kw(_TASK, 'write', [
                [tid], {'primary_cloud_service_id': self.svc_a.id}]),
            'owner 必須寫得動 primary_cloud_service_id')
        task = self.Task.sudo().browse(tid)
        self.assertIn(self.svc_a, task.extra_cloud_service_ids, 'extra 要落地')
        self.assertEqual(task.primary_cloud_service_id, self.svc_a,
                         'primary 要落地')

    def test_P4_sudo_paths_are_exempt(self):
        """`env.su` 豁免：seed／migration／cron／controller 的 sudo 產品路徑
        （例如 `ai_assistant._create_project`）必須照常綁得上。

        沒有這一格，守衛可能是靠「把所有人都擋住」通過上面那些否定性斷言。
        """
        project = self.Project.sudo().create({'name': 'P698 sudo'})
        project.write({'cloud_service_ids': [(4, self.svc_v.id)]})
        self.assertEqual(self._bound(project.id), {self.svc_v.id},
                         'sudo 路徑必須不受守衛限制')


@tagged('post_install', '-at_install')
class TestBindingGuardAddDirection(_Binding698Fixture):
    """新增方向：把不該綁的 service 綁進來。"""

    def test_A1_guest_cannot_link_cross_tenant_service(self):
        """🔴 原始缺陷：攻擊者對**看不到的**跨租戶 service 下 `(4, id)`。"""
        pid = self._new_project_as(self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_v.id)]}])
        self.assertEqual(self._bound(pid), set(),
                         'A1：跨租戶 service 不得落地（不變式）')
        self._assert_guard_denied(res, self.svc_v, 'A1')

    def test_A2_guest_cannot_set_cross_tenant_service(self):
        """`(6, 0, [id])`：整組取代也是同一條路（只改 relation 表）。"""
        pid = self._new_project_as(self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(6, 0, [self.svc_v.id])]}])
        self.assertEqual(self._bound(pid), set(), 'A2：不得落地')
        self._assert_guard_denied(res, self.svc_v, 'A2')

    def test_A3_guest_cannot_link_own_workspace_service(self):
        """界線是 owner／admin：**guest 連自己 workspace 的 service 也不能綁**
        （Eugene 拍板；與 #676 對 cloud_service 的界線一致）。"""
        pid = self._new_project_as(self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}])
        self.assertEqual(self._bound(pid), set(), 'A3：不得落地')
        self._assert_guard_denied(res, self.svc_a, 'A3')

    def test_A4_user_role_cannot_link_own_workspace_service(self):
        """role=`user` 與 guest 同側。這一格與 A3 不可互相取代：`user` 是
        `ROLE_HIERARCHY` 裡緊鄰 admin 的那一級，界線若寫成 `>= ROLE_USER`
        只有它會漏。"""
        pid = self._new_project_as(self.carol_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}])
        self.assertEqual(self._bound(pid), set(), 'A4：不得落地')
        self._assert_guard_denied(res, self.svc_a, 'A4')

    def test_A5_user_without_any_workspace_cannot_link(self):
        """零 workspace 的 internal user——修復前這一格是「受影響面＝每一個
        internal user」的證據。"""
        pid = self._new_project_as(self.lone_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_v.id)]}])
        proj = self.Project.sudo().browse(pid)
        self.assertEqual(set(proj.cloud_service_ids.ids), set(),
                         'A5：不得落地')
        self.assertNotIn(self.wv, proj.workspace_ids,
                         'A5：workspace_ids 不得吸收受害者 workspace')
        self._assert_guard_denied(res, self.svc_v, 'A5')


@tagged('post_install', '-at_install')
class TestBindingGuardRemoveDirection(_Binding698Fixture):
    """移除方向（#698 E7）：`(3, id)` 把 project 從整個 workspace 眼前藏起來。

    `project_rule_workspace_member` 的兩條分支互斥——有 service ⇒ workspace
    成員可見；無 service ⇒ 只有 follower／`create_uid` 可見。解掉最後一個
    關聯＝把可見性從「整個 workspace」翻成「建立者一人」，一般路徑救不回
    （連 admin 都讀不到那個 project）。所以守衛兩個方向都要守。
    """

    def _owner_project_bound_to_svc_a(self):
        pid = self._new_project_as(self.aowner_login, 'P698 shared')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：owner 先綁上自己 workspace 的 service')
        return pid

    def test_R1_admin_can_still_read_bound_project(self):
        """陽性對照：綁著的時候，非 create_uid、非 follower 的 admin 讀得到。"""
        pid = self._owner_project_bound_to_svc_a()
        self.authenticate(self.wamate_login, self.wamate_login)
        self._assert_ok(
            self._call_kw(_PROJECT, 'read', [[pid], ['name']]),
            'R1：bound 期間 workspace admin 必須讀得到')

    def test_R2_guest_cannot_unbind_and_hide_project(self):
        """🔴 E7：guest 解綁 → 守衛擋下 → admin 仍讀得到。"""
        pid = self._owner_project_bound_to_svc_a()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(3, self.svc_a.id)]}])
        self.assertEqual(self._bound(pid), {self.svc_a.id},
                         'R2：關聯必須還在（不變式）')
        self._assert_guard_denied(res, self.svc_a, 'R2')

        self.authenticate(self.wamate_login, self.wamate_login)
        self._assert_ok(
            self._call_kw(_PROJECT, 'read', [[pid], ['name']]),
            'R2：workspace admin 必須仍讀得到那個 project')

    def test_R3_guest_cannot_clear_all_bindings(self):
        """`(5,)` / `(6, 0, [])` 是另外兩種「清空」寫法。守衛比對的是實際
        差異而不是指令形態，所以這裡測的是**同一條不變式的另一個入口**，
        不是同一件事測兩次。"""
        pid = self._owner_project_bound_to_svc_a()
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(6, 0, [])]}])
        self.assertEqual(self._bound(pid), {self.svc_a.id},
                         'R3：關聯必須還在')
        self._assert_guard_denied(res, self.svc_a, 'R3')


@tagged('post_install', '-at_install')
class TestBindingGuardCreatePath(_Binding698Fixture):
    """create 與 write 是**兩條路**，守衛必須各掛一次。"""

    def test_C1_guest_cannot_create_project_prebound_to_own_service(self):
        """🔴 **這一格是 create guard 的唯一鑑別力來源。**

        攻擊者在 `create` 時一步到位綁**自己 workspace** 的 service：
        `project_rule_workspace_member` 的 bound 分支
        （`cloud_service_ids.workspace_id.access_ids.user_id = user.id`）對他
        **成立**（他是 wa 的 guest）⇒ Odoo 的 post-create rule check 放行
        ⇒ 只掛 write guard 的話這條路是通的。實測過：拿掉 mixin 的 `create`
        override，本格立刻轉紅（攻擊成功），補回來才綠。

        C2 那格（綁跨租戶 service）**證不了這件事**——它被 rule 先擋掉了。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'create', [{
            'name': 'C1 prebound own-ws',
            'cloud_service_ids': [(4, self.svc_a.id)],
        }])
        self.assertIn('error', res, 'C1：create 一步到位必須被擋（回 %r）' % res)
        self._assert_guard_denied(res, self.svc_a, 'C1')
        self.assertFalse(
            self.Service.sudo().browse(self.svc_a.id).project_ids.filtered(
                lambda p: p.name == 'C1 prebound own-ws'),
            'C1：那個 project 不得存在（交易必須整個回滾）')

    def test_C2_guest_cannot_create_project_prebound_to_victim(self):
        """跨租戶版本。

        ⚠ 這一格的歸因**刻意不斷言是守衛擋的**：Odoo 對 create 的 ir.rule
        檢查發生在寫入之後、且在守衛之前，所以先擋下來的是
        `project_rule_workspace_member`。修復前它就是綠的——那是**碰巧擋住**，
        不是任何人設計的防線（換一條 `perm_create` 較寬的 rule、或該路徑改走
        sudo，它就靜默打開）。真正的 create 守線由 C1 證明。
        """
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'create', [{
            'name': 'C2 prebound victim',
            'cloud_service_ids': [(4, self.svc_v.id)],
        }])
        self.assertIn('error', res, 'C2：不得建得起來（回 %r）' % res)
        self.assertNotIn(
            'C2 prebound victim',
            self.Project.sudo().search([]).mapped('name'),
            'C2：那個 project 不得存在')

    def test_C3_owner_can_create_project_prebound(self):
        """陽性對照：owner 一步到位建 + 綁必須成功。沒有它，C1／C2 的
        AccessError 可能來自「create 帶 x2many 本來就不work」。"""
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_PROJECT, 'create', [{
            'name': 'C3 prebound ok',
            'cloud_service_ids': [(4, self.svc_a.id)],
        }])
        self._assert_ok(res, 'C3：owner 必須建得起來')
        self.assertEqual(self._bound(res['result']), {self.svc_a.id},
                         'C3：綁定必須落地')


@tagged('post_install', '-at_install')
class TestBindingGuardOtherEntries(_Binding698Fixture):
    """另外三個入口。四個入口共用同一個 mixin ⇒ 這裡測的是「都掛上了」。"""

    def test_O1_task_extra_cloud_service_ids(self):
        """`task.extra_cloud_service_ids`。它的 `domain=` 是**字串**，
        `Field.get_domain_list` 對非 list 回 `[]` ⇒ ORM 層零效力，純 UI 候選
        清單，擋不了任何東西。"""
        pid = self._new_project_as(self.gary_login)
        created = self._call_kw(_TASK, 'create', [
            {'name': 'O1', 'project_id': pid}])
        self._assert_ok(created, '前提：guest 建得起自己 project 的 task')
        tid = created['result']
        res = self._call_kw(_TASK, 'write', [
            [tid], {'extra_cloud_service_ids': [(4, self.svc_v.id)]}])
        self.assertEqual(
            set(self.Task.sudo().browse(tid).extra_cloud_service_ids.ids), set(),
            'O1：不得落地')
        self._assert_guard_denied(res, self.svc_v, 'O1')

    def test_O2_task_primary_cloud_service_id(self):
        """`task.primary_cloud_service_id` 是 stored compute **`readonly=False`**
        ⇒ 可以被直接寫成任意 service，不是 M:N 的投影。"""
        pid = self._new_project_as(self.gary_login)
        created = self._call_kw(_TASK, 'create', [
            {'name': 'O2', 'project_id': pid}])
        self._assert_ok(created, '前提：guest 建得起 task')
        tid = created['result']
        res = self._call_kw(_TASK, 'write', [
            [tid], {'primary_cloud_service_id': self.svc_v.id}])
        self.assertFalse(
            self.Task.sudo().browse(tid).primary_cloud_service_id,
            'O2：不得落地')
        self._assert_guard_denied(res, self.svc_v, 'O2')

    def test_O3_deprecated_m2o_cloud_service_id(self):
        """`project.cloud_service_id`（deprecated M2O，stored compute **有
        inverse** ⇒ 可寫）。`_inverse_cloud_service_id` 走
        `[(6, 0, [id])]`，所以守衛在載體欄位那一層就看得見。"""
        pid = self._new_project_as(self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_id': self.svc_v.id}])
        self.assertEqual(self._bound(pid), set(), 'O3：不得落地')
        self.assertFalse(
            self.Project.sudo().browse(pid).cloud_service_id,
            'O3：deprecated M2O 也不得留值')
        self._assert_guard_denied(res, self.svc_v, 'O3')


@tagged('post_install', '-at_install')
class TestBindingGuardDownstream(_Binding698Fixture):
    """下游：綁不上 ⇒ 這些污染都不該發生。

    這些不是「同一件事測三次」——它們是三條**不同的**下游，修復前各自獨立
    可觸發（reach 推送需要 operator，agent context 不需要），任何一條漏掉
    都會讓修法看起來成功卻仍在洩漏。
    """

    def test_D1_workspace_ids_not_polluted(self):
        pid = self._new_project_as(self.gary_login)
        self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(4, self.svc_v.id)]}])
        proj = self.Project.sudo().browse(pid)
        self.assertNotIn(self.wv, proj.workspace_ids,
                         'D1：workspace_ids 不得吸收受害者 workspace')
        self.assertFalse(proj.workspace_id,
                         'D1：primary workspace_id 也不得被翻掉')

    def test_D2_operator_not_told_to_widen_reach(self):
        """守衛在 `super().write()` 之內 raise ⇒ `ProjectProject.write` 那層
        （在 `super()` **之後**）的 operator 推送根本不會被執行到。"""
        pid = self._new_project_as(self.gary_login)
        proj = self.Project.sudo().browse(pid)
        proj.write({'use_agent_runtime': True})
        self.env['woow_paas_platform.agent_session'].sudo().create({
            'project_id': pid,
            'state': 'active',
            'project_namespace_hash': 'deadbeef698',
        })
        client = MagicMock()
        target = ('odoo.addons.woow_paas_platform.services'
                  '.agent_runtime_client.get_agent_runtime_client')
        self.authenticate(self.gary_login, self.gary_login)
        with patch(target, return_value=client):
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_v.id)]}])
        self.assertFalse(
            client.sync_workspaces.called,
            'D2：operator 不得收到任何 reach 擴張（呼叫記錄 %r）'
            % (client.sync_workspaces.call_args_list,))

    def test_D3_victim_service_absent_from_agent_context(self):
        """agent context 這條下游**不需要 operator、不需要 K8s**，是最容易
        觸發的一條。組裝那一步直呼 `_ctx_services_sections`——它是系統自己以
        sudo 呼叫的（`_build_system_context_unsafe` 的
        `project.task.sudo().search`），攻擊者本來就不需要有權限呼叫它，所以
        這裡不是在測授權；授權那一步在上面的 `_call_kw`。
        """
        pid = self._new_project_as(self.gary_login)
        created = self._call_kw(_TASK, 'create', [
            {'name': 'D3', 'project_id': pid}])
        self._assert_ok(created, '前提：guest 建得起 task')
        tid = created['result']
        self._call_kw(_TASK, 'write', [
            [tid], {'primary_cloud_service_id': self.svc_v.id}])

        Session = self.env['woow_paas_platform.agent_session'].sudo()
        blocks = Session._ctx_services_sections(self.Task.sudo().browse(tid))
        self.assertNotIn(
            self.svc_v.name, '\n'.join(blocks),
            'D3：受害者 service 不得出現在 agent context')


@tagged('post_install', '-at_install')
class TestCloudServiceRuleStillGuards676(_Binding698Fixture):
    """#676 的回歸：`(0)`/`(1)`/`(2)` 仍必須由 **cloud_service 自己那層**擋。

    本修法沒有取代 #676——它補的是 rule 碰不到的 `(3)/(4)/(6)`。這三格如果
    改由綁定守衛擋，代表 #676 的 rule 已經被誰改壞了（守衛只看綁定變化，
    `(1)` 這種「改欄位但不動關聯」的操作它完全看不見）。
    """

    def test_S1_cmd0_create_via_relation_denied_by_cloud_service_rule(self):
        pid = self._new_project_as(self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [[pid], {
            'cloud_service_ids': [(0, 0, {
                'name': 'forged by guest',
                'workspace_id': self.wa.id,
                'template_id': self.template.id,
            })],
        }])
        self.assertIn('error', res, 'S1：不得建得起來（回 %r）' % res)
        self.assertIn('cloud_service', self._error_text(res),
                      'S1：歸因必須是 cloud_service 那一層')
        self.assertFalse(
            self.Service.sudo().search([('name', '=', 'forged by guest')]),
            'S1：偽造的 service 不得存在')

    def test_S2_cmd1_update_via_relation_denied_by_cloud_service_rule(self):
        """`(1)` 改的是 comodel 欄位、不動關聯 ⇒ 綁定守衛看不見它，只有
        #676 的 rule 擋得到。這是本 class 存在的理由。"""
        pid = self._new_project_as(self.aowner_login)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：由 owner 先把 service 綁上')
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [[pid], {
            'cloud_service_ids': [(1, self.svc_a.id, {'name': 'renamed by guest'})],
        }])
        self.assertIn('error', res, 'S2：不得改得動（回 %r）' % res)
        self.assertIn('cloud_service', self._error_text(res),
                      'S2：歸因必須是 cloud_service 那一層')
        self.assertEqual(self.svc_a.sudo().name, 'SVC attacker 698',
                         'S2：名稱不得被改動')

    def test_S3_cmd2_delete_via_relation_denied_by_cloud_service_rule(self):
        pid = self._new_project_as(self.aowner_login)
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(4, self.svc_a.id)]}]),
            '前提：由 owner 先把 service 綁上')
        self.authenticate(self.gary_login, self.gary_login)
        res = self._call_kw(_PROJECT, 'write', [
            [pid], {'cloud_service_ids': [(2, self.svc_a.id)]}])
        self.assertIn('error', res, 'S3：不得刪得掉（回 %r）' % res)
        self.assertIn('cloud_service', self._error_text(res),
                      'S3：歸因必須是 cloud_service 那一層')
        self.assertTrue(self.svc_a.sudo().exists(), 'S3：service 必須還在')

    def test_S4_positive_control_owner_can_012(self):
        """陽性對照：owner 對 `(0)`/`(1)`/`(2)` 三種指令都做得到。"""
        pid = self._new_project_as(self.aowner_login)
        res0 = self._call_kw(_PROJECT, 'write', [[pid], {
            'cloud_service_ids': [(0, 0, {
                'name': 'created by owner 698',
                'workspace_id': self.wa.id,
                'template_id': self.template.id,
            })],
        }])
        self._assert_ok(res0, '陽性對照 (0)')
        new_svc = self.Project.sudo().browse(pid).cloud_service_ids.filtered(
            lambda s: s.name == 'created by owner 698')
        self.assertTrue(new_svc, '陽性對照 (0)：新 service 必須落地')

        res1 = self._call_kw(_PROJECT, 'write', [[pid], {
            'cloud_service_ids': [(1, new_svc.id, {'name': 'renamed by owner'})],
        }])
        self._assert_ok(res1, '陽性對照 (1)')
        self.assertEqual(new_svc.sudo().name, 'renamed by owner',
                         '陽性對照 (1)：改名必須落地')

        with patch.object(type(self.Service), '_cleanup_helm', return_value=True):
            res2 = self._call_kw(_PROJECT, 'write', [
                [pid], {'cloud_service_ids': [(2, new_svc.id)]}])
        self._assert_ok(res2, '陽性對照 (2)')
        self.assertFalse(new_svc.sudo().exists(),
                         '陽性對照 (2)：service 必須真的消失')


@tagged('post_install', '-at_install')
class TestReverseEntryFromServiceSide(_Binding698Fixture):
    """反向入口：從 **service 側**寫 `cloud_service.project_ids`（M:N 的另一端）。

    #698 的四個入口都在 project／task 側。這一格問的是同一組 relation 的
    反向欄位有沒有第五條路：wa 的 **owner**（對 svc_a 有 write，#676 的 rule
    放行）把自己的 service 綁到**別人的 project** 上。

    ⚠ 方向與主缺陷相反——它不是「偷別人的 service」，而是「把自己的 service
    推進別人的 project」，後果是受害者的 `workspace_ids` 吸收攻擊者的
    workspace（受害者的 agent pod 取得攻擊者 ns 的可達性）。

    這一端的判準與另外四個入口**不同**：#676 的 rule 已經保證呼叫者是這個
    service 所屬 workspace 的 owner／admin，缺的是另一半——那個 project 也得
    是他管得動的。

    ⚠ 也因此它**不能**掛 `cloud_service_binding_guard`：那個 mixin 在
    `super().write()` 之後才比對差異，而綁定一旦寫進去，那個 project 就落進
    呼叫者的 bound 分支 ⇒ 事後 `check_access('write')` 反而通過。實測過（接上
    mixin 後 V1 照樣紅，RPC 回 `result: True`）。自我實現的判準只能前置評估，
    所以這一端走 `CloudService._assert_reverse_binding_authorized`。
    """

    def test_V1_owner_cannot_push_own_service_into_foreign_project(self):
        """🔴 實測確認可利用後才納入修法（修法前這一格是紅的：RPC 回
        `result: True`，service 真的被綁進受害者的 project）。"""
        victim_pid = self._new_project_as(self.victim_login, 'P698 victim')
        self._assert_ok(
            self._call_kw(_PROJECT, 'write', [
                [victim_pid], {'cloud_service_ids': [(4, self.svc_v.id)]}]),
            '前提：受害者先綁自己的 service')

        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'write', [
            [self.svc_a.id], {'project_ids': [(4, victim_pid)]}])
        self.assertFalse(
            self.svc_a.sudo().project_ids.filtered(lambda p: p.id == victim_pid),
            'V1：不得把自己的 service 推進別人的 project（回 %r）' % res)
        self.assertNotIn(
            self.wa, self.Project.sudo().browse(victim_pid).workspace_ids,
            'V1：受害者的 workspace_ids 不得吸收攻擊者的 workspace')
        self.assertIn('error', res, 'V1：必須是明確的 AccessError（回 %r）' % res)
        self.assertIn('AccessError', self._error_text(res),
                      'V1：期望 AccessError，回 %r' % self._error_text(res))

    def test_V2_owner_can_push_own_service_into_own_project(self):
        """陽性對照。沒有它，V1 的 AccessError 可能來自「`project_ids` 這個
        欄位根本寫不動」，那樣 V1 會恆綠、零鑑別力。"""
        own_pid = self._new_project_as(self.aowner_login, 'P698 own')
        self.authenticate(self.aowner_login, self.aowner_login)
        res = self._call_kw(_CS, 'write', [
            [self.svc_a.id], {'project_ids': [(4, own_pid)]}])
        self._assert_ok(res, 'V2：owner 必須推得進自己的 project')
        self.assertIn(
            own_pid, self.svc_a.sudo().project_ids.ids,
            'V2：綁定必須真的落地')
