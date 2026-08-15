# -*- coding: utf-8 -*-
"""#524：`woow_paas_platform.smart_home` 的租戶隔離 ＋ tunnel 身分錨點。

## 缺陷（修復前實測，兩個租戶帳號、真 HTTP、每軸配陽性對照）

`ir_rules.xml` 對 `smart_home` **一條 rule 都沒有**（陽性對照：`security_access`
命中 36 行 ⇒ grep 有效；DB 側 `ir_rule` join `ir_model` 亦為 0 / 2 / 3），
而 `ir.model.access.csv` 給 `base.group_user` 的是 `1,1,1,0`
⇒ **ir.rule 這一層是空的**，任何已登入租戶對全站的 smart_home 有 read/write。

| 軸 | 修復前實測 |
| --- | --- |
| 1 讀 | 非成員 `read` 拿到別的租戶整筆，含 **`tunnel_id` 明文**；`search_read` 空 domain 枚舉全站 |
| 2 寫 | 非成員 `write` 回 `True` 且 **真的落地**（`name` / `ha_port` / `tunnel_id` 皆被改） |
| 3 建 | 裸 `create` 成功，`subscription_id` 為空；連建進**別人的** workspace 也成功 |

第 1 軸的鑑別項：同一筆記錄的 `tunnel_token`（有 `groups='base.group_system'`）
回 `AccessError` ⇒ 「守衛整個沒生效」被排除，就是這個欄位沒被保護。

## 修法有兩層，**擋的東西不同**

1. **record rule**（`security/ir_rules.xml`）：`smart_home_rule_member_read`
   ＋ `smart_home_rule_owner_admin_manage`，形態沿用 #676 給 cloud_service 落地
   的那兩條。擋「動哪一筆」。
2. **`create()` 的 sentinel 建立閘**（`models/smart_home.py`）：第 3 軸靠 rule 擋
   **不住**——任何 `base.group_user` 都能自建 workspace 並自動成為 owner，
   owner 資格可自我滿足（#519 對 cloud_service 的同一個裁決）。
3. **tunnel 身分錨點**（同檔 `_IDENTITY_ANCHOR_FIELDS`，形態抄 #522／PR #537）：
   擋「在**自己**那一筆裡填別人的 tunnel UUID」——那一步 record rule 擋不到，
   因為租戶對自己的記錄本來就有合法 write 權。

## TDD 紀律（照抄 #556 / #658 / #676）

- **全部走真 HTTP RPC**（`/web/dataset/call_kw` 或 `/api/workspaces/*`）。
  ir.rule 對 `TransactionCase` 的 superuser env 不生效，直呼 controller 也繞開
  真實的授權路徑。
- fixture user 一律只掛 `base.group_user`（setUp 有前提斷言）。
- **否定性斷言一律配陽性對照**：斷言「被擋下來」之前先證明同一個操作在 owner
  身分下會成功——否則 AccessError 可能來自欄位不存在／記錄不存在／constraint，
  而「擋住了」跟「探針壞了」輸出長得一模一樣。
- **逐條標明是哪一層擋的**：三道守線都是 `AccessError` 家族（`ProvisionGateError`
  繼承 `AccessError`；錨點守衛丟 `UserError`），只斷言 `AccessError` 會讓三者
  互相頂替——把 rule 改回全開，測試照樣綠。
- W1 刻意掛滿 owner/admin/user/guest **四列** access：`('access_ids', 'any', [...])`
  與 `access_ids.role` 點鏈的差異只有在同一個 workspace 有多列 access 時才顯現，
  少一列，兩種寫法行為就一樣，A4/A5 會零鑑別力。
- 每次 RPC 後 `invalidate_all()`：先讀過同一筆記錄會把值餵進 env cache 而遮蔽
  AccessError。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_OWNER, ROLE_USER,
)
from ..models.smart_home import _internal_provision
from .smart_home_fixture import sh_model

_MODEL = 'woow_paas_platform.smart_home'


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 524' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _SmartHome524Fixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）。"""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SmartHome = self.env[_MODEL]

        self.alice, self.alice_login = _new_group_user(self.env, 'alice524')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob524')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol524')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave524')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary524')

        for u in (self.alice, self.bob, self.carol, self.dave, self.gary):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 524'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 524'})

        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})

        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            'fixture 前提：W1 必須同時掛 owner/admin/user/guest 四列'
            '（少了 admin 那列，跨列匹配的 domain 寫法就不會被 A4/A5 抓到）')
        self.assertFalse(
            self.w2.sudo().access_ids.filtered(
                lambda a: a.user_id in (self.alice | self.carol | self.dave
                                        | self.gary)),
            'fixture 前提：W1 的成員不得同時是 W2 的成員')

        # 下單流程（第 3 軸的陽性對照）需要的 plan / product / template
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'SH 524 Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.env['product.template'].sudo().create({
            'name': 'SH 524 Plan Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'list_price': 100.0,
        })
        self.template = self.env[
            'woow_paas_platform.smart_home_template'].sudo().create({
                'name': 'HA 524',
                'slug': 'ha-524-%s' % uuid.uuid4().hex[:6],
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })

        #: 租戶 B 的 tunnel UUID —— A 系列要具名證明它不再外洩。
        self.b_tunnel_uuid = 'b-tunnel-%s' % uuid.uuid4()
        self.sh2 = self._smart_home(self.w2, 'SH of tenant B',
                                    tunnel_id=self.b_tunnel_uuid,
                                    tunnel_name='b-tunnel-name')
        self.sh1 = self._smart_home(self.w1, 'SH of tenant A',
                                    tunnel_id='a-tunnel-%s' % uuid.uuid4())

    # ------------------------------------------------------------------ #
    #  fixture builders（一律開閘 + sudo：fixture 不該被受測的守線擋住）   #
    # ------------------------------------------------------------------ #
    def _smart_home(self, workspace, name, **extra):
        vals = {'name': name, 'workspace_id': workspace.id}
        vals.update(extra)
        return sh_model(self.env).sudo().create(vals)

    # ------------------------------------------------------------------ #
    #  RPC helper                                                        #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。"""
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open('/web/dataset/call_kw', data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _post_json(self, url, payload):
        """打 `/api/workspaces/*` 這種 `type="json"` 的產品端點。"""
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(url, data=body,
                             headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  斷言 helper —— 三道守線都要能分辨，不能互相頂替                     #
    # ------------------------------------------------------------------ #
    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_smart_home_rule_denied(self, result, msg):
        """斷言被 **smart_home 自己那層** ir.rule／ACL 擋下。"""
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % text)
        self.assertIn(
            'smart_home', text,
            msg + '（歸因：擋下來的必須是 smart_home 這一層，回 %r）' % text)

    def _assert_provision_gate_denied(self, result, msg):
        """斷言被 **建立閘**擋下，而不是被 record rule 擋下。

        分辨得出來才有意義——`_assert_smart_home_rule_denied` 在這裡會綠得毫無
        資訊（`ProvisionGateError` 繼承 `AccessError`，兩者都命中 smart_home）。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('ProvisionGateError', text,
                      msg + '（歸因：必須是 #524 的建立閘，回 %r）' % text)
        self.assertIn('開通流程', text,
                      msg + '（歸因：訊息必須可分辨，回 %r）' % text)

    def _assert_anchor_immutable(self, result, field, msg):
        """斷言被**身分錨點**守衛擋下（`UserError`，不是 AccessError 家族）。"""
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('UserError', text,
                      msg + '（歸因：必須是錨點守衛，回 %r）' % text)
        self.assertIn("'%s' is immutable" % field, text,
                      msg + '（歸因：訊息要點名欄位，回 %r）' % text)

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)
        return result.get('result')

    def _operator_client(self, **extra):
        client = MagicMock()
        client.create_tunnel.return_value = {
            'tunnel_id': 'fresh-tunnel-%s' % uuid.uuid4().hex[:8],
            'tunnel_token': 'fresh-token',
            'tunnel_name': 'fresh-name',
        }
        client.get_tunnel_status.return_value = {'connections': []}
        for k, v in extra.items():
            setattr(client, k, v)
        return client

    def _patched_operator(self, client):
        return patch('odoo.addons.woow_paas_platform.models.smart_home'
                     '.get_paas_operator_client', return_value=client)

    def _order_smart_home(self, login, workspace, name):
        """走**正常下單流程**開一台（第 3 軸與 C 系列的陽性對照主體）。"""
        self.authenticate(login, login)
        res = self._post_json(
            '/api/workspaces/%d/smarthomes' % workspace.id,
            {'action': 'create', 'name': name,
             'plan_id': self.plan.id, 'template_id': self.template.id})
        payload = self._assert_ok(res, '下單端點不該回 JSON-RPC error')
        self.assertTrue(payload.get('success'),
                        '陽性對照：正常下單流程必須成功（回 %r）' % payload)
        return self.SmartHome.sudo().browse(payload['data']['id'])


class TestSmartHomeTenantIsolation524(_SmartHome524Fixture):
    """A 系列：第 1／2 軸 —— record rule 的授權矩陣（走 `/web/dataset/call_kw`）。"""

    # ---------------------------------------------------------------- #
    #  陽性對照——先證明這些操作在該通的身分下真的做得到                  #
    # ---------------------------------------------------------------- #
    def _assert_can_read(self, login, label):
        self.authenticate(login, login)
        rows = self._assert_ok(
            self._call_kw(_MODEL, 'read', [[self.sh1.id], ['name', 'tunnel_id']]),
            '%s 必須讀得到本 workspace 的 smart_home' % label)
        self.assertEqual(rows[0]['name'], 'SH of tenant A')

    def test_A0a_owner_can_read_own(self):
        self._assert_can_read(self.alice_login, 'owner')

    def test_A0b_admin_can_read_own(self):
        self._assert_can_read(self.dave_login, 'admin')

    def test_A0c_user_role_can_read_own(self):
        """read 維持全體成員——BFF 的 list/get 對 role=user 仍要通。"""
        self._assert_can_read(self.carol_login, 'role=user')

    def test_A0d_guest_role_can_read_own(self):
        self._assert_can_read(self.gary_login, 'role=guest')

    def _assert_can_write(self, login, label):
        self.authenticate(login, login)
        new_name = 'renamed by %s' % label
        self._assert_ok(
            self._call_kw(_MODEL, 'write', [[self.sh1.id], {'name': new_name}]),
            '%s 必須改得動本 workspace 的 smart_home' % label)
        self.assertEqual(self.sh1.sudo().name, new_name,
                         '%s 的寫入必須真的落地（不是被吞掉回 True）' % label)

    def test_A0e_owner_can_write_own(self):
        self._assert_can_write(self.alice_login, 'owner')

    def test_A0f_admin_can_write_own(self):
        self._assert_can_write(self.dave_login, 'admin')

    # ---------------------------------------------------------------- #
    #  第 1 軸：跨租戶讀                                                #
    # ---------------------------------------------------------------- #
    def test_A1_non_member_cannot_read_other_tenant_record(self):
        """具名涵蓋 `tunnel_id`：修復前這條回的是 B 的 tunnel UUID 明文。"""
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_smart_home_rule_denied(
            self._call_kw(_MODEL, 'read',
                          [[self.sh2.id],
                           ['name', 'tunnel_id', 'tunnel_name', 'subdomain']]),
            '#524 第 1 軸：非成員不得讀到別的租戶的 smart_home（含 tunnel_id）')

    def test_A2_non_member_cannot_enumerate_other_tenant_records(self):
        """`search_read` 空 domain 只回得到自己 workspace 的那些。

        與 A1 是**不同的失敗模式**：`read` 被擋會噴 AccessError，`search` 被
        rule 過濾則是**安靜地少幾列** —— 只測 A1 的話，一條「rule 只掛
        perm_read=False」之類的錯誤形態會讓枚舉繼續通。
        """
        self.authenticate(self.alice_login, self.alice_login)
        rows = self._assert_ok(
            self._call_kw(_MODEL, 'search_read',
                          [[], ['id', 'name', 'tunnel_id']]),
            'search_read 本身要跑得起來（陽性對照：至少看得到自己的）')
        ids = [r['id'] for r in rows]
        self.assertIn(self.sh1.id, ids,
                      '陽性對照：自己 workspace 的那筆必須看得到'
                      '（看不到代表 rule 過頭了，不是擋住了）')
        self.assertNotIn(self.sh2.id, ids,
                         '#524 第 1 軸：非成員不得枚舉到別的租戶的 smart_home')
        self.assertNotIn(
            self.b_tunnel_uuid, json.dumps(rows),
            '#524：B 的 tunnel UUID 不得出現在 A 的任何回應裡')

    # ---------------------------------------------------------------- #
    #  第 2 軸：跨租戶寫                                                #
    # ---------------------------------------------------------------- #
    def test_A3_non_member_cannot_write_other_tenant_record(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_smart_home_rule_denied(
            self._call_kw(_MODEL, 'write',
                          [[self.sh2.id], {'name': 'PWNED by alice'}]),
            '#524 第 2 軸：非成員不得改得動別的租戶的 smart_home')
        self.assertEqual(self.sh2.sudo().name, 'SH of tenant B',
                         '被擋之後值不得改變（「回 error 但其實寫進去了」）')

    def test_A4_user_role_cannot_write_in_own_workspace(self):
        """role=user 讀得到、寫不動 —— 界線與 #676 給 cloud_service 的一致。

        這一條同時是 domain 寫法的鑑別項：若寫成 `access_ids.role` 點鏈，
        carol 會用 alice 那列 owner 的 role 過關（跨列匹配）。
        """
        self.authenticate(self.carol_login, self.carol_login)
        self._assert_smart_home_rule_denied(
            self._call_kw(_MODEL, 'write',
                          [[self.sh1.id], {'name': 'written by role=user'}]),
            '#524：role=user 不得對 smart_home 寫入')
        self.assertEqual(self.sh1.sudo().name, 'SH of tenant A',
                         '被擋之後值不得改變')

    def test_A5_guest_role_cannot_write_in_own_workspace(self):
        self.authenticate(self.gary_login, self.gary_login)
        self._assert_smart_home_rule_denied(
            self._call_kw(_MODEL, 'write',
                          [[self.sh1.id], {'name': 'written by role=guest'}]),
            '#524：role=guest 不得對 smart_home 寫入')
        self.assertEqual(self.sh1.sudo().name, 'SH of tenant A',
                         '被擋之後值不得改變')


class TestSmartHomeCreateGate524(_SmartHome524Fixture):
    """B 系列：第 3 軸 —— 繞過下單流程自建。"""

    def test_B1_tenant_cannot_create_over_rpc_in_own_workspace(self):
        """修復前這條回的是新 id、`subscription_id` 為空。"""
        self.authenticate(self.alice_login, self.alice_login)
        before = self.SmartHome.sudo().search_count([])
        self._assert_provision_gate_denied(
            self._call_kw(_MODEL, 'create',
                          [{'name': 'orphan by alice',
                            'workspace_id': self.w1.id}]),
            '#524 第 3 軸：owner 也不得用裸 ORM create 繞過下單流程')
        self.assertEqual(self.SmartHome.sudo().search_count([]), before,
                         '被擋之後不得有任何記錄落地')

    def test_B2_tenant_cannot_plant_record_into_other_workspace(self):
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_provision_gate_denied(
            self._call_kw(_MODEL, 'create',
                          [{'name': 'planted into W2',
                            'workspace_id': self.w2.id}]),
            '#524：更不得把記錄種進別人的 workspace')
        self.assertFalse(
            self.SmartHome.sudo().search([('name', '=', 'planted into W2')]),
            '被擋之後不得有任何記錄落地')

    def test_B3_admin_role_also_cannot_create_over_rpc(self):
        """建立閘對**任何** RPC 呼叫端一律拒絕，不論身分——不是 role gate。"""
        self.authenticate(self.dave_login, self.dave_login)
        self._assert_provision_gate_denied(
            self._call_kw(_MODEL, 'create',
                          [{'name': 'orphan by admin',
                            'workspace_id': self.w1.id}]),
            '#524：admin 走 RPC 一樣建不出來')

    def test_B4_context_key_forgery_does_not_open_the_gate(self):
        """呼叫端在 payload 裡帶同名 context key 打不開閘（值身分判準）。

        `/web/dataset/call_kw` 把呼叫端 context **整包**換進 env，所以任何 JSON
        表達得出的判準都會被這一招打開。#522 在 18.0 上實測過同一件事。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_provision_gate_denied(
            self._call_kw(
                _MODEL, 'create',
                [{'name': 'forged ctx', 'workspace_id': self.w1.id}],
                {'context': {'woow_smart_home_internal_provision': True}}),
            '#524：JSON 載不了物件身分，帶 key 也開不了閘')

    def test_B5_positive_control_normal_order_flow_still_works(self):
        """🔴 **這是第 3 軸唯一能證明沒有把正當的路一起擋掉的東西。**"""
        home = self._order_smart_home(self.alice_login, self.w1, 'ordered via BFF')
        self.assertTrue(home.exists(), '正常下單流程必須真的建出記錄')
        self.assertTrue(home.subscription_id,
                        '正常下單流程必須產生訂閱')
        self.assertEqual(home.subscription_id.state, 'in_progress',
                         '訂閱必須是生效狀態')
        self.assertEqual(home.workspace_id, self.w1)

    def test_B6_user_role_cannot_order_via_bff(self):
        """陰性對照：BFF 的 create 分支本來就是 owner/admin，收窄後仍一致。"""
        self.authenticate(self.carol_login, self.carol_login)
        res = self._post_json(
            '/api/workspaces/%d/smarthomes' % self.w1.id,
            {'action': 'create', 'name': 'by role=user',
             'plan_id': self.plan.id, 'template_id': self.template.id})
        payload = self._assert_ok(res, '端點不該回 JSON-RPC error')
        self.assertFalse(payload.get('success'))
        self.assertIn('Insufficient permissions', payload.get('error', ''))


