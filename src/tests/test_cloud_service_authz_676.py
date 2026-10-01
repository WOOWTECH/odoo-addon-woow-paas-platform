# -*- coding: utf-8 -*-
"""#676：`woow_paas_platform.cloud_service` 的寫入授權（ir.rule 層）。

## 缺陷

修復前只有一條 `cloud_service_rule_workspace_member`：domain
`[('workspace_id.access_ids.user_id', '=', user.id)]`、**沒有 role 條件**、
`perm_read`/`perm_write`/`perm_create`/`perm_unlink` 全 `True`
⇒ 任何 workspace 成員（含 `role='guest'`）對該 workspace 的每一筆 cloud_service
有完整 CRUD，其中 `unlink` 會走 `CloudService.unlink()` 的 helm 清理路徑（不可逆）。

model 層沒有補：`cloud_service.py` 的 `create()` / `write()` / `unlink()` 三個
override 只有完整性與清理邏輯，零授權判準。ACL 層 `access_cloud_service_user`
給 `base.group_user` 1,1,1,1 全開 ⇒ **ir.rule 是唯一守線**。

`write()` 自己的註解早就寫下了正解（#519 起）：「根因是租戶對本 model 有直接
write 權…正解是收緊 ACL，一旦租戶沒有直接 write 權，所有繞過路徑一次關閉。」

## 修法（兩條 rule，比照 #556 / #658 的樣板）

- `cloud_service_rule_member_read`：全體成員，只有 `perm_read`
- `cloud_service_rule_owner_admin_manage`：owner/admin，四向全 True

domain 一律用 `('workspace_id.access_ids', 'any', [...])`。**不可**寫成
`workspace_id.access_ids.role` 點鏈——點鏈在一個 workspace 有多列 access 時是
**跨列**匹配（A 列滿足 `user_id`、B 列滿足 `role`），等於沒有 role 條件（#556 根因之一）。
fixture 的 W1 刻意掛滿 owner/admin/user/guest 四列，少了 admin 那一列，跨列匹配的
寫法就不會被 A1/A4 抓到。

## 界線（Eugene 拍板）

| role | read | write | create | unlink |
| --- | --- | --- | --- | --- |
| guest | ✅ | ❌ | ❌ | ❌ |
| user | ✅ | ❌ | ❌ | ❌ |
| admin / owner | ✅ | ✅ | ✅ | ✅ |

「user 可 write」的選項**已被否決**：controller 的 create / update / delete 三個
gate（`paas.py:1410` / `:1456` / `:1464`）今天都已經是 owner/admin，全 repo 沒有
任何端點給 role=user 對 cloud_service 的寫入權 ⇒ 放寬 ORM 層會讓它比 UI 更鬆。

## B 系列在守什麼

#676 當時：`_list_services`／`_get_service`（無 role gate，guest 也走得到）會在
request 裡 poll operator 並寫入 state，收窄 ir.rule 之後那些寫入必須改系統身分，否則
端點照回 success、狀態永遠停在轉移中（靜默失敗）。B1/B2/B3 守的是那條修法。

#1159 起 list/get **純讀**（Q1）：request 裡不再 poll、不再寫入，由觀測推進的狀態轉移
只由背景 cron 以排程身分寫。原本的回歸（「guest 讀取時寫入被 rule 擋下」）在結構上已
不可能發生，B 系列改守新的不變式：guest 打 list/get 仍然成功、**不呼叫 operator**、
**不推進 state**，回應帶背景的最近觀測 `observed_at`。走真 HTTP RPC，確保是實際端點
（不是直呼 method）沒有寫入。

## TDD 紀律（照抄 #556 / #658）

- **全部走真 HTTP RPC**（`/web/dataset/call_kw` 或 `/api/workspaces/*`）。
  ir.rule 對 `TransactionCase` 的 superuser env 不生效，直呼 controller 也繞開
  真實的授權路徑。
- fixture user 一律只掛 `base.group_user`（setUp 有前提斷言）。
- **否定性斷言一律配陽性對照**：斷言「被擋下來」之前先證明同一個操作在 owner
  身分下會成功——否則 AccessError 可能來自欄位不存在／記錄不存在／constraint，
  而「擋住了」跟「探針壞了」輸出長得一模一樣。
- **逐條標明是哪一層擋的**：`_assert_cloud_service_rule_denied` 斷言錯誤訊息指向
  `cloud_service`，不是 workspace 那層順手擋掉的（#675 X13 守的是 workspace 層）。
- `assertRaises` 之前若先讀過同一筆記錄會把值餵進 env cache 而遮蔽 AccessError
  ⇒ 本檔全部走 HTTP（獨立 env），且每次 RPC 後 `invalidate_all()`。
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 676' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _CloudService676Fixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）。

    W1 刻意掛滿四種角色：`('access_ids', 'any', [...])` 與
    `access_ids.role` 點鏈的差異只有在「同一個 workspace 有多列 access」時才顯現
    ——少一列，兩種寫法的行為就一樣，A1/A4 會零鑑別力。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Service = self.env['woow_paas_platform.cloud_service']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice676')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob676')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol676')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave676')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary676')

        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 676'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 676'})

        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})

        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            'fixture 前提：W1 必須同時掛 owner/admin/user/guest 四列'
            '（少了 admin 那列，跨列匹配的 domain 寫法就不會被 A1/A4 抓到）')
        self.assertFalse(
            self.w2.sudo().access_ids.filtered(
                lambda a: a.user_id in (self.gary | self.carol | self.dave)),
            'fixture 前提：W1 的成員不得同時是 W2 的成員')

        self.template = self._template('tmpl-676')
        self.svc = self._service(self.template, self.w1, 'Svc W1 676')

    # ------------------------------------------------------------------ #
    #  fixture builders（一律 sudo：fixture 不該被受測的 rule 擋住）        #
    # ------------------------------------------------------------------ #
    def _template(self, slug, **extra):
        vals = {
            'name': '676 %s' % slug,
            'slug': '%s-%s' % (slug, uuid.uuid4().hex[:6]),
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/%s' % slug,
            # ⚠ 必須釘版：未釘版的 template 會被 create() 的供給閘
            # （`_enforce_supply_gate`）擋下，fixture 會在抵達受測步驟之前就死，
            # 而那個 error 長得跟「rule 擋住了」一模一樣。
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
            'helm_namespace': 'paas-ws-test-676',
            'helm_release_name': 'svc-676-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return _internal_provision(self.Service.sudo()).create(vals)

    # ------------------------------------------------------------------ #
    #  RPC helper                                                         #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。"""
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

    def _post_json(self, url, payload):
        """打 `/api/workspaces/*` 這種 `type="json"` 的產品端點。"""
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                         #
    # ------------------------------------------------------------------ #
    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_cloud_service_rule_denied(self, result, msg):
        """斷言被 **cloud_service 自己那層** ir.rule 擋下。

        歸因是刻意的：#675 已經把 workspace 那層收窄了，若不分辨，「其實是
        workspace 順手擋的」會被誤讀成「cloud_service 的 rule 生效」，而把
        cloud_service 的 rule 改回全開之後測試照樣綠。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % result)
        self.assertIn(
            'cloud_service', text,
            msg + '（歸因：擋下來的必須是 cloud_service 這一層，回 %r）' % result)

    def _assert_provision_gate_denied(self, result, msg):
        """斷言被 **#519 的建立閘**擋下，而不是被 #676 的 ir.rule 擋下。

        `create` 這一格在 #519 之後換了守門人：閘在 `CloudService.create()` 的
        第一行，比 `super().create()` 裡的 `check_access('create')` 更早 ⇒ 對
        **任何** RPC 呼叫端（含 owner／admin）都是拒絕，而且歸因是閘不是 rule。
        分辨得出來才有意義——`_assert_cloud_service_rule_denied` 在這裡會綠得
        毫無資訊（兩者都是 AccessError 家族）。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('ProvisionGateError', text,
                      msg + '（歸因：必須是 #519 的建立閘，回 %r）' % text)
        self.assertIn('provisioning flow', text,
                      msg + '（歸因：訊息必須可分辨，回 %r）' % text)

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)

    # ------------------------------------------------------------------ #
    #  operator mock（B 系列用）                                           #
    # ------------------------------------------------------------------ #
    def _status(self, **release_overrides):
        """operator `get_status` 的回應。

        ⚠ `pods` **不可留空**：`_is_container_ready` 對空陣列是 fail-closed
        （#456／F-423-1），不就緒就整段不會 write ⇒ 測試會在「什麼都沒發生」
        的狀態下對回寫下斷言，B 系列會退化成恆綠。
        """
        release = {'status': 'deployed', 'revision': 3, 'chart_version': '1.0.1'}
        release.update(release_overrides)
        return {
            'release': release,
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }

    def _operator_client(self, **extra):
        client = MagicMock()
        client.get_status.return_value = self._status()
        client.init_odoo.return_value = {
            'success': True, 'modules_added': 0, 'modules_updated': 0}
        for k, v in extra.items():
            setattr(client, k, v)
        return client