class TestSmartHomeTunnelAnchor524(_SmartHome524Fixture):
    """C 系列：tunnel 身分錨點（#522／PR #537 在 smart_home 上沒覆蓋到的同型缺口）。"""

    def test_C1_owner_cannot_repoint_tunnel_id_on_own_record(self):
        """租戶對自己那筆有合法 write 權 —— **record rule 擋不到這一步**。"""
        self.authenticate(self.alice_login, self.alice_login)
        original = self.sh1.sudo().tunnel_id
        self._assert_anchor_immutable(
            self._call_kw(_MODEL, 'write',
                          [[self.sh1.id], {'tunnel_id': self.b_tunnel_uuid}]),
            'tunnel_id',
            '#524：tunnel_id 建立後不可變（含 owner 自己那一筆）')
        self.assertEqual(self.sh1.sudo().tunnel_id, original,
                         '被擋之後值不得改變')

    def test_C2_owner_cannot_repoint_tunnel_name_or_token(self):
        """錨點是一份清單，不是一個欄位——#522 的教訓正是「逐欄位手工保護會漏」。"""
        self.authenticate(self.alice_login, self.alice_login)
        for field in ('tunnel_name', 'tunnel_token'):
            self._assert_anchor_immutable(
                self._call_kw(_MODEL, 'write', [[self.sh1.id], {field: 'x'}]),
                field, '#524：%s 建立後不可變' % field)

    def test_C3_anchor_guard_binds_to_object_identity_not_a_truthy_value(self):
        """帶同名 context key 打不開錨點閘（與 B4 同一個判準、不同守線）。"""
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_anchor_immutable(
            self._call_kw(
                _MODEL, 'write',
                [[self.sh1.id], {'tunnel_id': self.b_tunnel_uuid}],
                {'context': {
                    'woow_smart_home_internal_identity_write': True}}),
            'tunnel_id',
            '#524：JSON 載不了物件身分，帶 key 也打不開錨點閘')

    def test_C4_create_time_squat_is_stripped(self):
        """create 側：`super().create()` 不繞經 `write()`，只有 write 守衛會留洞。

        伺服器端**只開建立閘、沒開錨點閘**的呼叫，錨點會被剝除而不是落地——
        否則一筆全新的列可以在 provision 之前就綁上受害者的 tunnel，而
        `action_provision` 的 `if self.tunnel_id:` 分支會因此跳過建立 tunnel。
        """
        squatted = _internal_provision(self.SmartHome.sudo()).create({
            'name': 'squat attempt',
            'workspace_id': self.w1.id,
            'tunnel_id': self.b_tunnel_uuid,
            'tunnel_name': 'stolen-name',
        })
        self.assertFalse(squatted.tunnel_id,
                         '#524：create 時自帶的 tunnel_id 必須被剝除')
        self.assertFalse(squatted.tunnel_name,
                         '#524：create 時自帶的 tunnel_name 必須被剝除')
        self.assertEqual(squatted.name, 'squat attempt',
                         '陽性對照：非錨點欄位必須照常落地'
                         '（否則「被剝除」與「create 整個沒跑」分不出來）')

    def test_C5_destructive_path_is_closed_end_to_end(self):
        """整條路徑的收口：偷來的 UUID 進不了 DB ⇒ operator 不會收到它。

        修復前實測：`delete_tunnel(<B 的 UUID>)` **真的送出去了**，而且是在
        `super().unlink()` 的 ACL 檢查**之前**——端點回失敗，Cloudflare 側的
        動作已經發生。
        """
        self.authenticate(self.alice_login, self.alice_login)
        self._assert_anchor_immutable(
            self._call_kw(_MODEL, 'write',
                          [[self.sh1.id], {'tunnel_id': self.b_tunnel_uuid}]),
            'tunnel_id', '#524：第一步就要被擋')

        client = self._operator_client()
        with self._patched_operator(client):
            self._post_json(
                '/api/workspaces/%d/smarthomes/%d' % (self.w1.id, self.sh1.id),
                {'action': 'delete'})
        called = [c.args[0] for c in client.delete_tunnel.call_args_list
                  if c.args]
        self.assertNotIn(
            self.b_tunnel_uuid, called,
            '#524：operator 不得收到別的租戶的 tunnel UUID（實收 %r）' % called)

    # ---------------------------------------------------------------- #
    #  陽性對照：伺服器端合法寫入點必須穿得過錨點閘                      #
    # ---------------------------------------------------------------- #
    #
    # ⚠ C6／C7 走的是**伺服器端身分**（`.sudo()`），不是租戶的 HTTP 端點——
    #    這是刻意的，而且理由是量出來的、不是省事：
    #
    #    在 `origin/develop 5c641b7f`（**本 PR 的任何改動都還沒進去**）上實測，
    #    只有 `base.group_user` 的 workspace owner 打
    #    `POST /api/workspaces/<ws>/smarthomes/<id> {action: provision}` 會拿到
    #      You do not have enough rights to access the fields "tunnel_token" …
    #      Operation: write  User: 178
    #    而記錄停在 `state='provisioning'`、`tunnel_id` 為空。
    #    根因是 `tunnel_token` 自 model 誕生（PR #115, 2026-02-28）就帶
    #    `groups='base.group_system'`，而 `_provision_smarthome` 用的是**非 sudo**
    #    的租戶 recordset ⇒ 租戶經 `/woow` 的 provision **從來沒有通過**。
    #    同理 `action='delete'` 也一律撞 ACL `unlink=0`。
    #    既有測試沒抓到，是因為它們全在 `TransactionCase`（superuser env）裡呼叫
    #    `action_provision()`。
    #
    #    ⇒ 那是**與 #524 無關的既有缺陷**，依工單紀律不在本 PR 順手修（已回報）。
    #    但錨點閘要證明的是「**伺服器端合法寫入點穿得過去**」，那條路徑正是
    #    `action_provision` / `_cleanup_tunnel` 內部的 write，所以在這裡以
    #    `.sudo()` 驅動它是**對準受測對象**，不是繞過守線：錨點守衛對 su／sudo
    #    一律生效（見 `_INTERNAL_IDENTITY_WRITE` 的註解），sudo 打不開它。
    #    租戶身分那一側的陽性對照由 A0e／A0f（owner/admin 經 call_kw 寫得動）
    #    與 B5（正常下單流程開得出來）承擔。
    def test_C6_positive_control_provision_still_stores_the_anchor(self):
        """`action_provision` 是錨點的合法寫入點 —— 沒開閘就會被自己的守衛擋死。"""
        home = self._order_smart_home(self.alice_login, self.w1, 'to provision')
        self.assertFalse(home.tunnel_id, '前提：剛下單的還沒有 tunnel')

        client = self._operator_client()
        with self._patched_operator(client):
            home.sudo().action_provision()
        home.invalidate_recordset()
        self.assertTrue(client.create_tunnel.called,
                        'operator 必須真的被呼叫（否則測到的是空跑）')
        self.assertEqual(
            home.tunnel_id, client.create_tunnel.return_value['tunnel_id'],
            '#524 陽性對照：provision 寫進來的錨點必須落地'
            '（沒落地＝錨點閘把正當的路一起擋掉了）')
        self.assertEqual(
            home.tunnel_name, client.create_tunnel.return_value['tunnel_name'],
            '#524 陽性對照：tunnel_name 也是錨點，同樣要寫得進來')
        self.assertEqual(home.state, 'active')

    def test_C7_positive_control_teardown_still_clears_the_anchor(self):
        """`_cleanup_tunnel` 清空 `tunnel_id` 也是寫錨點 —— 同一個閘要放行。"""
        home = self._order_smart_home(self.alice_login, self.w1, 'to teardown')
        client = self._operator_client()
        with self._patched_operator(client):
            home.sudo().action_provision()
            home.invalidate_recordset()
            provisioned_uuid = home.tunnel_id
            self.assertTrue(provisioned_uuid, '前提：provision 有把 tunnel 存下來')

            home.sudo().action_delete()
        client.delete_tunnel.assert_called_once_with(provisioned_uuid)
        self.assertFalse(self.SmartHome.sudo().browse(home.id).exists(),
                         '記錄必須真的被刪掉'
                         '（還在＝`_cleanup_tunnel` 沒能清掉錨點、unlink fail-loud）')

    def test_C8_sudo_alone_does_not_open_the_anchor_gate(self):
        """鑑別項：C6／C7 用 sudo 驅動，所以必須證明 sudo **本身**打不開錨點閘。

        沒有這一條，C6／C7 的綠燈就與「守衛對 sudo 無效」不可區分——而那正是
        `su-env-exempt-guard-false-green.md` 記的那個陷阱。
        """
        with self.assertRaises(UserError) as ctx:
            self.sh1.sudo().write({'tunnel_id': self.b_tunnel_uuid})
        self.assertIn("'tunnel_id' is immutable", str(ctx.exception))
        self.assertNotEqual(self.sh1.sudo().tunnel_id, self.b_tunnel_uuid)