class TestCloudServiceWriteAuthz676(_CloudService676Fixture):
    """A 系列：ir.rule 的授權矩陣（走 `/web/dataset/call_kw`）。"""

    # ---------------------------------------------------------------- #
    #  陽性對照——先證明這些操作在 owner/admin 身分下真的做得到           #
    # ---------------------------------------------------------------- #
    def _assert_can_read_write_unlink(self, login, label):
        """read / write / unlink 三方向全通。**這是 A1-A6 的陽性對照**：沒有它，
        那六條的 AccessError 可能來自欄位不存在／記錄不存在／constraint，而不是
        rule。

        ⚠ `create` 不在這裡（#519 之後那一格對誰都不通，見
        `test_A11_*`）。unlink 的主體因此改用**伺服器端**（開閘 helper）建立的
        service——原本是「自己經 RPC 建一筆再刪掉」，那條路關掉之後若不補主體，
        unlink 的陽性對照會連同 create 一起消失，A3／A6 的綠燈就退化成「攻擊被擋」
        與「這條路徑本來就不通」分不出來。
        """
        disposable = self._service(self.template, self.w1,
                                   'Svc disposable %s 676' % label)
        self.authenticate(login, login)

        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[self.svc.id], ['name']]),
            '%s 必須讀得到' % label)

        written = self._call_kw(
            'woow_paas_platform.cloud_service', 'write',
            [[self.svc.id], {'name': 'renamed by %s' % label}])
        self._assert_ok(written, '%s 必須寫得動' % label)
        self.assertEqual(self.svc.sudo().name, 'renamed by %s' % label,
                         '%s 的寫入必須真的落地（不是被吞掉回 True）' % label)

        removed = self._call_kw(
            'woow_paas_platform.cloud_service', 'unlink', [[disposable.id]])
        self._assert_ok(removed, '%s 必須刪得掉' % label)
        self.assertFalse(
            self.Service.sudo().browse(disposable.id).exists(),
            '%s 的刪除必須真的落地' % label)

    def test_A9_admin_can_write_and_unlink(self):
        self._assert_can_read_write_unlink(self.dave_login, 'admin')

    def test_A10_owner_can_write_and_unlink(self):
        self._assert_can_read_write_unlink(self.alice_login, 'owner')

    def test_A13_even_owner_and_admin_cannot_create_over_rpc(self):
        """#519：`create` 對**任何** RPC 呼叫端都不通，不論身分。

        這條原本藏在 A9／A10 裡當「admin／owner 建得起來」的陽性對照。#519 把那個
        能力拿掉了——不變式換了，測試的價值不是消失而是反轉：現在要證明的是**連
        這兩個最有權限的身分都擋**。擋 guest 證不了這件事（A2／A5 已覆蓋 guest）。
        """
        for login, label in ((self.dave_login, 'admin'),
                             (self.alice_login, 'owner')):
            with self.subTest(role=label):
                self.authenticate(login, login)
                name = 'created by %s 676' % label
                # 可達性（自帶）：同一身分、同一條 RPC 路徑上有一個會成功的呼叫。
                self._assert_ok(
                    self._call_kw('woow_paas_platform.cloud_service', 'write',
                                  [[self.svc.id], {'name': 'reachable %s' % label}]),
                    '可達性前提：%s 對這筆服務寫得動' % label)
                result = self._call_kw(
                    'woow_paas_platform.cloud_service', 'create',
                    [{'name': name, 'workspace_id': self.w1.id,
                      'template_id': self.template.id}])
                self._assert_provision_gate_denied(
                    result, '%s 也不得經 RPC 建立 cloud_service' % label)
                self.assertFalse(
                    self.Service.sudo().search([('name', '=', name)]),
                    '「沒有 raise」與「沒有寫進去」是兩件事，這裡驗後者')

    # ---------------------------------------------------------------- #
    #  guest —— 讀得到、寫不動                                           #
    # ---------------------------------------------------------------- #
    def test_A7_guest_can_still_read(self):
        """收窄不得收過頭：guest 仍要讀得到自己 workspace 的服務。

        這條同時是 A1-A3 的**可達性前提**：guest 讀不到的話，A1-A3 的
        AccessError 會變成「記錄不可見」而不是「寫入被拒」。
        """
        self.authenticate(self.gary_login, self.gary_login)
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'read',
            [[self.svc.id], ['name', 'state']])
        self._assert_ok(result, 'guest 必須讀得到自己 workspace 的 cloud_service')
        self.assertEqual(result['result'][0]['name'], self.svc.sudo().name,
                         'guest 讀到的必須是真的內容')

    def test_A1_guest_cannot_write(self):
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[self.svc.id], ['name']]),
            '可達性前提：guest 必須讀得到這筆服務')

        original = self.svc.sudo().name
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'write',
            [[self.svc.id], {'name': 'pwned by guest'}])
        self._assert_cloud_service_rule_denied(
            result, 'guest 不得寫 cloud_service')
        self.assertEqual(self.svc.sudo().name, original,
                         '值不得落地（被擋卻仍寫進去＝守衛在 super() 之後）')

    def test_A2_guest_cannot_create(self):
        """#519 之後這一格的守門人是建立閘，不是 #676 的 rule（見
        `_assert_provision_gate_denied` 的說明）。"""
        self.authenticate(self.gary_login, self.gary_login)
        # 可達性（自帶）：同一身分、同一條 RPC 路徑上有一個會成功的呼叫。少了它，
        # 下面的「被擋」可能只是身分／路徑根本不通。
        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[self.svc.id], ['name']]),
            '可達性前提：guest 必須讀得到這筆服務')
        before = self.Service.sudo().search_count(
            [('workspace_id', '=', self.w1.id)])
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'create',
            [{'name': 'created by guest', 'workspace_id': self.w1.id,
              'template_id': self.template.id}])
        self._assert_provision_gate_denied(
            result, 'guest 不得建立 cloud_service')
        self.assertEqual(
            self.Service.sudo().search_count([('workspace_id', '=', self.w1.id)]),
            before, '記錄不得落地')

    def test_A3_guest_cannot_unlink(self):
        """unlink 是最重的一格：`CloudService.unlink()` 會走 helm 清理（不可逆）。"""
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[self.svc.id], ['name']]),
            '可達性前提：guest 必須讀得到這筆服務')

        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'unlink', [[self.svc.id]])
        self._assert_cloud_service_rule_denied(
            result, 'guest 不得刪除 cloud_service')
        self.assertTrue(self.svc.sudo().exists(), '記錄必須還在')

    # ---------------------------------------------------------------- #
    #  user —— 與 guest 同界線（「user 可 write」已被否決）                #
    # ---------------------------------------------------------------- #
    def test_A8_user_can_still_read(self):
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'read',
            [[self.svc.id], ['name', 'state']])
        self._assert_ok(result, 'role=user 必須讀得到')

    def test_A4_user_cannot_write(self):
        """role=user 也不得寫——ORM 層不可比 UI 更寬。

        controller 的 update gate（`paas.py:1456`）今天就是
        `role in [ROLE_OWNER, ROLE_ADMIN]`，全 repo 沒有任何端點給 role=user
        對 cloud_service 的寫入權。
        """
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[self.svc.id], ['name']]),
            '可達性前提：user 必須讀得到這筆服務')

        original = self.svc.sudo().name
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'write',
            [[self.svc.id], {'name': 'pwned by user'}])
        self._assert_cloud_service_rule_denied(
            result, 'role=user 不得寫 cloud_service')
        self.assertEqual(self.svc.sudo().name, original, '值不得落地')

    def test_A5_user_cannot_create(self):
        self.authenticate(self.carol_login, self.carol_login)
        before = self.Service.sudo().search_count(
            [('workspace_id', '=', self.w1.id)])
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'create',
            [{'name': 'created by user', 'workspace_id': self.w1.id,
              'template_id': self.template.id}])
        self._assert_provision_gate_denied(
            result, 'role=user 不得建立 cloud_service')
        self.assertEqual(
            self.Service.sudo().search_count([('workspace_id', '=', self.w1.id)]),
            before, '記錄不得落地')

    def test_A6_user_cannot_unlink(self):
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'unlink', [[self.svc.id]])
        self._assert_cloud_service_rule_denied(
            result, 'role=user 不得刪除 cloud_service')
        self.assertTrue(self.svc.sudo().exists(), '記錄必須還在')

    # ---------------------------------------------------------------- #
    #  非成員 —— 連 read 都不行（修復前就擋著，純回歸對照）                #
    # ---------------------------------------------------------------- #
    def test_A11_non_member_cannot_even_read(self):
        """W2 的 owner（bob）對 W1 的服務連 read 都不行。

        bob 在自己的 workspace 是 owner——若新 rule 誤把 domain 寫成
        「使用者在**任何** workspace 是 owner/admin」（漏掉 `workspace_id` 這一維），
        這條會紅。
        """
        self.authenticate(self.bob_login, self.bob_login)

        own_svc = self._service(self.template, self.w2, 'Svc W2 676')
        self._assert_ok(
            self._call_kw('woow_paas_platform.cloud_service', 'read',
                          [[own_svc.id], ['name']]),
            '陽性對照：bob 在自己的 W2 必須讀得到（否則下一條零鑑別力）')

        result = self._call_kw(
            'woow_paas_platform.cloud_service', 'read',
            [[self.svc.id], ['name']])
        self.assertIn('error', result,
                      '非成員不得讀 W1 的服務（回 %r）' % result)

    # ---------------------------------------------------------------- #
    #  o2m 反向通道 —— 縱深證明，不是 #675 X13 的複本                     #
    # ---------------------------------------------------------------- #
    def test_A12_cloud_service_layer_blocks_o2m_even_without_workspace_rule(self):
        """經 workspace 的 `service_ids` o2m 通道，**cloud_service 這一層**也要擋。

        #675 的 `test_X13_*` 已經證明 workspace 那一層會擋。但那條測的是
        workspace 的 rule——把 cloud_service 的 rule 改回全開，X13 照樣綠。
        本條要證明的是**兩層各自獨立成立**（縱深防禦）。

        🔴 為什麼要暫時放寬 workspace 的 rule：新界線下 workspace 與 cloud_service
        的寫入權都是 owner/admin ⇒ 不存在「過得了 workspace 那層、卻該被
        cloud_service 擋下」的角色 ⇒ 在預設狀態下 cloud_service 這一層被 workspace
        那層**完全遮蔽**，結構上不可達。直接照抄 X13 的寫法，這條就只是 X13 的
        複本（測到的仍是 workspace 那層）。故先把 workspace 的成員讀規則暫時
        補上 `perm_write`，讓 guest 過得了第一層，再看第二層擋不擋。

        ⚠ 只改 `workspace_rule_member_read` 這一條的 `perm_write`，
        不動 cloud_service 的任何 rule——受測對象必須維持原狀。
        """
        ws_read_rule = self.env.ref(
            'woow_paas_platform.workspace_rule_member_read').sudo()
        self.assertFalse(
            ws_read_rule.perm_write,
            '前提：workspace 的成員讀規則本來不該有 perm_write'
            '（若上游改了，本測試的放寬手法就失去意義，要重新設計）')
        ws_read_rule.write({'perm_write': True})
        self.addCleanup(ws_read_rule.write, {'perm_write': False})

        self.authenticate(self.gary_login, self.gary_login)

        # 陽性對照：確認放寬真的生效——guest 現在寫得動 workspace 本體。
        # 沒有這一步，下面的 AccessError 可能還是 workspace 那層擋的，
        # 這條就退化成 X13 的複本。
        probe = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'description': 'probe 676'}])
        self._assert_ok(
            probe,
            '放寬前提失效：guest 仍寫不動 workspace 本體 ⇒ 下面測到的會是'
            'workspace 那層，本條退化成 #675 X13 的複本')

        original = self.svc.sudo().name
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'service_ids': [(1, self.svc.id,
                                             {'name': 'pwned via o2m'})]}])
        self._assert_cloud_service_rule_denied(
            result,
            'guest 經 service_ids o2m 改子模型時，必須由 cloud_service 這一層擋下')
        self.assertEqual(self.svc.sudo().name, original, '子模型的值不得落地')


@tagged('post_install', '-at_install')
class TestInitControlFieldsWriteAuthz1257(_CloudService676Fixture):
    """#1257 r1 M1：init 執行管控欄位只有平台寫得進去（走真 `/web/dataset/call_kw`）。

    沒有這道閘時 owner 可以寫 `init_redeploy_requested=True`，下一分鐘 cron 就以
    superuser 對它跑 `_redeploy()`——繞過 #1185 的 group gate 與停權／欠費檢查；寫
    `init_exit='failed'` 則讓 helm 失敗的 error 也通過 #1159 Q30 的 reconfigure 放行。
    """

    def test_owner_cannot_write_init_control_fields_over_rpc(self):
        template = self._template('tmpl-1257-m1', post_deploy_init_type='odoo')
        svc = self._service(template, self.w1, 'M1 1257', state='error')
        self.authenticate(self.alice_login, self.alice_login)

        renamed = self._call_kw(
            'woow_paas_platform.cloud_service', 'write',
            [[svc.id], {'name': 'M1 renamed'}])
        self._assert_ok(renamed, '陽性對照：owner 本來就寫得進一般欄位')

        for field, value in (('init_redeploy_requested', True), ('init_exit', 'failed'),
                             ('init_retries', 0), ('init_unknown_count', 0),
                             ('init_pending', False), ('init_lease_until', False),
                             ('init_wait_spent_seconds', 0),
                             ('init_wait_started_at', False),
                             ('init_retry_eligible_at', False),
                             ('init_queue_turn', False), ('init_capacity_wait_since', False),
                             ('n8n_owner_alerted_at', False)):
            with self.subTest(field=field):
                result = self._call_kw(
                    'woow_paas_platform.cloud_service', 'write',
                    [[svc.id], {field: value}])
                self.assertIn('error', result, '%s 不得讓租戶寫入（回 %r）' % (field, result))
                self.assertIn('managed by the platform', self._error_text(result),
                              '歸因：必須是 init 管控欄位的閘（回 %r）' % result)
        svc.invalidate_recordset()
        self.assertFalse(svc.sudo().init_redeploy_requested)
        self.assertFalse(svc.sudo().init_exit)

    def test_capacity_clock_rejects_owner_context_forgery_but_allows_platform_rpc(self):
        """真 call_kw 的 authority 正負對照，不以 readonly UI 或 workspace rule 代守。"""
        stamp = fields.Datetime.now()
        fake = fields.Datetime.to_string(stamp)
        self.svc.sudo().write({'init_capacity_wait_since': stamp})
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_ok(self._call_kw('woow_paas_platform.cloud_service', 'write',
            [[self.svc.id], {'name': 'capacity legitimate owner write'}]), 'owner 一般 write 可達')
        result = self._call_kw('woow_paas_platform.cloud_service', 'write',
            [[self.svc.id], {'init_capacity_wait_since': False}],
            {'context': {'lang': 'en_US', 'su': True, 'uid': 1,
                         'default_init_capacity_wait_since': fake}})
        self.assertIn('error', result)
        self.assertIn('managed by the platform', self._error_text(result))
        self.assertEqual(self.svc.sudo().init_capacity_wait_since, stamp, '偽造 context 不得清時計')
        platform, login = _new_group_user(self.env, 'capacity-platform1159')
        platform.sudo().write({'groups_id': [(4, self.env.ref('base.group_system').id)]})
        self.authenticate(login, login)
        result = self._call_kw('woow_paas_platform.cloud_service', 'write',
                              [[self.svc.id], {'init_capacity_wait_since': False}])
        self._assert_ok(result, '平台 system user 的同一 RPC 必須可合法寫入')
        self.assertFalse(self.svc.sudo().init_capacity_wait_since)

    def test_owner_cannot_write_observation_fields_over_rpc(self):
        """OBS guard 也須跨真 call_kw 成立，不能靠 workspace rule 假綠。"""
        svc = self._service(self.template, self.w1, 'Observation 1159', state='upgrading')
        self.authenticate(self.alice_login, self.alice_login)
        renamed = self._call_kw(
            'woow_paas_platform.cloud_service', 'write', [[svc.id], {'name': 'Observation renamed'}])
        self._assert_ok(renamed, '陽性對照：owner 可寫一般欄位')
        for field in ('observed_at', 'observation_episode_started_at'):
            with self.subTest(field=field):
                result = self._call_kw(
                    'woow_paas_platform.cloud_service', 'write', [[svc.id], {field: False}])
                self.assertIn('error', result)
                self.assertIn('observation fields are managed by the platform', self._error_text(result))