class TestSmartHomeRefreshStatusLockstep524(_SmartHome524Fixture):
    """D 系列：收窄 write 之後 `refresh_status` 的兩層 ACL 對齊。

    `action_refresh_status()` 內部是 `self.write(update_vals)` ⇒ 收窄後
    `role=user` 會吃到**裸 AccessError**：`str(e)` 洩漏 model 技術名與 rule 名，
    而且行為是 state-dependent（`tunnel_id` 還沒設時提早 return 所以成功、
    provision 之後才失敗）。與 `controllers/security_access.py` 的同一處前例對齊。
    """

    def test_D1_user_role_gets_permission_error_not_raw_access_error(self):
        home = self._order_smart_home(self.alice_login, self.w1, 'for refresh')
        client = self._operator_client()
        with self._patched_operator(client):
            # sudo 驅動 provision 的理由同 C6／C7 上方的長註解（既有缺陷，
            # 與 #524 無關）——這裡只是要把記錄推到「有 tunnel_id」的狀態，
            # 因為 `action_refresh_status` 對沒有 tunnel_id 的記錄會提早 return
            # ⇒ 不推到這個狀態，D1 會在「根本沒走到 write」的前提上恆綠。
            home.sudo().action_provision()
        home.invalidate_recordset()
        self.assertTrue(home.tunnel_id,
                        '前提：refresh_status 只有在有 tunnel_id 時才會 write')

        self.authenticate(self.carol_login, self.carol_login)
        res = self._post_json(
            '/api/workspaces/%d/smarthomes/%d' % (self.w1.id, home.id),
            {'action': 'refresh_status'})
        payload = self._assert_ok(res, '端點不該回 JSON-RPC error')
        self.assertFalse(payload.get('success'))
        self.assertEqual(payload.get('error'), 'Insufficient permissions',
                         '必須是 role gate 的訊息，不是裸 AccessError 的原文')
        self.assertNotIn('ir.rule', payload.get('error', ''))
        self.assertNotIn('smart_home', payload.get('error', ''),
                         'model 技術名不得洩漏給租戶')

    def test_D2_positive_control_owner_can_still_refresh(self):
        home = self._order_smart_home(self.alice_login, self.w1, 'for refresh ok')
        client = self._operator_client()
        with self._patched_operator(client):
            home.sudo().action_provision()
            home.invalidate_recordset()
            self.assertTrue(home.tunnel_id, '前提：要有 tunnel_id 才會真的 write')
            self.authenticate(self.alice_login, self.alice_login)
            res = self._post_json(
                '/api/workspaces/%d/smarthomes/%d' % (self.w1.id, home.id),
                {'action': 'refresh_status'})
        payload = self._assert_ok(res, '端點不該回 JSON-RPC error')
        self.assertTrue(payload.get('success'),
                        '陽性對照：owner 的 refresh_status 必須仍然可用（回 %r）'
                        % payload)
        self.assertTrue(client.get_tunnel_status.called,
                        'operator 必須真的被呼叫（否則測到的是空跑）')