@tagged('post_install', '-at_install')
class TestReadEndpointsStayPureForGuests(_CloudService676Fixture):
    """B 系列（#1159 改寫）：guest 打 list/get 成功，但不 poll、不寫入。"""

    def _guest_read(self, url, payload, svc):
        self.authenticate(self.gary_login, self.gary_login)
        with patch(f'{_CONTROLLER}.get_paas_operator_client') as factory:
            result = self._post_json(url, payload)
        self._assert_ok(result, 'guest 讀取服務不得失敗')
        factory.assert_not_called()
        svc.sudo().invalidate_recordset()
        self.assertTrue(result['result'].get('success'), result)
        return result['result']

    def test_B1_guest_get_transitional_service_does_not_poll(self):
        svc = self._service(self.template, self.w1, 'Deploying 676',
                            state='deploying')
        observed = fields.Datetime.now() - timedelta(minutes=2)
        svc.sudo().write({'observed_at': observed})
        result = self._guest_read(
            '/api/workspaces/%d/services/%d' % (self.w1.id, svc.id),
            {'action': 'get'}, svc)
        self.assertEqual(svc.sudo().state, 'deploying',
                         'get 不得推進 state（#1159 Q1：只由背景 poll 寫）')
        self.assertEqual(result['data']['observed_at'], observed.isoformat())

    def test_B2_guest_list_transitional_service_does_not_poll(self):
        svc = self._service(self.template, self.w1, 'Deploying list 676',
                            state='deploying')
        result = self._guest_read(
            '/api/workspaces/%d/services' % self.w1.id, {'action': 'list'}, svc)
        self.assertEqual(svc.sudo().state, 'deploying')
        row = next(r for r in result['data'] if r['id'] == svc.id)
        self.assertIn('observed_at', row)

    def test_B3_guest_get_initializing_service_reports_phase(self):
        template = self._template('tmpl-676-init', post_deploy_init_type='odoo')
        svc = self._service(template, self.w1, 'Init 676', state='initializing')
        svc.sudo().write({'init_pending': True, 'init_ready_observed': True})
        result = self._guest_read(
            '/api/workspaces/%d/services/%d' % (self.w1.id, svc.id),
            {'action': 'get'}, svc)
        self.assertEqual(svc.sudo().state, 'initializing')
        self.assertEqual(result['data']['init_phase'], 'queued')
